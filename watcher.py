#!/usr/bin/env python3
"""Sellen - observability agent (Prometheus + Loki + Grafana).

  watcher.py check              # every 2 min (watcher.timer): evaluate rules, dedupe, alert/resolve on Discord
  watcher.py digest             # 08:00 (watcher-digest.timer): morning health digest
  watcher.py ask "<question>" [--reply-to MID] [--channel CID]   # on demand via Gideon

Detection is deterministic PromQL/LogQL; gpt-6-luna is only used to explain newly firing alerts
and to answer questions.
"""
import argparse
import json
import os
import re
import time
import urllib.parse

import olympus_core as o

AGENT = "watcher"
CHANNEL = "infrastructure-alerts"
PROM = os.environ.get("PROM_URL", "http://192.168.40.13:9090")
LOKI = os.environ.get("LOKI_URL", "http://192.168.40.13:3100")
GRAFANA_PUBLIC = "https://grafana.hrmsmrflrii.xyz"
FIRE_AFTER = 2       # consecutive failing checks before alerting (2 x 2 min)
RESOLVE_AFTER = 2    # consecutive clean checks before resolving
REMIND_HOURS = {"critical": 4, "high": 12, "medium": 24, "low": 0}
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# --------------------------------------------------------------------------- data sources

def prom(query):
    code, d = o.http("GET", f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query}), timeout=20)
    if code != 200 or not isinstance(d, dict) or d.get("status") != "success":
        raise RuntimeError(f"PromQL failed ({code}): {query[:80]}")
    return d["data"]["result"]


def loki_ok():
    code, _ = o.http("GET", f"{LOKI}/ready", timeout=5, retries=1)
    return code == 200


def loki(query, kind="query", minutes=None, limit=100):
    params = {"query": query, "limit": limit}
    if kind == "query_range":
        end = time.time()
        params.update(start=int((end - (minutes or 60) * 60) * 1e9), end=int(end * 1e9), direction="backward")
    code, d = o.http("GET", f"{LOKI}/loki/api/v1/{kind}?" + urllib.parse.urlencode(params), timeout=30)
    if code != 200 or not isinstance(d, dict):
        raise RuntimeError(f"LogQL failed ({code}): {str(d)[:200]}")
    return d["data"]["result"]


def val(r):
    return float(r["value"][1])


# --------------------------------------------------------------------------- GitLab (one issue per alert)

GITLAB = os.environ.get("SRE_GITLAB_BASE", "http://192.168.40.23/api/v4")
PROJECT = int(os.environ.get("SRE_GITLAB_PROJECT", "6"))


def gl(method, path, data=None):
    try:
        return o.http(method, f"{GITLAB}/projects/{PROJECT}{path}",
                      {"PRIVATE-TOKEN": os.environ.get("GITLAB_TOKEN", "")}, data, timeout=15, retries=1)
    except Exception as e:  # noqa: BLE001 - GitLab outages must not stop alerting
        o.log(AGENT, f"gitlab {method} {path} failed: {e}")
        return 0, None


def gl_open(a, note=None):
    """Open a GitLab issue for a firing alert; returns iid or None."""
    body = (f"**Sellen alert** · severity **{a['sev']}** · source `{a.get('source', '?')}`\n\n"
            f"- First seen: {o.local(o.parse_iso(a['first'])).strftime('%Y-%m-%d %H:%M')} (Asia/Manila)\n"
            f"- Detail: {a['detail']}\n")
    if note:
        body += f"- Impact: {note.get('impact', '')}\n- First check: `{note.get('check', '')}`\n"
    body += (f"\nGrafana: {GRAFANA_PUBLIC}\n\nThis issue is opened and closed automatically by the Roundtable "
             f"Sellen agent (closes after {RESOLVE_AFTER} consecutive clean checks).")
    code, d = gl("POST", "/issues", {"title": f"[alert] {a['title']}"[:250], "description": body,
                                     "labels": f"alert,severity::{a['sev']},source::watcher"})
    return d.get("iid") if code == 201 and isinstance(d, dict) else None


def gl_note(iid, body):
    if iid:
        gl("POST", f"/issues/{iid}/notes", {"body": body[:60000]})


def gl_close(iid, body):
    if iid:
        gl_note(iid, body)
        gl("PUT", f"/issues/{iid}", {"state_event": "close", "add_labels": "resolved"})

def guest_names():
    out = {}
    for r in prom('max by (id, name, node, template) (pve_guest_info)'):
        m = r["metric"]
        out[m["id"]] = (m.get("name", m["id"]), m.get("node", "?"), m.get("template") == "1")
    return out


def gb(x):
    return f"{x / 1e9:,.0f} GB" if x < 1e12 else f"{x / 1e12:,.2f} TB"


# --------------------------------------------------------------------------- rules -> list of findings

def evaluate():
    """Return {key: finding}. finding = dict(sev, title, detail, source)."""
    F = {}

    def add(key, sev, title, detail, source="prometheus"):
        F[key] = {"sev": sev, "title": title, "detail": detail, "source": source}

    names = guest_names()

    for r in prom('up == 0'):
        m = r["metric"]
        add(f"target:{m.get('job')}:{m.get('instance')}", "medium", f"Scrape target down: {m.get('job')} / {m.get('instance')}",
            "Prometheus cannot scrape this exporter, so its dashboards are blind.")

    for r in prom('max by (id) (pve_up{id=~"node/.*"}) == 0'):
        add(f"node:{r['metric']['id']}", "critical", f"Proxmox {r['metric']['id']} is DOWN",
            "Cluster reports the node offline; its guests are unavailable unless HA moved them.")

    onboot = {r["metric"]["id"] for r in prom('max by (id) (pve_onboot_status) == 1')}
    for r in prom('max by (id) (pve_up{id=~"(qemu|lxc)/.*"}) == 0'):
        gid = r["metric"]["id"]
        name, node, tpl = names.get(gid, (gid, "?", False))
        if tpl or gid not in onboot:
            continue
        add(f"guest:{gid}", "high", f"{name} ({gid}) is stopped", f"Guest is set to start on boot but is not running on {node}.")

    for r in prom('max by (id) (avg_over_time(pve_cpu_usage_ratio{id=~"node/.*"}[10m])) > 0.9'):
        add(f"cpu:{r['metric']['id']}", "high", f"{r['metric']['id']} CPU {val(r) * 100:.0f}% (10 min avg)",
            "Sustained CPU saturation on a hypervisor slows every guest on it.")

    for r in prom('max by (id) (pve_memory_usage_bytes{id=~"node/.*"} / pve_memory_size_bytes{id=~"node/.*"}) > 0.92'):
        add(f"mem:{r['metric']['id']}", "high", f"{r['metric']['id']} memory {val(r) * 100:.0f}%",
            "Hypervisor memory nearly exhausted; risk of OOM kills and ballooning.")

    for r in prom('max by (id) (avg_over_time(pve_memory_usage_bytes{id=~"(qemu|lxc)/.*"}[10m]) / '
                  'pve_memory_size_bytes{id=~"(qemu|lxc)/.*"}) > 0.95'):
        gid = r["metric"]["id"]
        name = names.get(gid, (gid,))[0]
        add(f"gmem:{gid}", "medium", f"{name} memory {val(r) * 100:.0f}% (10 min avg)",
            "Guest is close to its memory limit; services inside may be OOM-killed.")

    for r in prom('max by (id) (pve_disk_usage_bytes{id=~"storage/.*"} / pve_disk_size_bytes{id=~"storage/.*"}) > 0.85'):
        sev = "critical" if val(r) > 0.95 else "high"
        add(f"storage:{r['metric']['id']}", sev, f"Storage {r['metric']['id'].split('/', 1)[1]} {val(r) * 100:.0f}% full",
            "Proxmox storage filling up; backups and new disks will fail when it is full.")

    for r in prom('max by (id) (pve_disk_usage_bytes{id=~"lxc/.*"} / pve_disk_size_bytes{id=~"lxc/.*"}) > 0.88'):
        gid = r["metric"]["id"]
        name = names.get(gid, (gid,))[0]
        sev = "high" if val(r) > 0.95 else "medium"
        add(f"lxcdisk:{gid}", sev, f"{name} root disk {val(r) * 100:.0f}% full", "LXC rootfs nearly full.")

    for r in prom('(node_filesystem_avail_bytes{fstype=~"ext4|xfs|btrfs|zfs"} / node_filesystem_size_bytes) < 0.1'):
        m = r["metric"]
        add(f"fs:{m.get('instance')}:{m.get('mountpoint')}", "high",
            f"{m.get('instance')} {m.get('mountpoint')} only {val(r) * 100:.0f}% free", "Filesystem almost full.")

    for r in prom('max by (datastore) (pbs_used / pbs_size) > 0.85'):
        add(f"pbs:{r['metric']['datastore']}", "high", f"PBS datastore {r['metric']['datastore']} {val(r) * 100:.0f}% full",
            "Backups will start failing when the datastore is full; check prune/GC.")

    # Backup freshness for running, onboot guests (pbs-daily)
    last = {}
    for r in prom('max by (vm_id) (pbs_snapshot_vm_last_timestamp)'):
        last[r["metric"]["vm_id"]] = val(r)
    now = time.time()
    for gid in onboot:
        name, node, tpl = names.get(gid, (gid, "?", False))
        vmid = gid.split("/")[1]
        if tpl or name in ("pbs-server",):
            continue
        age_h = (now - last[vmid]) / 3600 if vmid in last else None
        if age_h is None or age_h > 36:
            add(f"backup:{gid}", "medium", f"{name} has no backup in {'ever' if age_h is None else f'{age_h:.0f} h'}",
                "The patch backup gate requires a recent PBS snapshot; this guest would block patching.")

    for metric, label in (("synologyRaidStatus", "RAID"), ("synologyDiskHealthStatus", "disk health"),
                          ("synologySystemStatus", "system")):
        for r in prom(f'{metric} != 1'):
            idx = r["metric"].get("raidIndex") or r["metric"].get("diskIndex") or ""
            add(f"syno:{metric}:{idx}", "critical" if label == "RAID" else "high",
                f"Synology {label} {idx} status {val(r):.0f}", "NAS reports a degraded component.")
    for r in prom('synologyDiskTemperature > 55'):
        add(f"synotemp:{r['metric'].get('diskIndex')}", "medium", f"Synology disk {r['metric'].get('diskIndex')} at {val(r):.0f}°C",
            "Drive running hot.")

    for r in prom('max by (device) (avg_over_time(omada_device_cpu_percentage[10m])) > 90'):
        add(f"omadacpu:{r['metric']['device']}", "medium", f"{r['metric']['device']} CPU {val(r):.0f}%",
            "Network device CPU saturated; can cause latency or dropped clients.")

    for r in prom('sum by (service) (rate(traefik_service_requests_total{code=~"5.."}[5m])) > 0.2 and '
                  'sum by (service) (rate(traefik_service_requests_total{code=~"5.."}[5m])) / '
                  'sum by (service) (rate(traefik_service_requests_total[5m])) > 0.2'):
        svc = r["metric"]["service"]
        add(f"5xx:{svc}", "high", f"{svc} returning 5xx ({val(r):.2f}/s)", "Traefik sees server errors from this backend.")

    if loki_ok():
        try:
            for r in loki('sum by (host, container) (count_over_time({container=~".+"} '
                          '|~ "(?i)(level=error|\\\\berror\\\\b|fatal|panic|exception)" [10m])) > 100'):
                m = r["metric"]
                add(f"logs:{m.get('host')}:{m.get('container')}", "low",
                    f"Error-log spike: {m.get('container')} on {m.get('host')} ({val(r):.0f} in 10 min)",
                    "Many error lines in the container logs.", "loki")
        except RuntimeError as e:
            o.log(AGENT, f"loki rule failed: {e}")
    return F


# --------------------------------------------------------------------------- alert lifecycle

def explain(run, new):
    """One cheap luna call explaining all newly firing alerts."""
    items = "\n".join(f"- [{f['sev']}] {f['title']}: {f['detail']}" for f in new)
    S = o.S(items=o.A(o.S(title=o.STR, impact=o.STR, check=o.STR)))
    try:
        r = o.llm(run, f"""Homelab alerts that just started firing (Proxmox cluster MorpheusCluster, Docker hosts,
Synology NAS, PBS, Omada network, Traefik):
{items}

For each alert, in the same order: `impact` = one plain-English sentence on what the owner will notice;
`check` = the single most useful first diagnostic command or Grafana panel. No speculation about causes you cannot know.""",
                  schema=S, max_output_tokens=1200)
        return {x["title"]: x for x in r["items"]}
    except o.BudgetExceeded:
        return {}
    except Exception as e:
        o.log(AGENT, f"explain failed: {e}")
        return {}


def check():
    st = o.kv_get("watcher_state", {"alerts": {}, "baseline": False})
    alerts = st["alerts"]
    try:
        found = evaluate()
    except RuntimeError as e:
        n = st.get("prom_fail", 0) + 1
        st["prom_fail"] = n
        if n == 3:
            o.post(CHANNEL, embeds=[o.embed(AGENT, "Prometheus unreachable", f"Sellen cannot query Prometheus: {e}",
                                            color=o.SEV_COLORS["high"])])
        o.kv_set("watcher_state", st)
        o.heartbeat(AGENT, f"prometheus error: {e}")
        return
    st["prom_fail"] = 0
    now = o.iso()
    to_fire, resolved = [], []
    for k, f in found.items():
        a = alerts.setdefault(k, {"seen": 0, "clear": 0, "fired": False, "first": now})
        a.update(sev=f["sev"], title=f["title"], detail=f["detail"], source=f["source"], last=now)
        a["seen"] += 1
        a["clear"] = 0
        if not a["fired"] and a["seen"] >= FIRE_AFTER:
            to_fire.append((k, a))
        elif a["fired"] and REMIND_HOURS.get(a["sev"]) and \
                (o.utcnow() - o.parse_iso(a["notified"])).total_seconds() > REMIND_HOURS[a["sev"]] * 3600:
            to_fire.append((k, a))
    for k, a in list(alerts.items()):
        if k in found:
            continue
        a["clear"] += 1
        if a["clear"] >= RESOLVE_AFTER:
            if a["fired"]:
                import hierarchy
                audit = hierarchy.track_source("watcher_state", k, a, AGENT, CHANNEL)
                hierarchy.resolve(audit, "resolved", f"Verified clear for {RESOLVE_AFTER} consecutive checks at {now}")
                resolved.append(a)
            del alerts[k]

    if not st.get("baseline"):
        # First run: one summary of pre-existing problems instead of a burst of alerts.
        st["baseline"] = True
        for k, a in alerts.items():
            a["fired"], a["notified"] = True, now
        if alerts:
            lines = sorted(alerts.values(), key=lambda a: SEV_RANK[a["sev"]])
            o.post(CHANNEL, embeds=[o.embed(AGENT, f"Sellen is watching · {len(alerts)} pre-existing issues",
                                            "\n".join(f"{SEV_ICON[a['sev']]} {a['title']}" for a in lines)[:3900],
                                            footer="Baseline - these will only be re-announced if they change. "
                                                   "Alerts fire after 2 failing checks (4 min).")])
        o.kv_set("watcher_state", st)
        o.heartbeat(AGENT, f"baseline: {len(alerts)} active")
        return

    if to_fire:
        new = [a for _, a in to_fire if not a["fired"]]
        with o.Run(AGENT, f"alert: {len(to_fire)} firing", "alert") as run:
            notes = explain(run, new) if new else {}
            to_fire.sort(key=lambda x: SEV_RANK[x[1]["sev"]])
            worst = to_fire[0][1]["sev"]
            fields = []
            for k, a in to_fire[:10]:
                n = notes.get(a["title"], {})
                body = a["detail"]
                if n:
                    body += f"\n**Impact:** {n['impact']}\n**First check:** `{n['check'][:200]}`"
                if a["fired"]:
                    body = f"⏰ Still firing since {o.local(o.parse_iso(a['first'])).strftime('%d %b %H:%M')}. " + body
                fields.append((f"{SEV_ICON[a['sev']]} {a['title']}", body, False))
            mention = worst in ("critical", "high") and any(not a["fired"] for _, a in to_fire)
            o.post(CHANNEL, content=f"<@{o.OWNER_ID}>" if mention else None, mention_owner=mention,
                   embeds=[o.embed(AGENT, f"{len(to_fire)} alert{'s' if len(to_fire) > 1 else ''} firing", 
                                   f"Grafana: {GRAFANA_PUBLIC}", fields, color=o.SEV_COLORS[worst],
                                   footer=f"Roundtable run #{run.id} · Corhyn handles container/host incidents")])
            run.summary = "; ".join(a["title"] for _, a in to_fire)[:500]
        for _, a in to_fire:
            if a["fired"]:
                gl_note(a.get("iid"), f"⏰ Still firing ({a['detail']})")
            else:
                a["iid"] = gl_open(a, notes.get(a["title"]))
            a["fired"], a["notified"] = True, now
    if resolved:
        for a in resolved:
            gl_close(a.get("iid"), f"✅ Resolved at {o.local().strftime('%Y-%m-%d %H:%M')} — clean for "
                                   f"{RESOLVE_AFTER} consecutive checks (firing since "
                                   f"{o.local(o.parse_iso(a['first'])).strftime('%d %b %H:%M')}).")
        o.post(CHANNEL, embeds=[o.embed(AGENT, f"✅ {len(resolved)} resolved",
                                        "\n".join(f"~~{a['title']}~~ (since {o.local(o.parse_iso(a['first'])).strftime('%d %b %H:%M')})"
                                                  + (f" · GitLab #{a['iid']} closed" if a.get("iid") else "")
                                                  for a in resolved), color=o.SEV_COLORS["ok"])])
        o.record_run(AGENT, f"resolved: {len(resolved)}", "ok", "; ".join(a["title"] for a in resolved), trigger="alert")
    # backfill issues for alerts that fired before GitLab tracking existed (baseline), a few per run
    for a in [a for a in alerts.values() if a["fired"] and not a.get("iid") and not a.get("gl_tried")][:5]:
        a["gl_tried"] = True
        a["iid"] = gl_open(a)
    o.kv_set("watcher_state", st)
    active = [a for a in alerts.values() if a["fired"]]
    o.heartbeat(AGENT, f"{len(active)} active alerts" + (f" ({', '.join(a['title'] for a in active[:3])})" if active else ""))


# --------------------------------------------------------------------------- digest

def snapshot():
    names = guest_names()
    lines = []
    for r in prom('max by (id) (pve_cpu_usage_ratio{id=~"node/.*"})'):
        nid = r["metric"]["id"]
        mem = prom(f'max(pve_memory_usage_bytes{{id="{nid}"}} / pve_memory_size_bytes{{id="{nid}"}})')
        lines.append(f"{nid.split('/')[1]}: CPU {val(r) * 100:.0f}% · RAM {val(mem[0]) * 100 if mem else 0:.0f}%")
    stor = sorted(((r["metric"]["id"], val(r)) for r in prom(
        'max by (id) (pve_disk_usage_bytes{id=~"storage/.*"} / pve_disk_size_bytes{id=~"storage/.*"})')),
        key=lambda x: -x[1])
    seen, st_lines = set(), []
    for sid, v in stor:
        nm = sid.split("/")[-1]
        if nm in seen:
            continue
        seen.add(nm)
        st_lines.append(f"{nm} {v * 100:.0f}%")
    top_mem = sorted(((names.get(r["metric"]["id"], (r["metric"]["id"],))[0], val(r)) for r in prom(
        'max by (id) (pve_memory_usage_bytes{id=~"(qemu|lxc)/.*"} / pve_memory_size_bytes{id=~"(qemu|lxc)/.*"})')),
        key=lambda x: -x[1])[:5]
    pbs = [f"{r['metric']['datastore']} {val(r) * 100:.0f}%" for r in prom('max by (datastore) (pbs_used / pbs_size)')]
    down = [f"{r['metric'].get('job')}/{r['metric'].get('instance')}" for r in prom('up == 0')]
    req = prom('sum(increase(traefik_service_requests_total[24h]))')
    err5 = prom('sum(increase(traefik_service_requests_total{code=~"5.."}[24h]))')
    logs = []
    if loki_ok():
        try:
            logs = sorted(((r["metric"].get("container"), r["metric"].get("host"), val(r)) for r in loki(
                'topk(5, sum by (host, container) (count_over_time({container=~".+"} '
                '|~ "(?i)(level=error|\\\\berror\\\\b|fatal|panic)" [24h])))')), key=lambda x: -x[2])
        except RuntimeError:
            pass
    return {"nodes": lines, "storage": st_lines[:8], "top_guest_mem": [f"{n} {v * 100:.0f}%" for n, v in top_mem],
            "pbs": pbs, "targets_down": down,
            "traefik_24h": f"{val(req[0]) if req else 0:,.0f} requests, {val(err5[0]) if err5 else 0:,.0f} 5xx",
            "error_logs_24h": [f"{c}@{h}: {v:,.0f}" for c, h, v in logs]}


def digest():
    st = o.kv_get("watcher_state", {"alerts": {}})
    active = [a for a in st["alerts"].values() if a.get("fired")]
    with o.Run(AGENT, "morning health digest", "schedule") as run:
        snap = snapshot()
        verdict = o.llm(run, f"""Homelab metrics snapshot (morning digest) and active alerts.
Snapshot: {json.dumps(snap)}
Active alerts: {json.dumps([a['title'] for a in active])}
Write 2-4 short sentences: overall health verdict, then the one or two things worth watching today. Plain English, no fluff.""",
                        max_output_tokens=400)
        fields = [("🖥️ Nodes", "\n".join(snap["nodes"]) or "n/a", True),
                  ("💾 Storage", "\n".join(snap["storage"]) or "n/a", True),
                  ("🧠 Top guest RAM", "\n".join(snap["top_guest_mem"]) or "n/a", True),
                  ("🗄️ PBS", ", ".join(snap["pbs"]) or "n/a", True),
                  ("🌐 Traefik 24 h", snap["traefik_24h"], True),
                  ("🎯 Targets down", ", ".join(snap["targets_down"]) or "none", True)]
        if snap["error_logs_24h"]:
            fields.append(("📜 Noisiest error logs 24 h", "\n".join(snap["error_logs_24h"]), False))
        if active:
            fields.append(("🚨 Active alerts", "\n".join(f"{SEV_ICON[a['sev']]} {a['title']}" for a in active)[:1000], False))
        o.post(CHANNEL, embeds=[o.embed(AGENT, f"Morning health · {o.local().strftime('%a %d %b')}", verdict, fields,
                                        footer=f"Roundtable run #{run.id} · {o.money(run.cost)} · {GRAFANA_PUBLIC}")])
        run.summary = verdict[:500]


# --------------------------------------------------------------------------- ask

QUERY_SCHEMA = o.S(queries=o.A(o.S(kind=o.E("promql", "logql_metric", "logql_logs", "grafana_dashboard", "glance_page"),
                                    query=o.STR, why=o.STR)))


def ask(question, reply_to=None, channel=None):
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", question, "discord", channel, reply_to or "")
    with o.Run(AGENT, f"ask: {question[:200]}", "chat") as run:
        catalogue = """Prometheus jobs: proxmox (pve-exporter: pve_up, pve_cpu_usage_ratio, pve_memory_usage_bytes/size, pve_disk_usage_bytes/size,
pve_guest_info{id,name,node}, pve_network_*_bytes; ids node/<n>, qemu/<vmid>, lxc/<vmid>, storage/<node>/<name>; scraped 3x so wrap in max by (id)),
proxmox-nodes (node_exporter on Proxmox nodes: node_load1, node_memory_MemAvailable_bytes, node_filesystem_*), node-immich,
cadvisor/cadvisor-media (container_cpu_usage_seconds_total, container_memory_working_set_bytes{name}, container_network_*),
pbs (pbs_used, pbs_size, pbs_snapshot_vm_last_timestamp{vm_id,vm_name}), synology (synologyRaidStatus, synologyDiskTemperature, hrStorageUsed),
omada (omada_device_cpu_percentage{device}, omada_client_connected_total, omada_client_rssi_dbm, omada_port_link_status), traefik (traefik_service_requests_total{service,code}).
Loki labels: host, container, compose_project, compose_service, stream, unit (journal).
Grafana dashboards (kind grafana_dashboard, query = dashboard name; returns every panel's current value): Container
Monitoring, Immich Host Health, PBS Backup Status, Proxmox Cluster Health, Proxmox Cluster Overview, Synology NAS
Storage (network dashboards belong to Torrent). Glance pages (kind glance_page, query = page name): home (lab overview), roundtable, compute,
storage, backup, services, media, news."""
        plan = o.llm(run, f"""Question from Master Hermes about his homelab: {question}

{catalogue}

Write 1-4 queries that answer it (instant PromQL; LogQL metric queries over a range like [1h]; logql_logs for raw
lines; grafana_dashboard / glance_page when he asks what a dashboard or page shows).""",
                     schema=QUERY_SCHEMA)
        results = []
        import observe
        for q in plan["queries"][:4]:
            try:
                if q["kind"] == "grafana_dashboard":
                    r = observe.grafana_tool(AGENT, q["query"])
                    results.append({"grafana": q["query"], "result": r.get("summary") or r})
                    continue
                if q["kind"] == "glance_page":
                    results.append({"glance": q["query"], "result": observe.glance_tool(AGENT, q["query"])})
                    continue
                if q["kind"] == "promql":
                    res = [(r["metric"], r["value"][1]) for r in prom(q["query"])][:40]
                elif q["kind"] == "logql_metric":
                    res = [(r["metric"], r["value"][1]) for r in loki(q["query"])][:40]
                else:
                    res = [line for s in loki(q["query"], "query_range", 60, 60) for _, line in s["values"]][:60]
                results.append({"query": q["query"], "result": res})
            except Exception as e:
                results.append({"query": q["query"], "error": str(e)[:200]})
        import inventory
        try:
            inv, _ = inventory.scoped_text(AGENT, "all", limit=6000)
        except Exception as e:  # noqa: BLE001
            inv = f"(inventory unavailable: {e})"
        answer = o.llm(run, f"""Question: {question}
Query results: {json.dumps(results)[:30000]}
Live inventory within your boundary ({inventory.boundary_text(AGENT)}):
{inv[:14000]}
Answer the question plainly from these results and the inventory (Discord markdown, <=1500 chars). Quote the numbers.
For a dashboard/page, explain what it shows panel by panel in plain English and call out anything abnormal.
If it is about the network layer, say it is outside your boundary and that Torrent owns it. If the data
does not answer it, say so and suggest which Grafana dashboard or query would. End with the queries used in a code block.""",
                       max_output_tokens=1500)
        run.summary = answer[:500]
        o.respond(AGENT, channel, "Sellen answers", answer, reply_to=reply_to, run=run)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "digest", "ask"])
    ap.add_argument("text", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "check":
        check()
    elif a.cmd == "digest":
        digest()
    else:
        ask(a.text, a.reply_to or None, a.channel or None)


if __name__ == "__main__":
    main()
