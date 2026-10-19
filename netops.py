#!/usr/bin/env python3
"""Torrent - network SRE (Omada ER605/SG3210/EAPs, VLANs, DNS, Traefik routes).

  netops.py watch                      # every 10 min, deterministic, posts only on change
  netops.py check                      # daily 07:00 full health report (+ luna summary)
  netops.py ask "<question>" [--reply-to MID] [--channel CID]   # Codex 6.1-sol high, read-only investigation
  netops.py omada <api-path>           # read-only Omada GET helper (used by Torrent's Codex sessions)
  netops.py acl status                 # Torrent-managed Gateway ACLs: applied phase, live rules, drift (read-only)
  netops.py acl plan <phase>           # post an owner approval to roll the rule set out up to <phase> (1-4)
  netops.py acl apply <phase>          # approved executor: sync, health checks, ✅ confirmation window or rollback
  netops.py acl probe                  # approved executor: learn the controller's ACL body format with disabled rules
  netops.py acl off                    # emergency: disable every Torrent-managed rule now (loosening only)

Torrent's Omada account (claude-reader, read/write since 2026-10-15) does reads everywhere; Gateway ACL writes
happen only inside an owner-approved Roundtable approval and only on Torrent-managed objects (see aclctl.py).
"""
import argparse
import concurrent.futures as cf
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request

import olympus_core as o

AGENT = "netops"
REPORT_CH, ALERT_CH = "homelab-infrastructure", "infrastructure-alerts"
# Primary first (Technitium on homelab-dns01 since 2026-10-18), Pi-hole CT202 is the secondary
DNS_SERVERS = {"Technitium": "192.168.90.60", "Pi-hole": "192.168.90.53"}
DNS_NAMES = ["google.com", "grafana.hrmsmrflrii.xyz", "github.com"]
DNS_BLOCK_PROBE = "doubleclick.net"   # on every blocklist: must come back 0.0.0.0
INTERNET = ["1.1.1.1", "8.8.8.8"]
LOSS_WARN, RTT_WARN = 5.0, 80.0
CPU_WARN, MEM_WARN = 85, 90


# --------------------------------------------------------------------------- Omada (read-only)

class Omada:
    def __init__(self, user=None, password=None):
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        self.op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                              urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.base = os.environ["OMADA_HOST"].strip().rstrip("/")
        self.cid = self._req("GET", "/api/info")["result"]["omadacId"]
        r = self._req("POST", f"/{self.cid}/api/v2/login",
                      {"username": user or os.environ["OMADA_USER"], "password": password or os.environ["OMADA_PASS"]})
        if r.get("errorCode") != 0:
            raise RuntimeError(f"Omada login failed: {r.get('msg')}")
        self.h = {"Csrf-Token": r["result"]["token"]}
        sites = self.get("/sites?currentPage=1&currentPageSize=100", site=False)["data"]
        name = os.environ.get("OMADA_SITE", "")
        self.site = next((s["id"] for s in sites if s["name"] == name), sites[0]["id"])

    def _req(self, method, path, data=None, h=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None,
                                   method=method, headers={"Content-Type": "application/json", **(h or {})})
        with self.op.open(r, timeout=25) as f:
            return json.loads(f.read())

    def get(self, path, site=True):
        p = f"/{self.cid}/api/v2" + (f"/sites/{self.site}" if site else "") + path
        r = self._req("GET", p, h=self.h)
        if r.get("errorCode") != 0:
            raise RuntimeError(f"Omada {path}: {r.get('errorCode')} {r.get('msg')}")
        return r.get("result")


# --------------------------------------------------------------------------- probes

def ping(host, count=5):
    try:
        out = subprocess.run(["ping", "-n", "-q", "-c", str(count), "-i", "0.3", "-W", "2", host],
                             capture_output=True, text=True, timeout=30).stdout
    except subprocess.TimeoutExpired:
        return {"host": host, "loss": 100.0, "avg": None}
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= [\d.]+/([\d.]+)/", out)
    return {"host": host, "loss": float(loss.group(1)) if loss else 100.0, "avg": float(rtt.group(1)) if rtt else None}


def dns(name, server="Technitium"):
    t = time.time()
    try:
        out = subprocess.run(["dig", "+short", "+time=2", "+tries=1", f"@{DNS_SERVERS[server]}", name],
                             capture_output=True, text=True, timeout=8).stdout.split()
    except subprocess.TimeoutExpired:
        out = []
    return {"server": server, "name": name, "ok": bool(out), "answer": out[:2], "ms": round((time.time() - t) * 1000)}


def dns_all():
    """Every probe name on every DNS server, plus one blocklist probe per server."""
    jobs = [(n, s) for s in DNS_SERVERS for n in DNS_NAMES + [DNS_BLOCK_PROBE]]
    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda a: dns(*a), jobs))
    for q in res:
        if q["name"] == DNS_BLOCK_PROBE:
            q["ok"] = q["answer"][:1] in (["0.0.0.0"], [])   # blocked (0.0.0.0) or NXDOMAIN; a real IP = not blocking
            q["blockprobe"] = True
    return res


def traefik_hosts():
    rc, out, _ = o.ssh("traefik-lxc", "grep -rhoE 'Host\\(`[^`]+`\\)' /opt/traefik/config/dynamic/*.yml | sort -u",
                       timeout=30)
    return sorted({m for m in re.findall(r"`([^`]+)`", out)}) if rc == 0 else []


def http_check(host):
    try:
        out = subprocess.run(["curl", "-sk", "-o", "/dev/null", "-m", "10", "-w", "%{http_code} %{time_total}",
                              f"https://{host}/"], capture_output=True, text=True, timeout=15).stdout.split()
        code, secs = int(out[0]), float(out[1])
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        code, secs = 0, None
    return {"host": host, "code": code, "secs": secs, "ok": 0 < code < 500}


def collect(full=False):
    snap = {"t": o.iso(), "errors": []}
    try:
        om = Omada()
        ov = om.get("/dashboard/overviewDiagram")
        devs = om.get("/devices")
        nets = om.get("/setting/lan/networks?currentPage=1&currentPageSize=100")["data"]
        snap["overview"] = {k: ov.get(k) for k in ("totalClientNum", "wiredClientNum", "wirelessClientNum", "guestNum",
                                                   "connectedSwitchNum", "totalSwitchNum", "connectedApNum", "totalApNum",
                                                   "connectedGatewayNum", "totalGatewayNum", "availablePorts", "totalPorts",
                                                   "powerConsumption", "wanHealth", "gatewayHealth")}
        snap["devices"] = [{"name": d["name"], "type": d["type"], "model": d["model"], "ip": d.get("ip"),
                            "connected": d.get("statusCategory") == 1, "status": d.get("status"),
                            "fw": d.get("version"), "need_upgrade": bool(d.get("needUpgrade")),
                            "cpu": d.get("cpuUtil"), "mem": d.get("memUtil"), "uptime_s": d.get("uptimeLong") or 0,
                            "clients": d.get("clientNum")} for d in devs]
        snap["vlans"] = [{"name": n["name"], "vlan": n.get("vlan"), "subnet": n.get("gatewaySubnet"),
                          "dhcp": (n.get("dhcpSettings") or {}).get("enable")} for n in nets]
        if full:
            al = om.get("/alerts?currentPage=1&currentPageSize=20&filters.resolved=false")
            snap["alerts"] = [{"time": a.get("time"), "msg": a.get("msg") or a.get("content")} for a in al.get("data", [])]
    except Exception as e:  # noqa: BLE001
        snap["errors"].append(f"Omada API: {e}")
        snap["devices"], snap["vlans"] = [], []
    gws = [v["subnet"].split("/")[0] for v in snap["vlans"] if v.get("subnet")] or \
          [f"192.168.{n}.1" for n in (0, 10, 20, 30, 40, 50, 60, 80, 90, 91)]
    with cf.ThreadPoolExecutor(16) as ex:
        gw = list(ex.map(lambda h: ping(h, 3), gws))
        inet = list(ex.map(ping, INTERNET))
        routes = list(ex.map(http_check, traefik_hosts())) if full else []
    snap.update(gateways=gw, internet=inet, dns=dns_all(), routes=routes)
    return snap


def problems(snap, prev):
    """Deterministic findings: list of (key, severity, text)."""
    out = [(f"err:{e[:30]}", "medium", e) for e in snap["errors"]]
    for d in snap["devices"]:
        n = d["name"]
        if not d["connected"]:
            sev = "critical" if d["type"] == "gateway" or "Core" in n else "high"
            out.append((f"dev:{n}", sev, f"{d['type']} **{n}** ({d['model']}, {d['ip']}) is DISCONNECTED from the controller"))
            continue
        if (d["cpu"] or 0) >= CPU_WARN:
            out.append((f"cpu:{n}", "medium", f"{n} CPU {d['cpu']}%"))
        if (d["mem"] or 0) >= MEM_WARN:
            out.append((f"mem:{n}", "medium", f"{n} memory {d['mem']}%"))
        old = next((p for p in (prev or {}).get("devices", []) if p["name"] == n), None)
        if old and d["uptime_s"] and old.get("uptime_s") and d["uptime_s"] < old["uptime_s"]:
            out.append((f"reboot:{n}:{snap['t'][:13]}", "medium",
                        f"{n} rebooted (uptime now {d['uptime_s'] // 3600}h {d['uptime_s'] % 3600 // 60}m)"))
    for g in snap["gateways"]:
        if g["loss"] >= 100:
            out.append((f"gw:{g['host']}", "high", f"VLAN gateway {g['host']} not answering ping"))
    bad_inet = [i for i in snap["internet"] if i["loss"] >= LOSS_WARN or (i["avg"] or 0) >= RTT_WARN]
    if len(bad_inet) == len(snap["internet"]):
        down = all(i["loss"] >= 100 for i in bad_inet)
        out.append(("inet", "critical" if down else "medium",
                    "Internet DOWN (no reply from 1.1.1.1 / 8.8.8.8)" if down else
                    "Internet degraded: " + ", ".join(f"{i['host']} loss {i['loss']:.0f}% avg {i['avg']}ms" for i in bad_inet)))
    dead = {s for s in DNS_SERVERS if not any(q["ok"] for q in snap["dns"]
                                               if q.get("server", "Pi-hole") == s and not q.get("blockprobe"))}
    if len(dead) == len(DNS_SERVERS):
        out.append(("dns:all", "critical", "No DNS server answers (Technitium 192.168.90.60 and Pi-hole 192.168.90.53)"))
    for q in snap["dns"]:
        s = q.get("server", "Pi-hole")
        if q["ok"] or (s in dead and len(dead) == len(DNS_SERVERS)):
            continue
        if q.get("blockprobe"):
            out.append((f"dnsblock:{s}", "medium", f"{s} {DNS_SERVERS[s]} is not blocking ads ({q['name']} -> {q['answer'][:1]})"))
        elif s in dead:
            if not any(p[0] == f"dnsdown:{s}" for p in out):
                out.append((f"dnsdown:{s}", "high" if s == "Technitium" else "medium",
                            f"{s} {DNS_SERVERS[s]} is not answering DNS"
                            + (" (primary: clients fall back to Pi-hole, slower)" if s == "Technitium" else " (secondary)")))
        else:
            out.append((f"dns:{s}:{q['name']}", "high", f"{s} {DNS_SERVERS[s]} cannot resolve {q['name']}"))
    prev_routes = {r["host"]: r for r in (prev or {}).get("routes", [])}
    for r in snap["routes"]:
        if not r["ok"] and (not prev_routes or prev_routes.get(r["host"], {}).get("ok", True)):
            out.append((f"route:{r['host']}", "medium",
                        f"https://{r['host']} returns {r['code'] or 'no response'}"
                        + (" (was OK yesterday)" if prev_routes else "")))
    return out


def fw_changes(snap, prev):
    old = {d["name"]: d.get("fw") for d in (prev or {}).get("devices", [])}
    return [f"{d['name']}: {old[d['name']]} → {d['fw']}" for d in snap["devices"]
            if d["name"] in old and old[d["name"]] and d.get("fw") and old[d["name"]] != d["fw"]]


# --------------------------------------------------------------------------- modes

def watch():
    snap = collect(full=False)
    prev = o.kv_get("netops:watch")
    st = o.kv_get("netops:active", {})
    now = {k: (s, t) for k, s, t in problems(snap, prev) if not k.startswith(("route:", "reboot:"))}
    reboots = [(k, s, t) for k, s, t in problems(snap, prev) if k.startswith("reboot:")]
    new = {k: v for k, v in now.items() if k not in st}
    cleared = {k: v for k, v in st.items() if k not in now}
    for k, s, t in reboots:
        o.post(ALERT_CH, embeds=[o.embed(AGENT, "🔄 Network device rebooted", t, color=o.SEV_COLORS["medium"])])
    if new:
        worst = min((s for s, _ in new.values()), key=lambda s: ["critical", "high", "medium", "low"].index(s))
        o.post(ALERT_CH, embeds=[o.embed(AGENT, f"{'🔴' if worst == 'critical' else '🟠'} Network problem",
                                         "\n".join(f"• {t}" for _, t in new.values()), color=o.SEV_COLORS[worst],
                                         footer="Ask in #roundtable-hold: 'netops, why …' for a deep investigation")],
               content=f"<@{o.OWNER_ID}>" if worst in ("critical", "high") else None,
               mention_owner=worst in ("critical", "high"))
    if cleared:
        o.post(ALERT_CH, embeds=[o.embed(AGENT, "✅ Network recovered", "\n".join(f"• {t}" for _, t in cleared.values()),
                                         color=o.SEV_COLORS["ok"])])
    o.kv_set("netops:active", {k: list(v) for k, v in now.items()})
    o.kv_set("netops:watch", {"t": snap["t"], "devices": snap["devices"]})
    up = sum(d["connected"] for d in snap["devices"])
    o.heartbeat(AGENT, f"watch: {up}/{len(snap['devices'])} devices up, {len(now)} problems")


def check():
    with o.Run(AGENT, "daily network health check") as run:
        snap = collect(full=True)
        prev = o.kv_get("netops:daily")
        probs = problems(snap, prev)
        fws = fw_changes(snap, prev)
        ov = snap.get("overview", {})
        dev_rows = [(("🟢" if d["connected"] else "🔴") + " " + d["name"], d["model"], d["fw"], f"{d['cpu']}%/{d['mem']}%",
                     f"{d['uptime_s'] // 86400}d {d['uptime_s'] % 86400 // 3600}h", d["clients"]) for d in snap["devices"]]
        inet = ", ".join(f"{i['host']} {i['avg']}ms/{i['loss']:.0f}%" for i in snap["internet"])
        gw_ok = sum(g["loss"] < 100 for g in snap["gateways"])
        routes_ok = sum(r["ok"] for r in snap["routes"])
        facts = {"problems": [t for _, _, t in probs], "firmware_changes": fws, "overview": ov,
                 "devices": snap["devices"], "internet": snap["internet"], "dns": snap["dns"],
                 "gateways_ok": f"{gw_ok}/{len(snap['gateways'])}", "routes_ok": f"{routes_ok}/{len(snap['routes'])}",
                 "failing_routes": [r for r in snap["routes"] if not r["ok"]], "alerts": snap.get("alerts", [])}
        try:
            summary = o.llm(run, "You are Torrent, the calm, precise network SRE of Hermes's homelab (TP-Link Omada: ER605 "
                                 "gateway, SG3210/SG3210X/SG2210P/ES206GP switches, 3 EAPs; 11 VLANs). Write a 3-5 sentence "
                                 "morning network briefing from these facts: overall verdict first, then anything that "
                                 "needs attention and why it matters, then one concrete recommendation if any. No "
                                 "markdown headings.\n\n" + json.dumps(facts, default=str)[:24000],
                            model="gpt-6-luna", max_output_tokens=600)
        except Exception as e:  # noqa: BLE001
            summary = f"(summary unavailable: {e})"
        sev = "ok" if not probs else min((s for _, s, _ in probs),
                                         key=lambda s: ["critical", "high", "medium", "low"].index(s))
        fields = [("Clients", f"{ov.get('totalClientNum', '?')} ({ov.get('wiredClientNum', '?')} wired / "
                              f"{ov.get('wirelessClientNum', '?')} Wi-Fi)", True),
                  ("VLAN gateways", f"{gw_ok}/{len(snap['gateways'])} reachable", True),
                  ("Internet", inet, True),
                  ("DNS", "\n".join(f"{s} ({ip}): " + ", ".join(
                      f"{'blocking' if q.get('blockprobe') else q['name']} {'✅' if q['ok'] else '❌'} {q['ms']}ms"
                      for q in snap["dns"] if q.get("server", "Pi-hole") == s) for s, ip in DNS_SERVERS.items()), False),
                  ("Traefik routes", f"{routes_ok}/{len(snap['routes'])} answering", True),
                  ("Switch ports", f"{(ov.get('totalPorts') or 0) - (ov.get('availablePorts') or 0)}/{ov.get('totalPorts', '?')} used", True)]
        if probs:
            fields.append(("⚠️ Needs attention", "\n".join(f"• {t}" for _, _, t in probs[:15]), False))
        if fws:
            fields.append(("📦 Firmware changed", "\n".join(fws), False))
        report = (f"# Network health {o.local().strftime('%Y-%m-%d')}\n\n{summary}\n\n## Devices\n"
                  + o.md_table(dev_rows, ["Device", "Model", "Firmware", "CPU/Mem", "Uptime", "Clients"])
                  + "\n\n## VLANs\n" + o.md_table([(v["vlan"], v["name"], v["subnet"], "on" if v["dhcp"] else "off",
                                                    next((("✅" if g["loss"] < 100 else "❌") for g in snap["gateways"]
                                                          if g["host"] == (v["subnet"] or "").split("/")[0]), "–"))
                                                   for v in snap["vlans"] if v.get("subnet")],
                                                  ["VLAN", "Name", "Gateway", "DHCP", "Ping"])
                  + "\n\n## Traefik routes\n" + o.md_table([(r["host"], r["code"], r["secs"]) for r in snap["routes"]],
                                                          ["Host", "HTTP", "Seconds"]))
        o.post(REPORT_CH, embeds=[o.embed(AGENT, "🌈 Morning network report", summary, fields,
                                          color=o.SEV_COLORS[sev], footer=f"Roundtable run #{run.id} · {o.money(run.cost)}")],
               files=[(f"network-{o.local().strftime('%Y%m%d')}.md", report)])
        o.kv_set("netops:daily", {k: snap[k] for k in ("t", "devices", "routes")})
        run.summary = f"{len(probs)} problems, {gw_ok}/{len(snap['gateways'])} gateways, {routes_ok}/{len(snap['routes'])} routes"


# --------------------------------------------------------------------------- Gateway ACLs (Torrent-managed)

ACL_RE = re.compile(r"\b(acls?|firewall|inter-?vlan|phase\s*[1-4]|probe)\b", re.I)


def _ctl(write=False):
    import aclctl
    return aclctl.Controller(Omada(*aclctl.admin_credentials()) if write else Omada())


def acl_status_text():
    import aclctl
    rs = aclctl.load()
    applied = o.kv_get(aclctl.KV_APPLIED, {}) or {}
    api = o.kv_get(aclctl.KV_API)
    lines = [f"Rule set v{rs['version']} `{aclctl.digest(rs)}` · applied phase **{applied.get('phase', 0)}**"
             + (f" ({applied.get('t', '')[:16]})" if applied.get("t") else ""),
             f"Write login: {'~/.omada_admin_env' if aclctl.ADMIN_ENV.exists() else os.environ.get('OMADA_USER', '?') + ' (writes need its read/write role)'} · "
             f"API format: {'✅ probed' if api else '❌ not probed yet (say: Torrent, probe the ACL API)'}"]
    errs = aclctl.validate(rs)
    lines.append("Validation: ✅ clean" if not errs else "Validation: ❌ " + "; ".join(errs[:5]))
    try:
        live, meta = _ctl().acls()
        managed = [x for x in live if aclctl.RULE_NAME.match(x.get("name", ""))]
        lines.append(f"Controller: {len(live)} gateway rules ({len(managed)} Torrent-managed, "
                     f"{len(live) - len(managed)} other)" + (" · aclDisable flag set" if meta.get("aclDisable") else ""))
        if api:
            diffs = aclctl.drift(_ctl(), rs, applied.get("phase", 0), api)
            lines.append("Drift: ✅ controller matches the rule set" if not diffs else
                         "⚠️ Drift (hand edits are undone by my next rollout unless the rule set is updated):\n"
                         + "\n".join(f"  • {d}" for d in diffs[:15]))
        lines += [f"• {'🟢' if x.get('status', True) else '⚪'} `{x['name']}`" for x in managed[:40]]
    except Exception as e:  # noqa: BLE001
        lines.append(f"Controller read failed: {e}")
    return "\n".join(lines)


def acl_plan(phase, reply_to=None, channel=None, proposal_id=None):
    import aclctl
    rs = o.kv_get(aclctl.KV_PROPOSAL + str(proposal_id))["ruleset"] if proposal_id else aclctl.load()
    errs = aclctl.validate(rs)
    channel = channel or REPORT_CH
    if errs:
        o.respond(AGENT, channel, "🌈 ACL plan blocked", "The rule set fails validation:\n" + "\n".join(f"• {e}" for e in errs),
                  reply_to=reply_to)
        return None
    if not o.kv_get(aclctl.KV_API):
        o.respond(AGENT, channel, "🌈 ACL API not probed yet",
                  "Before the first rollout I must learn this controller's ACL format with disabled throw-away rules. "
                  "Say **Torrent, probe the ACL API**.", reply_to=reply_to)
        return None
    prev = (o.kv_get(aclctl.KV_APPLIED, {}) or {}).get("phase", 0)
    cmd = [sys.executable, os.path.abspath(__file__), "acl", "apply", str(phase)] + \
          (["--proposal", str(proposal_id)] if proposal_id else [])
    new_rules = aclctl.phase_text(rs, phase, prev if not proposal_id else 0)
    rfc = {"what": f"Torrent syncs the Torrent-managed Omada Gateway ACLs (LAN→LAN only) to phase {phase} of "
                   f"`acl_ruleset.json` v{rs['version']} (`{aclctl.digest(rs)}`), creating rules in order after the "
                   f"needed IP and IP-port groups."
                   + (f" Proposal: {(o.kv_get(aclctl.KV_PROPOSAL + str(proposal_id)) or {}).get('summary', '')}"
                      if proposal_id else ""),
           "change": new_rules[:2500] + "\nInternet traffic is never filtered; rules Torrent did not create are untouched. "
                     "During the rebuild there are a few seconds with no Torrent rules (fail-open).",
           "recovery": f"Automatic: health checks right after applying (controller, Technitium + Pi-hole DNS, internet, SSH node01, "
                       f"Grafana/VLAN Flow); any failure, ❌, or no ✅ on the 'Keep these ACLs?' message within "
                       f"{aclctl.CONFIRM_MIN} min rebuilds the previous phase ({prev}). Manual: `@torrent acl off` disables "
                       "every Torrent rule at once. Last resort: Omada → Settings → Network Security → ACL → Gateway ACL, "
                       "disable the `T###-` rules. ALLOW-Admin-Infra stays first so this PC keeps controller access.",
           "risk": "A missing permit could block a service between VLANs until rolled back; return traffic depends on "
                   "the gateway's stateful handling, which the owner verifies from VLAN 10 during the confirmation window."}
    desc = ((o.kv_get(aclctl.KV_PROPOSAL + str(proposal_id)) or {}).get("summary", "") + "\n\n" if proposal_id else "") + \
        aclctl.phase_text(rs, phase, prev if not proposal_id else 0)[:3500] + \
        f"\n\nAfter applying I run health checks, then you get **{aclctl.CONFIRM_MIN} minutes** to test from your PC/phone " \
        "and react ✅ to keep it. ❌, no reaction, or a failed check rolls back to the previous state."
    return o.request_approval(AGENT, f"Gateway ACLs → phase {phase}" + (f" (proposal {proposal_id})" if proposal_id else ""),
                              desc, cmd, channel, severity="high",
                              payload={"digest": aclctl.digest(rs), "phase": phase, "proposal": proposal_id},
                              reply_to=reply_to, rfc=rfc)


def acl_health():
    checks = []
    try:
        Omada()
        checks.append(("Omada controller login", True, "ok"))
    except Exception as e:  # noqa: BLE001
        checks.append(("Omada controller login", False, str(e)[:120]))
    for s in DNS_SERVERS:
        q = dns("google.com", s)
        checks.append((f"DNS via {s}", q["ok"], f"{q['ms']} ms"))
    i = ping("1.1.1.1", 3)
    checks.append(("Internet (1.1.1.1)", i["loss"] < 100, f"loss {i['loss']:.0f}%"))
    rc, _, _ = o.ssh("node01", "true", timeout=20)
    checks.append(("SSH node01 (VLAN 20)", rc == 0, f"rc {rc}"))
    for host in ("grafana.hrmsmrflrii.xyz", "vlanflow.hrmsmrflrii.xyz"):
        h = http_check(host)
        checks.append((f"https://{host}", h["ok"], str(h["code"])))
    return all(ok for _, ok, _ in checks), "\n".join(f"{'✅' if ok else '❌'} {n} — {d}" for n, ok, d in checks)


def _confirm(channel, reply_to, text, minutes):
    mid = o.post(channel, content=f"<@{o.OWNER_ID}>", embeds=[o.embed(AGENT, "🌈 Keep these ACLs?", text,
                                                                      color=o.SEV_COLORS["high"],
                                                                      footer=f"✅ keep · ❌ roll back · no answer in {minutes} min = roll back")],
                 reply_to=reply_to, mention_owner=True)
    if not mid:
        return False
    o.react(channel, mid, "✅"); time.sleep(0.4); o.react(channel, mid, "❌")
    end = time.time() + minutes * 60
    while time.time() < end:
        time.sleep(15)
        if o.OWNER_ID in o.reactors(channel, mid, "❌"):
            return False
        if o.OWNER_ID in o.reactors(channel, mid, "✅"):
            return True
    return False


def acl_apply(phase, proposal_id=None):
    import aclctl
    import hierarchy
    approval = hierarchy.require_approval(AGENT)
    ch, reply = approval["channel_id"], approval["message_id"]
    rs = o.kv_get(aclctl.KV_PROPOSAL + str(proposal_id))["ruleset"] if proposal_id else aclctl.load()
    if aclctl.digest(rs) != approval["payload"].get("digest") or approval["payload"].get("phase") != phase:
        raise PermissionError("Rule set changed after approval; plan again")
    errs = aclctl.validate(rs)
    if errs:
        raise RuntimeError("Validation failed: " + "; ".join(errs))
    api = o.kv_get(aclctl.KV_API)
    if not api:
        raise RuntimeError("ACL API format not probed")
    prev = o.kv_get(aclctl.KV_APPLIED, {}) or {}
    prev_rs, prev_phase = prev.get("ruleset") or rs, prev.get("phase", 0)
    log_lines = []

    def log(m):
        log_lines.append(m)
        o.log(AGENT, f"acl: {m}")

    with o.Run(AGENT, f"Gateway ACLs → phase {phase}", "approval") as run:
        report = "(health checks not run)"

        def rollback(reason):
            log(f"rolling back: {reason}")
            try:
                aclctl.sync(_ctl(write=True), prev_rs, prev_phase, api, log)
                restored = f"Restored phase {prev_phase}."
            except Exception as error:  # noqa: BLE001
                restored = f"⚠️ Rollback itself failed: {error}. Say `@torrent acl off` to disable my rules."
            o.respond(AGENT, ch, "↩️ ACLs rolled back", f"{reason}\n{restored}\n\n{report}\n\n"
                      + "\n".join(log_lines[-15:]), reply_to=reply, run=run)
            run.summary = f"phase {phase} rolled back: {reason}"

        try:
            changed = aclctl.sync(_ctl(write=True), rs, phase, api, log)
        except Exception as error:  # noqa: BLE001
            rollback(f"Applying failed: {error}")
            raise SystemExit(1)
        time.sleep(5)
        ok, report = acl_health()
        if not ok:
            rollback("A health check failed right after applying")
            raise SystemExit(1)
        kept = _confirm(ch, reply, f"Phase {phase} is live ({'changed' if changed else 'already in place'}).\n\n"
                                   f"**React ✅ on THIS message to keep it** (the approval ✅ only started the change).\n\n"
                                   f"{report}\n\nTest from your PC and phone (VLAN Flow: https://vlanflow.hrmsmrflrii.xyz "
                                   f"→ Denied only).", aclctl.CONFIRM_MIN)
        if not kept:
            rollback("Not confirmed with ✅ in time (or ❌)")
            raise SystemExit(1)
        if proposal_id:
            backup = aclctl.RULESET.with_name(f"acl_ruleset.json.bak-{o.local().strftime('%Y%m%d-%H%M%S')}")
            backup.write_text(aclctl.RULESET.read_text(encoding="utf-8"), encoding="utf-8")
            aclctl.RULESET.write_text(json.dumps(rs, indent=2) + "\n", encoding="utf-8")
            log(f"rule set file updated (backup {backup.name})")
        o.kv_set(aclctl.KV_APPLIED, {"phase": phase, "ruleset": rs, "t": o.iso()})
        o.respond(AGENT, ch, f"✅ Gateway ACLs at phase {phase}", report + "\n\n" + "\n".join(log_lines[-15:]),
                  reply_to=reply, run=run)
        run.summary = f"phase {phase} applied and confirmed"


def acl_probe():
    import aclctl
    import hierarchy
    approval = hierarchy.require_approval(AGENT)
    lines = []
    with o.Run(AGENT, "probe Omada ACL API", "approval") as run:
        cand, meta = aclctl.probe(_ctl(write=True), aclctl.api_candidates(), lines.append)
        if cand:
            o.kv_set(aclctl.KV_API, cand)
        o.respond(AGENT, approval["channel_id"], "🌈 ACL API probe " + ("✅ learned" if cand else "❌ failed"),
                  ("Format stored; ACL rollouts can be planned now." if cand else
                   "No body format was accepted. Nothing was left behind; send me the log below.") + "\n\n" + "\n".join(lines[-20:]),
                  reply_to=approval["message_id"], run=run)
        run.summary = f"probe {'ok' if cand else 'failed'}"
        if not cand:
            raise SystemExit(1)


def acl_off(reply_to=None, channel=None):
    import aclctl
    lines = []
    with o.Run(AGENT, "ACL emergency off", "chat") as run:
        n = aclctl.disable_all(_ctl(write=True), lines.append)
        o.kv_set(aclctl.KV_APPLIED, {"phase": 0, "ruleset": aclctl.load(), "t": o.iso(), "disabled": True})
        o.respond(AGENT, channel or ALERT_CH, "🛑 Torrent-managed ACLs disabled",
                  f"{n} rules switched off (kept on the controller, not deleted). Re-enable with a new phase rollout.",
                  reply_to=reply_to, run=run)
        run.summary = f"disabled {n} rules"


def acl_ask(question, reply_to=None, channel=None):
    import aclctl
    channel = channel or "roundtable-hold"
    q = question.lower()
    if re.search(r"\b(acl off|turn off (the )?acls?|disable (all )?(the )?acls?|emergency|panic|locked out)\b", q):
        return acl_off(reply_to, channel)
    if re.search(r"\bprobe\b", q):
        return o.request_approval(AGENT, "Probe Omada ACL API (disabled throw-away rules)",
                                  "Torrent creates two **disabled** test rules and one test group, reads them back to learn "
                                  "this controller's ACL format, then deletes them. No traffic is affected.",
                                  [sys.executable, os.path.abspath(__file__), "acl", "probe"], channel,
                                  severity="medium", reply_to=reply_to)
    m = re.search(r"\bphase\s*([1-4])\b", q)
    if m and re.search(r"\b(apply|enable|roll ?out|go|start|begin|run|execute|proceed|retry|redo|deploy|move|do)\b", q):
        return acl_plan(int(m.group(1)), reply_to, channel)
    if re.fullmatch(r"\W*(acls?\s*)?(status|show( me)?( the)? acls?|acl status)\W*", q) or q.strip() in ("acl", "acls"):
        return o.respond(AGENT, channel, "🌈 Gateway ACLs", acl_status_text(), reply_to=reply_to)
    o.transcript(AGENT, "Request", question, "discord", channel, reply_to or "")
    with o.Run(AGENT, f"acl ask: {question[:200]}", "chat") as run:
        rs = aclctl.load()
        prompt = f"""You are Torrent, the network engineer of Hermes's homelab, and you own the Omada Gateway ACLs.
QUESTION from Master Hermes: {question}

The rule set you manage (source of truth, applied phase {(o.kv_get(aclctl.KV_APPLIED, {}) or {}).get('phase', 0)}):
{json.dumps(rs, indent=1)[:18000]}

Live status:
{acl_status_text()[:3000]}

Investigate READ-ONLY. Tools on this host (codex-agent-lxc 192.168.40.16):
- Omada (read-only): python3 {os.path.abspath(__file__)} omada /setting/firewall/acls?type=0&currentPage=1&currentPageSize=200
- ACL decision log (VLAN Flow, only logging rules appear):
  curl -s -H 'Host: vlanflow.hrmsmrflrii.xyz' 'http://192.168.40.13:8787/api/trace?src=IP&dst=IP&port=N&proto=TCP&minutes=1440'
  curl -s -H 'Host: vlanflow.hrmsmrflrii.xyz' 'http://192.168.40.13:8787/api/state?minutes=60'   (events, flows, recent syslog)
- ping, nc, dig, traceroute, curl; ssh <host> per ~/.ssh/sre-hosts.txt.
Never POST/PUT/PATCH/DELETE to Omada yourself: changes happen only through your approved proposal, which runs with
rollback protection.

Policy you must keep: LAN->LAN only (internet is never filtered); VLAN 10 is one-way (nothing starts connections into
VLAN 10 unless the rule has "reverse_exception" with the reason); every deny logs; ALLOW-Admin-Infra stays first;
a permit must have a lower "order" than any deny covering the same VLANs (orders 0-100, one decimal; e.g. 24.5);
prefer narrow host groups and ports over VLAN-wide permits.

Answer in Markdown: **Findings** (evidence), **Explanation**, **Recommendation**. If a rule change is needed, end with
exactly one fenced block:
```acl-proposal
{{"summary": "one line", "phase": <phase to apply at, normally the applied phase>, "upsert": [<full rule objects>], "remove": ["RULE-NAME"], "groups": {{"G_NAME": ["ip", ...]}}}}
```
Omit the block when no change is needed."""
        text, transcript, _ = o.codex_tiered(run, prompt + o.ESCALATE_RULE, o.needs_escalation, sandbox="danger",
                                             timeout=1500, tag="acl")
        text = None if o.needs_escalation(text) else text
        run.summary = (text or "no answer")[:300]
        o.respond(AGENT, channel, "🌈 ACL investigation", text or f"No answer (transcript `{transcript}`)",
                  reply_to=reply_to, run=run)
        m = re.search(r"```acl-proposal\s*(\{.*?\})\s*```", text or "", re.S)
        if not m:
            return
        try:
            prop = json.loads(m.group(1))
            new = aclctl.apply_proposal(rs, prop)
        except (ValueError, KeyError, TypeError) as e:
            o.post(channel, f"⚠️ Torrent's proposal was not valid JSON/rules: {e}", reply_to=reply_to)
            return
        errs = aclctl.validate(new)
        if errs:
            o.post(channel, "⚠️ Proposal rejected by the safety checks:\n" + "\n".join(f"• {e}" for e in errs[:10]),
                   reply_to=reply_to)
            return
        pid = int(time.time())
        o.kv_set(aclctl.KV_PROPOSAL + str(pid), {"ruleset": new, "summary": prop.get("summary", ""), "t": o.iso()})
        phase = int(prop.get("phase") or (o.kv_get(aclctl.KV_APPLIED, {}) or {}).get("phase", 0) or 1)
        acl_plan(phase, reply_to, channel, proposal_id=pid)


def ask(question, reply_to=None, channel=None):
    if ACL_RE.search(question):
        return acl_ask(question, reply_to, channel)
    if re.search(r"\b(restart|remediate|apply|change|fix)\b", question, re.I):
        import council
        return council.ask(question, reply_to, channel, aid=AGENT)
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", question, "discord", channel, reply_to or "")
    with o.Run(AGENT, f"ask: {question[:200]}", "chat") as run:
        import inventory
        try:
            inv, _ = inventory.scoped_text(AGENT, "network")
        except Exception as e:  # noqa: BLE001
            inv = f"(inventory unavailable: {e})"
        prompt = f"""You are Torrent, the network engineer reporting to Corhyn, the ITOps Lead.
Expertise: Proxmox bridges and VLAN-aware networking, Omada, firewalls, OPNsense, ACLs, multicast/mDNS
and printer ports (IPP/631, JetDirect/9100, LPR/515 and discovery only as the device requires).
Never open ports indiscriminately; verify model, driver, VLAN and direction. For unsupported controller APIs,
provide reviewed manual steps instead of claiming a change. You are an expert in VLANs, inter-VLAN routing/ACLs, DHCP, DNS
(primary Technitium 192.168.90.60 on homelab-dns01, web console https://dns.hrmsmrflrii.xyz; secondary Pi-hole + Unbound
192.168.90.53 on CT202, kept in sync by dnssync.py), Traefik reverse proxy, and the TP-Link Omada ecosystem (controller v6 at 192.168.0.101,
ER605 gateway 192.168.0.1, switches SG3210 / SG3210X-M2 / SG2210P / ES206GP, EAP610 / EAP603-Outdoor / EAP225).
{inventory.boundary_text(AGENT)}

LIVE network inventory (Omada, refreshed every ~2 min; use it first, it already answers counting questions):
{inv}

QUESTION from Master Hermes: {question}

If the inventory above answers it, answer directly without running tools. Otherwise investigate READ-ONLY.
Fresh inventory any time: python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inventory.py')} network
Grafana / Glance network views: python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'observe.py')} grafana "Omada Network Overview" --agent netops
  (also "Network Utilization"; glance network --agent netops)
Tools available on this host (codex-agent-lxc, 192.168.40.16, VLAN 40):
- Omada controller (read-only account): python3 {os.path.abspath(__file__)} omada <api-path>
  e.g. /devices, /dashboard/overviewDiagram, /setting/lan/networks?currentPage=1&currentPageSize=100,
  /clients?currentPage=1&currentPageSize=100, /insight/clients?currentPage=1&currentPageSize=100,
  /alerts?currentPage=1&currentPageSize=50, /setting/firewall/acls?type=0&currentPage=1&currentPageSize=100,
  /setting/lan/profiles?currentPage=1&currentPageSize=100, /setting/wlans
- ping, traceroute, dig, nc, curl. ssh <host> to any homelab host (aliases in ~/.ssh/sre-hosts.txt), e.g.
  ssh traefik-lxc (Traefik dynamic config in /opt/traefik/config/dynamic), ssh pihole, ssh homelab-dns01 (Technitium,
  systemctl status dns; read-only API: python3 dnssync.py status), ssh node01 (ip a, bridge vlan).
- Technitium metrics in Prometheus: technitium_probe_success, technitium_queries_last_hour{{result=...}}.
- Prometheus: curl -s 'http://192.168.40.13:9090/api/v1/query' --data-urlencode 'query=...' (omada_* metrics).
Do NOT change anything (no POST/PUT to Omada, no config edits, no restarts).

Answer in Markdown: **Findings** (with evidence), **Root cause / explanation**, **Recommended fix** with exact
Omada controller click-path or commands, and **Risk** of the fix. If no fix is needed, say so. Be concise."""
        text, transcript, used = o.codex_tiered(run, prompt + o.ESCALATE_RULE, o.needs_escalation, sandbox="danger",
                                                timeout=1500, tag="ask")
        text = None if o.needs_escalation(text) else text
        run.summary = (text or "no answer")[:300]
        o.respond(AGENT, channel, "🌈 Network investigation", text or (o.BUDGET_STOP_TEXT if run.budget_stopped else f"No answer (transcript `{transcript}`)"),
                  reply_to=reply_to, run=run)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["watch", "check", "ask", "omada", "acl"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("arg2", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    ap.add_argument("--proposal", type=int)
    a = ap.parse_args()
    if a.cmd == "acl":
        if a.arg == "status":
            print(acl_status_text())
        elif a.arg == "plan":
            print("approval", acl_plan(int(a.arg2), a.reply_to, a.channel, a.proposal))
        elif a.arg == "apply":
            acl_apply(int(a.arg2), a.proposal)
        elif a.arg == "probe":
            acl_probe()
        elif a.arg == "off":
            acl_off(a.reply_to, a.channel)
        else:
            ap.error("acl status | plan <phase> | apply <phase> | probe | off")
        return
    if a.cmd == "watch":
        watch()
    elif a.cmd == "check":
        check()
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    else:
        path = a.arg if a.arg.startswith("/") else "/" + a.arg
        print(json.dumps(Omada().get(path), indent=1)[:60000])


if __name__ == "__main__":
    main()
