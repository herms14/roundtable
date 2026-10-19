#!/usr/bin/env python3
"""Corhyn - ITOps lead and incident response; Hewg owns patch delivery through ~/sre_agent.py.

  medic.py scan [--dry]          # every 5 min (medic-scan.timer)
  medic.py diagnose <key>        # (internal) Codex read-only diagnosis -> Discord + approval
  medic.py fix <incident-id>     # run by Gideon after ✅: apply the approved fix, verify, report, document
  medic.py ask "<symptom>" [--reply-to MID] [--channel CID]
  medic.py list

Detects: crash-looping / restarting / unhealthy / OOM-killed / errored-exit containers, failed systemd units,
unreachable hosts and failed patch runs (GitLab `patch-failed`). Each incident: full diagnosis (what happened,
root cause, impact + severity, exact fix plan, risk, rollback) -> owner ✅ -> fix -> verification -> status report ->
GitLab issue closed -> Miriel documents it in the vault (changelog, troubleshooting, service doc).
"""
import argparse
import concurrent.futures as cf
import contextlib
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import olympus_core as o

AGENT = "medic"
CHANNEL = "infrastructure-alerts"
HOME = Path.home()
SRE_LOCK = HOME / ".sre-agent" / "executor.lock"
SRE_HISTORY = HOME / ".sre-agent" / "history.jsonl"
DOC_AGENT = HOME / "doc_agent.py"
GITLAB = os.environ.get("SRE_GITLAB_BASE", "http://192.168.40.23/api/v4")
PROJECT = int(os.environ.get("SRE_GITLAB_PROJECT", "6"))
SKIP_HOSTS = {"codex-agent-lxc"}
CONFIRM_SCANS = 2            # condition must be seen in 2 consecutive scans (~10 min) before diagnosis
UNREACHABLE_SCANS = 3
MAX_DIAG_PER_SCAN = 2
MUTE_DAYS = 7                # rejected incidents stay quiet this long unless their state changes
DIAG_MODEL, FIX_MODEL = "gpt-6.1-sol", "gpt-6.1-sol"
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}

SCAN_SCRIPT = r"""
D=""
if command -v docker >/dev/null 2>&1; then
  if docker ps -q >/dev/null 2>&1; then D="docker"; elif sudo -n docker ps -q >/dev/null 2>&1; then D="sudo -n docker"; fi
fi
if [ -n "$D" ]; then
  ids=$($D ps -aq)
  [ -n "$ids" ] && $D inspect $ids --format '{{.Name}}|{{.State.Status}}|{{.RestartCount}}|{{.State.ExitCode}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.HostConfig.RestartPolicy.Name}}|{{.State.OOMKilled}}|{{.State.FinishedAt}}|{{index .Config.Labels "com.docker.compose.project"}}|{{.Config.Image}}' | sed 's/^/C|/'
fi
systemctl --failed --no-legend --plain 2>/dev/null | awk '{print "U|"$1}'
echo "OK|$(hostname)"
"""


# --------------------------------------------------------------------------- state

def load():
    return o.kv_get("medic", {"incidents": {}, "restarts": {}, "unreach": {}, "next_id": 1, "baseline": False})


def save(st):
    o.kv_set("medic", st)


def find(st, iid):
    for k, inc in st["incidents"].items():
        if inc["id"] == int(iid):
            return k, inc
    return None, None


def owner(st, finding_key):
    """Most recent incident that covers this finding key."""
    best = None
    for inc in st["incidents"].values():
        if finding_key in inc.get("keys", []) and (best is None or inc["id"] > best["id"]):
            best = inc
    return best


PRIORITY = {"unreachable": 0, "crash-loop": 1, "exited": 2, "unhealthy": 3, "failed-unit": 4, "patch-failed": 5}


def group(todo):
    """One incident per host (patch failures stay separate: they own a GitLab issue). Most urgent first."""
    groups = {}
    for k, f in todo:
        gk = k if f["type"] == "patch-failed" else f.get("host", k)
        groups.setdefault(gk, []).append((k, f))
    out = []
    for items in groups.values():
        items.sort(key=lambda kf: PRIORITY.get(kf[1]["type"], 9))
        if len(items) == 1:
            out.append(([items[0][0]], items[0][1]))
            continue
        host = items[0][1].get("host", "")
        out.append(([k for k, _ in items], {
            "type": items[0][1]["type"], "host": host, "items": [f for _, f in items],
            "summary": f"{len(items)} problems on {host}: " + "; ".join(f["summary"] for _, f in items)}))
    out.sort(key=lambda kf: PRIORITY.get(kf[1]["type"], 9))
    return out


# Units that always fail inside unprivileged LXCs (no kernel/apparmor/network management) - not incidents.
LXC_NOISE = ("apparmor.service", "systemd-logind.service", "systemd-networkd.service", "systemd-networkd.socket",
             "systemd-networkd-wait-online.service", "systemd-modules-load.service", "systemd-sysctl.service",
             "systemd-journald-audit.socket", "sys-kernel-", "console-getty.service", "systemd-remount-fs.service")


def ignored(st, key, finding, hosts):
    if any(key.startswith(p) for p in st.get("ignore", [])):  # owner-managed ignore list (kv medic.ignore)
        return True
    if finding["type"] == "failed-unit" and hosts.get(finding["host"], {}).get("type") == "lxc":
        return finding["unit"].startswith(LXC_NOISE)
    return False


@contextlib.contextmanager
def sre_lock(wait=0):
    """Hold the SRE executor lock so incident fixes never overlap a patch run (and vice versa)."""
    SRE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(SRE_LOCK, "w")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                f.close()
                yield False
                return
            time.sleep(10)
    try:
        yield True
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


# --------------------------------------------------------------------------- GitLab

def gl(method, path, data=None):
    return o.http(method, f"{GITLAB}/projects/{PROJECT}{path}", {"PRIVATE-TOKEN": os.environ.get("GITLAB_TOKEN", "")}, data)


def gl_issue_create(title, body, labels):
    code, d = gl("POST", "/issues", {"title": title[:250], "description": body, "labels": ",".join(labels)})
    return d.get("iid") if code == 201 and isinstance(d, dict) else None


def gl_note(iid, body):
    if iid:
        gl("POST", f"/issues/{iid}/notes", {"body": body[:60000]})


def gl_close(iid, labels_add=()):
    if iid:
        gl("PUT", f"/issues/{iid}", {"state_event": "close", "add_labels": ",".join(labels_add)})


# --------------------------------------------------------------------------- detection

def scan_host(name):
    rc, out, err = o.ssh(name, SCAN_SCRIPT, timeout=45)
    return name, rc, out, err


def detect(st):
    """Return {key: finding} for everything currently wrong."""
    hosts_info = o.load_hosts()
    hosts = [h for h in hosts_info if h not in SKIP_HOSTS]
    found = {}
    restarts = st.setdefault("restarts", {})
    unreach = st.setdefault("unreach", {})
    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(scan_host, hosts))
    for name, rc, out, err in results:
        if "OK|" not in out:
            unreach[name] = unreach.get(name, 0) + 1
            if unreach[name] >= UNREACHABLE_SCANS:
                found[f"unreachable:{name}"] = {"type": "unreachable", "host": name,
                                                "summary": f"{name} unreachable over SSH ({err[:120] or f'rc {rc}'})"}
            continue
        unreach.pop(name, None)
        for line in out.splitlines():
            p = line.split("|")
            if p[0] == "U" and len(p) > 1:
                unit = p[1]
                found[f"unit:{name}:{unit}"] = {"type": "failed-unit", "host": name, "unit": unit,
                                                "summary": f"systemd unit {unit} failed on {name}"}
            elif p[0] == "C" and len(p) >= 11:
                cname, status, rcount, exitc, health, policy, oom, finished, proj, image = (
                    p[1].lstrip("/"), p[2], int(p[3] or 0), int(p[4] or 0), p[5], p[6], p[7] == "true", p[8], p[9], p[10])
                rk = f"{name}:{cname}"
                prev = restarts.get(rk, rcount)
                restarts[rk] = rcount
                base = {"host": name, "container": cname, "project": proj, "image": image, "restart_count": rcount}
                if status == "restarting" or rcount - prev >= 3:
                    found[f"ctr:{name}:{cname}"] = dict(base, type="crash-loop",
                                                        summary=f"{cname} on {name} is crash-looping "
                                                                f"(restarts {rcount}, exit {exitc})")
                elif health == "unhealthy":
                    found[f"ctr:{name}:{cname}"] = dict(base, type="unhealthy",
                                                        summary=f"{cname} on {name} is unhealthy")
                elif status in ("exited", "dead") and policy in ("always", "unless-stopped", "on-failure") \
                        and (exitc not in (0, 143) or oom) and not cname.endswith(("-init", "_init", "-migrate", "-setup")):
                    why = "OOM-killed" if oom else f"exit code {exitc}"
                    found[f"ctr:{name}:{cname}"] = dict(base, type="exited",
                                                        summary=f"{cname} on {name} is down ({why}, since {finished[:16]})")
    # Failed automated patch runs
    code, issues = gl("GET", "/issues?labels=patch-failed&state=opened&per_page=50")
    if code == 200 and isinstance(issues, list):
        for i in issues:
            found[f"patchfail:{i['iid']}"] = {"type": "patch-failed", "host": i["title"].split(":", 1)[-1].strip(),
                                              "gitlab_iid": i["iid"], "summary": f"Automated patch run failed: {i['title']}"}
    return {k: f for k, f in found.items() if not ignored(st, k, f, hosts_info)}


# --------------------------------------------------------------------------- diagnosis

DIAG_SCHEMA = o.S(
    title=o.STR, severity=o.E("critical", "high", "medium", "low"), impact=o.STR, what_happened=o.STR,
    root_cause=o.STR, evidence=o.A(o.STR), confidence=o.E("low", "medium", "high"),
    fix_plan=o.A(o.STR), commands=o.A(o.STR), risk=o.STR, rollback=o.STR, expected_downtime=o.STR,
    auto_fixable=o.BOOL, manual_reason=o.STR, verification=o.A(o.STR), prevention=o.STR, still_broken=o.BOOL,
)

SEVERITY_GUIDE = """Severity: critical = lab-wide outage or data at risk (Proxmox node, NAS/RAID, Traefik, Authentik,
Pi-hole DNS, PBS, a database corrupting); high = a user-facing app is down (Immich, Jellyfin/Arr, Home Assistant,
GitLab, Paperless, Grafana, Sentinel bot); medium = degraded or non-critical service down, a failed unit with no
user impact yet, a failed patch run; low = cosmetic, one-shot/helper containers, noise."""

READONLY_RULES = """You are STRICTLY READ-ONLY in this step. Allowed: docker ps/logs/inspect/stats/compose ps/config,
cat/grep/ls of configs and logs, journalctl, systemctl status/cat/list-units, df, free, uptime, ss, ip, ping, dig,
curl to health endpoints, pvesh get, pct/qm status/config/list, apt list/policy. Forbidden: starting/stopping/restarting
anything, editing/deleting files, docker pull/up/down/rm/prune, package changes, reboots, kill.
Logs for every Docker host are also in Loki: curl -sG http://192.168.40.13:3100/loki/api/v1/query_range
--data-urlencode 'query={host="<host>",container="<name>"}' --data-urlencode limit=200 (labels: host, container, unit).
Keep it focused: at most ~30 commands."""


def diagnose(key, finding, symptom=None):
    host = finding.get("host", "")
    with o.Run(AGENT, f"diagnose {key}", "incident" if not symptom else "chat") as run:
        triage = ""
        if finding.get("gitlab_iid") and SRE_HISTORY.exists():
            for line in SRE_HISTORY.read_text().splitlines()[-400:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("kind") == "triage" and e.get("iid") == finding["gitlab_iid"]:
                    triage = f"Earlier automated triage: {e.get('root_cause')} -> {e.get('suggested_fix')}"
        prompt = f"""You are Corhyn, the incident-response SRE for Hermes's homelab (Proxmox cluster node01-03, Docker hosts,
LXCs; full topology in ~/.ssh/sre-hosts.txt as name|ip|user|type). SSH aliases work for every host (ssh <name>, sudo -n
available). Proxmox layer: ssh node01 (pvesh/pct/qm).

INCIDENT: {finding['summary']}
Details: {json.dumps(finding)}
{('Owner report: ' + symptom) if symptom else ''}
{triage}

{READONLY_RULES}

Produce a full diagnosis:
- what_happened: timeline in plain English (when it started, what the logs show).
- root_cause: the actual cause, not the symptom. If unsure, say what is most likely and confidence low.
- evidence: short quotes of log lines / command output that prove it.
- impact + severity. {SEVERITY_GUIDE}
- fix_plan: ordered, concrete steps a careful engineer would take, including taking a backup of any file before
  editing it and a DB dump before touching a database container. commands: the exact commands for those steps.
- risk, rollback (how to undo each change), expected_downtime, verification (checks proving it is fixed), prevention.
- auto_fixable=true only if the fix can be done safely over SSH without physical access, without deleting user data
  or volumes, and without guessing secrets. Otherwise false and explain in manual_reason (else "").
- still_broken: false if the problem has already cleared by itself while you looked."""
        # Luna first (~20x cheaper input); the strong model re-diagnoses only when Luna returns nothing, is unsure,
        # or calls it critical
        res, transcript, used = o.codex_tiered(run, prompt, lambda r: not r or r.get("confidence") == "low"
                                               or r.get("severity") == "critical", schema=DIAG_SCHEMA,
                                               sandbox="danger", timeout=1200, tag="diag", critical=True)
        run.summary = (res or {}).get("title", "no diagnosis")
        return res, str(transcript), run


def post_diagnosis(st, key, inc, d, run, reply_to=None, channel=None):
    channel = channel or CHANNEL
    sev = d["severity"]
    body = (f"**What happened**\n{d['what_happened']}\n\n**Root cause** ({d['confidence']} confidence)\n{d['root_cause']}\n\n"
            f"**Impact**\n{d['impact']}")
    fields = [("Severity", f"{SEV_ICON[sev]} **{sev.upper()}**", True),
              ("Expected downtime", d["expected_downtime"][:200] or "none", True),
              ("Incident", f"#{inc['id']}" + (f" · GitLab #{inc['gitlab_iid']}" if inc.get("gitlab_iid") else ""), True),
              ("🔎 Evidence", "\n".join(f"• {e}" for e in d["evidence"][:6]), False),
              ("🛠️ Fix plan", "\n".join(f"{i + 1}. {s}" for i, s in enumerate(d["fix_plan"][:10])), False),
              ("⚠️ Risk", d["risk"], True), ("↩️ Rollback", d["rollback"], True),
              ("✅ Verification", "\n".join(f"• {v}" for v in d["verification"][:6]), False)]
    full = (f"# Incident #{inc['id']} — {d['title']}\n\nSeverity: {sev} · Confidence: {d['confidence']}\n\n"
            f"## What happened\n{d['what_happened']}\n\n## Root cause\n{d['root_cause']}\n\n## Impact\n{d['impact']}\n\n"
            "## Evidence\n" + "\n".join(f"- {e}" for e in d["evidence"]) + "\n\n## Fix plan\n"
            + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(d["fix_plan"])) + "\n\n## Commands\n```bash\n"
            + "\n".join(d["commands"]) + f"\n```\n\n## Risk\n{d['risk']}\n\n## Rollback\n{d['rollback']}\n\n"
            "## Verification\n" + "\n".join(f"- {v}" for v in d["verification"]) + f"\n\n## Prevention\n{d['prevention']}\n")
    e = o.embed(AGENT, f"{SEV_ICON[sev]} Incident #{inc['id']} · {d['title']}", body, fields,
                color=o.SEV_COLORS[sev], footer=f"Roundtable run #{run.id} · {DIAG_MODEL} · diagnosis {o.money(run.cost)}")
    mid = o.post(channel, embeds=[e], files=[(f"incident-{inc['id']}-diagnosis.md", full)], reply_to=reply_to)
    if reply_to:
        o.transcript(AGENT, "Corhyn", full, "discord", channel, reply_to)
    inc["diag_msg"] = mid
    if d["auto_fixable"]:
        nb = None if sev in ("critical", "high") else o.next_window()
        inc["approval"] = o.request_approval(
            AGENT, f"Fix incident #{inc['id']}: {d['title']}",
            f"Corhyn will apply the {len(d['fix_plan'])}-step fix plan above on `{inc['finding'].get('host')}`, "
            f"verify it, report back here and have Miriel update the vault.",
            [sys.executable, str(Path(__file__).resolve()), "fix", str(inc["id"])], channel, severity=sev,
            not_before=nb, reply_to=mid,
            rfc={"what": "\n".join(f"{i + 1}. {s}" for i, s in enumerate(d["fix_plan"])) +
                         (f"\n\nCommands:\n```bash\n" + "\n".join(d["commands"])[:1500] + "\n```" if d.get("commands") else ""),
                 "change": f"{d['impact']}\n\nHost: `{inc['finding'].get('host')}` · expected downtime: "
                           f"{d['expected_downtime'] or 'none'} · severity {sev}.",
                 "recovery": f"{d['rollback']}\n\nVerification:\n" + "\n".join(f"- {v}" for v in d["verification"]),
                 "risk": d["risk"]},
            fields=[("When", "Immediately after ✅" if not nb else
                     f"Next maintenance window ({o.local(nb).strftime('%a %d %b %H:%M')}) — {sev} severity", True)])
        inc["state"] = "awaiting"
    else:
        o.post(channel, f"🩺 Incident #{inc['id']} needs you — not safe to automate: {d['manual_reason'][:1500]}",
               reply_to=mid, mention_owner=sev in ("critical", "high"))
        inc["state"] = "manual"
    return full


def open_incident(st, keys, finding, symptom=None, reply_to=None, channel=None):
    for old in st["incidents"].values():
        if old.get("state") in ("diag-failed", "muted") and set(old.get("keys", [])) & set(keys):
            old["state"] = "superseded"
    inc = {"id": st["next_id"], "first": o.iso(), "keys": keys}  # a recurrence is a new incident
    st["next_id"] += 1
    key = f"inc:{inc['id']}"
    inc.update(finding=finding, state="diagnosing", last=o.iso(), diag_started=o.iso())
    st["incidents"][key] = inc
    save(st)
    o.log(AGENT, f"incident #{inc['id']} {keys}: diagnosing")
    d, transcript, run = diagnose(key, finding, symptom)
    st = load()
    inc = st["incidents"][key]
    if not d:
        inc["state"] = "diag-failed"
        o.post(channel or CHANNEL, embeds=[o.embed(AGENT, f"Incident #{inc['id']} · diagnosis failed",
                                                   f"{finding['summary']}\nCodex returned no diagnosis. Transcript: `{transcript}`",
                                                   color=o.SEV_COLORS["high"])], reply_to=reply_to)
        save(st)
        return
    inc["diag"], inc["transcript"] = d, transcript
    if not d["still_broken"] and not symptom:
        inc["state"] = "resolved"
        inc["resolution_evidence"] = f"Diagnostic investigation found condition cleared: {d['root_cause']}"
        o.post(channel or CHANNEL, embeds=[o.embed(AGENT, f"Incident #{inc['id']} cleared on its own",
                                                   f"{finding['summary']}\n\n**Root cause:** {d['root_cause']}",
                                                   color=o.SEV_COLORS["ok"])])
        save(st)
        return
    if not finding.get("gitlab_iid"):
        inc["gitlab_iid"] = gl_issue_create(
            f"Incident #{inc['id']}: {d['title']}",
            f"**Detected by Corhyn** {o.local().strftime('%Y-%m-%d %H:%M')}\n\n{finding['summary']}\n\n"
            f"**Severity:** {d['severity']}\n\n**Root cause:** {d['root_cause']}\n\n**Impact:** {d['impact']}\n\n"
            "**Fix plan:**\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(d["fix_plan"])),
            ["incident", f"severity::{d['severity']}"])
    else:
        inc["gitlab_iid"] = finding["gitlab_iid"]
    full = post_diagnosis(st, key, inc, d, run, reply_to, channel)
    gl_note(inc.get("gitlab_iid"), "🩺 **Corhyn diagnosis**\n\n" + full)
    save(st)


# --------------------------------------------------------------------------- scan

def scan(dry=False):
    st = load()
    with sre_lock() as got:  # never scan mid-patch: containers being recreated would look broken
        if not got:
            o.heartbeat(AGENT, "scan skipped - patch executor busy")
            return
        found = detect(st)
    if dry:
        for k, f in found.items():
            print(k, "|", f["summary"])
        save(st)
        return
    now = o.iso()
    # a diagnosis killed mid-way (timeout/reboot) must not block the finding forever
    for inc in st["incidents"].values():
        if inc.get("state") == "diagnosing" and o.parse_iso(inc.get("diag_started", inc["first"])) < o.utcnow() - dt.timedelta(minutes=45):
            inc["state"] = "diag-failed"
    todo = []
    for k, f in found.items():
        inc = owner(st, k)
        if inc and inc.get("state") == "muted" and o.parse_iso(inc.get("muted_until", now)) > o.utcnow():
            continue
        if inc and inc.get("state") == "diag-failed" and \
                o.parse_iso(inc.get("diag_started", inc["first"])) < o.utcnow() - dt.timedelta(hours=6):
            inc = None  # retry the diagnosis every 6 h
        if inc and inc.get("state") in ("diagnosing", "awaiting", "manual", "fixing", "scheduled", "fix-failed", "diag-failed"):
            inc["last"] = now
            continue
        seen = st.setdefault("pending", {}).get(k, 0) + 1
        st["pending"][k] = seen
        if seen >= CONFIRM_SCANS or f["type"] in ("patch-failed",):
            todo.append((k, f))
    for k in list(st.get("pending", {})):
        if k not in found:
            del st["pending"][k]
    # incidents whose condition cleared (fix-failed too: a later fix or manual repair must close it)
    for k, inc in st["incidents"].items():
        if inc.get("state") not in ("awaiting", "scheduled", "manual", "diag-failed", "muted", "fix-failed"):
            continue
        if not inc.get("keys") or any(x in found for x in inc["keys"]):  # owner reports have no keys
            continue
        if inc.get("state") == "muted":
            inc["state"] = "resolved"
            inc["resolution_evidence"] = f"Incident keys no longer detected by scan at {o.iso()}"
            continue
        inc["state"] = "resolved"
        inc["resolution_evidence"] = f"Incident keys no longer detected by scan at {o.iso()}"
        if inc.get("approval"):
            ap = o.get_approval(inc["approval"])
            if ap and ap["status"] in ("pending", "scheduled"):
                o.set_approval(inc["approval"], status="expired", finished=o.iso(), result="condition cleared")
        o.post(CHANNEL, embeds=[o.embed(AGENT, f"✅ Incident #{inc['id']} cleared on its own",
                                        inc["finding"]["summary"] + " — no longer detected; approval withdrawn.",
                                        color=o.SEV_COLORS["ok"])])
        gl_note(inc.get("gitlab_iid"), "✅ Condition cleared on its own; approval withdrawn.")
        if not inc["finding"].get("gitlab_iid"):
            gl_close(inc.get("gitlab_iid"), ["self-resolved"])
    # first run: list pre-existing problems once, then diagnose them gradually
    if not st.get("baseline"):
        st["baseline"] = True
        if found:
            o.post(CHANNEL, embeds=[o.embed(AGENT, f"Corhyn is on call · {len(found)} existing problem(s)",
                                            "\n".join(f"• {f['summary']}" for f in found.values())[:3900],
                                            footer=f"Each will be diagnosed (max {MAX_DIAG_PER_SCAN} per 5-min scan) "
                                                   "and sent for your approval.")])
    # mark muted rejections
    for k, inc in st["incidents"].items():
        if inc.get("state") == "awaiting" and inc.get("approval"):
            ap = o.get_approval(inc["approval"])
            if ap and ap["status"] == "rejected":
                inc["state"], inc["muted_until"] = "muted", o.iso(o.utcnow() + dt.timedelta(days=MUTE_DAYS))
                gl_note(inc.get("gitlab_iid"), f"❌ Fix rejected by the owner; muted for {MUTE_DAYS} days.")
            elif ap and ap["status"] == "scheduled":
                inc["state"] = "scheduled"
    # forget closed incidents after 30 days (GitLab + history.jsonl keep the record)
    cutoff = o.utcnow() - dt.timedelta(days=30)
    for k in [k for k, i in st["incidents"].items() if i.get("state") in ("resolved", "fixed")
              and o.parse_iso(i.get("last", i["first"])) < cutoff]:
        del st["incidents"][k]
    save(st)
    for keys, f in group(todo)[:MAX_DIAG_PER_SCAN]:
        st = load()
        for k in keys:
            st["pending"].pop(k, None)
        save(st)
        try:
            open_incident(st, keys, f)
        except o.BudgetExceeded:
            pass
    active = [i for i in load()["incidents"].values() if i.get("state") in ("awaiting", "manual", "fixing", "scheduled")]
    o.heartbeat(AGENT, f"{len(found)} problems detected, {len(active)} open incidents")


# --------------------------------------------------------------------------- fix

FIX_SCHEMA = o.S(status=o.E("fixed", "partial", "failed"), summary=o.STR, actions=o.A(o.STR),
                 changes=o.A(o.S(target=o.STR, change=o.STR)), backups=o.A(o.STR), verification=o.A(o.STR),
                 remaining=o.STR, docs_notes=o.STR)


def recheck(inc):
    """Deterministic re-check that the incident condition is gone."""
    st = load()
    f = inc["finding"]
    if f["type"] == "patch-failed":
        return True, "patch issue handled by the fix (re-approval/retry is tracked on the GitLab issue)"
    for attempt in range(6):
        found = detect(st)
        left = [found[k]["summary"] for k in inc.get("keys", []) if k in found]
        if not left:
            return True, "condition no longer detected"
        time.sleep(30)
    return False, "still detected: " + "; ".join(left)[:400]


def record_history(inc, status, fix):
    entry = {"ts": o.local().isoformat(timespec="seconds"), "iid": int(inc.get("gitlab_iid") or 0),
             "host": inc["finding"].get("host"), "kind": "incident", "project": inc["finding"].get("project"),
             "result": f"incident-{status}", "incident": inc["id"], "title": inc["diag"]["title"],
             "severity": inc["diag"]["severity"], "symptom": inc["finding"]["summary"],
             "root_cause": inc["diag"]["root_cause"], "fix_summary": fix.get("summary"),
             "actions": fix.get("actions"), "changes": fix.get("changes"), "prevention": inc["diag"]["prevention"],
             "verification_result": fix.get("verification"), "docs_notes": fix.get("docs_notes")}
    if entry["iid"]:
        SRE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with SRE_HISTORY.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")


def fix(incident_id):
    st = load()
    key, inc = find(st, incident_id)
    if not inc or not inc.get("diag"):
        print("unknown incident")
        sys.exit(1)
    d, f = inc["diag"], inc["finding"]
    ch = o.get_approval(inc["approval"])["channel_id"] if inc.get("approval") else CHANNEL
    reply = inc.get("diag_msg")
    inc["state"] = "fixing"
    save(st)
    with sre_lock(wait=3600) as got:
        if not got:
            o.post(ch, f"⚠️ Incident #{inc['id']}: patch executor busy for an hour — fix not started. Re-approve later.",
                   reply_to=reply)
            st = load()
            st["incidents"][key]["state"] = "fix-failed"
            save(st)
            sys.exit(1)
        with o.Run(AGENT, f"fix incident #{inc['id']}: {d['title']}", "approval") as run:
            prompt = f"""You are Corhyn, the incident-response SRE for Hermes's homelab. The owner APPROVED the fix below.
SSH aliases work for every host (ssh <name>; sudo -n available). Proxmox: ssh node01.

INCIDENT #{inc['id']}: {d['title']}  (severity {d['severity']})
Detected: {f['summary']}
Root cause: {d['root_cause']}

APPROVED FIX PLAN (execute exactly this, in order):
{chr(10).join(f'{i + 1}. {s}' for i, s in enumerate(d['fix_plan']))}
Reference commands:
{chr(10).join(d['commands'])}
Rollback if it goes wrong: {d['rollback']}

RULES:
- Stay within the approved plan and the affected host(s). Small adjustments needed to make the SAME fix work are fine;
  anything materially different (other services, deleting data, new software) -> stop and report status=partial.
- Before editing any file: cp it to <file>.bak-medic-{o.local().strftime('%Y%m%d')} and list it in backups.
- Before touching a database container: take a dump (pg_dumpall / mysqldump) to the stack directory and list it.
- Never delete volumes, user data or images in use; never `docker system prune`; never reboot a Proxmox node.
- If a step fails, try at most one reasonable correction; if still failing, roll back what you changed and report failed.
- Then run every verification check: {'; '.join(d['verification'])}
Final JSON: status, summary (<=120 words), actions (each command/step done), changes (target + change),
backups, verification (each check with its result), remaining (anything left for the human, '' if none),
docs_notes (what the homelab docs should now say about this service/config, '' if nothing changed)."""
            # token_budget=0: a fix changes live systems and must never be killed half-way
            res, transcript = o.codex(run, prompt, model=FIX_MODEL, effort="medium", schema=FIX_SCHEMA,
                                      sandbox="danger", timeout=2400, tag=f"fix{inc['id']}", critical=True,
                                      token_budget=0)
            res = res or {"status": "failed", "summary": f"Codex returned no result; transcript {transcript}",
                          "actions": [], "changes": [], "backups": [], "verification": [], "remaining": "check manually",
                          "docs_notes": ""}
            ok, why = recheck(inc) if res["status"] != "failed" else (False, "fix failed")
            final = "fixed" if res["status"] == "fixed" and ok else ("failed" if res["status"] == "failed" else "partial")
            run.summary = f"{final}: {res['summary'][:300]}"
            sev_color = {"fixed": o.SEV_COLORS["ok"], "partial": o.SEV_COLORS["medium"], "failed": o.SEV_COLORS["critical"]}[final]
            fields = [("Result", {"fixed": "✅ Fixed", "partial": "🟡 Partially fixed", "failed": "❌ Fix failed"}[final], True),
                      ("Independent re-check", ("✅ " if ok else "❌ ") + why, True),
                      ("Cost", o.money(run.cost), True),
                      ("🛠️ Actions", "\n".join(f"• {a}" for a in res["actions"][:12]) or "none", False),
                      ("📝 Changes", "\n".join(f"• `{c['target']}` — {c['change']}" for c in res["changes"][:8]) or "none", False),
                      ("💾 Backups", "\n".join(f"• `{b}`" for b in res["backups"][:6]) or "none", False),
                      ("✅ Verification", "\n".join(f"• {v}" for v in res["verification"][:8]) or "n/a", False)]
            if res["remaining"]:
                fields.append(("👤 Left for you", res["remaining"], False))
            fields.append(("📚 Documentation", "Miriel is updating the vault changelog, troubleshooting guide and "
                                              "service docs now." if inc.get("gitlab_iid") else "No GitLab issue — not documented.", False))
            report = (f"# Incident #{inc['id']} — fix report\n\nResult: {final}\nRe-check: {why}\n\n## Summary\n{res['summary']}\n\n"
                      "## Actions\n" + "\n".join(f"- {a}" for a in res["actions"]) + "\n\n## Changes\n"
                      + "\n".join(f"- {c['target']}: {c['change']}" for c in res["changes"]) + "\n\n## Backups\n"
                      + "\n".join(f"- {b}" for b in res["backups"]) + "\n\n## Verification\n"
                      + "\n".join(f"- {v}" for v in res["verification"]) + f"\n\n## Remaining\n{res['remaining'] or 'none'}\n")
            o.post(ch, content=f"<@{o.OWNER_ID}>", mention_owner=True,
                   embeds=[o.embed(AGENT, f"Incident #{inc['id']} · status report", res["summary"], fields, color=sev_color,
                                   footer=f"Roundtable run #{run.id} · {FIX_MODEL}")],
                   files=[(f"incident-{inc['id']}-fix-report.md", report)], reply_to=reply)
            gl_note(inc.get("gitlab_iid"), f"🩺 **Corhyn fix report** — {final}\n\n" + report)
            if final == "fixed" and not f.get("gitlab_iid"):
                gl_close(inc.get("gitlab_iid"), ["fixed"])
            if final != "fixed":
                run.finish("failed" if final == "failed" else "partial", run.summary)
    st = load()
    st["incidents"][key].update(state="fixed" if final == "fixed" else "fix-failed", fix=res, fixed_at=o.iso())
    save(st)
    record_history(inc, final, res)
    if inc.get("gitlab_iid") and DOC_AGENT.exists():
        subprocess.Popen([sys.executable, str(DOC_AGENT), "run", "--hours", "6", "--trigger", "incident"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    sys.exit(0 if final == "fixed" else 1)


# --------------------------------------------------------------------------- ask / list

def ask(text, reply_to=None, channel=None):
    o.transcript(AGENT, "Request", text, "discord", channel or "roundtable-hold", reply_to or "")
    st = load()
    finding = {"type": "owner-report", "host": "", "summary": text[:500]}
    open_incident(st, [], finding, symptom=text, reply_to=reply_to, channel=channel or "roundtable-hold")


def close(incident_id, reason):
    """Close an incident by hand (owner reports have no detection keys, so they never clear on their own)."""
    st = load()
    _, inc = find(st, incident_id)
    if not inc:
        sys.exit(f"incident {incident_id} not found")
    if inc.get("state") in ("resolved", "fixed"):
        print(f"#{inc['id']} already {inc['state']}")
        return
    inc["state"], inc["last"] = "resolved", o.iso()
    inc["resolution_evidence"] = f"Closed by hand at {o.iso()}: {reason}"
    if inc.get("approval"):
        ap = o.get_approval(inc["approval"])
        if ap and ap["status"] in ("pending", "scheduled"):
            o.set_approval(inc["approval"], status="expired", finished=o.iso(), result=f"incident closed: {reason}")
    save(st)
    gl_note(inc.get("gitlab_iid"), f"✅ Closed: {reason}")
    if not inc["finding"].get("gitlab_iid"):  # patch-failed issues belong to the SRE pipeline
        gl_close(inc.get("gitlab_iid"), ["resolved"])
    print(f"#{inc['id']} closed: {reason}")


def list_incidents():
    st = load()
    for k, i in sorted(st["incidents"].items(), key=lambda kv: kv[1]["id"]):
        print(f"#{i['id']:<4} {i.get('state', '?'):<12} {k:<50} {(i.get('diag') or {}).get('title', '')[:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "fix", "ask", "list", "close"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("reason", nargs="?", default="closed by the owner")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "scan":
        scan(a.dry)
    elif a.cmd == "fix":
        fix(a.arg)
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    elif a.cmd == "close":
        close(a.arg, a.reason)
    else:
        list_incidents()


if __name__ == "__main__":
    main()
