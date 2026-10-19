#!/usr/bin/env python3
"""Iji - backup keeper: Proxmox vzdump jobs, Proxmox Backup Server, the PBS -> Synology NAS copy and Omada config.

  backup.py watch                    # every 30 min, deterministic: failures, stale guests, guests without a job,
                                     # PBS task errors, NAS sync, storage thresholds, Omada export age
  backup.py digest                   # daily 08:00 status of every guest's backups + storage
  backup.py forecast                 # weekly capacity forecast: growth, days to full, "add storage?" advice
  backup.py omada-export             # weekly settings-only Omada controller backup -> NAS (needs OMADA_BACKUP_*)
  backup.py ask "<text>" [--reply-to MID] [--channel CID]   # Master Hermes asks (Gideon routes it)
  backup.py run 205,207 [--storage pbs-daily]                # on-demand backup now (non-destructive)
  backup.py add-to-jobs 212,213      # add guests to every scheduled vzdump job (runs only after ✅)
  backup.py status                   # print the live brief
  backup.py free-space [--storage pbs-daily] [--dry-run]   # low-space cleanup now (watch runs it every 30 min)

On-demand backups run immediately (Master Hermes's choice: a backup changes nothing on the guest). Changing the
backup schedule (adding guests to jobs) is proposed and needs ✅. Low-space cleanup runs without ✅ (Master Hermes's
choice, 2026-10-19): a backup store at WARN_PCT or more loses its backups older than PRUNE_AGE_D days, oldest first,
never a guest's newest backup or a protected one.
"""
import argparse
import concurrent.futures as cf
import contextlib
import http.cookiejar
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import olympus_core as o

AGENT = "backup"
CHANNEL = "backups"
TOPIC = ("🛡️ Iji, keeper of backups: Proxmox/PBS backup results, NAS copy, Omada config, storage capacity. "
         "Ask him in #roundtable-hold (e.g. 'Iji, back up 205 now').")
NODES = ["node01", "node02", "node03"]
PBS_NODE, PBS_CT = "node03", "100"
STORES = {"pbs-daily": "PBS daily · Kingston NVMe 1 TB", "pbs-main": "PBS main · Seagate HDD 4 TB",
          "ProxmoxData": "Synology NAS · ProxmoxData (PBS offsite copy, node backups)"}
EXTRA_STORES = {"VMDisks": "Synology NAS · VM disks volume"}
WARN_PCT, CRIT_PCT = 80, 90
SOON_DAYS, PLAN_DAYS = 30, 90
DAILY_MAX_H, NAS_MAX_H, OMADA_MAX_D = 30, 27, 8
OMADA_DIR = "/mnt/pve/ProxmoxData/omada-config-backups"
OMADA_KEEP = 12
PROM = os.environ.get("PROMETHEUS_URL", "http://192.168.40.13:9090")
KV_STATE, KV_OMADA, KV_SAMPLES = "backup:state", "backup:omada", "backup:samples"
PRUNE_AGE_D, PRUNE_TARGET_PCT, PRUNE_BATCH = 30, WARN_PCT - 5, 40
PBS_DATASTORE = {"pbs-daily": "daily", "pbs-main": "main"}   # Proxmox storage -> PBS datastore (frees after GC)
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
GB = 1024 ** 3


# --------------------------------------------------------------------------- helpers

def _gb(b):
    return round((b or 0) / GB, 1)


def _tb(b):
    return f"{b / 1024 ** 4:.2f} TB" if b >= 1024 ** 4 else f"{b / GB:.0f} GB"


def _ago(ts):
    if not ts:
        return "never"
    h = (time.time() - ts) / 3600
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.1f} d ago"


def _when(ts):
    return o.local(o.dt.datetime.fromtimestamp(ts, o.dt.timezone.utc)).strftime("%a %d %b %H:%M") if ts else "never"


def _sections(out):
    sec, cur = {}, None
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            sec[cur] = []
        elif cur:
            sec[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in sec.items()}


def _json(txt, default):
    try:
        return json.loads(txt) if txt else default
    except json.JSONDecodeError:
        return default


def pve(script, timeout=90):
    """Run on the first reachable Proxmox node. Returns (node, stdout) or (None, error)."""
    errs = []
    for n in NODES:
        rc, out, err = o.ssh(n, script, timeout=timeout)
        if rc == 0:
            return n, out
        errs.append(f"{n}: {(err or str(rc))[:120]}")
    return None, "; ".join(errs)


def prom(q):
    code, d = o.http("GET", f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": q}), timeout=20, retries=1)
    return d.get("data", {}).get("result", []) if code == 200 and isinstance(d, dict) else []


# --------------------------------------------------------------------------- collect (read-only)

PVE_SCRIPT = r"""
echo '@@jobs'; pvesh get /cluster/backup --output-format json
echo '@@res'; pvesh get /cluster/resources --output-format json
echo '@@nojob'; pvesh get /cluster/backup-info/not-backed-up --output-format json
echo '@@tasks'; pvesh get /cluster/tasks --output-format json
for st in pbs-daily pbs-main ProxmoxData; do
  echo "@@snap:$st"; pvesh get /nodes/$(hostname)/storage/$st/content --content backup --output-format json 2>/dev/null || echo '[]'
done
"""

PBS_SCRIPT = r"""pct exec 100 -- bash -s <<'EOF'
echo '@@tasks'; proxmox-backup-manager task list --all --limit 150 --output-format json 2>/dev/null || echo '[]'
echo '@@df'; df -B1 --output=target,size,used,avail /backup /backup-ssd /mnt/nas-backup 2>/dev/null | tail -n +2
echo '@@nas'; tail -n 60 /var/log/pbs-nas-backup.log 2>/dev/null
echo '@@cron'; cat /etc/cron.d/pbs-nas-backup 2>/dev/null
EOF
"""

LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) - (.*)$")


def _nas_sync(log):
    runs, cur = [], None
    for line in (log or "").splitlines():
        m = LOG_TS.match(line.strip())
        if not m:
            continue
        ts = o.dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=o.TZ).timestamp()
        msg = m.group(2)
        if msg.startswith("Starting"):
            cur = {"start": ts, "end": None, "status": "running", "errors": [], "sizes": []}
            runs.append(cur)
        elif cur is not None:
            if re.search(r"(?i)error|fail", msg):
                cur["errors"].append(msg[:200])
            if "completed successfully" in msg:
                cur["end"], cur["status"] = ts, "ok"
            elif "datastore on NAS" in msg:
                cur["sizes"].append(msg)
    if not runs:
        return {"status": "unknown", "detail": "no runs found in /var/log/pbs-nas-backup.log"}
    r = runs[-1]
    if r["status"] == "running" and (r["errors"] or time.time() - r["start"] > 6 * 3600):
        r["status"] = "failed"
    return {"status": r["status"], "start": r["start"], "end": r["end"], "errors": r["errors"][-5:],
            "sizes": r["sizes"], "minutes": round((r["end"] - r["start"]) / 60) if r["end"] else None}


def _growth():
    """bytes/day per storage over the last 14 days from Prometheus (pve-exporter)."""
    out = {}
    for r in prom('deriv(pve_disk_usage_bytes{id=~"storage/node0[1-3]/.*"}[14d])'):
        sid = r["metric"].get("id", "").split("/")
        if len(sid) == 3:
            out.setdefault(sid[2], float(r["value"][1]) * 86400)
    return out


def collect():
    s = {"ts": time.time(), "errors": []}
    with cf.ThreadPoolExecutor(3) as ex:
        f_pve = ex.submit(pve, PVE_SCRIPT, 120)
        f_pbs = ex.submit(o.ssh, PBS_NODE, PBS_SCRIPT, 90)
        f_gr = ex.submit(_growth)
        node, out = f_pve.result()
        prc, pout, perr = f_pbs.result()
        try:
            growth = f_gr.result()
        except Exception as e:  # noqa: BLE001
            growth, _ = {}, s["errors"].append(f"Prometheus growth: {e}")
    if not node:
        s["errors"].append(f"Proxmox unreachable: {out}")
        sec = {}
    else:
        sec = _sections(out)
    s["pve_node"] = node
    s["jobs"] = [j for j in _json(sec.get("jobs"), []) if j.get("type", "vzdump") == "vzdump"]
    res = _json(sec.get("res"), [])
    s["guests"] = sorted([{"id": r["vmid"], "name": r.get("name"), "kind": "VM" if r["type"] == "qemu" else "LXC",
                           "node": r.get("node"), "status": r.get("status"), "template": bool(r.get("template"))}
                          for r in res if r.get("type") in ("qemu", "lxc")], key=lambda g: g["id"])
    jobbed = {}
    for j in s["jobs"]:
        if j.get("all"):
            for g in s["guests"]:
                jobbed.setdefault(g["id"], []).append(j["id"])
        for v in str(j.get("vmid") or "").split(","):
            if v.strip().isdigit():
                jobbed.setdefault(int(v), []).append(j["id"])
    s["no_job"] = [g for g in s["guests"] if not g["template"] and g["id"] not in jobbed]
    for g in s["guests"]:
        g["jobs"] = jobbed.get(g["id"], [])
    # storage (shared once, local per node)
    st, seen = [], set()
    for r in res:
        if r.get("type") != "storage" or r.get("status") != "available":
            continue
        name = r.get("storage")
        key = name if r.get("shared") else f"{name}@{r.get('node')}"
        if key in seen or name not in {**STORES, **EXTRA_STORES}:
            continue
        seen.add(key)
        size, used = r.get("maxdisk") or 0, r.get("disk") or 0
        g = growth.get(name)
        free = max(size - used, 0)
        st.append({"name": name, "label": {**STORES, **EXTRA_STORES}[name], "size": size, "used": used, "free": free,
                   "pct": round(used / size * 100, 1) if size else 0, "growth_day": g,
                   "days_full": round(free / g) if g and g > 0 else None, "backup": name in STORES})
    s["storage"] = sorted(st, key=lambda x: (not x["backup"], x["name"]))
    # snapshots: newest per guest per storage
    s["snaps"], s["content"] = {}, {}
    for stname in ("pbs-daily", "pbs-main", "ProxmoxData"):
        s["content"][stname] = _json(sec.get(f"snap:{stname}"), [])
        for c in s["content"][stname]:
            v = c.get("vmid")
            cur = s["snaps"].setdefault(str(v), {}).get(stname)
            if not cur or c.get("ctime", 0) > cur["ctime"]:
                s["snaps"][str(v)][stname] = {"ctime": c.get("ctime", 0), "size": c.get("size", 0),
                                              "verified": (c.get("verification") or {}).get("state")}
    s["vzdump"] = [t for t in _json(sec.get("tasks"), []) if t.get("type") == "vzdump"]
    # PBS
    if prc != 0:
        s["errors"].append(f"PBS (CT{PBS_CT} on {PBS_NODE}) unreachable: {(perr or str(prc))[:200]}")
        s["pbs"] = None
    else:
        ps = _sections(pout)
        df = {}
        for line in ps.get("df", "").splitlines():
            p = line.split()
            if len(p) == 4 and p[1].isdigit():
                df[p[0]] = {"size": int(p[1]), "used": int(p[2]), "free": int(p[3])}
        s["pbs"] = {"tasks": _json(ps.get("tasks"), []), "df": df}
        s["nas"] = _nas_sync(ps.get("nas"))
        s["nas"]["cron"] = (ps.get("cron") or "").strip()
    s["omada"] = o.kv_get(KV_OMADA, {}) or {}
    return s


# --------------------------------------------------------------------------- evaluate

def _guest_rows(s):
    rows = []
    for g in s["guests"]:
        if g["template"]:
            continue
        sn = s["snaps"].get(str(g["id"]), {})
        d, m, nfs = (sn.get(k, {}).get("ctime") for k in ("pbs-daily", "pbs-main", "ProxmoxData"))
        newest = max([x for x in (d, m, nfs) if x] or [0])
        age_h = (time.time() - newest) / 3600 if newest else None
        if not g["jobs"]:
            state = "⚪ no job"
        elif age_h is None:
            state = "🔴 never"
        elif age_h > (DAILY_MAX_H if d else 8 * 24 + 6) and g["status"] == "running":
            state = "🟠 stale"
        else:
            state = "✅ ok"
        rows.append({**g, "daily": d, "weekly": m, "nfs": nfs, "age_h": age_h, "state": state,
                     "size": (sn.get("pbs-daily") or sn.get("pbs-main") or {}).get("size", 0)})
    return rows


def evaluate(s):
    """Current problems as {key: {sev, title, detail}}. Failed task events are handled separately."""
    out = {}
    for e in s["errors"]:
        k = "unreachable:" + ("pbs" if "PBS" in e else "pve" if "Proxmox" in e else "prom")
        if k != "unreachable:prom":
            out[k] = {"sev": "high", "title": e.split(":")[0], "detail": e}
    pbs_guest = lambda g: "pbs" in (g["name"] or "").lower()  # noqa: E731
    for r in _guest_rows(s):
        if r["state"].startswith("🔴") and not pbs_guest(r):
            out[f"never:{r['id']}"] = {"sev": "high", "title": f"{r['id']} {r['name']} has never been backed up",
                                       "detail": f"{r['kind']} on {r['node']} is in job(s) {', '.join(r['jobs'])} "
                                                 "but no snapshot exists on PBS."}
        elif r["state"].startswith("🟠") and not pbs_guest(r):
            out[f"stale:{r['id']}"] = {"sev": "medium", "title": f"{r['id']} {r['name']}: last backup "
                                                                 f"{r['age_h']:.0f} h ago",
                                       "detail": f"Expected a daily backup (max {DAILY_MAX_H} h)."}
    for st in s["storage"]:
        if not st["backup"]:
            continue
        sev = ("critical" if st["pct"] >= CRIT_PCT or (st["days_full"] is not None and st["days_full"] < 7) else
               "high" if st["days_full"] is not None and st["days_full"] < SOON_DAYS else
               "medium" if st["pct"] >= WARN_PCT else None)
        if sev:
            out[f"storage:{st['name']}"] = {
                "sev": sev, "title": f"{st['label']} is {st['pct']:.0f}% full",
                "detail": f"{_tb(st['free'])} free of {_tb(st['size'])}"
                          + (f"; growing {st['growth_day'] / GB:.1f} GB/day → full in ~{st['days_full']} days"
                             if st["days_full"] is not None else "") + "."}
    nas = s.get("nas") or {}
    if s.get("pbs") is not None:
        if nas.get("status") == "failed":
            out["nas-sync"] = {"sev": "high", "title": "PBS → NAS copy failed",
                               "detail": "; ".join(nas.get("errors") or ["run did not complete"])}
        elif nas.get("start") and time.time() - nas["start"] > NAS_MAX_H * 3600:
            out["nas-sync"] = {"sev": "high", "title": "PBS → NAS copy has not run",
                               "detail": f"Last run {_when(nas['start'])} (cron: {nas.get('cron') or 'missing'})."}
        elif nas.get("status") == "unknown":
            out["nas-sync"] = {"sev": "medium", "title": "PBS → NAS copy status unknown", "detail": nas.get("detail")}
    om = s.get("omada") or {}
    if os.environ.get("OMADA_BACKUP_USER"):
        if om.get("error"):
            out["omada"] = {"sev": "medium", "title": "Omada config export failed", "detail": om["error"][:300]}
        elif not om.get("last") or time.time() - om["last"] > OMADA_MAX_D * 86400:
            out["omada"] = {"sev": "medium", "title": "No recent Omada config backup",
                            "detail": f"Last export: {_when(om.get('last'))}."}
    return out


# --------------------------------------------------------------------------- task logs

def task_errors(node, upid, limit=400):
    rc, out, err = o.ssh(node, f"pvesh get /nodes/{node}/tasks/{upid}/log --limit {limit} --output-format json",
                         timeout=60)
    lines = [x.get("t", "") for x in _json(out, [])] if rc == 0 else []
    failed = sorted({int(m) for m in re.findall(r"ERROR: Backup of VM (\d+) failed", "\n".join(lines))})
    errs = [x for x in lines if re.search(r"(?i)\berror\b|failed|unable|locked|permission", x)]
    return failed, errs[-12:], lines


def known_fix(text):
    t = text.lower()
    if "is locked (backup)" in t or "locked (backup)" in t:
        return "A previous backup left a stale lock: `pct unlock <id>` / `qm unlock <id>` on its node, then retry."
    if "datastore.prune" in t or "datastore.modify" in t:
        return ("The PBS token lacks prune rights: on PBS `proxmox-backup-manager acl update /datastore/<store> "
                "DatastoreAdmin --auth-id backup@pbs` (see 47 - PBS Monitoring).")
    if "connection refused" in t or "timed out" in t or "no route" in t or "can't connect" in t:
        return "PBS (CT100 on node03) was unreachable: check that node03 and CT100 are up, then retry."
    if "no space left" in t or "enospc" in t:
        return "Target storage is full: prune/GC the datastore or add storage before retrying."
    return ""


# --------------------------------------------------------------------------- watch

# --------------------------------------------------------------------------- low-space cleanup (deletes backups)

def prune_candidates(s, store):
    """Backups on `store` older than PRUNE_AGE_D days, oldest first; never a guest's newest backup, never protected."""
    items = s.get("content", {}).get(store, [])
    newest = {}
    for c in items:
        v = str(c.get("vmid"))
        if c.get("ctime", 0) > newest.get(v, (0, None))[0]:
            newest[v] = (c.get("ctime", 0), c.get("volid"))
    keep = {vol for _, vol in newest.values()}
    cut = time.time() - PRUNE_AGE_D * 86400
    return sorted((c for c in items if c.get("volid") and c.get("ctime", 0) < cut and c["volid"] not in keep
                   and not c.get("protected")), key=lambda c: c["ctime"])


def _gc_running(s, ds):
    return any(t.get("worker_type") == "garbage_collection" and t.get("worker_id") == ds and not t.get("endtime")
               for t in (s.get("pbs") or {}).get("tasks", []))


def prune_plan(s, store=None):
    """What a cleanup would delete now. A store qualifies at WARN_PCT or more. Plain storage (the NAS) frees space at
    once, so it takes oldest backups until it is under PRUNE_TARGET_PCT. PBS frees space only after garbage
    collection, so it takes a batch of PRUNE_BATCH, and the next watch run (after GC) decides whether to go on."""
    plans = []
    for st in s["storage"]:
        if not st["backup"] or (store and st["name"] != store):
            continue
        p = {"store": st["name"], "label": st["label"], "pct": st["pct"], "low": st["pct"] >= WARN_PCT,
             "datastore": PBS_DATASTORE.get(st["name"]), "pick": [], "skipped": None}
        cands = prune_candidates(s, st["name"])
        p["eligible"] = len(cands)
        if not p["low"]:
            p["skipped"] = f"{st['pct']:.0f}% used, under {WARN_PCT}%"
        elif p["datastore"] and _gc_running(s, p["datastore"]):
            p["skipped"] = "garbage collection from the last cleanup is still running"
        elif p["datastore"]:
            p["pick"] = cands[:PRUNE_BATCH]
        else:
            free, target = st["free"], st["size"] * (1 - PRUNE_TARGET_PCT / 100)
            for c in cands:
                if free >= target:
                    break
                p["pick"].append(c)
                free += c.get("size", 0)
        plans.append(p)
    return plans


def free_space(s=None, store=None, dry=False, cid=None):
    """Delete what prune_plan picks, start PBS garbage collection, and report to #backups. Returns the plans."""
    s = s or collect()
    plans = prune_plan(s, store)
    if dry or not s.get("pve_node"):
        return plans
    names = {str(g["id"]): g["name"] for g in s["guests"]}
    for p in plans:
        if not p["pick"]:
            continue
        p["deleted"], p["failed"] = [], []
        for c in p["pick"]:
            rc, _, err = o.ssh(s["pve_node"], f"pvesm free {shlex.quote(c['volid'])}", timeout=180)
            if rc == 0:
                p["deleted"].append(c)
            else:
                p["failed"].append((c, (err or str(rc))[:200]))
        if p["datastore"] and p["deleted"]:
            rc, _, err = o.ssh(PBS_NODE, f"pct exec {PBS_CT} -- bash -c 'setsid nohup proxmox-backup-manager "
                                         f"garbage-collection start {p['datastore']} >/dev/null 2>&1 &'", timeout=60)
            p["gc"] = "started" if rc == 0 else f"could not start: {(err or str(rc))[:200]}"
        d = p["deleted"]
        lines = [f"Removed **{len(d)}** backup(s) older than {PRUNE_AGE_D} days, oldest first "
                 f"({_when(d[0]['ctime'])} → {_when(d[-1]['ctime'])}), {_gb(sum(c.get('size', 0) for c in d))} GB "
                 "before deduplication." if d else "Removed nothing."]
        guests = sorted({str(c.get("vmid")) for c in d}, key=lambda v: int(v) if v.isdigit() else 0)
        if guests:
            lines.append("Guests: " + ", ".join(f"{v} {names.get(v) or ''}".strip() for v in guests))
        if p.get("gc"):
            lines.append(f"PBS garbage collection on `{p['datastore']}`: {p['gc']}. Space comes back when it "
                         "finishes; the next watch run checks again.")
        if p["failed"]:
            lines.append(f"⚠️ {len(p['failed'])} could not be removed: {p['failed'][0][1]}")
        lines.append(f"Each guest keeps its newest backup; {p['eligible'] - len(d)} older backup(s) remain.")
        o.post(cid or o.channel_id(CHANNEL, TOPIC),
               embeds=[o.embed(AGENT, f"🧹 {p['label']} was {p['pct']:.0f}% full: freeing space", "\n".join(lines),
                               color=o.SEV_COLORS["medium"], footer="Iji · low-space cleanup")])
    return plans


def _post_alert(cid, key, a, mention=False):
    e = o.embed(AGENT, f"{SEV_ICON.get(a['sev'], '🟡')} {a['title']}", a["detail"], color=o.SEV_COLORS.get(a["sev"]),
                footer=f"Iji · backup watch · {key}")
    return o.post(cid, content=f"<@{o.OWNER_ID}>" if mention else None, embeds=[e], mention_owner=mention)


def watch():
    with o.Run(AGENT, "backup watch", "schedule") as run:
        cid = o.channel_id(CHANNEL, TOPIC)
        s = collect()
        st = o.kv_get(KV_STATE, {}) or {}
        active, seen = st.get("active", {}), set(st.get("seen_tasks", []))
        proposed = st.get("proposed", {})
        first_run = not st
        cur = evaluate(s)
        new = [k for k in cur if k not in active]
        gone = [k for k in active if k not in cur]
        for k in new:
            if not first_run or cur[k]["sev"] in ("critical", "high"):
                _post_alert(cid, k, cur[k], mention=cur[k]["sev"] in ("critical", "high"))
            active[k] = {**cur[k], "since": o.iso()}
        for k in gone:
            o.post(cid, f"✅ Resolved: {active[k]['title']} (since {active[k]['since'][:16].replace('T', ' ')} UTC)")
            del active[k]
        # failed vzdump tasks (events)
        horizon = time.time() - 2 * 86400
        for t in s["vzdump"]:
            up = t.get("upid")
            if not up or up in seen or not t.get("endtime") or t["endtime"] < horizon:
                continue
            seen.add(up)
            if first_run or t.get("status") == "OK":
                continue
            failed, errs, lines = task_errors(t["node"], up)
            storage = next((m.group(1) for x in lines for m in [re.search(r"--storage (\S+)", x)] if m), "pbs-daily")
            fix = known_fix("\n".join(errs) + " " + (t.get("status") or ""))
            names = {g["id"]: g["name"] for g in s["guests"]}
            fields = [("Status", t.get("status", "?")[:200], True), ("Node", t["node"], True),
                      ("When", _when(t.get("starttime")), True)]
            if failed:
                fields.append(("Failed guests", ", ".join(f"{v} {names.get(v, '')}" for v in failed)[:1000], False))
            if errs:
                fields.append(("Errors", "```\n" + "\n".join(errs)[-900:] + "\n```", False))
            if fix:
                fields.append(("Likely fix", fix, False))
            e = o.embed(AGENT, "🔴 Backup job failed", f"vzdump on **{t['node']}** finished with errors.", fields,
                        color=o.SEV_COLORS["high"], footer=f"Iji · {up[:60]}")
            mid = o.post(cid, content=f"<@{o.OWNER_ID}>", embeds=[e], mention_owner=True)
            if failed:
                o.request_approval(AGENT, f"Retry backup of {', '.join(map(str, failed))}",
                                   "Iji will back up " + ", ".join(f"{v} {names.get(v, '')}" for v in failed) + " to "
                                   f"`{storage}` now and report the result here.",
                                   [sys.executable, os.path.abspath(__file__), "run", ",".join(map(str, failed)),
                                    "--storage", storage], CHANNEL, severity="high", reply_to=mid)
        # PBS task errors (GC, prune, verify, sync)
        for t in (s.get("pbs") or {}).get("tasks", []):
            up = t.get("upid")
            if not up or up in seen or not t.get("endtime") or t["endtime"] < horizon:
                continue
            seen.add(up)
            if first_run or t.get("status") in ("OK", None) or str(t.get("status")).startswith("WARNINGS"):
                continue
            _post_alert(cid, up, {"sev": "medium", "title": f"PBS {t.get('worker_type')} task failed",
                                  "detail": f"`{t.get('worker_id') or ''}` · {t.get('status')}\n"
                                            f"{known_fix(str(t.get('status')))}"})
        # new guests without a backup job -> propose adding them (✅)
        for g in s["no_job"]:
            k = str(g["id"])
            if k in proposed:
                continue
            proposed[k] = o.request_approval(
                AGENT, f"Add {g['id']} {g['name']} to the backup schedule",
                f"New {g['kind']} **{g['id']} {g['name']}** on {g['node']} is not in any backup job. Iji will add it "
                "to every scheduled job (" + ", ".join(f"`{j['id']}` {j.get('schedule')} → {j.get('storage')}" for j in s["jobs"]) + ")"
                f" and take a first backup to pbs-daily right away.",
                [sys.executable, os.path.abspath(__file__), "add-to-jobs", k], CHANNEL, severity="medium")
        for k in list(proposed):
            if all(str(g["id"]) != k for g in s["no_job"]):
                del proposed[k]
        _sample(s)
        o.kv_set(KV_STATE, {"active": active, "seen_tasks": sorted(seen)[-600:], "proposed": proposed,
                            "last": o.iso()})
        freed = sum(len(p.get("deleted", [])) for p in free_space(s, cid=cid))
        run.summary = f"{len(cur)} open issue(s), {len(new)} new, {len(gone)} resolved" + (
            f", {freed} old backup(s) removed for space" if freed else "")
        o.heartbeat(AGENT, run.summary)


def _sample(s):
    smp = o.kv_get(KV_SAMPLES, {}) or {}
    day = o.local().strftime("%Y-%m-%d")
    smp[day] = {x["name"]: [x["used"], x["size"]] for x in s["storage"]}
    o.kv_set(KV_SAMPLES, dict(sorted(smp.items())[-180:]))


# --------------------------------------------------------------------------- reports

def storage_table(s):
    rows = [[x["label"], f"{x['pct']:.0f}%", _tb(x["used"]), _tb(x["free"]),
             f"{x['growth_day'] / GB:+.1f} GB/d" if x["growth_day"] is not None else "?",
             (f"~{x['days_full']} d" if x["days_full"] is not None else "not growing")] for x in s["storage"]]
    return o.md_table(rows, ["Storage", "Used", "Used", "Free", "Growth", "Full in"])


def guest_table(s):
    rows = [[r["id"], r["name"], r["kind"], r["node"], _when(r["daily"]), _when(r["weekly"]),
             f"{_gb(r['size'])} GB" if r["size"] else "-", r["state"]] for r in _guest_rows(s)]
    return o.md_table(rows, ["ID", "Name", "Type", "Node", "Last daily (pbs-daily)", "Last weekly (pbs-main)",
                             "Size", "State"])


def brief(s=None, guests=True):
    s = s or collect()
    gr = _guest_rows(s)
    ok = sum(1 for r in gr if r["state"].startswith("✅"))
    nas = s.get("nas") or {}
    om = s.get("omada") or {}
    jobs = "\n".join(f"- `{j['id']}`: {j.get('schedule')} → {j.get('storage')} ({'enabled' if j.get('enabled', 1) else 'DISABLED'}, "
                     f"{len(str(j.get('vmid') or '').split(',')) if j.get('vmid') else 'all'} guests, "
                     f"retention {', '.join(f'{k}={v}' for k, v in (j.get('prune-backups') or {}).items())})"
                     for j in s["jobs"])
    last = sorted(s["vzdump"], key=lambda t: t.get("starttime", 0), reverse=True)[:6]
    runs = "\n".join(f"- {_when(t.get('starttime'))} on {t['node']}: {t.get('status', 'running')}"
                     + (f" ({round((t['endtime'] - t['starttime']) / 60)} min)" if t.get("endtime") else "")
                     for t in last)
    txt = (f"Backups at {o.local().strftime('%a %d %b %H:%M')}: {ok}/{len(gr)} guests OK.\n"
           f"Scheduled jobs:\n{jobs or '- none'}\nRecent vzdump runs:\n{runs or '- none'}\n"
           f"PBS → NAS copy: {nas.get('status', 'unknown')}, last {_when(nas.get('start'))}"
           + (f", took {nas['minutes']} min" if nas.get("minutes") else "") + (f"; {'; '.join(nas['sizes'])}" if nas.get('sizes') else "")
           + f"\nOmada config backup: " + (f"last {_when(om.get('last'))} ({om.get('file')})" if om.get("last") else
                                            "not set up yet (needs an Omada admin account)" if not os.environ.get("OMADA_BACKUP_USER")
                                            else "none yet")
           + f"\nStorage:\n{storage_table(s)}\n"
           + ("Guests without any backup job: " + ", ".join(f"{g['id']} {g['name']}" for g in s["no_job"]) + "\n" if s["no_job"] else "")
           + (f"Problems now: " + "; ".join(f"[{a['sev']}] {a['title']}" for a in evaluate(s).values()) + "\n"
              if evaluate(s) else "No open problems.\n")
           + (f"Errors collecting data: {'; '.join(s['errors'])}\n" if s["errors"] else ""))
    if guests:
        txt += f"\nPer guest:\n{guest_table(s)}"
    return txt


def advice_text(s):
    lines = []
    for x in s["storage"]:
        if not x["backup"]:
            continue
        if x["pct"] >= CRIT_PCT or (x["days_full"] is not None and x["days_full"] < SOON_DAYS):
            lines.append(f"🔴 **{x['label']}**: add storage now (or tighten retention) - {x['pct']:.0f}% used"
                         + (f", full in ~{x['days_full']} days" if x["days_full"] is not None else "") + ".")
        elif x["pct"] >= WARN_PCT or (x["days_full"] is not None and x["days_full"] < PLAN_DAYS):
            lines.append(f"🟡 **{x['label']}**: plan more storage within ~{x['days_full'] or 60} days "
                         f"({x['pct']:.0f}% used).")
    return "\n".join(lines) or "🟢 No new backup storage needed: every backup target has room for 90+ days at today's growth."


def digest():
    with o.Run(AGENT, "daily backup digest", "schedule") as run:
        cid = o.channel_id(CHANNEL, TOPIC)
        s = collect()
        gr = _guest_rows(s)
        bad = [r for r in gr if not r["state"].startswith("✅")]
        nas = s.get("nas") or {}
        problems = evaluate(s)
        fields = [("Guests", f"✅ {len(gr) - len(bad)} ok · ⚠️ {len(bad)} need attention", True),
                  ("PBS → NAS", f"{'✅' if nas.get('status') == 'ok' else '⚠️'} {nas.get('status', '?')} · "
                                f"{_when(nas.get('start'))}", True),
                  ("Omada config", _when((s.get("omada") or {}).get("last")) if os.environ.get("OMADA_BACKUP_USER")
                   else "⏳ waiting for an Omada admin account", True),
                  ("Storage", "\n".join(f"{'🔴' if x['pct'] >= CRIT_PCT else '🟡' if x['pct'] >= WARN_PCT else '🟢'} "
                                        f"{x['label']}: {x['pct']:.0f}% · {_tb(x['free'])} free"
                                        + (f" · full ~{x['days_full']} d" if x["days_full"] else "")
                                        for x in s["storage"] if x["backup"]), False),
                  ("Advice", advice_text(s), False)]
        if bad:
            fields.insert(1, ("Needs attention", "\n".join(f"{r['state']} {r['id']} {r['name']} "
                                                            f"(last {_ago(max(r['daily'] or 0, r['weekly'] or 0) or None)})"
                                                            for r in bad[:15]), False))
        if problems:
            fields.append(("Open problems", "\n".join(f"{SEV_ICON[a['sev']]} {a['title']}"
                                                      for a in problems.values())[:1000], False))
        e = o.embed(AGENT, f"🛡️ Backup report · {o.local().strftime('%a %d %b')}",
                    f"Every guest, job, NAS copy and storage target checked. Full per-guest table attached.",
                    fields, color=o.SEV_COLORS["ok"] if not problems else o.SEV_COLORS["medium"],
                    footer=f"Iji · Roundtable run #{run.id} · no LLM")
        o.post(cid, embeds=[e], files=[(f"backups-{o.local().strftime('%Y%m%d')}.md",
                                        f"# Backup report {o.local().strftime('%Y-%m-%d %H:%M')}\n\n" + brief(s))])
        run.summary = f"{len(gr) - len(bad)}/{len(gr)} ok, {len(problems)} problems"


def forecast():
    with o.Run(AGENT, "weekly capacity forecast", "schedule") as run:
        cid = o.channel_id(CHANNEL, TOPIC)
        s = collect()
        smp = o.kv_get(KV_SAMPLES, {}) or {}
        ctx = brief(s, guests=False) + "\nDaily usage samples (bytes used, size) by date:\n" + json.dumps(
            dict(list(smp.items())[-30:]))[:6000] + f"\nGuest backup sizes:\n{guest_table(s)}"
        try:
            text = o.llm(run, f"""You are Iji, keeper of Master Hermes's homelab backups. Write the weekly backup capacity
forecast for Discord (Markdown, under 350 words):
1. One line per backup target: used/free, growth per day, days until full.
2. Biggest consumers (guests with the largest backups) and whether retention (keep-daily/weekly/monthly) looks right.
3. A clear verdict: should Master Hermes add backup storage? If yes: which target, how much (round, e.g. 2 TB) and by
   when; if no: when to look again. Mention cheaper alternatives first (retention, prune/GC, excluding big disks).
Use only the numbers below; never invent.

{ctx}""", model="gpt-6-luna", max_output_tokens=1200, tips=False)
        except Exception as e:  # noqa: BLE001
            text = f"(LLM unavailable: {e})\n\n{advice_text(s)}\n\n{storage_table(s)}"
        o.respond(AGENT, cid, "📈 Weekly backup capacity forecast", text, run=run)
        run.summary = advice_text(s)[:300]


# --------------------------------------------------------------------------- actions

def _wait_task(node, upid, timeout=4 * 3600):
    end = time.time() + timeout
    while time.time() < end:
        rc, out, _ = o.ssh(node, f"pvesh get /nodes/{node}/tasks/{upid}/status --output-format json", timeout=40)
        st = _json(out, {}) if rc == 0 else {}
        if st.get("status") == "stopped":
            return st.get("exitstatus", "?")
        time.sleep(15)
    return "timeout (still running?)"


def run_backups(ids, storage=None, reply_to=None, channel=None):
    """Back up guests now (non-destructive). Reports each result to Discord."""
    cid = o.channel_id(channel or CHANNEL, TOPIC if not channel else None)
    s = collect()
    guests = {g["id"]: g for g in s["guests"]}
    with o.Run(AGENT, f"backup now: {','.join(map(str, ids))}", "chat" if reply_to else "approval") as run:
        results = []
        for vid in ids:
            g = guests.get(int(vid))
            if not g:
                results.append((vid, "?", "❌ unknown VMID", 0, 0))
                continue
            st = storage or ("ProxmoxData" if "pbs" in (g["name"] or "").lower() else "pbs-daily")
            t0 = time.time()
            rc, out, err = o.ssh(g["node"], f"pvesh create /nodes/{g['node']}/vzdump --vmid {g['id']} --storage {st} "
                                            f"--mode snapshot --notes-template '{{{{guestname}}}} (on demand, Iji)' "
                                            f"--output-format json", timeout=90)
            upid = _json(out, out).strip('"') if rc == 0 else None
            if not upid or not str(upid).startswith("UPID"):
                results.append((g["id"], g["name"], f"❌ could not start: {(err or out)[:200]}", 0, 0))
                continue
            o.post(cid, f"🛡️ Backing up **{g['id']} {g['name']}** ({g['kind']} on {g['node']}) → `{st}`…",
                   reply_to=reply_to)
            status = _wait_task(g["node"], upid)
            mins = (time.time() - t0) / 60
            if status == "OK":
                s2 = collect()
                size = (s2["snaps"].get(str(g["id"]), {}).get(st) or {}).get("size", 0)
                results.append((g["id"], g["name"], f"✅ OK → {st}", mins, size))
            else:
                _, errs, _ = task_errors(g["node"], upid)
                fix = known_fix("\n".join(errs) + status)
                results.append((g["id"], g["name"], f"❌ {status}: {' / '.join(errs[-3:])[:300]}"
                                + (f" → {fix}" if fix else ""), mins, 0))
        ok = all(r[2].startswith("✅") for r in results)
        text = "\n".join(f"{'✅' if r[2].startswith('✅') else '❌'} **{r[0]} {r[1]}** · {r[2].lstrip('✅❌ ')} · "
                         f"{r[3]:.1f} min" + (f" · {_gb(r[4])} GB" if r[4] else "") for r in results)
        run.summary = text[:300]
        o.respond(AGENT, cid, "🛡️ Backup finished" if ok else "⚠️ Backup finished with errors", text,
                  reply_to=reply_to, run=run, color=o.SEV_COLORS["ok" if ok else "high"])
        if not ok:
            run.finish("failed", run.summary)
        return ok


def add_to_jobs(ids):
    ids = [int(i) for i in ids]
    cid = o.channel_id(CHANNEL, TOPIC)
    with o.Run(AGENT, f"add {ids} to backup jobs", "approval") as run:
        s = collect()
        done = []
        for j in s["jobs"]:
            if j.get("all"):
                done.append(f"`{j['id']}` already covers all guests")
                continue
            cur = [int(v) for v in str(j.get("vmid") or "").split(",") if v.strip().isdigit()]
            new = sorted(set(cur) | set(ids))
            if new == sorted(cur):
                done.append(f"`{j['id']}` already had them")
                continue
            node, out = pve(f"pvesh set /cluster/backup/{j['id']} --vmid {','.join(map(str, new))} && echo OK")
            done.append(f"`{j['id']}` ({j.get('schedule')} → {j.get('storage')}): "
                        + ("✅ added" if node and "OK" in out else f"❌ {out[:200]}"))
        run.summary = "; ".join(done)[:300]
        o.respond(AGENT, cid, f"🛡️ Backup schedule updated for {', '.join(map(str, ids))}", "\n".join(done), run=run)
        o.log(AGENT, f"add-to-jobs {ids}: {done}")
    run_backups(ids)


# --------------------------------------------------------------------------- Omada config backup

class _OmadaAdmin:
    def __init__(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        self.op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                              urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.base = os.environ["OMADA_HOST"].rstrip("/")
        self.cid = self._json("GET", "/api/info")["result"]["omadacId"]
        r = self._json("POST", f"/{self.cid}/api/v2/login", {"username": os.environ["OMADA_BACKUP_USER"],
                                                             "password": os.environ["OMADA_BACKUP_PASS"]})
        if r.get("errorCode") != 0:
            raise RuntimeError(f"Omada login failed: {r.get('msg')}")
        self.h = {"Csrf-Token": r["result"]["token"]}

    def _raw(self, method, path, data=None, h=None, timeout=120):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json", **(h or {})})
        with self.op.open(req, timeout=timeout) as f:
            return f.read(), f.headers

    def _json(self, method, path, data=None, h=None):
        return json.loads(self._raw(method, path, data, h)[0])

    def export(self):
        r = self._json("POST", f"/{self.cid}/api/v2/maintenance/backup/prepareBackup", h=self.h)
        if r.get("errorCode") != 0:
            raise RuntimeError(f"prepareBackup refused: {r.get('errorCode')} {r.get('msg')}")
        body, hdr = self._raw("GET", f"/{self.cid}/api/v2/files/backup?retention=-1", h=self.h, timeout=300)
        if body[:12].startswith(b'{"errorCode'):
            raise RuntimeError(f"download refused: {body[:200].decode(errors='replace')}")
        if len(body) < 1024:
            raise RuntimeError(f"backup file suspiciously small ({len(body)} bytes)")
        return body


def omada_export(reply_to=None, channel=None):
    if not os.environ.get("OMADA_BACKUP_USER"):
        import access
        access.report(AGENT, "Omada controller backup (settings export)",
                      "an Omada admin account for Iji (OMADA_BACKUP_USER / OMADA_BACKUP_PASS in ~/.olympus_env); "
                      "the read-only claude-reader account gets -1007 'no permission' on maintenance/backup",
                      "weekly Omada config backups to the NAS")
        print("no OMADA_BACKUP_USER: access gap logged")
        return False
    cid = o.channel_id(channel or CHANNEL, TOPIC if not channel else None)
    with o.Run(AGENT, "Omada config export", "chat" if reply_to else "schedule") as run:
        name = f"omada-settings-{o.local().strftime('%Y%m%d-%H%M')}.cfg"
        try:
            data = _OmadaAdmin().export()
            cmd = (f"mkdir -p {OMADA_DIR} && cat > {OMADA_DIR}/{name} && chmod 600 {OMADA_DIR}/{name} && "
                   f"ls -1t {OMADA_DIR}/omada-settings-*.cfg | tail -n +{OMADA_KEEP + 1} | xargs -r rm -f -- && "
                   f"ls -1 {OMADA_DIR} | wc -l")
            err = None
            for n in NODES:
                p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", n, cmd], input=data,
                                   capture_output=True, timeout=120)
                if p.returncode == 0:
                    break
                err = p.stderr.decode(errors="replace")[:200]
            else:
                raise RuntimeError(f"upload to NAS failed: {err}")
            o.kv_set(KV_OMADA, {"last": time.time(), "file": f"{OMADA_DIR}/{name}", "size": len(data)})
            run.summary = f"Omada settings exported ({len(data) // 1024} KB) to {OMADA_DIR}/{name}"
            o.respond(AGENT, cid, "🛡️ Omada config backed up", f"Settings-only export (**{len(data) // 1024} KB**) saved to "
                      f"the NAS: `{OMADA_DIR}/{name}` (keeping the last {OMADA_KEEP}).\nRestore: Omada controller → "
                      f"Settings → Maintenance → Restore.", reply_to=reply_to, run=run)
            return True
        except Exception as e:  # noqa: BLE001
            o.kv_set(KV_OMADA, {**(o.kv_get(KV_OMADA, {}) or {}), "error": str(e)[:300], "error_ts": time.time()})
            o.respond(AGENT, cid, "⚠️ Omada config backup failed", str(e)[:1500], reply_to=reply_to, run=run,
                      color=o.SEV_COLORS["high"])
            run.finish("failed", f"Omada export failed: {e}"[:300])
            return False


# --------------------------------------------------------------------------- ask (Master Hermes)

INTENT = o.S(action=o.E("backup_now", "add_to_schedule", "omada_backup", "answer"),
             targets=o.A(o.STR), storage=o.E("default", "pbs-daily", "pbs-main", "ProxmoxData"))


def _resolve(targets, s):
    out, unknown = [], []
    gs = [g for g in s["guests"] if not g["template"]]
    for t in targets:
        t = t.strip().lower()
        if t in ("new", "unprotected", "not backed up", "without backup"):
            out += [g["id"] for g in s["no_job"]]
            continue
        if t in ("all", "everything"):
            out += [g["id"] for g in gs]
            continue
        m = [g for g in gs if str(g["id"]) == t] or [g for g in gs if (g["name"] or "").lower() == t] or \
            [g for g in gs if t and t in (g["name"] or "").lower()]
        if len(m) == 1 or (m and str(m[0]["id"]) == t):
            out.append(m[0]["id"])
        else:
            unknown.append(t + (" (matches " + ", ".join(f"{g['id']} {g['name']}" for g in m[:5]) + ")" if m else ""))
    return sorted(set(out)), unknown


def ask(text, reply_to=None, channel=None):
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", text, "discord", channel, reply_to or "")
    s = collect()
    with o.Run(AGENT, f"ask: {text[:200]}", "chat") as run:
        guests = ", ".join(f"{g['id']} {g['name']}" for g in s["guests"] if not g["template"])
        intent = o.llm(run, f"""Classify Master Hermes's request to Iji, the homelab backup agent.
backup_now: he wants a backup taken now (targets = VMIDs or guest names exactly as he said them, or "new" for guests
  that have no backup job, or "all").
add_to_schedule: he wants guests added to the scheduled backup jobs.
omada_backup: he wants the Omada network controller configuration backed up now.
answer: anything else (status, failures, storage space, advice, explanations). targets=[] then.
storage: only if he names a target (daily/NVMe=pbs-daily, main/weekly/HDD=pbs-main, NAS/NFS=ProxmoxData), else default.
Guests: {guests}
Request: {text}""", schema=INTENT, model="gpt-6-luna")
        act = intent["action"]
        if act in ("backup_now", "add_to_schedule"):
            ids, unknown = _resolve(intent["targets"], s)
            if unknown or not ids:
                o.respond(AGENT, channel, "🛡️ Which guest?",
                          ("I could not match: " + "; ".join(unknown) + "\n" if unknown else "") +
                          "Tell me the VMID or exact name, Master Hermes (e.g. `Iji, back up 205`).",
                          reply_to=reply_to, run=run)
                if not ids:
                    return
            if act == "add_to_schedule":
                names = {g["id"]: g["name"] for g in s["guests"]}
                o.request_approval(AGENT, f"Add {', '.join(map(str, ids))} to the backup schedule",
                                   "Iji will add " + ", ".join(f"**{i} {names.get(i, '')}**" for i in ids) +
                                   " to every scheduled job (" + ", ".join(f"`{j['id']}` {j.get('schedule')} → "
                                                                           f"{j.get('storage')}" for j in s["jobs"]) +
                                   ") and take a first backup to pbs-daily.",
                                   [sys.executable, os.path.abspath(__file__), "add-to-jobs",
                                    ",".join(map(str, ids))], channel, reply_to=reply_to)
                run.summary = f"proposed adding {ids} to jobs"
                return
            run.summary = f"backing up {ids}"
        elif act == "omada_backup":
            run.summary = "Omada export"
        else:
            answer = o.llm(run, f"""You are Iji, the Roundtable's keeper of backups (Elden Ring's Iji: a calm, loyal war
counsellor and smith who guards what matters). Answer Master Hermes in Discord Markdown, concise and practical.
Use ONLY the live data below. For storage questions give used/free/growth/days-to-full and a clear "add storage or not"
verdict. For failures give cause and exact fix. Mention he can say "Iji, back up <vmid>" for an immediate backup.

LIVE BACKUP DATA:
{brief(s)}

QUESTION: {text}""", model="gpt-6-luna", max_output_tokens=1500)
            run.summary = (answer or "")[:300]
            o.respond(AGENT, channel, "🛡️ Iji answers", answer or "No answer.", reply_to=reply_to, run=run)
            return
    if act == "backup_now":
        run_backups(ids, None if intent["storage"] == "default" else intent["storage"], reply_to, channel)
    else:
        omada_export(reply_to, channel)


# --------------------------------------------------------------------------- voice tool

def voice_brief(filter_=None):
    s = collect()
    txt = brief(s)
    if filter_:
        f = filter_.lower()
        keep = [l for l in txt.splitlines() if f in l.lower()]
        txt = "\n".join(keep) or txt
    return {"backups": txt[:11000],
            "note": "To back something up now, delegate to agent 'backup' with e.g. 'back up 205 now'. "
                    "Adding guests to the schedule needs Master Hermes's ✅ in Discord."}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["watch", "digest", "forecast", "omada-export", "ask", "run", "add-to-jobs",
                                    "free-space", "status"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    ap.add_argument("--storage")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "watch":
        watch()
    elif a.cmd == "free-space":
        for p in free_space(store=a.storage, dry=a.dry_run):
            print(f"{p['store']}: {p['eligible']} backup(s) older than {PRUNE_AGE_D} d; "
                  + (p["skipped"] or f"{'would delete' if a.dry_run else 'deleted'} "
                     f"{len(p.get('deleted', p['pick']))}"))
    elif a.cmd == "digest":
        digest()
    elif a.cmd == "forecast":
        forecast()
    elif a.cmd == "omada-export":
        omada_export()
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    elif a.cmd == "run":
        sys.exit(0 if run_backups([int(x) for x in a.arg.split(",")], a.storage) else 1)
    elif a.cmd == "add-to-jobs":
        add_to_jobs(a.arg.split(","))
    else:
        print(brief())


if __name__ == "__main__":
    main()
