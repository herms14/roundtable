#!/usr/bin/env python3
"""Lab Feed - one read-only JSON view of the lab, shared by the Glance dashboard and the Roundtable agents.

Glance (CT200) renders it; agents read the same numbers (observe.py scrapes Glance pages, or call these functions
directly), so "how's the lab?" never disagrees with what Master Hermes sees. Everything comes from live sources:
Prometheus (Proxmox, Synology, Omada exporters), the NAS backup API, the Service Version Manager and the Roundtable
database. No secrets are returned. Private agents are never listed.

HTTP (port 8790): GET /api/{overview,nas,network,services,roundtable} with header X-Labfeed-Token (or from
localhost). GET /update/<service> shows a confirm page; its POST files a Roundtable approval (Hewg) - the update
only runs after the owner's ✅ in Discord, and the SVM key never leaves this host.

CLI: labfeed.py serve | check | overview | nas | network | services | roundtable | svm-update <service>
"""
import argparse
import contextlib
import datetime as dt
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import olympus_core as o

AGENT = "builder"  # Hewg owns container/patch delivery, so update requests are his approvals
PORT = int(os.environ.get("LABFEED_PORT", "8790"))
PROM = os.environ.get("PROM_URL", "http://192.168.40.13:9090")
SVM = os.environ.get("SVM_URL", "http://192.168.40.13:5070")
BACKUP_API = os.environ.get("BACKUP_API_URL", "http://192.168.40.13:9102")
SPEEDTEST = os.environ.get("SPEEDTEST_URL", "http://192.168.40.13:3000")
VLANS = {"1": "Default", "10": "Internal", "20": "Homelab", "30": "IoT", "40": "Production", "50": "Guest",
         "80": "HyperV", "90": "Management"}
DISK_HEALTH = {1: "Normal", 2: "Warning", 3: "Critical", 4: "Failing"}
DISK_STATUS = {1: "Normal", 2: "Initialized", 3: "Not initialized", 4: "System partition failed", 5: "Crashed"}
RAID_STATUS = {1: "Normal", 2: "Repairing", 3: "Migrating", 4: "Expanding", 5: "Deleting", 6: "Creating",
               7: "RaidSyncing", 8: "RaidParityChecking", 9: "RaidAssembling", 10: "Canceling", 11: "Degraded",
               12: "Crashed", 13: "DataScrubbing", 14: "RaidDeploying", 15: "RaidUnDeploying",
               16: "RaidMountCache", 17: "RaidUnmountCache", 18: "RaidExpandingUnfinishedSHR",
               19: "RaidConvertSHRToPool", 20: "RaidMigrateSHR1ToSHR2", 21: "RaidUnknownStatus"}
CACHE_S = 30
_cache, _lock = {}, threading.Lock()
_nonces = {}  # nonce -> (service, expires)


# --------------------------------------------------------------------------- helpers

def cached(key, fn, ttl=CACHE_S):
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time() + ttl, val)
    return val


def prom(q):
    code, d = o.http("GET", f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": q}), timeout=10, retries=2)
    if code != 200 or not isinstance(d, dict):
        raise RuntimeError(f"Prometheus HTTP {code}")
    return [(r["metric"], float(r["value"][1])) for r in d["data"]["result"]]


def get_json(url, timeout=10):
    code, d = o.http("GET", url, timeout=timeout, retries=2)
    if code != 200 or not isinstance(d, (dict, list)):
        raise RuntimeError(f"{url.split('/')[2]} HTTP {code}")
    return d


def guard(fn):
    """Run one section; a broken source becomes an 'unknown' card instead of failing the whole feed."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "error": str(e)[:160]}


def ago(ts):
    if not ts:
        return None
    try:
        t = o.parse_iso(ts) if ts.endswith("Z") else dt.datetime.fromisoformat(ts).replace(tzinfo=o.TZ)
    except ValueError:
        return None
    s = int((o.utcnow() - t).total_seconds())
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 172800 else f"{s // 86400}d ago"


def worst(*states):
    order = ["ok", "info", "unknown", "warn", "crit"]
    return max((s for s in states if s), key=order.index, default="ok")


# --------------------------------------------------------------------------- sections

def by_id(q):
    """The PVE exporter is scraped through every node, so each series arrives once per instance: key by id."""
    return {m["id"]: (m, v) for m, v in prom(q)}


def cluster():
    nodes = sorted((i.split("/", 1)[1], v) for i, (_, v) in by_id('pve_up{id=~"node/.*"}').items())
    info = {i: m for i, (m, _) in by_id("pve_guest_info").items()}
    tpl = {i for i, m in info.items() if m.get("template") == "1"}
    guests = {i: x for i, x in by_id('pve_up{id=~"(qemu|lxc)/.*"}').items() if i not in tpl}
    run = {k: sum(1 for i, (_, v) in guests.items() if i.startswith(k) and v == 1) for k in ("qemu", "lxc")}
    tot = {k: sum(1 for i in guests if i.startswith(k)) for k in ("qemu", "lxc")}
    stopped = sorted(info.get(i, {}).get("name", i) for i, (_, v) in guests.items() if v != 1)
    up = sum(1 for _, v in nodes if v == 1)
    return {"state": "ok" if up == len(nodes) and nodes else "crit",
            "nodes": [{"name": n, "up": v == 1} for n, v in nodes], "nodes_up": up, "nodes_total": len(nodes),
            "vms_running": run["qemu"], "vms_total": tot["qemu"], "cts_running": run["lxc"], "cts_total": tot["lxc"],
            "stopped": stopped, "templates": len(tpl)}


def nas():
    by = lambda q, lbl: {m[lbl]: v for m, v in prom(q)}  # noqa: E731
    ids = {m["diskIndex"]: m["synologyDiskID"] for m, _ in prom("synologyDiskID")}
    models = {m["diskIndex"]: m["synologyDiskModel"] for m, _ in prom("synologyDiskModel")}
    temp, health, status = (by(q, "diskIndex") for q in ("synologyDiskTemperature", "synologyDiskHealthStatus",
                                                          "synologyDiskStatus"))
    disks = []
    for i in sorted(ids, key=int):
        h, s = int(health.get(i, 0)), int(status.get(i, 0))
        ok = h == 1 and s == 1
        disks.append({"bay": ids[i], "model": models.get(i, ""), "temp_c": int(temp.get(i, 0)),
                      "kind": "NVMe" if re.search(r"M\.2|nvme|cache", ids[i], re.I) else "HDD",
                      "health": DISK_HEALTH.get(h, f"code {h}"), "status": DISK_STATUS.get(s, f"code {s}"),
                      "state": "ok" if ok else "crit" if h >= 3 or s >= 4 else "warn"})
    names = {m["raidIndex"]: m["synologyRaidName"] for m, _ in prom("synologyRaidName")}
    rstat, rtot, rfree = (by(q, "raidIndex") for q in ("synologyRaidStatus", "synologyRaidTotalSize",
                                                       "synologyRaidFreeSize"))
    raids = []
    for i in sorted(names, key=int):
        st, total, free = int(rstat.get(i, 0)), rtot.get(i, 0), rfree.get(i, 0)
        vol = names[i].lower().startswith("volume")  # a pool's "free" is unallocated space, not free data space
        raids.append({"name": names[i], "status": RAID_STATUS.get(st, f"code {st}"),
                      "state": "ok" if st in (1, 13) else "crit" if st in (11, 12) else "warn",
                      "total_tb": round(total / 1e12, 2), "used_tb": round((total - free) / 1e12, 2),
                      "used_pct": round(100 * (total - free) / total, 1) if total and vol else None,
                      "is_volume": vol})
    sys_status = next((int(v) for _, v in prom("synologySystemStatus")), 0)
    sys_temp = next((int(v) for _, v in prom("synologySystemTemperature")), 0)
    vol = next((r for r in raids if r["is_volume"]), None)
    state = worst("ok" if sys_status == 1 else "crit", *(d["state"] for d in disks), *(r["state"] for r in raids),
                  "warn" if vol and vol["used_pct"] and vol["used_pct"] >= 85 else "ok")
    return {"state": state, "system": "Normal" if sys_status == 1 else "Failed", "temp_c": sys_temp,
            "disks": disks, "disks_ok": sum(d["state"] == "ok" for d in disks), "disks_total": len(disks),
            "raids": raids, "volume": vol, "hottest_c": max((d["temp_c"] for d in disks), default=0)}


def backups():
    d = get_json(f"{BACKUP_API}/status")
    jobs = d.get("job_status") or {}
    stale = []
    for name, j in jobs.items():
        try:
            age_h = (o.local().replace(tzinfo=None) - dt.datetime.strptime(j["last_backup"], "%Y-%m-%d %H:%M")
                     ).total_seconds() / 3600
        except (KeyError, ValueError):
            age_h = None
        j["age_h"] = age_h and round(age_h, 1)
        if j.get("status") != "success" or (name == "daily" and (age_h or 99) > 36):
            stale.append(name)
    tpl = {i for i, (m, _) in by_id("pve_guest_info").items() if m.get("template") == "1"}
    missing = sorted(m.get("name") or i for i, (m, _) in by_id("pve_not_backed_up_info").items() if i not in tpl)
    nb = len(missing)
    return {"state": "crit" if any(jobs.get(n, {}).get("status") not in ("success", None) for n in jobs)
            else "warn" if stale or nb else "ok",
            "jobs": [{"name": n, **j} for n, j in jobs.items()], "last_sync": d.get("last_sync"),
            "sync_status": d.get("status"), "schedule": d.get("schedule"), "not_backed_up": nb,
            "not_backed_up_names": missing,
            "sizes": {"daily": d.get("daily_size"), "main": d.get("main_size")}}


def services():
    summary = get_json(f"{SVM}/api/summary")
    cats, online, total, updates, down = [], 0, 0, 0, []
    for c in summary.get("categories") or []:
        d = get_json(f"{SVM}/api/services/{c['key']}")
        svcs = [{k: s.get(k) for k in ("name", "display_name", "health", "current_version", "update_available",
                                        "can_update", "type", "icon", "web_url", "host")} for s in d.get("services", [])]
        on = sum(s["health"] == "online" for s in svcs)
        up = sum(bool(s["update_available"]) for s in svcs)
        down += [s["display_name"] for s in svcs if s["health"] == "offline"]
        cats.append({"key": c["key"], "name": c["name"], "online": on, "total": len(svcs), "updates": up,
                     "services": svcs})
        online, total, updates = online + on, total + len(svcs), updates + up
    crit_down = any(s["health"] == "offline" for c in cats if c["key"] in ("infrastructure", "auth")
                    for s in c["services"])
    return {"state": "crit" if crit_down else "warn" if down else "ok", "online": online, "total": total,
            "offline": total - online, "offline_names": down, "updates_available": updates, "categories": cats}


def network():
    clients = {m.get("vlan_id"): int(v) for m, v in prom("count by (vlan_id)(omada_client_download_activity_bytes)")}
    devices = prom("omada_device_uptime_seconds")
    cpu = {m.get("device"): v for m, v in prom("omada_device_cpu_percentage")}
    upgrade = sum(1 for _, v in prom("omada_device_need_upgrade") if v == 1)
    vlans = [{"id": int(k), "name": n, "subnet": f"192.168.{0 if k == '1' else k}.0/24", "clients": clients.get(k, 0)}
             for k, n in VLANS.items()]
    speed = guard(lambda: get_json(f"{SPEEDTEST}/api/speedtest/latest"))
    s = speed.get("data") if isinstance(speed, dict) and isinstance(speed.get("data"), dict) else speed
    return {"state": "ok" if devices else "warn", "devices": len(devices), "devices_need_upgrade": upgrade,
            "busiest_device_cpu": max(cpu.values(), default=0), "clients": sum(clients.values()), "vlans": vlans,
            "speedtest": {k: s.get(k) for k in ("download", "upload", "ping", "created_at")
                          if isinstance(s, dict)} if isinstance(s, dict) else None}


def _kv_items(ns, field):
    return list(((o.kv_get(ns, {}) or {}).get(field) or {}).values())


def attention():
    import hierarchy
    inc = [i for i in _kv_items("medic", "incidents") if i.get("state") not in ("fixed", "resolved", "superseded")]
    fnd = [f for f in _kv_items("security", "findings") if f.get("state") in ("awaiting", "manual", "open")]
    gaps = [g for g in _kv_items("access_gaps", "gaps") if g.get("state") == "open"
            and not hierarchy.private(g.get("agent") or "")]
    alerts = [a for a in _kv_items("watcher_state", "alerts") if isinstance(a, dict) and a.get("fired")
              and not a.get("resolved")]
    with contextlib.closing(o.db()) as con:
        aps = [dict(r) for r in con.execute(
            "SELECT id, agent, title, severity, created FROM approvals WHERE status='pending' ORDER BY id DESC")]
    aps = [a for a in aps if not hierarchy.private(a["agent"] or "")]
    title = lambda i: ((i.get("diag") or i.get("expl") or {}).get("title") or i.get("title")  # noqa: E731
                       or (i.get("finding") or {}).get("type") or i.get("resource") or "issue")
    return {
        "incidents": [{"id": i.get("id"), "title": title(i), "state": i.get("state"), "since": ago(i.get("first"))}
                      for i in inc][:8],
        "findings": [{"id": f.get("id"), "title": title(f), "severity": f.get("severity"), "state": f.get("state"),
                      "since": ago(f.get("first"))} for f in sorted(
                          fnd, key=lambda f: ["critical", "high", "medium", "low"].index(f.get("severity", "low"))
                          if f.get("severity") in ("critical", "high", "medium", "low") else 9)][:8],
        "access_gaps": [{"id": g.get("id"), "agent": o.agent_info(g.get("agent", "")).get("name", g.get("agent")),
                         "resource": g.get("resource"), "since": ago(g.get("first_seen"))} for g in gaps][:8],
        "alerts": [{"title": a.get("title") or a.get("name"), "since": ago(a.get("fired"))} for a in alerts][:8],
        "approvals": [{"id": a["id"], "agent": o.agent_info(a["agent"]).get("name", a["agent"]),
                       "title": a["title"], "severity": a["severity"], "since": ago(a["created"])} for a in aps][:8],
        "counts": {"incidents": len(inc), "findings": len(fnd), "high_findings": sum(
            f.get("severity") in ("critical", "high") for f in fnd), "access_gaps": len(gaps),
            "alerts": len(alerts), "approvals": len(aps)}}


def timers():
    try:
        out = subprocess.run(["systemctl", "list-timers", "--all", "--no-pager", "-o", "json"], capture_output=True,
                             text=True, timeout=10).stdout
        rows = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    res = []
    for r in rows:
        if not r.get("next"):
            continue
        t = dt.datetime.fromtimestamp(r["next"] / 1e6, dt.timezone.utc)
        res.append({"unit": r["unit"].removesuffix(".timer"), "next": o.local(t).strftime("%a %H:%M"),
                    "in_min": int((t - o.utcnow()).total_seconds() // 60), "_t": t})
    res.sort(key=lambda r: r["_t"])
    return [{k: v for k, v in r.items() if k != "_t"} for r in res if r["in_min"] >= 1][:10]


def roundtable():
    import hierarchy
    roster = [a for a in json.loads((Path(__file__).with_name("agents.json")).read_text())["agents"]
              if not hierarchy.private(a["id"])]
    day0 = o.iso(o.local().replace(hour=0, minute=0, second=0, microsecond=0))
    with contextlib.closing(o.db()) as con:
        last = {r["agent"]: dict(r) for r in con.execute(
            """SELECT agent, status, task, started, ended, summary FROM runs WHERE id IN
               (SELECT MAX(id) FROM runs GROUP BY agent)""")}
        today = {r["agent"]: (r["n"], r["c"]) for r in con.execute(
            "SELECT agent, COUNT(*) n, COALESCE(SUM(cost_usd),0) c FROM runs WHERE started>=? GROUP BY agent", (day0,))}
        recent = [dict(r) for r in con.execute(
            "SELECT agent, status, task, started, summary FROM runs ORDER BY id DESC LIMIT 60")]
        cost_today = con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE started>=?", (day0,)).fetchone()[0]
    ids = {a["id"] for a in roster}
    team = []
    for a in roster:
        lr = last.get(a["id"]) or {}
        n, c = today.get(a["id"], (0, 0.0))
        team.append({"id": a["id"], "name": a["name"], "role": a.get("role", "").split("·")[0].strip(),
                     "emoji": a.get("emoji", ""), "color": a.get("color", ""), "channel": a.get("channel"),
                     "working": lr.get("status") == "running", "last_status": lr.get("status"),
                     "last_run": ago(lr.get("started")), "runs_today": n, "cost_today": round(c, 3),
                     "doing": (lr.get("task") or "")[:90] if lr.get("status") == "running" else None})
    team.sort(key=lambda t: (not t["working"], -t["runs_today"], t["name"]))
    activity = [{"agent": o.agent_info(r["agent"]).get("name", r["agent"]), "status": r["status"],
                 "when": ago(r["started"]), "what": re.sub(r"\s+", " ", (r["summary"] or r["task"] or ""))[:140]}
                for r in recent if r["agent"] in ids][:12]
    return {"team": team, "working": sum(t["working"] for t in team), "agents": len(team),
            "cost": {"today": round(cost_today, 2), "month": round(o.month_spend(), 2),
                     "budget": round(o.budget_limit(), 0)},
            "attention": attention(), "timers": timers(), "activity": activity}


def overview():
    cl, na, bk, sv, nw = (guard(f) for f in (cluster, nas, backups, services, network))
    at = guard(attention)
    cnt = at.get("counts", {}) if isinstance(at, dict) else {}
    issues = []
    if cl.get("state") == "crit":
        issues.append(f"{cl.get('nodes_total', 0) - cl.get('nodes_up', 0)} Proxmox node(s) down")
    if na.get("state") not in ("ok", None):
        issues.append("NAS needs attention")
    if bk.get("state") not in ("ok", None):
        issues.append("backups need attention")
    if sv.get("offline"):
        issues.append(f"{sv['offline']} service(s) offline")
    if cnt.get("incidents"):
        issues.append(f"{cnt['incidents']} open incident(s)")
    if cnt.get("high_findings"):
        issues.append(f"{cnt['high_findings']} high security finding(s)")
    state = worst(*(x.get("state") for x in (cl, na, bk, sv, nw)), "warn" if cnt.get("incidents") else "ok")
    return {"time": o.local().strftime("%Y-%m-%d %H:%M"), "state": state,
            "headline": "All systems normal" if not issues else "; ".join(issues),
            "cluster": cl, "nas": {k: v for k, v in na.items() if k not in ("disks", "raids")},
            "backups": bk, "services": {k: v for k, v in sv.items() if k != "categories"}, "network": nw,
            "attention": cnt}


# --------------------------------------------------------------------------- update requests (approval-gated)

def _updatable(name):
    for c in cached("services", services, 60).get("categories", []):
        for s in c["services"]:
            if s["name"] == name and s.get("can_update"):
                return s
    return None


def request_update(name):
    svc = _updatable(name)
    if not svc:
        return None, f"{name} is not a one-click-updatable service"
    title = f"Update {svc['display_name']} container"
    with contextlib.closing(o.db()) as con:
        dup = con.execute("SELECT id FROM approvals WHERE status='pending' AND title LIKE ?", (title + "%",)).fetchone()
    if dup:
        return dup["id"], "already waiting for your ✅"
    aid = o.request_approval(
        AGENT, title, f"Requested from the Glance Services page. Pulls the latest image for **{svc['display_name']}** "
        f"(now v{svc.get('current_version')}) on {svc.get('host')} via the Service Version Manager and recreates it.",
        ["python3", str(Path(__file__).resolve()), "svm-update", name], "homelab-infrastructure", severity="low",
        payload={"source": "glance", "service": name})
    return aid, "posted"


def svm_update(name):
    key = os.environ.get("SVM_UPDATE_KEY")
    if not key:
        sys.exit("SVM_UPDATE_KEY missing in ~/.olympus_env")
    code, d = o.http("GET", f"{SVM}/api/update/{urllib.parse.quote(name)}?key={urllib.parse.quote(key)}",
                     timeout=600, retries=1)
    print(json.dumps({"http": code, "result": d if not isinstance(d, dict) else
                      {k: v for k, v in d.items() if k != "api_key"}}, ensure_ascii=False)[:2000])
    sys.exit(0 if code == 200 else 1)


# --------------------------------------------------------------------------- dashboard self-check (Sellen)

GLANCE = os.environ.get("GLANCE_URL", "http://192.168.40.12:8080")


def check():
    """Open every Glance page and list widgets that render an error; Sellen posts only when that set changes."""
    code, home = o.http("GET", GLANCE + "/", timeout=20, retries=2)
    if code != 200 or not isinstance(home, str):
        broken = {"Glance itself": f"HTTP {code}"}
    else:
        broken = {}
        for slug in dict.fromkeys(re.findall(r'href="/([^"/.?#]+)"', home)):
            c, page = o.http("GET", f"{GLANCE}/api/pages/{slug}/content/", timeout=60, retries=1)
            if c != 200 or not isinstance(page, str):
                broken[f"{urllib.parse.unquote(slug)} page"] = f"HTTP {c}"
                continue
            for w in re.split(r'(?=<div class="widget widget-type-)', page)[1:]:
                if "widget-error" in w:
                    t = re.search(r"<h2[^>]*>\s*(?:<a[^>]*>)?\s*([^<]{1,80})", w)
                    title = t.group(1).strip() if t else "untitled widget"
                    while html.unescape(title) != title:   # Glance double-escapes titles (&amp;amp;)
                        title = html.unescape(title)
                    broken[f"{urllib.parse.unquote(slug)} / {title}"] = "error"
    prev = o.kv_get("glance_check", {}).get("broken", {})
    o.kv_set("glance_check", {"broken": broken, "checked": o.iso()})
    new, fixed = sorted(set(broken) - set(prev)), sorted(set(prev) - set(broken))
    if new or fixed:
        lines = [f"🔴 **{k}** - {broken[k]}" for k in new] + [f"🟢 recovered: {k}" for k in fixed]
        o.post("infrastructure-alerts", embeds=[o.embed("watcher", "Glance dashboard check", "\n".join(lines)[:3500],
               footer="Lab Feed self-check · https://glance.hrmsmrflrii.xyz")])
    print(json.dumps({"broken": broken, "new": new, "fixed": fixed}, ensure_ascii=False))
    return broken


# --------------------------------------------------------------------------- HTTP

ROUTES = {"overview": overview, "nas": nas, "network": network, "services": services, "roundtable": roundtable,
          "cluster": cluster, "backups": backups}
PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Lab Feed</title><style>body{{font:15px system-ui;background:#0f1419;color:#e6e9ef;display:grid;
place-items:center;min-height:100vh;margin:0}}.c{{background:#171d25;padding:28px;border-radius:14px;max-width:420px;
border:1px solid #2a3340}}button{{background:#e8a33d;border:0;color:#111;padding:10px 18px;border-radius:8px;
font-weight:600;cursor:pointer}}a{{color:#8ab4f8}}</style><div class=c>{body}</div>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        tok = os.environ.get("LABFEED_TOKEN", "")
        return self.client_address[0] in ("127.0.0.1", "::1") or (
            tok and secrets.compare_digest(self.headers.get("X-Labfeed-Token", ""), tok))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        m = re.fullmatch(r"/api/(\w+)", path)
        if m and m.group(1) in ROUTES:
            if not self._authed():
                return self._send(401, {"error": "token required"})
            try:
                return self._send(200, cached(m.group(1), ROUTES[m.group(1)]))
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": str(e)[:200]})
        m = re.fullmatch(r"/update/([\w.-]+)", path)
        if m:
            svc = _updatable(m.group(1))
            if not svc:
                return self._send(404, PAGE.format(body="That service can't be updated from here."), "text/html")
            nonce = secrets.token_urlsafe(16)
            now = time.time()
            for k in [k for k, (_, exp) in _nonces.items() if exp < now]:
                _nonces.pop(k, None)
            _nonces[nonce] = (svc["name"], now + 600)
            n = html.escape(svc["display_name"])
            return self._send(200, PAGE.format(body=(
                f"<h3>Update {n}?</h3><p>Hewg will post an approval in <b>#homelab-infrastructure</b>. Nothing runs "
                f"until you react ✅ there.</p><form method=post><input type=hidden name=n value={nonce}>"
                f"<button>Request update</button></form>")), "text/html")
        if path == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        m = re.fullmatch(r"/update/([\w.-]+)", urllib.parse.urlparse(self.path).path.rstrip("/"))
        ln = min(int(self.headers.get("Content-Length") or 0), 4096)
        nonce = urllib.parse.parse_qs(self.rfile.read(ln).decode()).get("n", [""])[0]
        svc, exp = _nonces.pop(nonce, (None, 0))
        if not m or svc != m.group(1) or exp < time.time():
            return self._send(403, PAGE.format(body="This request expired. Go back and press Update again."),
                              "text/html")
        try:
            aid, how = request_update(svc)
        except Exception as e:  # noqa: BLE001
            return self._send(502, PAGE.format(body=f"Couldn't post the approval: {html.escape(str(e)[:200])}"),
                              "text/html")
        body = (f"<h3>Approval #{aid} {html.escape(how)}</h3><p>React ✅ on it in <b>#homelab-infrastructure</b> to "
                f"run the update.</p><p><a href='https://glance.hrmsmrflrii.xyz/services'>Back to Services</a></p>"
                if aid else html.escape(how))
        self._send(200, PAGE.format(body=body), "text/html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["serve", "svm-update", "check", *ROUTES])
    ap.add_argument("name", nargs="?")
    a = ap.parse_args()
    if a.cmd == "serve":
        o.log("labfeed", f"listening on :{PORT}")
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    elif a.cmd == "check":
        check()
    elif a.cmd == "svm-update":
        svm_update(a.name or sys.exit("service name required"))
    else:
        print(json.dumps(ROUTES[a.cmd](), indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
