#!/usr/bin/env python3
"""Blaidd - Roundtable checker: trusts nothing that has not been re-checked with his own nose.

  checker.py run                      # daily 05:10: patch/update verification (Agent 6) + re-check of every guest
                                      # Hewg built and every incident Corhyn fixed in the last 48 h
  checker.py verify <host|ip|vmid>    # ad-hoc independent health check of one guest
  checker.py ask "<text>" [--reply-to MID] [--channel CID]
  checker.py show                     # recent verdicts

Deterministic and read-only: he never fixes. A failed re-check of a Corhyn fix is handed back to Corhyn.
"""
import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
import time

import olympus_core as o

AGENT = "checker"
CHANNEL = "homelab-infrastructure"
CTL = "ansible-controller01"
KV_VERDICTS, KV_SEEN = "checker:verdicts", "checker:seen"
ICON = {"pass": "✅", "warn": "🟡", "fail": "❌"}
# Proxmox LXC template first-boot job; it fails once before ssl-cert exists and is harmless.
BENIGN_FAILED_UNITS = {"proxmox-regenerate-snakeoil.service"}

CHECK = r"""S=""; [ "$(id -u)" != 0 ] && S="sudo -n"
echo "HOST|$(hostname)"
echo "STATE|$(systemctl is-system-running 2>/dev/null)"
echo "FAILED|$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"
echo "DISK|$(df -P / | awk 'NR==2{print $5}' | tr -d '%')"
echo "MEM|$(free -m | awk '/^Mem:/{printf "%d", ($2-$7)*100/$2}')"
echo "UPTIME|$(cut -d. -f1 /proc/uptime)"
[ -f /var/run/reboot-required ] && echo "REBOOT|yes"
if command -v docker >/dev/null 2>&1; then
  echo "DOCKER|$($S docker version --format '{{.Server.Version}}' 2>/dev/null || echo down)"
  $S docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null | sed 's/^/CTR|/'
fi
"""


def _resolve(target):
    """Return (name, ip, guest) for a VMID, name, hostname alias or IP."""
    import inventory
    t = str(target).strip().lower()
    inv = inventory.get("compute")
    for g in inv.get("guests", []) if isinstance(inv, dict) else []:
        ips = [i.strip() for i in (g.get("ip") or "").split(",") if i.strip()]
        if t in (str(g["id"]), (g.get("name") or "").lower()) or t in ips:
            return g.get("name"), (ips[0] if ips else None), g
    for h in o.load_hosts().values():
        if t in (h.get("name", "").lower(), h.get("ip")):
            return h["name"], h.get("ip"), None
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", t):
        return t, t, None
    return None, None, None


def _run_check(name, ip):
    """Prefer the agent's SSH alias; fall back to the controller with the homelab key (guests built by Hewg)."""
    aliases = {h["name"] for h in o.load_hosts().values()}
    if name in aliases:
        rc, out, err = o.ssh(name, CHECK, timeout=60)
        if rc == 0 or out:
            return out, f"ssh {name}"
    if ip:
        script = (f"ssh -i ~/.ssh/homelab_ed25519 -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "
                  f"-o UserKnownHostsFile=~/hewg/known_hosts hermes-admin@{ip} bash -s <<'BLAIDD_EOF'\n{CHECK}\nBLAIDD_EOF")
        rc, out, err = o.ssh(CTL, script, timeout=90)
        if rc == 0 or out:
            return out, "via ansible-controller01"
        return None, (err or f"rc {rc}")[:200]
    return None, "no SSH route"


def verify_guest(target, expect=None, quiet=False, reason=""):
    """Independent health check. Returns {verdict, problems, info, target}."""
    expect = expect or {}
    name, ip, g = _resolve(target)
    problems, info = [], []
    if not name and not ip:
        return {"target": str(target), "verdict": "fail", "problems": [f"unknown target {target}"], "info": []}
    if g:
        info.append(f"Proxmox: {g['kind']} {g['id']} on {g['node']} {g['status']}")
        if g["status"] != "running":
            problems.append(f"Proxmox says {g['status']}")
    if ip:
        up = subprocess.run(["ping", "-c", "2", "-W", "2", ip], capture_output=True).returncode == 0
        info.append(f"ping {ip}: {'ok' if up else 'no reply'}")
        if not up:
            problems.append(f"{ip} does not answer ping")
    out, how = _run_check(name, ip)
    if out is None:
        problems.append(f"SSH failed ({how})")
    else:
        kv = {}
        ctrs = []
        for line in out.splitlines():
            k, _, v = line.partition("|")
            if k == "CTR":
                ctrs.append(v.split("|"))
            else:
                kv[k] = v.strip()
        info.append(f"SSH ok ({how}), host `{kv.get('HOST')}`, systemd {kv.get('STATE')}")
        if expect.get("name") and kv.get("HOST") and kv["HOST"] != expect["name"]:
            problems.append(f"hostname is {kv['HOST']}, expected {expect['name']}")
        failed_units = [u for u in (kv.get("FAILED") or "").split() if u not in BENIGN_FAILED_UNITS]
        if failed_units:
            problems.append(f"failed units: {' '.join(failed_units)}")
        elif kv.get("FAILED"):
            info.append("ignored known first-boot unit " + kv["FAILED"].strip())
        disk = int(kv.get("DISK") or 0)
        mem = int(kv.get("MEM") or 0)
        info.append(f"disk / {disk}% · memory {mem}%")
        if disk >= 90:
            problems.append(f"root disk {disk}% full")
        if mem >= 95:
            problems.append(f"memory {mem}% used")
        if kv.get("REBOOT"):
            info.append("reboot pending")
        if expect.get("docker") and not kv.get("DOCKER"):
            problems.append("Docker expected but not installed")
        if kv.get("DOCKER") == "down":
            problems.append("Docker daemon not responding")
        elif kv.get("DOCKER"):
            bad = [c[0] for c in ctrs if len(c) > 2 and ("unhealthy" in c[2] or c[1] in ("restarting", "dead"))]
            exited = [c[0] for c in ctrs if len(c) > 1 and c[1] == "exited"]
            info.append(f"Docker {kv['DOCKER']}: {len(ctrs)} container(s)" + (f", exited: {', '.join(exited[:6])}"
                                                                          if exited else ""))
            if bad:
                problems.append(f"unhealthy/restarting containers: {', '.join(bad[:8])}")
    hard = [p for p in problems if any(w in p for w in ("SSH failed", "does not answer", "Proxmox says", "unknown",
                                                         "not responding", "hostname is"))]
    verdict = "fail" if hard else ("warn" if problems else "pass")
    rec = {"target": name or ip, "ip": ip, "verdict": verdict, "problems": problems, "info": info,
           "reason": reason, "t": o.iso()}
    v = o.kv_get(KV_VERDICTS, []) or []
    o.kv_set(KV_VERDICTS, (v + [rec])[-60:])
    if not quiet:
        o.log(AGENT, f"verify {rec['target']}: {verdict} {problems}")
    return rec


def _patch_verify():
    """Agent 6 (verify_agent.py, home dir) re-checks every patch/update run; it posts to GitLab/Discord itself."""
    sys.path.insert(0, str(o.HOME))
    try:
        import verify_agent
    except Exception as e:  # noqa: BLE001
        return [], f"verify_agent unavailable: {e}"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = verify_agent.run(hours=72, min_age=20)
    return res, None


def recent_changes(hours=48):
    """Guests Hewg built and hosts Corhyn fixed recently, not yet re-checked."""
    seen = o.kv_get(KV_SEEN, {}) or {}
    since = o.utcnow().timestamp() - hours * 3600
    out = []
    for name, b in (o.kv_get("builder:builds", {}) or {}).items():
        t = b.get("built")
        if b.get("status") == "built" and t and o.parse_iso(t).timestamp() > since and seen.get(f"b:{name}") != t:
            s = b.get("spec", {})
            out.append({"key": f"b:{name}", "stamp": t, "target": s.get("ip") or name, "why": f"Hewg built {name}",
                        "expect": {"docker": s.get("docker"), "name": name}})
    for key, inc in ((o.kv_get("medic", {}) or {}).get("incidents") or {}).items():
        t = inc.get("fixed_at")
        if inc.get("state") == "fixed" and t and o.parse_iso(t).timestamp() > since and seen.get(f"m:{key}") != t:
            host = (inc.get("finding") or {}).get("host") or ""
            targets = [host] if host else [c.get("target", "").split(":")[0].split("/")[0]
                                           for c in (inc.get("fix") or {}).get("changes", [])[:3]]
            for h in [t_ for t_ in dict.fromkeys(targets) if t_]:
                out.append({"key": f"m:{key}", "stamp": t, "target": h, "incident": inc.get("id"),
                            "why": f"Corhyn fixed incident #{inc.get('id')}: {(inc.get('diag') or {}).get('title', '')[:80]}"})
    return out


def run_daily(reply_to=None, channel=None):
    channel = channel or CHANNEL
    with o.Run(AGENT, "daily verification", "chat" if reply_to else "schedule") as run:
        patch, err = _patch_verify()
        changes = recent_changes()
        seen = o.kv_get(KV_SEEN, {}) or {}
        lines = []
        for p in patch:
            lines.append(f"{ICON[p['verdict']]} patch #{p['iid']} `{p['host']}`"
                         + (f": {'; '.join(p['problems'])[:200]}" if p["problems"] else ""))
        reopened = []
        for c in changes:
            name, _, _ = _resolve(c["target"])
            if not name:
                continue  # not a guest/host Blaidd can reach (e.g. a router config)
            v = verify_guest(c["target"], c.get("expect"), reason=c["why"])
            seen[c["key"]] = c["stamp"]
            lines.append(f"{ICON[v['verdict']]} {c['why']} → `{v['target']}`"
                         + (f": {'; '.join(v['problems'])[:200]}" if v["problems"] else ""))
            if v["verdict"] == "fail" and c.get("incident"):
                reopened.append(c)
                subprocess.Popen([sys.executable, str(o.CODE_DIR / "medic.py"), "ask",
                                  f"Blaidd's independent re-check FAILED after incident #{c['incident']} was marked fixed "
                                  f"({c['why']}). Problems on {v['target']}: {'; '.join(v['problems'])}. Re-diagnose.",
                                  "--channel", "infrastructure-alerts"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                 cwd=str(o.CODE_DIR))
        o.kv_set(KV_SEEN, seen)
        run.summary = (f"{len(patch)} patch run(s), {len(changes)} change(s) re-checked"
                       + (f", {len(reopened)} sent back to Corhyn" if reopened else "") + (f"; {err}" if err else ""))
        if lines or reply_to:
            o.respond(AGENT, channel, "🐺 Blaidd's verification round",
                      ("\n".join(lines) or "Nothing changed that needs re-checking, Master Hermes. My nose is quiet.")
                      + ("\n\n🩺 Sent back to Corhyn: " + ", ".join(f"#{c['incident']}" for c in reopened)
                         if reopened else ""), reply_to=reply_to, run=run,
                      color=o.SEV_COLORS["high" if any(l.startswith("❌") for l in lines) else "ok"])
        return lines


def verdicts_text(n=15):
    v = list(reversed(o.kv_get(KV_VERDICTS, []) or []))[:n]
    lines = [f"{ICON[x['verdict']]} {x['t'][:16].replace('T', ' ')} UTC `{x['target']}`"
             + (f" ({x['reason']})" if x.get("reason") else "")
             + (f": {'; '.join(x['problems'])[:200]}" if x["problems"] else "") for x in v]
    sys.path.insert(0, str(o.HOME))
    try:
        import verify_agent
        pv = sorted(verify_agent.load_verifications(72).values(), key=lambda x: x["ts"], reverse=True)[:10]
        lines += ["", "**Patch verifications (72 h)**"] + [
            f"{ICON.get(x['verdict'], '?')} #{x['iid']} `{x['host']}` {x['ts'][:16]}"
            + (f": {'; '.join(x['problems'])[:150]}" if x["problems"] else "") for x in pv]
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines) or "No verdicts yet."


INTENT = o.S(action=o.E("verify", "run", "show", "answer"), targets=o.A(o.STR))


def ask(text, reply_to=None, channel=None):
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", text, "discord", channel, reply_to or "")
    with o.Run(AGENT, f"ask: {text[:200]}", "chat") as run:
        intent = o.llm(run, f"""Classify Master Hermes's request to Blaidd, the homelab verification agent.
verify: re-check specific hosts/guests now (targets = VMIDs, names or IPs exactly as said).
run: run the full verification round now (recent patches, builds, fixes).
show: list recent verdicts.
answer: anything else. targets=[] unless verify.
Request: {text}""", schema=INTENT, model="gpt-6-luna")
        act = intent["action"]
        if act == "verify":
            out = []
            for t in intent["targets"][:6]:
                v = verify_guest(t, reason="asked by Master Hermes")
                out.append(f"{ICON[v['verdict']]} **{v['target']}** {v['verdict'].upper()}\n"
                           + "\n".join(f"  ❗ {p}" for p in v["problems"]) + ("\n" if v["problems"] else "")
                           + "\n".join(f"  · {i}" for i in v["info"]))
            run.summary = "; ".join(x.split("\n")[0] for x in out)[:300]
            o.respond(AGENT, channel, "🐺 Blaidd sniffed it out", "\n\n".join(out) or "Which host, Master Hermes?",
                      reply_to=reply_to, run=run)
            return
        if act == "show":
            o.respond(AGENT, channel, "🐺 Blaidd's recent verdicts", verdicts_text(), reply_to=reply_to, run=run)
            return
        if act == "answer":
            ans = o.llm(run, f"""You are Blaidd the Half-Wolf, the Roundtable's verifier: loyal, suspicious, trusts nothing
not re-checked with his own nose. You independently re-check patches, container updates, Hewg's new guests and
Corhyn's fixes; you never fix anything. Answer Master Hermes briefly in Discord Markdown using ONLY this data.
Tell him he can say "Blaidd, check <host>" for a fresh check.

RECENT VERDICTS:
{verdicts_text()}

QUESTION: {text}""", model="gpt-6-luna", max_output_tokens=900)
            o.respond(AGENT, channel, "🐺 Blaidd answers", ans or "No answer.", reply_to=reply_to, run=run)
            return
    run_daily(reply_to, channel)


VOICE_TOOLS = [
    {"type": "function", "name": "verify_host",
     "description": "Blaidd's independent health re-check of one host/guest (VMID, name or IP): Proxmox state, ping, "
                    "SSH, systemd failed units, disk, memory, Docker containers. Read-only, ~10 s.",
     "parameters": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"],
                    "additionalProperties": False}},
    {"type": "function", "name": "recent_verdicts",
     "description": "Blaidd's recent verdicts: re-checks of patches, updates, Hewg's builds and Corhyn's fixes.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def voice_tool(name, args):
    if name == "verify_host":
        return verify_guest(args.get("target", ""), reason="voice")
    if name == "recent_verdicts":
        return {"verdicts": verdicts_text().replace("**", "")}
    return {"error": f"unknown tool {name}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "verify", "ask", "show"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "run":
        print("\n".join(run_daily()) or "nothing to verify")
    elif a.cmd == "verify":
        print(json.dumps(verify_guest(a.arg, reason="cli"), indent=1))
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    else:
        print(verdicts_text())


if __name__ == "__main__":
    main()
