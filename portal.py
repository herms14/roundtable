#!/usr/bin/env python3
"""Roundtable Portal - read-only web dashboard for the agent team.

Shows every agent (role, character, skills, schedule, model), when it last ran and what it found,
the next scheduled run (systemd timers), tokens and cost per task, the approval queue and the
monthly budget, plus the voice room (talk to the agents through the OpenAI Realtime API, see voice.py).
Stdlib only; served on :8088 and published as https://roundtable.hrmsmrflrii.xyz behind Traefik + Authentik
(/avatars/* is public so Discord can show the agents' avatars).
"""
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import olympus_core as o

PORT = 8088
TRUSTED_PROXIES = {"192.168.40.20"}  # traefik-lxc (forwards X-authentik-* only after a successful login)
_cache = {}
_lock = threading.Lock()


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


# Overview page: lab links (live Traefik routes checked 2026-10-18; dead ones left out)
D = "https://{}.hrmsmrflrii.xyz"
LINKS = [
    ("Start here", [
        ("🗺️", "Hermes Lab Atlas", "/atlas", "Architecture, network, services, agents and hardware diagrams; PDF export"),
        ("🧩", "Glance", D.format("glance"), "Lab dashboard: health, media, alerts"),
        ("🏠", "Homepage", D.format("homepage"), "Service tiles with widgets"),
        ("📈", "Grafana", D.format("grafana"), "Metrics dashboards"),
        ("🚦", "VLAN Flow", D.format("vlanflow"), "Inter-VLAN traffic and ACL hits"),
    ]),
    ("Infrastructure", [
        ("🖥️", "Proxmox", D.format("proxmox"), "MorpheusCluster"),
        ("1️⃣", "node01", D.format("node01"), "Proxmox node01 UI"),
        ("2️⃣", "node02", D.format("node02"), "Proxmox node02 UI"),
        ("💾", "Proxmox Backup", D.format("pbs"), "PBS datastores"),
        ("🗄️", "Synology NAS", D.format("nas"), "DSM (LAN/Tailscale)"),
        ("🔀", "Traefik", D.format("traefik"), "Reverse proxy routes"),
        ("🔐", "Authentik", D.format("auth"), "SSO"),
        ("🗝️", "OpenBao", D.format("bao"), "Secrets store, kept by Varys (LAN/Tailscale)"),
        ("🧭", "Technitium", D.format("dns"), "Primary DNS + blocking"),
        ("🕳️", "Pi-hole", D.format("pihole"), "Secondary DNS"),
        ("📡", "Omada", D.format("omada"), "Network controller"),
        ("🦊", "GitLab", D.format("gitlab"), "Repos, CI, wiki"),
        ("⚡", "Power", D.format("power"), "Power usage"),
    ]),
    ("Monitoring", [
        ("🟢", "Uptime Kuma", D.format("uptime"), "Uptime checks"),
        ("🔥", "Prometheus", D.format("prometheus"), "Metrics and alerts"),
        ("🔎", "Jaeger", D.format("jaeger"), "Traces"),
        ("🚀", "Speedtest", D.format("speedtest"), "Internet speed history"),
    ]),
    ("Media", [
        ("🎬", "Jellyfin", D.format("jellyfin"), "Media server"),
        ("🙋", "Jellyseerr", D.format("jellyseerr"), "Requests"),
        ("🙋", "Overseerr", D.format("overseerr"), "Requests"),
        ("📺", "Sonarr", D.format("sonarr"), "TV"),
        ("🎞️", "Radarr", D.format("radarr"), "Movies"),
        ("🎵", "Lidarr", D.format("lidarr"), "Music"),
        ("🧭", "Prowlarr", D.format("prowlarr"), "Indexers"),
        ("💬", "Bazarr", D.format("bazarr"), "Subtitles"),
        ("📥", "SABnzbd", D.format("sabnzbd"), "Usenet downloads"),
        ("🌊", "Deluge", D.format("deluge"), "Torrent downloads"),
        ("🤖", "Autobrr", D.format("autobrr"), "Release automation"),
        ("⚙️", "Tdarr", D.format("tdarr"), "Transcoding"),
        ("👀", "Tracearr", D.format("tracearr"), "Playback tracking"),
        ("🎟️", "Wizarr", D.format("wizarr"), "Invites"),
        ("▶️", "MeTube", D.format("metube"), "Video downloads"),
    ]),
    ("Apps", [
        ("📷", "Immich", D.format("photos"), "Photos"),
        ("🏡", "Home Assistant", D.format("ha"), "Home automation"),
        ("📄", "Paperless-ngx", D.format("paperless"), "Documents"),
        ("🔖", "Karakeep", D.format("karakeep"), "Bookmarks"),
        ("🔁", "n8n", D.format("n8n"), "Workflows"),
        ("📰", "Chronicle", D.format("chronicle"), "Chronicle"),
    ]),
]


def links_payload():
    """LINKS plus a live up/down check of each external site (no redirects followed; 7 s timeout)."""
    import concurrent.futures, http.client, ssl
    ctx = ssl.create_default_context()

    def probe(url):
        if not url.startswith("https://"):
            return "up"
        try:
            c = http.client.HTTPSConnection(urllib.parse.urlparse(url).netloc, timeout=7, context=ctx)
            c.request("GET", "/", headers={"User-Agent": "roundtable-overview"})
            code = c.getresponse().status
            c.close()
        except ssl.SSLCertVerificationError:
            return "cert"   # it answers, but its certificate isn't trusted
        except Exception:
            return "down"
        return "down" if code >= 500 else "check" if code == 404 else "up"

    urls = [u for _, items in LINKS for _, _, u, _ in items]
    with concurrent.futures.ThreadPoolExecutor(12) as ex:
        st = dict(zip(urls, ex.map(probe, urls)))
    return {"checked": time.time(), "groups": [{"name": g, "links": [
        {"icon": i, "name": n, "url": u, "desc": d, "status": st[u]} for i, n, u, d in items]} for g, items in LINKS]}

PROM = os.environ.get("PROM_URL", "http://192.168.40.13:9090")


def dns_checks_payload():
    """Uptime Kuma's DNS checks (group "DNS") as Prometheus scrapes them: status 1 up, 0 down, 2 pending, 3 maintenance."""
    def prom(qry):
        code, d = o.http("GET", f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": qry}), timeout=8, retries=2)
        if code != 200 or not isinstance(d, dict) or d.get("status") != "success":
            raise RuntimeError(f"Prometheus {code}")
        return d["data"]["result"]
    try:
        up = prom('up{job="uptime-kuma"}')
        status = prom('monitor_status{monitor_type="dns"}')
        ms = {r["metric"].get("monitor_name"): float(r["value"][1])
              for r in prom('monitor_response_time{monitor_type="dns"}')}
    except Exception as e:  # noqa: BLE001 - shown on the page
        return {"ok": False, "error": str(e), "checks": []}
    checks = []
    for r in sorted(status, key=lambda r: r["metric"].get("monitor_name", "")):
        name = r["metric"].get("monitor_name", "?")
        server, _, what = name.partition(" · ")
        checks.append({"name": name, "server": server, "what": what or name, "status": int(float(r["value"][1])),
                       "ms": ms.get(name), "host": r["metric"].get("monitor_hostname")})
    return {"ok": bool(up) and up[0]["value"][1] == "1", "checked": time.time(), "checks": checks,
            "url": D.format("uptime")}


DEP_GROUPS = [  # id, label, colour, columns, description
    ("you", "You & the front door", "#ffd84d", 2, "Where you talk to the Roundtable"),
    ("exec", "Executive", "#fb923c", 2, "Gideon routes the work and runs what you approve"),
    ("ops", "Operations · Corhyn", "#4ce5b0", 3, "Lab health, network, builds, backups, media"),
    ("sec", "Security · Loretta", "#ff6c8b", 2, ""),
    ("gov", "Governance · Ranni", "#a78bfa", 3, "Checks, docs, reviews, research, writing"),
    ("spec", "Specialized", "#38bdf8", 2, ""),
    ("other", "Other agents", "#94a3b8", 2, ""),
    ("chan", "Discord channels", "#7c8cff", 4, "Everything is posted through the Sentinel bot"),
    ("sys", "Systems", "#e2e8f0", 3, "What the agents read and change"),
]
DEP_LAYOUT = [["you", "exec"], ["ops", "sec", "gov"], ["spec", "other"], ["chan", "sys"]]
DEP_KIND = {"command": "cmd", "post": "post", "approval": "approval", "trigger": "trigger", "data": "sys", "report": "report"}
DEP_SYS_TYPE = {"sys:proxmox": "node", "sys:hosts": "lxc", "sys:prom": "monitor", "sys:omada": "router",
                "sys:gitlab": "devops", "sys:github": "devops", "sys:vault": "data", "sys:web": "internet",
                "sys:openai": "cloud", "sys:storage": "storage", "sys:media": "container"}


def depmap_html(reporting=False):
    """The operational dependency map rendered by the Hermes Lab Atlas engine (grouped tiles, bundled lines)."""
    import atlas
    g = graph_payload()
    reg = {a["id"]: a for a in o.registry()["agents"]}

    def team(aid):
        a = reg.get(aid, {})
        lead = aid if a.get("is_lead") else a.get("reports_to")
        if aid == "boss":
            return "exec"
        if a.get("group") == "specialized":
            return "spec"
        return {"medic": "ops", "security": "sec", "governance": "gov"}.get(lead, "other")

    status = {"error": "down", "failed": "down"}
    nodes, ids = [], set()
    for n in g["nodes"]:
        if n["type"] == "agent":
            a = reg.get(n["id"])
            if not a:
                continue
            f = {"role": a.get("role", ""), "status": n.get("health") or "idle"}
            if n.get("last_run"):
                f["last run"] = n["last_run"].replace("T", " ")[:16] + " UTC"
            node = {"id": n["id"], "g": team(n["id"]), "t": "agent", "n": f"{a.get('emoji', '')} {a['name']}".strip(),
                    "f": f, "link": f"/#agent/{n['id']}", "linkLabel": "Open profile"}
            if n.get("health") in status:
                node["s"] = status[n["health"]]
                node["note"] = f"Last run {n['health']}"
        elif n["type"] == "discord":
            node = {"id": n["id"], "g": "you" if n["id"] == "ch:olympus" else "chan", "t": "channel", "n": n["label"],
                    "f": {"via": n.get("sub") or "Sentinel bot"}}
        elif n["type"] == "human":
            node = {"id": n["id"], "g": "you", "t": "person", "n": n["label"], "f": {"approves": "✅ in Discord"}}
        else:
            node = {"id": n["id"], "g": "sys", "t": DEP_SYS_TYPE.get(n["id"], "service"), "n": n["label"]}
            if n.get("sub"):
                node["f"] = {"via": n["sub"]}
        nodes.append(node)
        ids.add(n["id"])
    edges = []
    for e in g["edges"]:
        if e["from"] not in ids or e["to"] not in ids or (e["kind"] == "report" and not reporting):
            continue
        label = e.get("label") or ""
        if e.get("count") is not None:
            label += f" · {e['count']} in 30 days"
        edges.append([e["from"], e["to"], label, DEP_KIND.get(e["kind"], "dep")])
    used = {n["g"] for n in nodes}
    view = {"id": "deps", "title": "Dependency map",
            "blurb": "Operational connections between you, the agents, Discord channels and the systems they touch",
            "groups": [{"id": i, "label": lb, "c": c, "cols": cols, **({"desc": ds} if ds else {})}
                       for i, lb, c, cols, ds in DEP_GROUPS if i in used],
            "layout": [[x for x in row if x in used] for row in DEP_LAYOUT if any(x in used for x in row)],
            "nodes": nodes, "edges": edges}
    html = atlas.render([view], [], time.strftime("%d %b %Y %H:%M"))
    return (html.replace("const SNAPSHOT=", "var DEPMAP=1;\nconst SNAPSHOT=", 1)
                .replace("Hermes Lab Atlas", "Roundtable dependency map")
                .replace(ABOUT_ATLAS, "Built <b id=\"snap\"></b>, live from <code>graph.json</code>, <code>agents.json</code> and the run ledger "
                         "(event counts cover the last 30 days). Click a tile for its connections; an agent's panel links "
                         "to its profile. Edit connections in <code>agents/olympus/graph.json</code>.", 1))


ABOUT_ATLAS = ("Snapshot <b id=\"snap\"></b> from Proxmox, Omada, Docker on each host, node hardware and <code>agents.json</code>. "
               "Links marked “docs” come from older notes. Miriel redraws it from the live lab after every changelog entry "
               "and every 30 minutes; to move or relabel things, edit <code>agents/olympus/atlas_layout.json</code>.")


# Portal themes: one palette shared by the portal, /office and /atlas through /theme-kit.css + /theme-kit.js.
# Pages map these --k-* tokens onto their own variables; "glass" means no theme class (each page's original look).
THEME_DEFAULT = "bento"
THEME_NAMES = {"bento": "Aurora Bento", "mini": "Minimal Pro", "frost": "Nordic Night", "pastel": "Pastel Night", "glass": "Glass (classic)"}
THEMES = {
    "bento": {"bg": "#07070d", "panel": "rgba(255,255,255,.055)", "solid": "#13131d", "inset": "rgba(0,0,0,.3)",
              "bd": "rgba(255,255,255,.09)", "bdh": "rgba(255,255,255,.22)", "txt": "#f4f4f8", "txt2": "#d4d5e2", "mut": "#9a9cb0",
              "acc": "#8b7bff", "acc2": "#06b6d4", "ok": "#4ade80", "warn": "#fbbf24", "bad": "#f87171", "r": "24px",
              "blur": "blur(24px) saturate(160%)", "font": '"Plus Jakarta Sans","Inter",sans-serif',
              "deco": "radial-gradient(40vw 40vw at 15% 10%,rgba(124,58,237,.42),transparent 60%),radial-gradient(38vw 38vw at 90% 20%,rgba(6,182,212,.30),transparent 60%),radial-gradient(44vw 44vw at 60% 100%,rgba(249,115,22,.22),transparent 60%)"},
    "mini": {"bg": "#0a0a0b", "panel": "#111113", "solid": "#111113", "inset": "#0c0c0e",
             "bd": "#222226", "bdh": "#3a3a42", "txt": "#ededef", "txt2": "#b4b4bc", "mut": "#8a8a93",
             "acc": "#6e8bff", "acc2": "#9aa7ff", "ok": "#3ecf8e", "warn": "#f5a524", "bad": "#f2555a", "r": "10px",
             "blur": "none", "font": '"Inter",system-ui,sans-serif', "deco": "none"},
    "frost": {"bg": "#0e141f", "panel": "rgba(255,255,255,.055)", "solid": "#151d2b", "inset": "rgba(0,0,0,.25)",
              "bd": "rgba(255,255,255,.085)", "bdh": "rgba(122,162,255,.45)", "txt": "#e7edf6", "txt2": "#c4cede", "mut": "#8b98ae",
              "acc": "#7aa2ff", "acc2": "#9c84e6", "ok": "#4ade80", "warn": "#fbbf24", "bad": "#f87171", "r": "16px",
              "blur": "blur(18px) saturate(130%)", "font": '"Manrope","Inter",sans-serif',
              "deco": "radial-gradient(40vw 34vw at 6% 0%,rgba(94,129,224,.22),transparent 60%),radial-gradient(36vw 30vw at 100% 8%,rgba(156,132,230,.16),transparent 60%),radial-gradient(40vw 30vw at 60% 105%,rgba(80,170,210,.13),transparent 60%)"},
    "pastel": {"bg": "#16131d", "panel": "#201b2b", "solid": "#201b2b", "inset": "#1a1624",
               "bd": "#2b2439", "bdh": "#4a3f66", "txt": "#f5f0fb", "txt2": "#d8d0e8", "mut": "#a59cba",
               "acc": "#b9a4ff", "acc2": "#7ee2b0", "ok": "#7ee2b0", "warn": "#ffd88a", "bad": "#ff9aae", "r": "22px",
               "blur": "none", "font": '"Nunito","Inter",sans-serif',
               "deco": "radial-gradient(38vw 30vw at 0% 0%,rgba(185,164,255,.10),transparent 60%),radial-gradient(34vw 28vw at 100% 100%,rgba(126,226,176,.07),transparent 60%)"},
}
THEME_FONTS = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@500;600;700;800&family=Manrope:wght@500;600;700;800&family=Nunito:wght@600;700;800;900&display=swap"


def theme_kit_css():
    out = [f'@import url("{THEME_FONTS}");', "/* generated by portal.py THEMES; html.th.t-<name> */"]
    for name, p in THEMES.items():
        out.append(f"html.t-{name}{{color-scheme:dark;" + ";".join(f"--k-{k}:{v}" for k, v in p.items()) + "}")
    out.append(".rt-pick{display:inline-flex;align-items:center;gap:4px;font-size:12px;opacity:.9}"
               ".rt-pick select{font:inherit;font-size:12px;color:inherit;background:transparent;border:1px solid currentColor;border-radius:8px;padding:4px 6px;cursor:pointer}"
               ".rt-pick option{color:#111;background:#fff}")
    return "\n".join(out) + "\n"


def theme_kit_js():
    order = list(THEME_NAMES)
    return ("(function(){var T=%s,N=%s,D=%s,K='rt-theme',R=document.documentElement;\n"
            "function get(){var t=null;try{t=new URLSearchParams(location.search).get('theme')||localStorage.getItem(K)}catch(e){}return T.indexOf(t)>=0?t:D}\n"
            "function apply(t){T.forEach(function(x){R.classList.remove('t-'+x)});R.classList.toggle('th',t!='glass');if(t!='glass')R.classList.add('t-'+t);R.setAttribute('data-rt-theme',t);\n"
            " document.querySelectorAll('select[data-rt-theme]').forEach(function(s){s.value=t});try{window.dispatchEvent(new CustomEvent('rt-theme',{detail:t}))}catch(e){}}\n"
            "function set(t){try{localStorage.setItem(K,t)}catch(e){}apply(t)}\n"
            "function fill(){document.querySelectorAll('select[data-rt-theme]').forEach(function(s){if(!s.options.length)s.innerHTML=T.map(function(k){return '<option value=\"'+k+'\">'+N[k]+'</option>'}).join('');s.value=R.getAttribute('data-rt-theme')||get();s.onchange=function(){set(s.value)}})}\n"
            "apply(get());if(document.readyState=='loading')document.addEventListener('DOMContentLoaded',fill);else fill();\n"
            "window.addEventListener('storage',function(e){if(e.key==K)apply(get())});\n"
            "window.RTTheme={get:get,set:set,fill:fill,list:T,names:N};})();\n") % (
        json.dumps(order), json.dumps(THEME_NAMES), json.dumps(THEME_DEFAULT))


def unit_states():
    """State of every unit named in the registry (timers: last/next trigger; services: active state)."""
    units = sorted({u for a in o.registry()["agents"] for u in a.get("units", [])})
    out = {}
    if not units:
        return out
    try:
        r = subprocess.run(["systemctl", "show", *units, "-p", "Id,ActiveState,SubState,NextElapseUSecRealtime,"
                            "LastTriggerUSec,ExecMainStartTimestamp,UnitFileState"],
                           capture_output=True, text=True, timeout=10, env={"TZ": "UTC", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.TimeoutExpired):
        return out
    for block in r.stdout.strip().split("\n\n"):
        d = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if not d.get("Id"):
            continue
        def ts(k):
            v = (d.get(k) or "").strip().lstrip("@")
            if v.isdigit():
                return o.iso(dt.datetime.fromtimestamp(int(v), dt.timezone.utc)) if int(v) > 0 else None
            try:
                return o.iso(dt.datetime.strptime(v, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=dt.timezone.utc))
            except ValueError:
                return None
        out[d["Id"]] = {"active": d.get("ActiveState"), "sub": d.get("SubState"),
                        "enabled": d.get("UnitFileState"),
                        "next": ts("NextElapseUSecRealtime"),
                        "last": ts("LastTriggerUSec"), "since": ts("ExecMainStartTimestamp")}
    return out


def q(sql, args=()):
    with contextlib.closing(o.db()) as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def _code_built(a):
    """Every .py named in the registry 'code' field exists (paths are relative to ~ on the agent LXC)."""
    files = [p.strip() for p in re.split(r"[+,]", a.get("code") or "") if p.strip().endswith(".py")]
    return bool(files) and all((Path.home() / p).exists() for p in files)


def live_error(lr, ack=0):
    """The agent's latest run failed in the last 24 h and that failure is not marked fixed (kv error_ack {id: run})."""
    return bool(lr and lr["status"] == "error" and lr["id"] > int(ack or 0)
                and o.parse_iso(lr["ended"] or lr["started"]) > o.utcnow() - dt.timedelta(hours=24))


def agent_state(a, u, last_status, last_t, busy):
    """Status light: running (green) / idle (yellow) / stopped (red) / issue (red with '!'), plus why."""
    failed = [n for n, x in u.items() if x.get("active") == "failed"]
    if failed:
        return "issue", "unit failed: " + ", ".join(failed)
    if last_status == "error" and last_t and o.parse_iso(last_t) > o.utcnow() - dt.timedelta(hours=24):
        return "issue", "last run ended with an error"
    if not _code_built(a):
        return "stopped", "not built yet"
    if busy:
        return "running", f"working now ({busy} run{'s' if busy > 1 else ''} in progress)"
    svc = [n for n, x in u.items() if n.endswith(".service") and x.get("active") == "active"
           and x.get("sub") == "running"]
    if svc:
        return "running", "service running: " + ", ".join(svc)
    if u:
        timers = [n for n, x in u.items() if n.endswith(".timer") and x.get("active") == "active"]
        if not timers:
            return "stopped", "no active timer or service (" + ", ".join(
                f"{n} {x.get('active') or 'missing'}" for n, x in u.items()) + ")"
        return "idle", "waiting for its next timer"
    return "idle", "on demand: waiting to be asked"


def agents_payload():
    reg = o.registry()
    busy = {r["agent"]: r["n"] for r in q("SELECT agent, COUNT(*) n FROM runs WHERE status='running' AND started>=? "
                                          "GROUP BY agent", (o.iso(o.utcnow() - dt.timedelta(hours=3)),))}
    since30 = o.iso(o.utcnow() - dt.timedelta(days=30))
    stats = {r["agent"]: r for r in q(
        """SELECT agent, COUNT(*) runs, SUM(input_tokens) inp, SUM(cached_tokens) cached, SUM(output_tokens) outp,
                  SUM(reasoning_tokens) reas, SUM(cost_usd) cost, SUM(status='error') errors
           FROM runs WHERE started>=? GROUP BY agent""", (since30,))}
    last = {r["agent"]: r for r in q(
        """SELECT r.* FROM runs r JOIN (SELECT agent, MAX(id) mid FROM runs GROUP BY agent) m ON r.id=m.mid""")}
    hbs = {r["k"][3:]: json.loads(r["v"]) for r in q("SELECT k, v FROM kv WHERE k LIKE 'hb:%'")}
    units = cached("units", 20, unit_states)
    acks = o.kv_get("error_ack") or {}
    out = []
    for a in reg["agents"]:
        aid = a["id"]
        lr = last.get(aid)
        hb = hbs.get(aid)
        err = live_error(lr, acks.get(aid))
        last_t, last_s, last_status = None, None, None
        if lr:
            last_t, last_s, last_status = lr["ended"] or lr["started"], lr["summary"], lr["status"]
            if lr["status"] == "error" and not err:
                last_status = "ok" if lr["id"] <= int(acks.get(aid) or 0) else "old-error"
        if hb and (not last_t or hb["t"] > last_t):
            last_t, last_s, last_status = hb["t"], hb.get("summary"), "ok"
        u = {n: units.get(n, {}) for n in a.get("units", [])}
        nexts = [x["next"] for x in u.values() if x.get("next")]
        lasts = [x["last"] for x in u.values() if x.get("last")]
        if lasts and (not last_t or max(lasts) > last_t):
            last_t = max(lasts)
        health = "idle"
        if any(x.get("active") == "failed" for x in u.values()):
            health = "failed"
        elif err:
            health = "error"
        elif any(x.get("active") == "active" and n.endswith(".service") and x.get("sub") == "running"
                 for n, x in u.items()):
            health = "running"
        elif last_t:
            health = "ok"
        s = stats.get(aid) or {}
        state, why = agent_state(a, u, "error" if err else None, lr and (lr["ended"] or lr["started"]),
                                 busy.get(aid, 0))
        out.append({**a, "last_run": last_t, "last_summary": last_s, "last_status": last_status,
                    "last_run_id": lr["id"] if lr else None, "next_run": min(nexts) if nexts else None,
                    "units_state": u, "health": health, "state": state, "state_why": why,
                    "stats30": {"runs": s.get("runs") or 0, "errors": s.get("errors") or 0,
                                "input": s.get("inp") or 0, "cached": s.get("cached") or 0,
                                "output": s.get("outp") or 0, "reasoning": s.get("reas") or 0,
                                "cost": round(s.get("cost") or 0, 4)}})
    return out


def summary_payload():
    spent = o.month_spend()
    cap = o.budget_limit()
    since = o.iso(o.utcnow() - dt.timedelta(days=30))
    daily = q("""SELECT substr(datetime(started, '+8 hours'), 1, 10) d, agent, SUM(cost_usd) c,
                        SUM(input_tokens + output_tokens) t, COUNT(*) n
                 FROM runs WHERE started>=? GROUP BY d, agent ORDER BY d""", (since,))
    tot = q("""SELECT COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) c
               FROM runs WHERE started>=?""", (o.month_start_utc(),))[0]
    import calendar
    dim = calendar.monthrange(o.local().year, o.local().month)[1]
    days_left = max(dim - (o.local() - o.local().replace(day=1, hour=0, minute=0, second=0)).total_seconds() / 86400, 0)
    # run rate: the last 7 days of scheduled/chat/voice work; one-off manual jobs (e.g. avatar batches) are left out
    week = q("SELECT COALESCE(SUM(cost_usd),0) c FROM runs WHERE started>=? AND trigger!='manual'",
             (o.iso(o.utcnow() - dt.timedelta(days=7)),))[0]["c"]
    bill = o.openai_bill()
    return {"team": o.registry().get("portal_name", "Hermes Roundtable"), "host": o.registry().get("host"),
            "now": o.iso(), "month": o.local().strftime("%B %Y"), "spent": round(spent, 4), "cap": cap,
            "ledger": round(o.ledger_spend(), 4), "bill": bill, "run_rate_day": round(week / 7, 2),
            "forecast": round(spent + week / 7 * days_left, 2), "month_runs": tot["n"] or 0,
            "month_tokens": (tot["i"] or 0) + (tot["o"] or 0), "daily": daily,
            "open_approvals": q("SELECT COUNT(*) n FROM approvals WHERE status IN ('pending','scheduled','running')")[0]["n"]}


def runs_payload(params):
    agent = params.get("agent", [""])[0]
    limit = min(int(params.get("limit", ["100"])[0] or 100), 500)
    status = params.get("status", [""])[0]
    sql = ("SELECT id, agent, task, trigger, started, ended, status, model, calls, input_tokens, cached_tokens, "
           "output_tokens, reasoning_tokens, web_calls, cost_usd, summary FROM runs WHERE 1=1")
    args = []
    if agent:
        sql += " AND agent=?"
        args.append(agent)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return q(sql, args)


def run_payload(rid):
    r = q("SELECT * FROM runs WHERE id=?", (rid,))
    if not r:
        return None
    r = r[0]
    r["detail"] = o.strip_secrets(r.get("detail") or "")
    r["summary"] = o.strip_secrets(r.get("summary") or "")
    return r


def approvals_payload():
    rows = q("""SELECT id, agent, title, severity, status, not_before, created, decided, finished, result
                FROM approvals ORDER BY id DESC LIMIT 50""")
    queued = {int(r["k"].split(":")[1]) for r in q("SELECT k FROM kv WHERE k LIKE 'portal_decision:%'")}
    for r in rows:
        r["result"] = o.strip_secrets(r.get("result") or "")[:1500]
        r["queued"] = r["id"] in queued
    return rows


# Owner decisions from the portal's Approvals page. The portal only queues them (kv portal_decision:<id>); Gideon
# (boss.py) applies them on his next approval poll through the same decide() path as a Discord ✅/❌ and posts
# who decided in the approval's Discord thread. Only named Authentik users may decide (not the desktop device token).
DECISIONS = {"approve": "pending", "reject": "pending", "run_now": "scheduled"}


def approvers():
    return {u.strip() for u in (os.environ.get("PORTAL_APPROVERS") or "akadmin").split(",") if u.strip()}


def queue_decision(aid, decision, who):
    if type(aid) is not int or decision not in DECISIONS:
        return 400, {"error": "Choose an approval id and approve, reject or run_now"}
    ap = o.get_approval(aid)
    if not ap:
        return 404, {"error": f"No approval #{aid}"}
    if ap["status"] != DECISIONS[decision]:
        return 409, {"error": f"Approval #{aid} is {ap['status']}, so it cannot be {decision.replace('_', ' ')}d now"}
    o.kv_set(f"portal_decision:{aid}", {"decision": decision, "by": who, "at": o.iso()})
    o.log("portal", f"approval #{aid} {decision} queued by {who}")
    return 202, {"ok": True, "queued": decision, "note": "Gideon applies it within about 15 seconds"}


def profile_payload(aid):
    agent = next((a for a in agents_payload() if a["id"] == aid), None)
    if not agent:
        return None
    since = o.iso(o.utcnow() - dt.timedelta(days=30))
    by_model = q("""SELECT COALESCE(NULLIF(model,''),'no LLM') model, COUNT(*) runs, SUM(calls) calls,
                           SUM(input_tokens) input, SUM(cached_tokens) cached, SUM(output_tokens) output,
                           SUM(reasoning_tokens) reasoning, SUM(web_calls) web, SUM(cost_usd) cost
                    FROM runs WHERE agent=? AND started>=? GROUP BY 1 ORDER BY cost DESC""", (aid, since))
    by_trigger = q("""SELECT COALESCE(trigger,'?') trigger, COUNT(*) runs, SUM(cost_usd) cost
                      FROM runs WHERE agent=? AND started>=? GROUP BY 1 ORDER BY runs DESC""", (aid, since))
    daily = q("""SELECT substr(datetime(started,'+8 hours'),1,10) d, COUNT(*) runs, SUM(status='error') errors,
                        SUM(cost_usd) cost, SUM(input_tokens+output_tokens) tokens
                 FROM runs WHERE agent=? AND started>=? GROUP BY d ORDER BY d""", (aid, since))
    k = q("""SELECT COUNT(*) n, SUM(status='ok') ok, SUM(status='error') err,
                    AVG(CASE WHEN ended IS NOT NULL THEN (julianday(ended)-julianday(started))*86400 END) dur,
                    MAX(cost_usd) max_cost, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cached_tokens) c,
                    SUM(reasoning_tokens) r, SUM(cost_usd) cost
             FROM runs WHERE agent=?""", (aid,))[0]
    recent = q("""SELECT id, task, trigger, started, ended, status, model, input_tokens, cached_tokens,
                         output_tokens, web_calls, cost_usd, summary FROM runs WHERE agent=? ORDER BY id DESC LIMIT 40""",
               (aid,))
    aps = q("""SELECT id, title, severity, status, created, finished FROM approvals WHERE agent=?
               ORDER BY id DESC LIMIT 20""", (aid,))
    g = graph_payload()
    names = {n["id"]: n for n in g["nodes"]}
    conns = [{**e, "dir": "out" if e["from"] == aid else "in",
              "other": names.get(e["to"] if e["from"] == aid else e["from"], {})}
             for e in g["edges"] if aid in (e["from"], e["to"])]
    return {"agent": agent, "by_model": by_model, "by_trigger": by_trigger, "daily": daily,
            "lifetime": {"runs": k["n"] or 0, "ok": k["ok"] or 0, "errors": k["err"] or 0,
                         "avg_seconds": round(k["dur"] or 0, 1), "max_cost": k["max_cost"] or 0,
                         "input": k["i"] or 0, "output": k["o"] or 0, "cached": k["c"] or 0,
                         "reasoning": k["r"] or 0, "cost": k["cost"] or 0},
            "recent": recent, "approvals": aps, "connections": conns}


def hierarchy_payload():
    """Role metadata only; an optional pending snapshot supports UI-only rollout."""
    reg = o.registry()
    snapshot = o.CODE_DIR / "hierarchy.json"
    if snapshot.is_file():
        data = json.loads(snapshot.read_text())
        if data.get("status") != "pending":
            raise ValueError("A hierarchy snapshot must be explicitly marked pending")
    else:
        data = {"status": "configured", "agents": reg["agents"]}
    if not isinstance(data.get("agents"), list) or not data["agents"]:
        raise ValueError("Hierarchy roster must be a nonempty list")
    fields = ("id", "name", "role", "emoji", "avatar", "avatar_demo", "color", "reports_to", "is_lead")
    return {"status": data["status"],
            "agents": [{k: a[k] for k in fields if k in a} for a in data["agents"]
                       if a.get("participates_in_hierarchy") is not False],
            "active_ids": [a["id"] for a in reg["agents"]]}


def graph_payload():
    g = json.loads((o.CODE_DIR / "graph.json").read_text())
    reg = {a["id"]: a for a in o.registry()["agents"]}
    retired = set(o.registry().get("retired_agents", []))
    g["nodes"] = [n for n in g["nodes"] if n["id"] not in retired]
    g["edges"] = [e for e in g["edges"] if e["from"] not in retired and e["to"] not in retired and e["kind"] != "report"]
    g["edges"] = [e for e in g["edges"] if not (
        e["kind"] == "command" and e["from"] in reg and
        reg.get(e["to"], {}).get("homelab_visibility") is False)]
    known = {n["id"] for n in g["nodes"]}
    for i, a in enumerate(reg.values()):
        if a["id"] not in known:
            g["nodes"].append({"id": a["id"], "type": "agent", "x": 900, "y": 950 + i * 60})
        parent = a.get("reports_to")
        if parent in reg and a.get("participates_in_hierarchy") is not False:
            g["edges"].append({"from": a["id"], "to": parent, "kind": "report", "label": "Reports to " + reg[parent]["name"]})
    since = o.iso(o.utcnow() - dt.timedelta(days=30))
    runs = {(r["agent"], r["trigger"]): r["n"] for r in q(
        "SELECT agent, trigger, COUNT(*) n FROM runs WHERE started>=? GROUP BY agent, trigger", (since,))}
    aps = {r["agent"]: r["n"] for r in q(
        "SELECT agent, COUNT(*) n FROM approvals WHERE created>=? GROUP BY agent", (since,))}
    health = {a["id"]: a for a in agents_payload()}
    for n in g["nodes"]:
        if n["type"] == "agent" and n["id"] in reg:
            a = reg[n["id"]]
            n.update(label=a["name"], emoji=a["emoji"], sub=a["role"], color=a.get("color"), avatar=a.get("avatar"),
                     avatar_demo=a.get("avatar_demo"),
                     health=health.get(n["id"], {}).get("health"), last_run=health.get(n["id"], {}).get("last_run"))
    for e in g["edges"]:
        m = e.get("metric") or {}
        if "runs" in m:
            f = m["runs"]
            e["count"] = sum(v for (ag, tr), v in runs.items()
                             if ag == f["agent"] and (not f.get("trigger") or tr == f["trigger"]))
        elif "approvals" in m:
            e["count"] = aps.get(m["approvals"]["agent"], 0)
    return g


def desk_events():
    """Pop-up feed for the Roundtable Companion, where Gideon announces what needs Hermes: open critical/high
    incidents (secret-stripped titles) and approvals waiting for his ✅. Read-only, from the ledger."""
    import hierarchy
    ev = []
    for u in hierarchy.urgent_items():
        k = hashlib.sha1(f"{u['owner']}|{u['title']}|{u.get('first_seen')}".encode()).hexdigest()[:12]
        ev.append({"id": f"urgent:{k}", "kind": "urgent", "agent": u["owner"], "severity": u["severity"],
                   "title": u["title"][:200], "at": u.get("first_seen")})
    for r in q("SELECT id,agent,title,severity,created FROM approvals WHERE status='pending' ORDER BY id DESC LIMIT 10"):
        ev.append({"id": f"approval:{r['id']}", "kind": "approval", "agent": r["agent"], "severity": r["severity"],
                   "title": f"Approval #{r['id']}: {r['title'] or ''}"[:200], "at": r["created"]})
    return ev


# Office view (/office): a Stardew-style office where each homelab agent is at their laptop while a run is in
# progress and lounges otherwise. Mood comes from the task: first keyword match wins, else "focused".
OFFICE_MOODS = (
    (("incident", "alert", "fail", "error", "outage", "down", "urgent", "rollback", "restore"), "stressed"),
    (("security", "vuln", "cve", "threat", "access", "iam", "compliance", "audit", "firewall"), "serious"),
    (("patch", "build", "deploy", "forge", "terraform", "ansible", "upgrade", "destroy", "install"), "determined"),
    (("cost", "budget", "spend", "finops", "token", "invoice"), "worried"),
    (("scout", "research", "idea", "explore", "film", "movie", "media", "taste"), "curious"),
    (("doc", "write", "blog", "report", "digest", "summary", "sync", "brief", "changelog"), "content"),
)
OFFICE_MEETING_MAX = 2 * 3600  # a forgotten board meeting adjourns itself


def office_mood(working, task="", minutes=0, error=False, waiting=False, finished_recently=False):
    """(mood, why) for one agent; pure so it can be tested without the ledger."""
    t = (task or "").lower()
    if working:
        if minutes >= 45:
            return "tired", f"working for {int(minutes)} min"
        for words, mood in OFFICE_MOODS:
            if any(w in t for w in words):
                return mood, "working on: " + (task or "a task")[:80]
        return "focused", "working on: " + (task or "a task")[:80]
    if error:
        return "sad", "last run ended with an error"
    if waiting:
        return "anxious", "waiting for your approval"
    if finished_recently:
        return "happy", "just finished a task"
    return "relaxed", "no task right now"


def office_meeting():
    m = o.kv_get("office:meeting") or {}
    if m.get("on") and time.time() - float(m.get("t") or 0) > OFFICE_MEETING_MAX:
        m = {"on": False}
        o.kv_set("office:meeting", m)
    return {"on": bool(m.get("on")), "since": m.get("since"), "by": m.get("by")}


def office_payload():
    reg = o.registry()["agents"]
    now = o.utcnow()
    running = {}
    for r in q("SELECT agent, task, started FROM runs WHERE status='running' AND started>=? ORDER BY started",
               (o.iso(now - dt.timedelta(hours=3)),)):
        running.setdefault(r["agent"], r)
    acks = o.kv_get("error_ack") or {}
    errors = {r["agent"] for r in q("""SELECT r.agent, r.id FROM runs r JOIN (SELECT agent, MAX(id) mid FROM runs
                                        WHERE status!='running' GROUP BY agent) m ON r.id=m.mid
                                        WHERE r.status='error' AND r.started>=?""",
                                     (o.iso(now - dt.timedelta(hours=24)),))
              if r["id"] > int(acks.get(r["agent"]) or 0)}
    waiting = {r["agent"] for r in q("SELECT DISTINCT agent FROM approvals WHERE status='pending'")}
    recent = {r["agent"] for r in q("SELECT DISTINCT agent FROM runs WHERE status='ok' AND ended>=?",
                                    (o.iso(now - dt.timedelta(minutes=20)),))}
    out = []
    for a in reg:
        if a.get("group") in ("personal", "specialized"):
            continue
        r = running.get(a["id"])
        task = o.strip_secrets(r["task"] or "")[:140] if r else ""
        mins = (now - o.parse_iso(r["started"])).total_seconds() / 60 if r and r["started"] else 0
        mood, why = office_mood(bool(r), task, mins, a["id"] in errors, a["id"] in waiting, a["id"] in recent)
        p = a.get("personality") or {}
        out.append({"id": a["id"], "name": a["name"], "role": (a.get("role") or "").split("·")[0].strip(),
                    "emoji": a.get("emoji"), "color": a.get("color"), "is_lead": bool(a.get("is_lead")),
                    "reports_to": a.get("reports_to"), "temperament": p.get("temperament"),
                    "quirks": p.get("quirks"), "working": bool(r), "task": task, "since": r["started"] if r else None,
                    "mood": mood, "why": why})
    return {"now": o.iso(), "agents": out, "meeting": office_meeting()}


# Browser call -> desktop companion mirror: while a portal call runs in a minimized browser, the Roundtable Companion
# shows that agent's full-body figure (state + speech bubble) and relays its buttons back. Kept in memory only.
MIRROR = {"state": {}, "t": 0.0, "desk": 0.0, "cmds": []}
MIRROR_LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    server_version = "RoundtablePortal/1.0"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json"
                                                     else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, PAGE, "text/html")
            if u.path == "/healthz":
                return self.send(200, {"ok": True})
            if u.path == "/office":
                return self.send(200, (o.CODE_DIR / "office.html").read_text(encoding="utf-8"), "text/html")
            if u.path == "/atlas":   # Hermes Lab Atlas: Miriel's live build (atlas.py), else the deployed snapshot
                live = o.DATA_DIR / "atlas.html"
                return self.send(200, (live if live.exists() else o.CODE_DIR / "atlas.html").read_text(encoding="utf-8"),
                                 "text/html")
            if u.path == "/api/atlas/status":
                import atlas
                return self.send(200, atlas.status())
            if u.path == "/theme-kit.css":
                return self.send(200, theme_kit_css(), "text/css")
            if u.path == "/theme-kit.js":
                return self.send(200, theme_kit_js(), "application/javascript")
            if u.path == "/api/links":
                return self.send(200, cached("links", 120, links_payload))
            if u.path == "/api/dnschecks":
                return self.send(200, cached("dnschecks", 30, dns_checks_payload))
            if u.path == "/depmap":   # dependency map drawn by the Atlas engine
                rep = (urllib.parse.parse_qs(u.query).get("reporting") or [""])[0] == "1"
                return self.send(200, cached("depmap" + ("r" if rep else ""), 60, lambda: depmap_html(rep)), "text/html")
            if u.path == "/api/office":
                return self.send(200, cached("office", 5, office_payload))
            if u.path == "/api/agents":
                return self.send(200, agents_payload())
            if u.path == "/api/hierarchy":
                return self.send(200, hierarchy_payload())
            if u.path == "/api/summary":
                return self.send(200, summary_payload())
            if u.path == "/api/runs":
                return self.send(200, runs_payload(p))
            if u.path.startswith("/api/runs/") and u.path[10:].isdigit():
                r = run_payload(int(u.path[10:]))
                return self.send(200 if r else 404, r or {"error": "not found"})
            if u.path == "/api/approvals":
                return self.send(200, approvals_payload())
            if u.path == "/api/graph":
                return self.send(200, graph_payload())
            if u.path.startswith("/api/agents/"):
                r = profile_payload(u.path[12:])
                return self.send(200 if r else 404, r or {"error": "not found"})
            if u.path.startswith("/avatars/"):
                return self.avatar(u.path[9:])
            if u.path.startswith("/vendor/faceapi/"):
                return self.vendor(u.path[16:])
            if u.path.startswith("/vendor/three/"):
                return self.vendor(u.path[14:], "three")
            if u.path == "/api/mirror":
                if not self.authed():
                    return self.send(403, {"error": "forbidden"})
                with MIRROR_LOCK:
                    MIRROR["desk"] = time.time()
                    return self.send(200, {"state": MIRROR["state"], "age": time.time() - MIRROR["t"]})
            if u.path == "/api/desk/events":
                if not self.authed():
                    return self.send(403, {"error": "forbidden"})
                return self.send(200, cached("desk_events", 20, desk_events))
            if u.path == "/api/ipmap":   # Diallos's IP map: every assigned address and what uses it
                if not self.authed():
                    return self.send(403, {"error": "forbidden"})
                import ipmap
                return self.send(200, ipmap.snapshot(refresh=(p.get("refresh") or [""])[0] == "1"))
            if u.path == "/api/voice/info":
                import voice
                ok, why = voice.allowed()
                return self.send(200, {"model": voice.MODEL, "spend": voice.spend(), "cap": voice.cap(), "ok": ok,
                                       "why": why, "authed": self.authed(), "max_seconds": voice.MAX_SECONDS,
                                       "idle_seconds": voice.IDLE_SECONDS})
            if u.path == "/api/voice/local/info":
                if not self.authed():
                    return self.send(403, {"error": "forbidden"})
                import voice
                return self.send(200, voice.local_config())
            if u.path == "/api/voice/engine":
                # one engine choice for every device: the desktop app (Electron) has its own browser storage
                if not self.authed():
                    return self.send(403, {"error": "forbidden"})
                return self.send(200, {"engine": o.kv_get("voice_engine") or "local"})
            return self.send(404, {"error": "not found"})
        except (ValueError, PermissionError) as e:
            o.log("portal", f"error {u.path}: {e}")
            return self.send(403 if isinstance(e, PermissionError) else 400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - never crash the server on one bad request
            o.log("portal", f"error {u.path}: {e}")
            return self.send(500, {"error": str(e)})

    def avatar(self, name):
        if not re.fullmatch(r"[a-z0-9_-]{1,32}\.(jpg|png|svg|webm)", name):
            return self.send(404, {"error": "not found"})
        f = o.CODE_DIR / "avatars" / name
        if not f.is_file():
            return self.send(404, {"error": "not found"})
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", {".jpg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml",
                                          ".webm": "video/webm"}[f.suffix])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def vendor(self, name, pkg="faceapi"):
        """Self-hosted browser libraries: face-api (expression detection runs in the browser; no video leaves
        Hermes's PC) and three.js 0.160.0 for the 3D office."""
        if pkg not in ("faceapi", "three") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}\.(js|json|bin)", name) or ".." in name:
            return self.send(404, {"error": "not found"})
        f = o.CODE_DIR / "vendor" / pkg / name
        if not f.is_file():
            return self.send(404, {"error": "not found"})
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", {"js": "text/javascript", "json": "application/json"}.get(
            name.rsplit(".", 1)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        """Voice spends money and starts agent work: only requests that came through Traefik + Authentik, or the
        Roundtable Companion desktop app on the LAN with its device token (Google blocks SSO inside desktop apps)."""
        if self.client_address[0] in TRUSTED_PROXIES and bool(self.headers.get("X-authentik-username")):
            return True
        tok, want = self.headers.get("X-Companion-Token") or "", os.environ.get("COMPANION_TOKEN") or ""
        return len(want) >= 32 and hmac.compare_digest(tok.encode(), want.encode())

    def who(self):
        return self.headers.get("X-authentik-username") or "hermes-desktop"

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if not self.authed():
                return self.send(403, {"error": "voice is only available through https://roundtable.hrmsmrflrii.xyz"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 400_000:
                return self.send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/atlas/refresh":   # the Atlas page's redraw button
                origin = self.headers.get("Origin")
                if origin and origin.rstrip("/") != o.PORTAL_URL.rstrip("/"):
                    return self.send(403, {"error": "Atlas redraws must come from the protected portal"})
                import atlas
                atlas.spawn(f"portal button ({self.who()})")
                return self.send(200, {"started": True})
            if u.path == "/api/mirror":
                st = {k: body.get(k) for k in ("agent", "oncall", "hidden", "st", "me", "muted")}
                st["text"] = str(body.get("text") or "")[-400:]
                with MIRROR_LOCK:
                    MIRROR.update(state=st, t=time.time())
                    cmds, MIRROR["cmds"] = MIRROR["cmds"], []
                    return self.send(200, {"cmds": cmds, "desk": time.time() - MIRROR["desk"] < 6})
            if u.path == "/api/office/meeting":
                on = body.get("on") is True
                o.kv_set("office:meeting", {"on": on, "t": time.time(), "since": o.iso() if on else None,
                                            "by": str(body.get("by") or self.who())[:40]})
                with _lock:
                    _cache.pop("office", None)
                return self.send(200, office_meeting())
            if u.path == "/api/mirror/cmd":
                if body.get("cmd") in ("cut", "mute", "end"):
                    with MIRROR_LOCK:
                        MIRROR["cmds"] = (MIRROR["cmds"] + [body["cmd"]])[-5:]
                return self.send(200, {"ok": True})
            if u.path == "/api/voice/engine":
                if body.get("engine") not in ("openai", "local"):
                    return self.send(400, {"error": "engine must be openai or local"})
                o.kv_set("voice_engine", body["engine"])
                return self.send(200, {"engine": body["engine"]})
            if u.path == "/api/approvals/decide":
                sso = self.headers.get("X-authentik-username") if self.client_address[0] in TRUSTED_PROXIES else None
                if not sso or sso not in approvers():
                    return self.send(403, {"error": "Only the owner's signed-in portal (SSO) session can decide approvals"})
                if self.headers.get_content_type() != "application/json":
                    return self.send(415, {"error": "Approval decisions must be JSON"})
                origin = self.headers.get("Origin")
                if origin and origin.rstrip("/") != o.PORTAL_URL.rstrip("/"):
                    return self.send(403, {"error": "Approval decisions must come from the protected portal"})
                return self.send(*queue_decision(body.get("id"), body.get("decision"), sso))
            import voice
            who = self.who()
            if u.path == "/api/voice/session":
                try:
                    return self.send(200, voice.mint(body.get("agent", "boss"), (body.get("context") or "")[:2000],
                                                     who))
                except ValueError as e:
                    return self.send(409, {"error": str(e)})
            if u.path == "/api/voice/local/session":
                try:
                    return self.send(200, voice.mint_local(body.get("agent", "boss"), (body.get("context") or "")[:2000],
                                                           who))
                except ValueError as e:
                    return self.send(409, {"error": str(e)})
            if u.path == "/api/voice/local/turn":
                try:
                    return self.send(200, voice.local_turn(body.get("run_id"), body.get("agent"), body.get("messages"),
                                                           who=who, greet=bool(body.get("greet"))))
                except ValueError as e:
                    return self.send(409, {"error": str(e)})
            if u.path == "/api/voice/usage":
                c = voice.add_usage(body.get("run_id"), body.get("usage"), body.get("kind", "response"))
                return self.send(200, {"cost": c, "spend": voice.spend(), "cap": voice.cap()})
            if u.path == "/api/voice/tool":
                return self.send(200, voice.tool(body.get("run_id"), body.get("agent"), body.get("name"),
                                                 body.get("arguments"), who=who))
            if u.path == "/api/voice/end":
                threading.Thread(target=voice.finish, args=(body.get("run_id"), body.get("transcript"),
                                                            body.get("seconds", 0), "portal"), daemon=True).start()
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except (ValueError, PermissionError) as e:
            o.log("portal", f"error POST {u.path}: {e}")
            return self.send(403 if isinstance(e, PermissionError) else 400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            o.log("portal", f"error POST {u.path}: {e}")
            return self.send(500, {"error": str(e)})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="stylesheet" href="/theme-kit.css"><script src="/theme-kit.js"></script><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#07090f">
<title>Hermes Roundtable · Agent Portal</title>
<style>
:root{--bg:#0f1117;--card:#171a23;--line:#262a36;--txt:#e6e8ee;--mut:#8a90a2;--ok:#2ecc71;--warn:#f1c40f;--bad:#e74c3c;--run:#3498db;--acc:#ffd84d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between;padding:18px 24px;border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(15,17,23,.95);backdrop-filter:blur(6px);z-index:5}
h1{margin:0;font-size:20px}h1 small{color:var(--mut);font-weight:400;font-size:13px;margin-left:8px}
.kpis{display:flex;gap:12px;flex-wrap:wrap}.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;min-width:120px}
.kpi b{display:block;font-size:17px}.kpi span{color:var(--mut);font-size:12px}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:5px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ok)}
main{padding:20px 24px;max-width:1500px;margin:auto}
nav{display:flex;gap:6px;margin-bottom:16px}nav button{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 14px;cursor:pointer}
nav button.on{border-color:var(--acc);color:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;border-top:3px solid var(--c,#888);display:flex;flex-direction:column;gap:8px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}.name{font-size:17px;font-weight:600}.role{color:var(--mut);font-size:12.5px}
.pill{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid;white-space:nowrap}
.ok{color:var(--ok)}.error,.failed{color:var(--bad)}.running{color:var(--run)}.idle{color:var(--mut)}.skipped{color:var(--warn)}
.char{font-style:italic;color:#c9cdd8;font-size:13px}.does{font-size:13px;color:#b8bdca}
.chips{display:flex;flex-wrap:wrap;gap:5px}.chip{background:#20242f;border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-size:11.5px}
dl{display:grid;grid-template-columns:96px 1fr;gap:3px 10px;margin:4px 0 0;font-size:12.5px}dt{color:var(--mut)}dd{margin:0}
.last{background:#12151c;border:1px solid var(--line);border-radius:8px;padding:8px;font-size:12.5px;color:#c2c7d3;max-height:90px;overflow:auto;white-space:pre-wrap}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:12.5px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mut);font-weight:500;background:#141720;position:sticky;top:0}
tr.click{cursor:pointer}tr.click:hover{background:#1d212c}td.num{text-align:right;font-variant-numeric:tabular-nums}
.filters{display:flex;gap:8px;margin-bottom:10px}select,input{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px}
.dihead{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;justify-content:space-between;margin-bottom:14px}.dihead h2{margin:0 0 4px}.dihead p{margin:0}
.dinav{display:flex;gap:6px;align-items:center}.dinav select{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 10px}.dinav .back{margin:0}
.dioverall{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:14px;margin-bottom:16px}.dioverall .chart{margin:0}
.dikpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}.dikpi{border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.dikpi b{display:block;font-size:22px;line-height:1.2}.dikpi span{font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
.disum{line-height:1.55;margin:0 0 10px}.dilists{display:grid;grid-template-columns:1fr 1fr;gap:10px}.dilists h4{margin:4px 0 6px;font-size:12.5px}.dilists ul{margin:0;padding-left:18px;font-size:13px;line-height:1.5}
.dimeter{display:grid;grid-template-columns:72px 1fr 28px;gap:8px;align-items:center;font-size:12px}.dimeter .bar{margin:0}.dimeter b{text-align:right}
.dicard .dtop{display:flex;gap:10px;align-items:center}.dicard img{width:42px;height:42px;border-radius:50%;object-fit:cover;flex:none}.dicard .em{font-size:28px;width:42px;text-align:center}
.dicard h3{margin:0;font-size:15px}.dicard .dsub{font-size:12px;color:var(--mut)}.dichip{display:inline-block;border-radius:99px;padding:2px 9px;font-size:11.5px;border:1px solid currentColor}
.dimoods{display:flex;flex-wrap:wrap;gap:5px}.dimoods span{font-size:11px;background:rgba(255,255,255,.06);border-radius:99px;padding:2px 8px;color:var(--mut)}
.dicard p{margin:0;font-size:13px;line-height:1.5}.diwait{display:flex;align-items:center;gap:10px}.diwait i{width:14px;height:14px;border:2px solid var(--line2,#444);border-top-color:var(--acc);border-radius:50%;animation:dispin 1s linear infinite}
@keyframes dispin{to{transform:rotate(360deg)}}.ditrend svg{width:100%;height:120px}.dientry{border-left:3px solid var(--line);padding:2px 0 2px 14px;margin:0 0 18px}.dientry.r{border-left-color:var(--acc)}
.dientry h4{margin:0 0 6px}.dientry .t{color:var(--mut);font-weight:400;margin-right:6px}.dientry p{margin:0 0 8px;line-height:1.6}.dientry .m{font-size:12px;color:var(--mut);font-style:italic}
.iptab th.ipsorth{cursor:pointer;user-select:none;white-space:nowrap}.iptab th.ipsorth.on{color:var(--txt)}#ipsort{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font:inherit;font-size:12.5px}
.ipf{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}.ipf button{background:var(--card);color:var(--mut);border:1px solid var(--line);border-radius:99px;padding:5px 11px;font-size:12px;cursor:pointer}.ipf button.on{color:var(--txt);border-color:var(--acc)}
#ipq{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 10px;min-width:0;width:220px}
.ipuse{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin-bottom:12px}.ipuse button{text-align:left;cursor:pointer;color:var(--txt);background:var(--card);font:inherit;border:1px solid var(--line);border-radius:12px;padding:10px 12px}.ipuse button.on{border-color:var(--acc)}.ipuse .ub{height:6px;border-radius:4px;background:var(--line);overflow:hidden;margin:7px 0 6px;display:flex}.ipuse .ub i{display:block;height:100%}.ipuse .un{font-size:11.5px;line-height:1.5}
.ipkpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:12px}.ipkpis button{text-align:left;cursor:pointer;color:var(--txt);background:var(--card);font:inherit}.ipkpis button.on{border-color:var(--acc)}
.iptab{width:100%;border-collapse:collapse;font-size:13px}.iptab th{text-align:left;font-size:11.5px;color:var(--mut);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line)}
.iptab td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}.iptab td:first-child{font-family:ui-monospace,Consolas,monospace;white-space:nowrap}
.iptab a{color:var(--acc)}.ipsrc{display:flex;flex-wrap:wrap;gap:4px}.ipsrc span{font-size:10.5px;background:rgba(255,255,255,.06);border-radius:99px;padding:1px 7px;color:var(--mut)}
.ipst{display:inline-block;border-radius:99px;padding:2px 9px;font-size:11.5px;border:1px solid currentColor;white-space:nowrap}.ipai{font-size:11px;color:var(--mut)}.ipnote{font-size:11.5px;color:var(--mut)}
@media(max-width:900px){.ipkpis{grid-template-columns:repeat(2,minmax(0,1fr))}#ipq{width:100%}.iptab thead{display:none}.iptab tr{display:block;border:1px solid var(--line);border-radius:12px;margin-bottom:10px;padding:6px 4px;background:var(--card)}
 .iptab td{display:block;border:0;padding:4px 10px}.iptab td[data-l]:before{content:attr(data-l)': ';color:var(--mut);font-size:11.5px}}
@media(max-width:900px){.dioverall{grid-template-columns:1fr}.dikpis{grid-template-columns:1fr 1fr 1fr}.dilists{grid-template-columns:1fr}.dihead{align-items:stretch}.dinav{flex-wrap:wrap}}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:9}
#modal .box{background:var(--card);border:1px solid var(--line);border-radius:12px;max-width:900px;width:92%;max-height:86vh;overflow:auto;padding:18px}
pre{white-space:pre-wrap;word-break:break-word;background:#0c0e13;padding:10px;border-radius:8px;font-size:12px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.legend{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:var(--mut);margin-top:6px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
.mut{color:var(--mut)}footer{color:var(--mut);font-size:12px;text-align:center;padding:20px}
.card{cursor:pointer;transition:border-color .15s,transform .15s}.card:hover{border-color:var(--c);transform:translateY(-2px)}
.mapwrap{display:flex;gap:14px;align-items:flex-start}.side{width:320px;flex:none;font-size:13px;max-height:80vh;overflow:auto}
.side .conn{border-left:3px solid var(--k);padding:4px 8px;margin:6px 0;background:#12151c;border-radius:4px}
#map svg{width:100%;height:auto;display:block}
.node rect{rx:9;stroke-width:1.5}.node{cursor:pointer}.node text{fill:var(--txt);font-size:12px;pointer-events:none}.node .s{fill:var(--mut);font-size:10px}
.edge{fill:none;transition:opacity .2s}.edge.live{stroke-dasharray:6 5;animation:flow 1.2s linear infinite}
@keyframes flow{to{stroke-dashoffset:-22}}.dim{opacity:.08}.badge{font-size:10px;fill:#0f1117;font-weight:700}
.phead{display:flex;gap:18px;align-items:center;background:var(--card);border:1px solid var(--line);border-top:4px solid var(--c);border-radius:12px;padding:18px;margin-bottom:14px}
.phead .big{font-size:52px}.phead h2{margin:0}.pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}@media(max-width:900px){.two{grid-template-columns:1fr}.mapwrap{flex-direction:column}.side{width:100%}}
.back{background:none;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:6px 12px;cursor:pointer;margin-bottom:12px}
h3.sec{margin:0 0 8px;font-size:14px;color:var(--mut);font-weight:500;text-transform:uppercase;letter-spacing:.05em}
.av{width:44px;height:44px;border-radius:50%;object-fit:cover;border:2px solid var(--c,#888);flex:none;background:#000}
.avw{position:relative;display:inline-block;flex:none;line-height:0}.sd{position:absolute;right:-1px;bottom:-1px;width:15px;height:15px;border-radius:50%;border:2px solid var(--card);box-sizing:border-box;display:flex;align-items:center;justify-content:center;font:900 10px/1 system-ui,sans-serif;color:#fff}
.sd.big{width:28px;height:28px;font-size:17px;right:4px;bottom:4px;border-width:3px}.sd.s-running{background:var(--ok);animation:sdp 1.8s infinite}.sd.s-idle{background:var(--warn)}.sd.s-stopped,.sd.s-issue{background:var(--bad)}
@keyframes sdp{0%{box-shadow:0 0 0 0 rgba(46,204,113,.7)}70%{box-shadow:0 0 0 7px rgba(46,204,113,0)}100%{box-shadow:0 0 0 0 rgba(46,204,113,0)}}
.pill.s-running{color:var(--ok)}.pill.s-idle{color:var(--warn)}.pill.s-stopped,.pill.s-issue{color:var(--bad)}
.fbox{border:1px solid var(--line);border-radius:14px;padding:14px 14px 16px;margin:0 0 18px}.fbox h3{margin:0 0 12px;font-size:15px}
.slegend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--mut);margin:-4px 0 12px}.slegend span{display:flex;align-items:center;gap:6px}.slegend .sd{position:static;border-color:var(--bg)}
.who{display:flex;gap:10px;align-items:center}.phead .avbig{width:120px;height:120px;border-radius:50%;object-fit:cover;border:3px solid var(--c);box-shadow:0 0 24px color-mix(in srgb,var(--c) 40%,transparent)}
.vroom{display:grid;grid-template-columns:minmax(320px,440px) 1fr;gap:14px}@media(max-width:900px){.vroom{grid-template-columns:1fr}}
.vstage{text-align:center;display:flex;flex-direction:column;align-items:center;gap:10px}
.vstage img{width:190px;height:190px;border-radius:50%;object-fit:cover;border:4px solid var(--c);transition:box-shadow .2s,transform .2s}
.vstage img.talking{box-shadow:0 0 0 6px color-mix(in srgb,var(--c) 45%,transparent),0 0 42px var(--c);transform:scale(1.03)}
.vstage img.listening{box-shadow:0 0 0 5px rgba(46,204,113,.45)}
.vbtns{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}.vbtns button{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:9px 16px;cursor:pointer;font-size:14px}
.vbtns button.go{background:#1f6f43;border-color:#2ecc71;font-weight:600}.vbtns button.stop{background:#6f1f1f;border-color:#e74c3c}.vbtns button:disabled{opacity:.4;cursor:default}
.vmeter{width:220px;height:6px;background:var(--line);border-radius:3px;overflow:hidden}.vmeter i{display:block;height:100%;width:0;background:var(--ok);transition:width .08s}
.vpick{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.vpick button{background:#12151c;color:var(--txt);font:inherit;border:1px solid var(--line);border-radius:10px;padding:10px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center;font-size:12px}
.vgrp{display:flex;gap:6px;margin:0 0 10px}.vgrp button{background:#12151c;color:#ddd;border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px}
.vgrp button.on{border-color:#c9a227;color:#fff;background:#1c2030}
.vpick button.on{border-color:var(--c);box-shadow:0 0 0 1px var(--c)}.vpick img{width:64px;height:64px;border-radius:50%;object-fit:cover;border:2px solid var(--c)}
.vlog{max-height:46vh;overflow:auto;display:flex;flex-direction:column;gap:6px;font-size:13.5px}.vlog p{margin:0;padding:6px 10px;border-radius:8px;background:#12151c}
.vlog p.me{background:#17261d;align-self:flex-end;max-width:80%}.vlog p.ag{max-width:85%}.vlog p.ag.cut{opacity:.65}.vlog p.sys{background:none;color:var(--mut);font-size:12px;text-align:center;align-self:center}
.warnbox{background:#2a2410;border:1px solid #6b5a12;border-radius:8px;padding:8px 12px;font-size:13px}
#vcamw{display:flex;align-items:center;gap:10px;font-size:12px}#vcamw[hidden]{display:none}
#vcamv{width:128px;height:96px;object-fit:cover;border-radius:8px;border:1px solid var(--line);transform:scaleX(-1);background:#000}
.camdot{color:#e74c3c;font-weight:600}
#buddy .react{position:absolute;left:50%;top:8%;font-size:32px;pointer-events:none;z-index:2;animation:breact 2.2s ease-out forwards}
#buddy.full .react{top:10%;font-size:48px}
@keyframes breact{0%{transform:translate(-50%,10px) scale(.4);opacity:0}20%{transform:translate(-50%,0) scale(1.15);opacity:1}100%{transform:translate(-50%,-80px) scale(1);opacity:0}}
/* ---- companion overlay (floating agent while on a voice call) ---- */
#wall{position:fixed;inset:0;z-index:9000;background:rgba(8,10,16,.93);display:flex;flex-direction:column;padding:18px 22px;overflow:auto}
#wall[hidden]{display:none}
#wall h2{margin:0 0 4px;font-size:1.3rem}#wall .wsub{color:#9aa4b8;margin-bottom:14px;font-size:.9rem}
#wallx{position:absolute;right:16px;top:12px;font-size:1rem;padding:6px 12px}
#wgrid{position:relative;flex:1;min-height:420px;perspective:1800px;overflow:hidden;display:block}
#wring{position:absolute;left:50%;top:46%;width:0;height:0;transform-style:preserve-3d;transition:transform .7s cubic-bezier(.2,.8,.2,1)}
.wt{--c:#c9a227;position:absolute;left:-95px;top:-150px;width:190px;border:0;border-radius:0;background:none;padding:0;cursor:pointer;overflow:visible;color:#fff;text-align:center;
 transition:transform .7s cubic-bezier(.2,.8,.2,1),opacity .5s,filter .5s,box-shadow .5s;backface-visibility:hidden;opacity:0}
.wt img,.wt video{display:block;width:100%;aspect-ratio:2/3;object-fit:contain;object-position:50% 100%;background:none;pointer-events:none}
.wt b{display:block;width:max-content;max-width:100%;margin:4px auto 0;padding:2px 10px;border-radius:99px;background:#0d0f14d9;border:1px solid var(--c);font-size:.9rem}.wt small{display:block;padding:0 4px 8px;color:#9aa4b8;font-size:.72rem}
.wt.front{box-shadow:none}
.wt.live::after{content:"● on call";position:absolute;top:8px;right:8px;background:#16a34a;color:#fff;font-size:.68rem;padding:2px 7px;border-radius:99px}
.wt .wsay{position:absolute;left:50%;top:-6px;transform:translate(-50%,-100%);background:#fff;color:#111;font-weight:600;font-size:.95rem;padding:6px 12px;border-radius:14px;white-space:nowrap;box-shadow:0 4px 14px #0008;z-index:20}
.wt:focus-visible{outline:2px solid #fff}
#wlabel{text-align:center;font-size:1.15rem;min-height:2.6em;padding-top:6px}#wlabel small{display:block;color:#9aa4b8;font-size:.8rem}
@media (prefers-reduced-motion:reduce){#wring,.wt{transition:none}}
#buddy{position:fixed;left:0;top:0;z-index:9999;width:150px;pointer-events:none;--c:#c9a227;--amp:0;--tilt:0deg;
 transition:transform 2.4s cubic-bezier(.45,.05,.25,1);font-family:inherit}
#buddy.drag{transition:none}#buddy[hidden]{display:none}
#buddy .bwrap{position:relative;width:118px;height:118px;margin:0 auto;pointer-events:auto;cursor:grab;animation:bfloat 3.4s ease-in-out infinite}
#buddy.talking .bwrap{animation-duration:1.7s}
#buddy .bbody{width:100%;height:100%;transform:scale(calc(1 + var(--amp) * .16)) rotate(var(--tilt));transition:transform .09s linear}
#buddy.walking .bbody{animation:bwalk .55s ease-in-out infinite}
#buddy .bbody img{width:100%;height:100%;border-radius:50%;object-fit:cover;border:3px solid var(--c);background:#111;user-select:none;-webkit-user-drag:none;
 box-shadow:0 0 calc(10px + var(--amp) * 46px) var(--c),0 10px 26px #000b;transition:box-shadow .09s linear}
#buddy .aura{position:absolute;inset:-12px;border-radius:50%;border:2px dashed var(--c);opacity:0;transition:opacity .4s;animation:bspin 14s linear infinite;pointer-events:none}
#buddy.thinking .aura,#buddy.tool .aura{opacity:.85;animation-duration:2.2s}
#buddy .ring{position:absolute;inset:0;border-radius:50%;border:3px solid var(--c);opacity:0;pointer-events:none}
#buddy.listening .ring{animation:bping 1.3s ease-out infinite}
#buddy .shadow{width:76px;height:12px;margin:8px auto 0;border-radius:50%;background:#000;opacity:.4;filter:blur(4px);animation:bshadow 3.4s ease-in-out infinite}
#buddy.talking .shadow{animation-duration:1.7s}
#buddy .bname{position:absolute;left:50%;bottom:-8px;transform:translateX(-50%);white-space:nowrap;background:#0d0f14e6;border:1px solid var(--c);
 border-radius:10px;padding:1px 8px;font-size:11px;color:#eee;pointer-events:none}
#buddy .btools{position:absolute;right:-18px;top:-6px;display:flex;flex-direction:column;gap:3px;opacity:0;transition:opacity .2s}
#buddy .bwrap:hover .btools,#buddy.inpip .btools{opacity:1}
#buddy .btools button{width:26px;height:26px;border-radius:50%;border:1px solid var(--c);background:#0d0f14ee;color:#eee;font-size:12px;cursor:pointer;padding:0}
#buddy .bubble{position:absolute;bottom:calc(100% + 4px);left:50%;transform:translateX(-50%);width:max-content;max-width:270px;max-height:150px;overflow:hidden;
 background:#161a22f2;border:1px solid var(--c);border-radius:14px;padding:8px 12px;font-size:13px;line-height:1.35;color:#e8e6e3;
 box-shadow:0 6px 20px #000a;pointer-events:auto;opacity:0;transition:opacity .3s,transform .3s;display:flex;flex-direction:column-reverse}
#buddy .bubble{z-index:5}#buddy .bubble.on{opacity:1}#buddy .bubble.me{border-style:dashed;color:#b8e0c2}
#buddy .bubble::after{content:'';position:absolute;top:100%;left:50%;margin-left:-7px;border:7px solid transparent;border-top-color:var(--c)}
#buddy.below .bubble{bottom:auto;top:calc(100% + 26px)}#buddy.below .bubble::after{top:auto;bottom:100%;border-top-color:transparent;border-bottom-color:var(--c)}
#buddy.edge-l .bubble{left:0;transform:none}#buddy.edge-l .bubble::after{left:60px}
#buddy.edge-r .bubble{left:auto;right:0;transform:none}#buddy.edge-r .bubble::after{left:auto;right:60px}
#buddy .dots i{display:inline-block;width:6px;height:6px;margin:0 2px;border-radius:50%;background:var(--c);animation:bdot 1s infinite}
#buddy .dots i:nth-child(2){animation-delay:.15s}#buddy .dots i:nth-child(3){animation-delay:.3s}
#buddy .rune{position:absolute;left:50%;top:40%;color:var(--c);font-size:14px;pointer-events:none;text-shadow:0 0 6px var(--c);animation:brune 1.4s ease-out forwards}
#buddy.enter .bwrap{animation:benter .7s cubic-bezier(.2,1.6,.4,1)}#buddy.exit .bwrap{animation:bexit .55s ease-in forwards}
#buddy.mini{width:70px}#buddy.mini .bwrap{width:56px;height:56px}#buddy.mini .bubble,#buddy.mini .bname,#buddy.mini .shadow{display:none}
#buddy.inpip{position:absolute;left:50%!important;top:auto!important;bottom:18px;transform:translateX(-50%)!important;transition:none}
#buddy.inpip .bubble{max-width:290px;max-height:170px}
/* full-body mode: a large standing figure at the bottom-right; clicks pass through it to the page */
#buddy.full{left:auto;top:auto;right:1vw;bottom:0;width:min(42vw,calc(84vh * .667));transform:none!important;transition:none}
#buddy.full .bwrap{width:100%;height:84vh;pointer-events:none;cursor:default;animation:bbreath 4.6s ease-in-out infinite}
#buddy.full.talking .bwrap{animation-duration:3.2s}
#buddy.full .bbody{transform:scale(calc(1 + var(--amp) * .025));transform-origin:50% 100%}
#buddy.full .bbody img{border:0;border-radius:0;background:none;box-shadow:none;object-fit:contain;object-position:50% 100%;
 filter:drop-shadow(0 0 calc(var(--amp) * 26px) color-mix(in srgb,var(--c) calc(var(--amp) * 100%),transparent)) drop-shadow(0 16px 26px #000c);transition:filter .09s linear;
 -webkit-mask-image:linear-gradient(to top,transparent 0,#000 6%);mask-image:linear-gradient(to top,transparent 0,#000 6%)}
#buddy .bbody{position:relative}#buddy .bbody video{display:none}
#buddy.full .bbody.anim video{display:block;position:absolute;inset:0;width:100%;height:100%;object-fit:contain;object-position:50% 100%;
 opacity:0;transition:opacity .25s linear;pointer-events:none;filter:drop-shadow(0 0 calc(var(--amp) * 26px) color-mix(in srgb,var(--c) calc(var(--amp) * 100%),transparent)) drop-shadow(0 16px 26px #000c);
 -webkit-mask-image:linear-gradient(to top,transparent 0,#000 6%);mask-image:linear-gradient(to top,transparent 0,#000 6%)}
#buddy.full .bbody.anim video.on{opacity:1}#buddy.full .bbody.anim img{opacity:0}
#buddy.full .aura,#buddy.full .ring{inset:auto 18% 4px 18%;height:46px;border-radius:50%}
#buddy.full .aura{border-style:solid;filter:blur(1px)}
#buddy.full .shadow{display:none}
#buddy.full .bname{bottom:14px;font-size:13px;padding:2px 12px}
#buddy.full .btools{right:4px;top:10%;opacity:.4;pointer-events:auto}#buddy.full .btools:hover{opacity:1}
#buddy.full .bubble{bottom:auto;top:5%;left:auto;right:90%;transform:none;max-width:min(380px,44vw);max-height:42vh;font-size:14px}
#buddy.full .bubble::after{top:24px;left:100%;margin-left:0;border-top-color:transparent;border-left-color:var(--c)}
#buddy.full .rune{top:28%;font-size:18px}
#buddy.full.enter .bwrap{animation:bfullin .8s ease-out}#buddy.full.exit .bwrap{animation:bfullout .55s ease-in forwards}
@keyframes bbreath{0%,100%{translate:0 0;scale:1}50%{translate:0 -6px;scale:1.008}}
@keyframes bfullin{from{opacity:0;translate:80px 0;filter:blur(6px)}to{opacity:1;translate:0 0;filter:blur(0)}}
@keyframes bfullout{to{opacity:0;translate:80px 0;filter:blur(6px)}}
@keyframes bfloat{0%,100%{translate:0 0}50%{translate:0 -10px}}
@keyframes bshadow{0%,100%{transform:scale(1);opacity:.4}50%{transform:scale(.78);opacity:.22}}
@keyframes bwalk{0%,100%{rotate:-7deg}50%{rotate:7deg}}
@keyframes bspin{to{rotate:360deg}}
@keyframes bping{0%{transform:scale(1);opacity:.9}100%{transform:scale(1.45);opacity:0}}
@keyframes bdot{0%,100%{transform:translateY(0);opacity:.4}50%{transform:translateY(-4px);opacity:1}}
@keyframes brune{0%{transform:translate(-50%,0) scale(.6);opacity:1}100%{transform:translate(calc(-50% + var(--dx)),-90px) scale(1.2);opacity:0}}
@keyframes benter{0%{transform:scale(0) rotate(-180deg);opacity:0}100%{transform:scale(1) rotate(0);opacity:1}}
@keyframes bexit{0%{transform:scale(1);opacity:1;filter:blur(0)}100%{transform:scale(.2) translateY(-40px);opacity:0;filter:blur(6px)}}
@media (prefers-reduced-motion:reduce){#buddy,#buddy *{animation:none!important;transition:none!important}}
/* ---- desktop companion (?desk=1, loaded by the transparent Roundtable Companion app): only the character ---- */
html:not(.desk) .donly,html:not(.desk) #dpanel{display:none!important}
html.desk,html.desk body{background:transparent!important;overflow:hidden}
html.desk header,html.desk main,html.desk footer,html.desk #modal{display:none!important}
html.desk #buddy.full{right:0;bottom:0;width:calc((100vh - 8px) * .667)}
html.desk #buddy.full .bwrap{height:calc(100vh - 8px)}
html.desk #buddy.full .btools{opacity:0;transition:opacity .25s}html.desk #buddy.full.hov .btools{opacity:1}
html.desk #bfull,html.desk #bpip,html.desk #bmin{display:none}
html.desk #buddy:not(.oncall) :is(#bcut,#bmute,#bend),html.desk #buddy.oncall #dtalk{display:none}
html.desk #buddy .bubble{pointer-events:none}
html.desk #buddy.full .bubble{right:84%;max-width:min(300px,52vw)}
#dpanel{position:fixed;top:9%;right:46px;display:none;grid-template-columns:repeat(3,52px);gap:8px;padding:10px;z-index:10000;
 background:#0d0f14ee;border:1px solid #c9a22766;border-radius:14px;box-shadow:0 8px 24px #000a}#dpanel.open{display:grid}
#dpanel button{background:none;border:0;padding:0;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:2px;color:#ddd;font-size:10px}
#dpanel img{width:44px;height:44px;border-radius:50%;object-fit:cover;border:2px solid var(--c)}#dpanel button.on img{box-shadow:0 0 0 2px #fff}
/* desk pop-up: Gideon announces something that needs Hermes (incident or approval) */
html:not(.desk) #dnote{display:none!important}
#dnote{position:fixed;top:4%;right:calc((100vh - 8px) * .667 * .78);width:min(290px,52vw);display:none;z-index:10001;padding:12px 14px;
 background:#0d0f14f2;border:1px solid var(--nc,#f2a1c4);border-radius:14px;box-shadow:0 8px 28px #000c;color:#eee;font:13px/1.4 system-ui}
#dnote.on{display:block;animation:dnin .35s ease-out}#dnote b{display:block;margin-bottom:4px;color:var(--nc,#f2a1c4)}
#dnote .dnb{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}#dnote button{flex:1 1 auto;border:1px solid #ffffff2a;background:#ffffff12;color:#eee;
 border-radius:9px;padding:6px 8px;cursor:pointer;font:600 12px system-ui}#dnote button:hover{background:#ffffff26}#dnote button.go{background:var(--nc,#f2a1c4);color:#111;border-color:transparent}
#dnote .dnm{font-size:11px;color:#9aa}
/* desk team: summon_team stands every agent on the desktop (window covers the work area, Gideon stays bottom-right) */
html.desk.team #buddy.full{width:calc(min(100vh - 8px,852px) * .667)}html.desk.team #buddy.full .bwrap{height:min(calc(100vh - 8px),852px)}
html.desk #wall{background:transparent;padding:0;overflow:visible;pointer-events:none}
html.desk #wall h2,html.desk #wall .wsub,html.desk #wlabel{display:none}
html.desk #wgrid{position:fixed;inset:0;perspective:none;overflow:visible;min-height:0}
html.desk #wring{position:fixed;inset:0;width:auto;height:auto;transform:none!important;transform-style:flat;transition:none}
html.desk .wt.dt{position:absolute;top:auto;left:0;width:auto;border:0;border-radius:0;background:none;padding:0;overflow:visible;opacity:1;pointer-events:none;
 transform:translateX(-50%);backface-visibility:visible;filter:drop-shadow(0 10px 14px #000a);transition:filter .25s;animation:dtin .7s cubic-bezier(.2,.9,.3,1.25) backwards;animation-delay:var(--d)}
html.desk .wt.dt img{height:100%;width:auto;aspect-ratio:auto;object-fit:contain;background:none;transform-origin:50% 100%;animation:dtidle 5.2s ease-in-out infinite;animation-delay:var(--b)}
html.desk .wt.dt video{position:absolute;inset:0;width:100%;height:100%;aspect-ratio:auto;background:none;object-fit:contain;object-position:50% 100%;opacity:0;transition:opacity .25s linear;pointer-events:none;-webkit-mask-image:linear-gradient(to top,transparent 0,#000 6%);mask-image:linear-gradient(to top,transparent 0,#000 6%)}
html.desk .wt.dt.anim video.on{opacity:1}html.desk .wt.dt.anim img{opacity:0;animation:none}
html.desk .wt.dt b{position:absolute;left:50%;bottom:4%;transform:translateX(-50%);white-space:nowrap;padding:3px 10px;border-radius:99px;background:#0d0f14e6;
 border:1px solid var(--c);font-size:12px;opacity:0;transition:opacity .2s}html.desk .wt.dt small{display:none}
html.desk .wt.dt.front{filter:drop-shadow(0 0 16px var(--c)) drop-shadow(0 10px 14px #000a);box-shadow:none}html.desk .wt.dt.front b,html.desk .wt.dt.live b{opacity:1}
html.desk #wallx{position:fixed;left:50%;right:auto;top:14px;width:auto;transform:translateX(-50%);pointer-events:auto;z-index:10002;background:#0d0f14e6;color:#eee;border:1px solid #ffffff33;border-radius:99px;box-shadow:0 6px 18px #0008;cursor:pointer}
html.desk #wring.rc .wt.dt:not(.speak){filter:brightness(.5) drop-shadow(0 10px 14px #000a)}
html.desk .wt.dt.speak{z-index:5!important;filter:drop-shadow(0 0 26px var(--c)) drop-shadow(0 0 8px var(--c)) brightness(1.12)}
html.desk .wt.dt.speak img{animation:dtspeak .5s ease-in-out infinite alternate}html.desk .wt.dt.speak b{opacity:1}
html.desk .wt.dt .wsay{white-space:normal;width:max-content;max-width:300px;text-align:center;font-size:14px;line-height:1.35}
@keyframes dtspeak{from{scale:1.04}to{scale:1.07;translate:0 -4px}}
@keyframes dtidle{0%,100%{translate:0 0;rotate:0deg;scale:1}25%{translate:0 -5px;rotate:-1.1deg;scale:1.01}50%{translate:0 -2px;rotate:0deg}75%{translate:0 -5px;rotate:1.1deg;scale:1.01}}
@keyframes dtin{from{opacity:0;transform:translateX(-50%) translateY(45%) scale(.55)}to{opacity:1;transform:translateX(-50%)}}
@keyframes dnin{from{opacity:0;translate:0 -10px}to{opacity:1;translate:0 0}}

/* ===================== v2 · glass theme + responsive shell ===================== */
:root{--bg:#07090f;--card:rgba(255,255,255,.04);--line:rgba(255,255,255,.08);--line2:rgba(255,255,255,.16);--txt:#eceef4;--mut:#8b93a7;
 --acc:#a78bfa;--acc2:#22d3ee;--grad:linear-gradient(135deg,#a78bfa,#22d3ee);--r:16px;--side:236px;--glass:blur(16px) saturate(150%)}
body{background:var(--bg);font:14px/1.5 "Inter","Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh}
body::before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;
 background:radial-gradient(900px 600px at 6% -12%,rgba(139,92,246,.24),transparent 60%),radial-gradient(800px 520px at 102% -4%,rgba(34,211,238,.15),transparent 60%),
 radial-gradient(760px 700px at 62% 122%,rgba(236,72,153,.11),transparent 60%)}
html.desk body::before{display:none}
::selection{background:rgba(167,139,250,.35)}*{scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.16) transparent}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
header{padding:12px 24px;background:rgba(7,9,15,.62);backdrop-filter:var(--glass);-webkit-backdrop-filter:var(--glass);border-bottom:1px solid var(--line)}
h1{font-size:17px;font-weight:650;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
h1 .logo{width:32px;height:32px;border-radius:10px;background:var(--grad);display:grid;place-items:center;font-size:16px;box-shadow:0 6px 20px -4px rgba(139,92,246,.6)}
h1 #team{background:linear-gradient(90deg,#fff,#c7c2ff);-webkit-background-clip:text;background-clip:text;color:transparent}
h1 small{font-size:12px;padding:2px 8px;border:1px solid var(--line);border-radius:20px;margin-left:2px}h1 small:empty{display:none}
.kpis{gap:8px}.kpi{background:var(--card);backdrop-filter:var(--glass);-webkit-backdrop-filter:var(--glass);border:1px solid var(--line);border-radius:12px;padding:6px 12px;min-width:0}
.kpi b{font-size:15px;font-weight:650;font-variant-numeric:tabular-nums}.kpi span{font-size:11px}.bar{height:4px;background:rgba(255,255,255,.08)}
main{display:grid;grid-template-columns:var(--side) minmax(0,1fr);column-gap:24px;align-items:start;max-width:none;margin:0;padding:22px 24px}
main>section{grid-column:2;grid-row:1;min-width:0}main>section:not([hidden]){animation:vin .28s ease-out}
@keyframes vin{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
nav{grid-column:1;grid-row:1;position:sticky;top:84px;flex-direction:column;gap:2px;margin:0;padding:10px;background:var(--card);border:1px solid var(--line);
 border-radius:var(--r);backdrop-filter:var(--glass);-webkit-backdrop-filter:var(--glass);z-index:4}
nav .ng{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);padding:12px 10px 4px;font-weight:600}nav .ng:first-child{padding-top:4px}
nav button{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:transparent;border:1px solid transparent;border-radius:10px;padding:8px 10px;
 color:#c3c8d6;font:inherit;font-weight:500;transition:background .15s,color .15s,border-color .15s}
nav button i{font-style:normal;width:22px;text-align:center;flex:none}nav button span{flex:1}
nav button:hover{background:rgba(255,255,255,.05);color:#fff}
nav button.on{background:linear-gradient(90deg,rgba(167,139,250,.2),rgba(34,211,238,.06));border-color:rgba(167,139,250,.35);color:#fff;box-shadow:inset 3px 0 0 var(--acc)}
.nb{font-size:10.5px;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:#ef4444;color:#fff;display:grid;place-items:center;font-weight:700}.nb:empty{display:none}
.grid{grid-template-columns:repeat(auto-fill,minmax(min(340px,100%),1fr));gap:16px}
.card,.chart,.phead,#modal .box{backdrop-filter:var(--glass);-webkit-backdrop-filter:var(--glass)}
.card{position:relative;overflow:hidden;background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.018));border:1px solid var(--line);border-top:1px solid var(--line);
 border-radius:var(--r);padding:18px;box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 12px 32px -16px rgba(0,0,0,.7);transition:transform .2s,border-color .2s,box-shadow .2s}
.card::before{content:'';position:absolute;inset:0 0 auto;height:2px;background:linear-gradient(90deg,transparent,var(--c),transparent)}
.card::after{content:'';position:absolute;width:240px;height:240px;right:-90px;top:-120px;border-radius:50%;pointer-events:none;
 background:radial-gradient(circle,color-mix(in srgb,var(--c) 32%,transparent),transparent 70%);opacity:.5;transition:opacity .25s}
.card>*{position:relative;z-index:1}.card:hover{transform:translateY(-3px);border-color:color-mix(in srgb,var(--c) 50%,transparent);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 20px 44px -18px color-mix(in srgb,var(--c) 45%,#000)}.card:hover::after{opacity:1}
.name{font-weight:650;letter-spacing:-.01em}.av{width:48px;height:48px;box-shadow:0 0 0 3px color-mix(in srgb,var(--c) 22%,transparent)}
.pill{background:rgba(255,255,255,.04);font-weight:600;padding:3px 9px}.chip{background:rgba(255,255,255,.05);border-color:var(--line);border-radius:7px;padding:1px 8px}
.last{background:rgba(0,0,0,.28);border-color:var(--line);border-radius:10px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:16px}
.slegend{margin:0 0 14px}.slegend span{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:3px 10px 3px 6px}
table{border-collapse:separate;border-spacing:0;background:var(--card);border:1px solid var(--line);border-radius:14px;backdrop-filter:var(--glass);-webkit-backdrop-filter:var(--glass)}
th{background:rgba(14,16,25,.92);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}th,td{padding:9px 11px}tr:last-child td{border-bottom:0}
tr.click:hover{background:rgba(167,139,250,.07)}
select,input{background:rgba(255,255,255,.05);border:1px solid var(--line2);border-radius:10px;padding:7px 10px;font:inherit;color:var(--txt)}select option{background:#12141c}
#modal{background:rgba(3,4,8,.55);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
#modal .box{background:rgba(18,20,30,.88);border:1px solid var(--line2);border-radius:20px;padding:22px;box-shadow:0 30px 80px -20px #000}
pre{background:rgba(0,0,0,.35);border:1px solid var(--line);border-radius:10px}
.phead{background:linear-gradient(135deg,color-mix(in srgb,var(--c) 14%,transparent),rgba(255,255,255,.03));border:1px solid var(--line);border-top:1px solid var(--line);border-radius:20px;padding:22px}
.back,.vbtns button,.vgrp button{border-radius:999px;transition:background .15s,border-color .15s,transform .1s}.back{background:var(--card);border-color:var(--line2)}
.back:hover,.vbtns button:not(:disabled):hover{background:rgba(255,255,255,.08);border-color:var(--line2)}.vbtns button:not(:disabled):active{transform:scale(.97)}
.vbtns button{background:rgba(255,255,255,.05);border-color:var(--line2);padding:9px 16px}
.vbtns button.go{background:linear-gradient(135deg,#22c55e,#14b8a6);border-color:transparent;color:#04130b;box-shadow:0 8px 22px -8px #22c55e}
.vbtns button.stop{background:linear-gradient(135deg,#ef4444,#be123c);border-color:transparent}
.vstage{background:linear-gradient(180deg,color-mix(in srgb,var(--c,#a78bfa) 12%,transparent),rgba(255,255,255,.02))}
.vstage img{box-shadow:0 0 0 6px color-mix(in srgb,var(--c) 15%,transparent),0 20px 50px -18px var(--c)}
.vgrp{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:999px;padding:4px;width:max-content;max-width:100%;overflow-x:auto}
.vgrp button{background:transparent;border:0;color:var(--mut);white-space:nowrap}.vgrp button.on{background:rgba(167,139,250,.22);color:#fff;border:0}
.vpick button{background:rgba(255,255,255,.035);border-color:var(--line);border-radius:14px;transition:transform .15s,border-color .15s,background .15s}
.vpick button:hover{transform:translateY(-2px);background:rgba(255,255,255,.06)}.vpick button.on{background:color-mix(in srgb,var(--c) 14%,transparent)}
.vlog p{background:rgba(255,255,255,.05);border-radius:14px 14px 14px 4px;padding:8px 12px}
.vlog p.me{background:linear-gradient(135deg,rgba(139,92,246,.35),rgba(34,211,238,.2));border-radius:14px 14px 4px 14px}
.vmeter{background:rgba(255,255,255,.08)}.vmeter i{background:var(--grad)}
.warnbox{background:rgba(234,179,8,.1);border-color:rgba(234,179,8,.35);border-radius:12px}
footer{opacity:.7}
.hintro{background:linear-gradient(135deg,rgba(167,139,250,.15),rgba(34,211,238,.04));margin-bottom:18px}
.hintro h2{margin:0;font-size:24px;letter-spacing:-.02em}.hintro p{max-width:820px}
.hflow{color:var(--acc);font-weight:600;overflow-wrap:anywhere}
.htools{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.htools .back{margin:0}
#hierarchy-count{margin-right:auto}#hierarchy-error{color:var(--bad);overflow-wrap:anywhere}
.hbranches{list-style:none;margin:0;padding:18px 0 0;display:grid;gap:16px;min-width:0}
.hroots{padding:0}.hitem{min-width:0}.hnode{max-width:520px;margin:0 auto;padding:16px;border:1px solid var(--line);
 border-top:3px solid var(--c);border-radius:16px;background:var(--card);overflow-wrap:anywhere}
.hidentity{display:flex;align-items:center;gap:12px}.hprofile{display:flex;align-items:center;gap:12px;min-width:0;flex:1;color:var(--txt);text-decoration:none}
.hprofile[href]:hover .name{text-decoration:underline}.hprofile>span:last-child{min-width:0}
.hportrait{width:42px;height:42px;flex:none;border-radius:12px;background:color-mix(in srgb,var(--c) 16%,transparent);display:grid;place-items:center;font-size:24px;overflow:hidden}
.hportrait img{width:100%;height:100%;object-fit:cover}.hlead{color:var(--acc);flex:none}
.hpending{color:var(--warn);font-size:11px}#hierarchy-stage{color:var(--warn);font-weight:600}
.hparent{font-size:12px;color:var(--mut);margin-top:10px}.hbranch{margin-top:12px}
.hbranch>summary{width:max-content;max-width:100%;margin:auto;color:var(--mut);cursor:pointer;padding:6px 10px;border:1px solid var(--line);border-radius:20px;font-size:12px}
.hlead-branches{grid-template-columns:repeat(3,minmax(0,1fr));align-items:start}
.hlead-branches .hnode,.hadvisers .hnode{max-width:none}
.hlead-branches .hbranches{border-left:1px solid var(--line);padding-left:14px;gap:10px}
.hother{margin-top:20px;padding-top:16px;border-top:1px solid var(--line)}
.hother h3{margin:0;font-size:15px}.hother p{font-size:12px;margin:4px 0 0;color:var(--mut)}
.hadvisers{grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr))}
@media(max-width:1000px){.hlead-branches{grid-template-columns:minmax(0,1fr)}}
@media(max-width:1180px){:root{--side:64px}nav{padding:8px 6px}nav .ng{display:none}nav button span,nav button .nb{display:none}
 nav button{justify-content:center;padding:10px 0;position:relative}nav button i{font-size:17px}}
@media(max-width:760px){header{padding:10px 14px;gap:10px}h1{font-size:16px}h1 small{display:none}
 .kpis{width:100%;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}.kpis::-webkit-scrollbar{display:none}.kpi{flex:none}
 main{display:block;padding:14px 12px calc(92px + env(safe-area-inset-bottom))}
 nav{position:fixed;left:10px;right:10px;top:auto;bottom:calc(10px + env(safe-area-inset-bottom));z-index:20;flex-direction:row;overflow-x:auto;scrollbar-width:none;
  padding:6px;border-radius:20px;background:rgba(14,16,24,.85);box-shadow:0 12px 40px -10px #000}nav::-webkit-scrollbar{display:none}
 nav button{flex:1 0 auto;flex-direction:column;gap:1px;width:auto;min-width:62px;padding:6px 8px;font-size:10.5px;border-radius:14px}
 nav button span{display:block;flex:none;white-space:nowrap}nav button.on{box-shadow:none}
 .grid{gap:12px}.card{padding:15px}.filters{flex-wrap:wrap}table{display:block;overflow-x:auto;white-space:nowrap}
 .phead{flex-direction:column;text-align:center}.vstage img{width:150px;height:150px}.vbtns button{padding:8px 12px;font-size:13px}
 #modal{align-items:flex-end}#modal .box{width:100%;max-height:90vh;border-radius:20px 20px 0 0}}
.vroom{grid-template-columns:minmax(300px,440px) minmax(0,1fr)}.vroom>*,.two>*{min-width:0}@media(max-width:900px){.vroom{grid-template-columns:minmax(0,1fr)}}
html{-webkit-text-size-adjust:100%}body{-webkit-tap-highlight-color:transparent}
@media(hover:none){.card:hover,.vpick button:hover{transform:none}}
/* phones only (never the desktop companion window, which can also be narrow) */
@media(max-width:760px){html:not(.desk) header{position:static}
 html:not(.desk) select,html:not(.desk) input{font-size:16px}
 html:not(.desk) .top{flex-wrap:wrap}html:not(.desk) dl{grid-template-columns:84px 1fr}
 html:not(.desk) .pgrid{grid-template-columns:repeat(2,minmax(0,1fr))}html:not(.desk) .phead .avbig{width:96px;height:96px}
 html:not(.desk) .mapwrap .chart{overflow-x:auto}html:not(.desk) #map svg{min-width:880px}
 html:not(.desk) .vlog{max-height:52dvh}html:not(.desk) .vpick{grid-template-columns:repeat(auto-fill,minmax(104px,1fr))}html:not(.desk) .vpick img{width:52px;height:52px}
 html:not(.desk) .vmeter{width:min(220px,80%)}
 html:not(.desk) #buddy.full{right:2vw;bottom:calc(84px + env(safe-area-inset-bottom));width:min(40vw,calc(46dvh * .667))}
 html:not(.desk) #buddy.full .bwrap{height:46dvh}
 html:not(.desk) #buddy.full .bubble{top:auto;bottom:100%;right:0;max-width:72vw;max-height:30dvh}html:not(.desk) #buddy.full .bubble::after{display:none}
 html:not(.desk) #buddy .btools{opacity:1}}
.vprimary{width:100%}.vprimary .go{flex:1;font-size:16px;min-height:48px}
.vsecondary{padding-top:12px;border-top:1px solid var(--line);width:100%}
#buddy .bdepth{width:100%;height:100%;transform-style:preserve-3d;transition:transform .16s ease-out}
#buddy.dimensional .bwrap{perspective:1000px;isolation:isolate}
#buddy.dimensional .bdepth{transform:rotateX(var(--depth-x,0deg)) rotateY(var(--depth-y,0deg))}
#buddy.dimensional .bbody img{filter:brightness(calc(1 + var(--depth-light,0))) drop-shadow(12px 18px 12px #0007) drop-shadow(-3px -1px 2px color-mix(in srgb,var(--c) 25%,transparent))}
#buddy.dimensional.full .bwrap::after{content:'';position:absolute;bottom:1%;left:24%;right:24%;height:5%;border-radius:50%;background:radial-gradient(ellipse,#0009,transparent 70%);transform:translateX(var(--depth-shadow,0px));z-index:-1;pointer-events:none}
#buddy.dimensional.full.listening .bdepth{filter:drop-shadow(0 0 8px #22c55e55)}
#buddy.dimensional.full.talking .bdepth{filter:drop-shadow(0 0 calc(4px + var(--amp) * 16px) color-mix(in srgb,var(--c) 45%,transparent))}
#buddy.dimensional.full.thinking .bdepth,#buddy.dimensional.full.tool .bdepth{filter:drop-shadow(0 0 10px color-mix(in srgb,var(--c) 40%,transparent))}
#buddy:not(.dimensional) .bdepth{transform:none}
#vpreview:disabled{opacity:.45;cursor:default}
.hbtns{display:flex;gap:8px;flex-wrap:wrap}
.apb{background:#0f1117;border:1px solid #2a3040;color:var(--txt);border-radius:6px;padding:4px 10px;cursor:pointer;white-space:nowrap}
.apb.ok{background:#2b6a3f;border-color:#3fa45f;color:#fff}.apb:hover{filter:brightness(1.15)}
.vaudio-summary{width:100%;text-align:left;padding:12px 14px;border:1px solid var(--line2);border-radius:14px;background:rgba(0,0,0,.2);color:var(--txt);font:inherit;cursor:pointer}
.vaudio-summary strong,.vaudio-summary span{display:block}.vaudio-summary small{float:right;color:var(--acc)}
#vaudiostatus{font-size:12px;width:100%;text-align:left;overflow-wrap:anywhere}#vaudiostatus.error{color:var(--bad)}
.pickhead{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}.pickhead h3{margin:0}
#vsearch{width:100%;margin-bottom:12px}.vpick .empty{grid-column:1/-1;padding:20px;text-align:center;color:var(--mut)}
dialog#vaudio{width:min(440px,calc(100vw - 24px));max-height:calc(100dvh - 32px);overflow:auto;padding:22px;border:1px solid var(--line2);border-radius:20px;background:#12141c;color:var(--txt);box-shadow:0 30px 80px #0008}
#vaudio::backdrop{background:rgba(3,4,8,.65);backdrop-filter:blur(6px)}
#vaudio h2{margin:0;font-size:18px}#vaudio .audio-head{display:flex;justify-content:space-between;align-items:center;gap:12px}
#vaudio label{display:block;font-size:13px;margin:16px 0 6px}#vmicsel{width:100%}
#vaudio button{background:rgba(255,255,255,.06);color:var(--txt);border:1px solid var(--line2);border-radius:10px;padding:9px 12px;font:inherit;cursor:pointer}
#vaudio button:disabled{opacity:.45;cursor:default}#vaudio .audio-actions{display:flex;gap:8px;margin:14px 0}
#vmictestlevel{width:100%;height:12px;accent-color:var(--acc)}#vmicnote{font-size:13px;overflow-wrap:anywhere}
/* overview */
#ovq{background:rgba(255,255,255,.05);color:var(--txt);border:1px solid var(--line2);border-radius:10px;padding:9px 12px;font:inherit;min-width:220px}
.ovhero{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:14px 0 20px}
.ovhero a{display:flex;flex-direction:column;gap:6px;padding:18px;border-radius:var(--r);border:1px solid var(--line2);background:linear-gradient(135deg,rgba(167,139,250,.14),rgba(34,211,238,.08));color:var(--txt);text-decoration:none;transition:transform .15s,border-color .15s}
.ovhero a:hover,.ovgrid a:hover{transform:translateY(-2px);border-color:var(--acc)}
.ovhero b{font-size:17px}.ovhero .ovi{font-size:26px}
.ovg h3{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);margin:18px 0 8px}
.ovgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.ovgrid a{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--txt);text-decoration:none;transition:transform .15s,border-color .15s;min-width:0}
.ovgrid .ovi{font-size:19px}.ovgrid span{min-width:0}.ovgrid b{display:block;font-weight:600}
.ovgrid small,.ovhero small{color:var(--mut);font-size:12px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ovd{width:8px;height:8px;border-radius:50%;flex:none;margin-left:auto;background:#8a90a2}
.ovd.up{background:var(--ok);box-shadow:0 0 8px var(--ok)}.ovd.down{background:var(--bad)}.ovd.check,.ovd.cert{background:var(--warn)}
.ovhero .ovd{position:absolute;top:14px;right:14px;margin:0}.ovhero a{position:relative}
@media(max-width:640px){#ovq{min-width:0;width:100%}}
@media(prefers-reduced-motion:reduce){main>section,.sd,.edge,.card,.vpick button,.vstage img{animation:none!important;transition:none!important}}

/* ===================== v3 · themes (html.th.t-*; header dropdown switches them, glass = no class) ===================== */
.ovc{display:none}
.ovstrip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0 18px}
.ovdns{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 18px}
.ovdns h3{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);margin:0 0 10px;display:flex;gap:8px;align-items:center}
.ovdns h3 a{margin-left:auto;text-transform:none;letter-spacing:0;font-size:12px}
.ovdns .dg{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
.ovdns .ds{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.ovdns .ds b{display:block;margin-bottom:6px}.ovdns .dr{display:flex;align-items:center;gap:8px;font-size:13px;padding:2px 0}
.ovdns .dr .ovd{margin-left:0}.ovdns .dr small{margin-left:auto;color:var(--mut);font-variant-numeric:tabular-nums}
html.th .ovdns{background:var(--k-panel);border-color:var(--k-bd);border-radius:var(--r)}
.ovstrip>div{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
.ovstrip small{display:block;color:var(--mut);font-size:11px;letter-spacing:.06em;text-transform:uppercase}
.ovstrip b{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}.ovstrip b.w{color:var(--warn)}.ovstrip b.x{color:var(--bad)}
.ovmeter{display:flex;gap:2px;margin-top:6px}.ovmeter i{flex:1;height:6px;border-radius:2px;background:var(--ok)}.ovmeter i.w{background:var(--warn)}.ovmeter i.x{background:var(--bad)}
.thpick{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--mut)}.thpick select{font:inherit;font-size:12px;color:var(--txt);background:var(--panel,var(--card));border:1px solid var(--bd,var(--line));border-radius:8px;padding:5px 6px;cursor:pointer}.thpick option{color:#111;background:#fff}
.hbtns{flex-wrap:nowrap;align-items:center}@media(max-width:1640px){h1 small{display:none}}@media(max-width:1380px){.kpi span{display:none}}
.ovst{display:none}.ovu{display:none}html.th .ovu{display:block;font-size:12px;color:var(--mut);margin-top:6px}
html.th{--bg:var(--k-bg);--card:var(--k-panel);--line:var(--k-bd);--line2:var(--k-bdh);--txt:var(--k-txt);--txt2:var(--k-txt2);--mut:var(--k-mut);--acc:var(--k-acc);--acc2:var(--k-acc2);--ok:var(--k-ok);--warn:var(--k-warn);--bad:var(--k-bad);--run:var(--k-acc2);--r:var(--k-r);--glass:var(--k-blur);--fb:var(--k-font);--fh:var(--k-font);--panel:var(--k-panel);--bd:var(--k-bd);--bdh:var(--k-bdh);--deco:var(--k-deco);--solid:var(--k-solid);--inset:var(--k-inset);--grad:linear-gradient(135deg,var(--k-acc),var(--k-acc2));--bw:1px;--sh:none;--shh:var(--sh);--hdr:color-mix(in srgb,var(--k-bg) 70%,transparent);--hov:color-mix(in srgb,var(--k-txt) 6%,transparent);--onbg:var(--acc);--ontx:#fff;--onbd:transparent;--onsh:none;--hcase:none;--htrack:-.01em;--hw:700;--wallbg:radial-gradient(circle at 50% 70%,color-mix(in srgb,var(--k-acc) 14%,var(--k-solid)),var(--k-bg))}
html.th body{background:var(--bg);color:var(--txt);font-family:var(--fb)}
html.th body::before{background:var(--deco,none)}
html.th header{background:var(--hdr);border-bottom:var(--bw) solid var(--bd);box-shadow:none}
html.th h1{font-family:var(--fh)}html.th h1 #team{background:none;color:var(--txt);-webkit-text-fill-color:currentColor}
html.th h1 .logo{background:var(--acc);box-shadow:var(--sh);border-radius:var(--r)}
html.th h1 small{border-color:var(--bd);color:var(--mut)}
html.th h2,html.th h3{font-family:var(--fh);font-weight:var(--hw);letter-spacing:var(--htrack)}
html.th .mut{color:var(--mut)}html.th a{color:var(--acc)}
html.th .kpi,html.th nav,html.th .card,html.th .chart,html.th #modal .box,html.th .ovstrip>div,html.th .ovhero a,html.th .ovgrid a{
 background:var(--panel);border:var(--bw) solid var(--bd);border-radius:var(--r);box-shadow:var(--sh);backdrop-filter:var(--glass);-webkit-backdrop-filter:var(--glass)}
html.th .card::after,html.th .card::before{display:none}
html.th .card:hover,html.th .ovhero a:hover,html.th .ovgrid a:hover{transform:translateY(-2px);box-shadow:var(--shh);border-color:var(--bdh,var(--bd))}
html.th .kpi b{color:var(--txt)}html.th .kpi span{color:var(--mut)}
html.th nav button{color:var(--txt2);border-radius:calc(var(--r) * .7)}
html.th nav button:hover{background:var(--hov)}
html.th nav button.on{background:var(--onbg);color:var(--ontx);border-color:var(--onbd);box-shadow:var(--onsh)}
html.th nav .ng{color:var(--mut)}
html.th .hbtns button{font-family:var(--fb);background:var(--panel);color:var(--txt);border:var(--bw) solid var(--bd);border-radius:calc(var(--r) * .7);box-shadow:var(--sh)}
html.th #ovq{background:var(--panel);color:var(--txt);border:var(--bw) solid var(--bd);border-radius:calc(var(--r) * .7);box-shadow:var(--sh)}
html.th #v-overview .dihead h2{font-size:28px;text-transform:var(--hcase)}
html.th .ovg h3{font-family:var(--fh);text-transform:var(--hcase)}
html.th .ovhero b,html.th .ovgrid b{font-family:var(--fh);color:var(--txt)}
html.th .ovgrid small,html.th .ovhero small{color:var(--mut)}
html.th .ovc{display:block;font-size:11px;letter-spacing:.08em;color:var(--mut);text-transform:uppercase}
html.th :focus-visible{outline-color:var(--acc)}

/* 1 · Aurora Bento: drifting colour, big rounded tiles */
html.t-bento{--hw:800;--htrack:-.03em;--hdr:rgba(7,7,13,.55);--hov:rgba(255,255,255,.06);--onbg:rgba(255,255,255,.12);--ontx:#fff;--onbd:rgba(255,255,255,.18);--sh:0 1px 0 rgba(255,255,255,.06) inset;--shh:0 1px 0 rgba(255,255,255,.08) inset,0 24px 60px -24px rgba(139,123,255,.55)}
html.t-bento body::before{filter:blur(30px);animation:aurora 24s ease-in-out infinite alternate}
@keyframes aurora{0%{transform:translate(0,0) scale(1)}50%{transform:translate(-3%,2%) scale(1.08)}100%{transform:translate(3%,-2%) scale(1.03)}}
html.t-bento nav button{border-radius:14px}html.t-bento h1 .logo{border-radius:12px;background:linear-gradient(135deg,#8b7bff,#06b6d4)}
html.t-bento #v-overview .dihead h2{font-size:40px;background:linear-gradient(90deg,#fff,#c4b5fd 60%,#67e8f9);-webkit-background-clip:text;background-clip:text;color:transparent}
html.t-bento #v-overview .dihead h2 .ovemoji{display:none}
html.t-bento .ovhero{grid-template-columns:repeat(4,minmax(0,1fr));grid-auto-rows:150px;gap:14px}
html.t-bento .ovhero a{padding:20px;justify-content:flex-end}
html.t-bento .ovhero a:first-child{grid-column:span 2;grid-row:span 2;background:linear-gradient(145deg,rgba(139,123,255,.35),rgba(6,182,212,.18)),rgba(255,255,255,.04)}
html.t-bento .ovhero a:first-child b{font-size:34px}html.t-bento .ovhero a:first-child .ovi{font-size:56px}
html.t-bento .ovhero .ovi{font-size:26px;margin-bottom:auto}html.t-bento .ovhero a:first-child .ovi{position:absolute;top:22px;left:22px}
html.t-bento .ovhero b{font-size:19px;letter-spacing:-.02em}
html.t-bento .ovstrip b{font-size:30px;letter-spacing:-.03em}
html.t-bento .ovg h3{font-size:20px;text-transform:none;letter-spacing:-.02em;color:var(--txt);margin:26px 0 10px}
html.t-bento .ovg h3 em{font-style:normal;color:var(--mut);font-size:14px;font-weight:600}
html.t-bento .ovgrid{gap:10px}html.t-bento .ovgrid a{border-radius:18px;padding:13px 14px}
@media(max-width:900px){html.t-bento .ovhero{grid-template-columns:repeat(2,minmax(0,1fr))}}

/* 2 · Minimal Pro: quiet near-black, hairlines, no glow */
html.t-mini{--hw:600;--htrack:-.02em;--hdr:rgba(10,10,11,.85);--hov:#17171a;--onbg:#1b1b1f;--ontx:#fff;--onbd:#2c2c32;--shh:0 0 0 1px #3a3a42}
html.t-mini body{font-size:13.5px}
html.t-mini h1 .logo{background:#ededef;color:#0a0a0b;border-radius:8px}html.t-mini h1 #team{font-weight:600}
html.t-mini #v-overview .dihead h2{font-size:24px}html.t-mini #v-overview .dihead h2 .ovemoji{display:none}
html.t-mini .card:hover,html.t-mini .ovhero a:hover,html.t-mini .ovgrid a:hover{transform:none}
html.t-mini .ovhero a{padding:16px}html.t-mini .ovhero .ovi,html.t-mini .ovgrid .ovi{filter:grayscale(1);opacity:.85}
html.t-mini .ovhero b{font-size:15px}
html.t-mini .ovstrip b{font-size:24px;font-weight:600}
html.t-mini .ovg h3{font-size:12px;color:var(--mut);text-transform:none;letter-spacing:0;font-weight:500;margin:24px 0 8px}
html.t-mini .ovg h3 em{font-style:normal;color:#5a5a62}
html.t-mini .ovgrid{gap:0;border:1px solid #222226;border-radius:10px;overflow:hidden;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
html.t-mini .ovgrid a{border:0;border-radius:0;border-bottom:1px solid #1b1b1f;border-right:1px solid #1b1b1f;background:transparent}
html.t-mini .ovgrid a:hover{background:#141417}
html.t-mini .ovd{width:6px;height:6px}html.t-mini .ovd.up{box-shadow:none}
html.t-mini nav button.on{box-shadow:inset 2px 0 0 #ededef}


/* 3 · Nordic Night: cool slate, frosted panels, icy blue */
html.t-frost{--hw:800;--htrack:-.025em;--hdr:rgba(14,20,31,.72);--hov:rgba(122,162,255,.08);--onbg:rgba(122,162,255,.14);--ontx:#dbe6ff;--onbd:rgba(122,162,255,.35);--onsh:0 6px 18px -10px rgba(122,162,255,.6);--sh:inset 0 1px 0 rgba(255,255,255,.05),0 14px 30px -18px rgba(0,0,0,.8);--shh:inset 0 1px 0 rgba(255,255,255,.07),0 20px 40px -18px rgba(122,162,255,.4)}
html.t-frost h1 .logo{background:linear-gradient(135deg,#5b83f0,#9db6ff);border-radius:12px}
html.t-frost h1 #team{font-weight:800}
html.t-frost #v-overview .dihead h2{font-size:36px}html.t-frost #v-overview .dihead h2 .ovemoji{display:none}
html.t-frost .ovhero a{padding:20px}html.t-frost .ovhero b{font-size:19px}
html.t-frost .ovhero .ovi{font-size:28px;width:52px;height:52px;display:grid;place-items:center;border-radius:14px;background:rgba(122,162,255,.1);box-shadow:inset 0 0 0 1px rgba(122,162,255,.18)}
html.t-frost .ovg h3{font-size:18px;text-transform:none;color:var(--txt);margin:26px 0 10px}html.t-frost .ovg h3 em{font-style:normal;color:#64718a;font-size:14px}
html.t-frost .ovstrip b{font-size:30px}html.t-frost .ovd.up{box-shadow:0 0 0 3px rgba(74,222,128,.16)}
html.t-frost .ovmeter i{border-radius:3px}

/* 4 · Pastel Night: soft candy tints on deep plum */
html.t-pastel{--hw:900;--htrack:-.02em;--hdr:#16131d;--hov:#2a2338;--onbg:#b9a4ff;--ontx:#1b1529;--onsh:0 8px 22px -10px rgba(185,164,255,.7);--sh:0 12px 28px -18px rgba(0,0,0,.8);--shh:0 18px 34px -16px rgba(185,164,255,.35);--p1:#b9a4ff;--p2:#7ee2b0;--p3:#ffb59a;--p4:#8fd0ff;--p5:#ffe08a}
html.t-pastel body{font-weight:600}
html.t-pastel h1 .logo{background:#ffb5cf;border-radius:14px}html.t-pastel h1 #team{font-weight:900}
html.t-pastel nav button{border-radius:14px;font-weight:700}
html.t-pastel #v-overview .dihead h2{font-size:40px}
html.t-pastel .ovhero a{--pc:var(--p1);background:color-mix(in srgb,var(--pc) 13%,#1d1828);border:1px solid color-mix(in srgb,var(--pc) 28%,transparent)}
html.t-pastel .ovhero a:nth-child(5n+2){--pc:var(--p2)}html.t-pastel .ovhero a:nth-child(5n+3){--pc:var(--p3)}html.t-pastel .ovhero a:nth-child(5n+4){--pc:var(--p4)}html.t-pastel .ovhero a:nth-child(5n+5){--pc:var(--p5)}
html.t-pastel .ovhero a:hover{border-color:var(--pc);box-shadow:0 18px 34px -16px color-mix(in srgb,var(--pc) 45%,transparent)}
html.t-pastel .ovhero b{font-size:21px;color:var(--pc)}html.t-pastel .ovhero .ovc{color:color-mix(in srgb,var(--pc) 70%,#fff)}
html.t-pastel .ovhero .ovi{font-size:30px;width:54px;height:54px;display:grid;place-items:center;border-radius:16px;background:color-mix(in srgb,var(--pc) 18%,transparent)}
html.t-pastel .ovstrip>div{--pc:var(--p2);background:color-mix(in srgb,var(--pc) 11%,#1d1828);border-color:color-mix(in srgb,var(--pc) 24%,transparent)}
html.t-pastel .ovstrip>div:nth-child(2){--pc:var(--p5)}html.t-pastel .ovstrip>div:nth-child(3){--pc:#ff9aae}html.t-pastel .ovstrip>div:nth-child(4){--pc:var(--p1)}
html.t-pastel .ovstrip b{font-size:32px;color:var(--pc)}html.t-pastel .ovmeter i{border-radius:99px}
html.t-pastel .ovg h3{display:inline-flex;gap:8px;align-items:center;background:#241e31;padding:6px 14px;border-radius:99px;font-size:15px;text-transform:none;margin:26px 0 12px;color:var(--txt)}
html.t-pastel .ovg h3 em{font-style:normal;color:var(--acc)}
html.t-pastel .ovgrid{gap:12px}html.t-pastel .ovgrid a{border-radius:18px}
html.t-pastel .ovgrid .ovi{font-size:22px;width:40px;height:40px;display:grid;place-items:center;border-radius:12px;background:#2a2338}
html.t-pastel .ovd{width:10px;height:10px}html.t-pastel .ovd.up{box-shadow:0 0 0 3px rgba(126,226,176,.18)}
/* ---- themes · every page component follows the palette (values come from /theme-kit.css) ---- */
html.th ::selection{background:color-mix(in srgb,var(--acc) 35%,transparent)}
html.th *{scrollbar-color:var(--bdh) transparent}
html.th .bar,html.th .vmeter{background:color-mix(in srgb,var(--txt) 9%,transparent)}
html.th .char,html.th .does{color:var(--txt2)}
html.th .chip,html.th .pill,html.th .dimoods span,html.th .ipsrc span{background:color-mix(in srgb,var(--txt) 6%,transparent);border-color:var(--bd)}
html.th .last,html.th pre,html.th .side .conn,html.th .vaudio-summary{background:var(--inset);color:var(--txt2)}
html.th th{background:var(--solid);color:var(--mut)}html.th td,html.th th{border-color:var(--bd)}
html.th tr.click:hover{background:var(--hov)}
html.th select,html.th input,html.th textarea{background:color-mix(in srgb,var(--txt) 5%,transparent);color:var(--txt);border-color:var(--bd);font-family:var(--fb)}
html.th select option{background:var(--solid);color:var(--txt)}
html.th #modal{background:rgba(0,0,0,.55)}
html.th #modal .box,html.th dialog#vaudio{background:var(--solid);color:var(--txt);border-color:var(--bd);border-radius:var(--r);backdrop-filter:none}
html.th #vaudio::backdrop{background:rgba(0,0,0,.6)}
html.th .phead{background:linear-gradient(135deg,color-mix(in srgb,var(--c) 14%,transparent),var(--panel));border-color:var(--bd);border-radius:var(--r)}
html.th .back,html.th .vbtns button,html.th #vaudio button,html.th .apb,html.th .vgrp button,html.th main button.sec,html.th .dikpi{
 background:color-mix(in srgb,var(--txt) 6%,transparent);border-color:var(--bd);color:var(--txt);border-radius:calc(var(--r) * .6);font-family:var(--fb)}
html.th .back:hover,html.th .vbtns button:not(:disabled):hover,html.th #vaudio button:hover,html.th .dikpi:hover{background:var(--hov);border-color:var(--bdh)}
html.th .dikpi.on{border-color:var(--acc);background:color-mix(in srgb,var(--acc) 14%,transparent)}
html.th .vbtns button.go{background:var(--ok);border-color:var(--ok);color:#05140c;box-shadow:0 8px 22px -10px var(--ok)}
html.th .vbtns button.stop{background:var(--bad);border-color:var(--bad);color:#fff;box-shadow:none}
html.th .apb.ok{background:color-mix(in srgb,var(--ok) 30%,var(--solid));border-color:var(--ok);color:var(--txt)}
html.th .vstage{background:linear-gradient(180deg,color-mix(in srgb,var(--c,var(--acc)) 12%,transparent),var(--panel));border-color:var(--bd);border-radius:var(--r)}
html.th .vgrp{background:color-mix(in srgb,var(--txt) 4%,transparent);border-color:var(--bd);border-radius:calc(var(--r) * .7)}
html.th .vgrp button{background:transparent;color:var(--txt2);border-color:transparent}
html.th .vgrp button.on{background:color-mix(in srgb,var(--acc) 22%,transparent);color:var(--txt);border-color:color-mix(in srgb,var(--acc) 50%,transparent)}
html.th .vpick button{background:var(--panel);border-color:var(--bd);border-radius:var(--r);color:var(--txt)}
html.th .vpick button:hover{background:var(--hov)}
html.th .vlog p{background:color-mix(in srgb,var(--txt) 5%,transparent);border-radius:calc(var(--r) * .6)}
html.th .vlog p.me{background:color-mix(in srgb,var(--acc) 22%,transparent)}
html.th .warnbox{background:color-mix(in srgb,var(--warn) 10%,transparent);border-color:color-mix(in srgb,var(--warn) 35%,transparent);border-radius:calc(var(--r) * .6)}
html.th .hintro{background:linear-gradient(135deg,color-mix(in srgb,var(--acc) 15%,transparent),var(--panel));border-color:var(--bd);border-radius:var(--r)}
html.th nav button:hover{color:var(--txt)}
html.th .nb{background:var(--bad)}
html.th .badge{fill:var(--solid)}
html.th h1 small,html.th footer{color:var(--mut)}
html.th .legend,html.th .legend *{color:var(--mut)}
html.th .thpick select{border-radius:calc(var(--r) * .5)}
/* desk companion pop-ups follow the theme too */
html.th #dnote,html.th #dpanel{background:color-mix(in srgb,var(--solid) 95%,transparent);color:var(--txt);border-radius:var(--r);font-family:var(--fb)}
html.th #dnote button{background:color-mix(in srgb,var(--txt) 8%,transparent);color:var(--txt);border-color:var(--bd);font-family:var(--fb)}
html.th #dnote button.go{background:var(--nc,var(--acc));color:#111}
html.th #dnote .dnm{color:var(--mut)}
html.th #dpanel button{color:var(--txt2)}

html.desk body::after{display:none}
@media(prefers-reduced-motion:reduce){html.th body::before{animation:none!important}}
</style></head><body>
<header><h1><span class="logo">🛡️</span><span id="team">Hermes Roundtable</span><small id="host"></small></h1><div class="kpis" id="kpis"></div><div class="hbtns"><label class="thpick" title="Theme for every Roundtable page (remembered in this browser)">◐ <select data-rt-theme aria-label="Theme"></select></label><button id="persona" type="button" aria-pressed="false" title="Switch between the Elden Ring daily avatars and the realistic demo personas (remembered in this browser)">🎭 Daily</button></div></header>
<main>
<nav><span class="ng">Lab</span><button data-v="overview" class="on" title="Overview: Atlas, Glance and every homelab site"><i>🧭</i><span>Overview</span></button><span class="ng">Agents</span><button data-v="agents" title="Homelab agents"><i>🛠️</i><span>Homelab</span></button><button data-v="hierarchy" title="Agent reporting hierarchy"><i>🌳</i><span>Hierarchy</span></button><span class="ng">Talk</span><button data-v="voice" title="Voice"><i>🎙️</i><span>Voice</span></button><span class="ng">Operations</span><button data-v="map" title="Dependency map: agents, Discord, triggers and systems"><i>🗺️</i><span>Dependency map</span></button><button data-v="ipmap" title="IP map: every assigned address and the app or service using it"><i>🧭</i><span>IP map</span></button><button data-v="office" title="Pixel office: who is working, who is on a break"><i>🏢</i><span>Office</span></button><button data-v="runs" title="Runs &amp; tokens"><i>📜</i><span>Runs &amp; tokens</span></button><button data-v="approvals" title="Approvals"><i>✅</i><span>Approvals</span><b class="nb" id="apcount"></b></button><button data-v="cost" title="Cost"><i>💸</i><span>Cost</span></button></nav>
<section id="v-agents"><div class="slegend" id="slegend"></div><div id="agents"></div></section>
<section id="v-hierarchy" aria-labelledby="hierarchy-title" hidden>
<div class="chart hintro"><h2 id="hierarchy-title">Reporting hierarchy</h2>
<p class="mut">Who reports to whom across your agent team. Domain leads consolidate their teams' findings through Gideon, who brings you the summary.</p>
<p class="hflow">Specialists → Domain leads → Gideon → You</p>
<p class="mut">Governance policies apply across every team. Reporting shares privacy-safe activity, not private conversations, and does not grant system access.</p>
<p><a href="#map">Open the operational dependency map →</a> <span class="mut">Agents, Discord channels, triggers and systems—not just reporting lines.</span></p>
<p id="hierarchy-stage" role="status" hidden></p>
<div class="htools"><span id="hierarchy-count" class="mut" role="status">Loading configured roster…</span><button id="hierarchy-expand" class="back">Expand all</button><button id="hierarchy-collapse" class="back">Collapse all</button></div></div>
<p id="hierarchy-error" role="alert" hidden></p><div id="hierarchy-tree" aria-label="Configured agent reporting structure"></div>
</section>
<section id="v-voice" hidden><div class="vroom"><div class="chart vstage" id="vstage">
<img id="vav" alt=""><div><div class="name" id="vname" style="font-size:20px"></div><div class="role" id="vrole"></div></div><button id="vpreview" class="back" title="Preview the character without opening your microphone or starting a call">Preview character</button>
<div id="vstatus" class="mut" role="status">Pick an agent and press Talk. Audio goes straight from this device to OpenAI during calls.</div>
<div class="vbtns vprimary"><button id="vtalk" class="go">🎙️ Talk</button><button id="vend" class="stop" disabled>📴 Hang up</button><button id="vwall" title="Show every agent as a 3D carousel">👥 Team wall</button><select id="vengine" title="Which voice engine to use. Local = speech is recognised and spoken on your own PC; only text goes to OpenAI." aria-label="Voice engine"><option value="openai">☁️ OpenAI voice</option><option value="local">🖥️ Local voice (my PC)</option></select></div>
<div class="vbtns"><button id="vmute" disabled>🔇 Mute mic</button><button id="vcut" disabled title="Stop the agent mid-sentence (or press Esc)">✋ Stop talking</button></div>
<div class="vbtns"><button id="vmicresume" disabled title="Stop agent playback, unmute and restore your live microphone">🎙️ Listen now</button></div>
<button id="vaudiosettings" class="vaudio-summary"><small>Settings</small><strong>Microphone</strong><span id="vmiclabel">System default</span></button>
<div id="vaudiostatus" class="mut" role="status">Uses the input configured on this device. Test it locally before calling.</div>
<div class="vbtns vsecondary"><button id="vbuddy" title="Floating animated agent that follows you around the portal during a call">🧚 Companion</button><button id="vcam" title="Webcam: your expression is read ON THIS DEVICE (no video is sent) so the agent can adjust its tone. Turns off when the call ends.">📷 Camera: off</button><button id="vshow" disabled title="Send ONE webcam snapshot to the agent (goes to OpenAI)">📸 Show</button><label class="mut" style="font-size:12px;display:inline-flex;gap:4px;align-items:center" title="With speakers, use Stop or Esc to interrupt. Wearing headphones? Tick this to keep the mic open and talk over the agent."><input type="checkbox" id="vduplex">🎧 Headphones (talk over the agent)</label></div>
<div class="vmeter" title="mic level"><i id="vlvl"></i></div><div id="vcamw" hidden><video id="vcamv" muted playsinline></video><span><span class="camdot">● camera on</span> · reading on this PC only<br><b id="vmood">…</b></span></div><div id="vcost" class="mut" style="font-size:12px"></div><div id="vwarn"></div></div>
<div class="chart"><div class="pickhead"><h3 class="sec">Summon an agent</h3><span id="vcount" class="mut"></span></div><input id="vsearch" type="search" placeholder="Find an agent by name, role or skill" aria-label="Search agents in the selected group"><div id="vpick" class="vpick"></div>
<p class="mut" style="font-size:12px;margin-bottom:0">Talk naturally; they answer in their own voice. Ask for someone else ("let me talk to Sellen") and they hand you over. Real work is delegated to the agent and posted in #roundtable-hold; changes still need your ✅ in Discord. Say "goodbye" to hang up. Every call is transcribed into #roundtable-hold and charged to the voice cap.</p></div></div>
<div class="chart"><h3 class="sec">Transcript</h3><div id="vlog" class="vlog" role="log" aria-label="Voice conversation"><p class="sys">No conversation yet</p></div></div></section>
<section id="v-map" hidden><div class="chart"><h2>Dependency map</h2><p class="mut">Operational connections between you, the agents, Discord channels and systems, grouped like the Hermes Lab Atlas: links between two groups merge into one line with a count; click a tile or a line for details, or switch to Focus. <a href="#hierarchy">Reporting hierarchy</a> is available separately.</p><button class="back" id="map-refresh">Refresh map</button> <label class="mut"><input type="checkbox" id="map-reporting"> Include reporting lines</label> <a class="mut" id="map-open" href="/depmap" target="_blank" rel="noopener">Open full screen ↗</a></div><iframe id="depmap" title="Dependency map" style="width:100%;height:calc(100vh - 230px);min-height:560px;border:1px solid var(--line);border-radius:var(--r,14px);background:transparent"></iframe></section>
<section id="v-overview" hidden><div class="dihead"><div><h2><span class="ovemoji">🧭 </span>Overview</h2><p class="mut">Diagrams, dashboards and every homelab site in one place. Dots show whether each site answers right now (checked by the portal, refreshed every 2 minutes).</p></div><div class="ovqwrap"><input id="ovq" type="search" placeholder="Filter sites…" aria-label="Filter sites"></div></div><div id="ovstrip" class="ovstrip"></div><div id="ovdns"></div><div id="ovlist"></div></section>
<section id="v-ipmap" hidden><div class="dihead"><div><h2>🧭 IP map</h2><p class="mut">Every assigned address and the app or service using it. Diallos keeps it current from Proxmox, Omada, IPAM, Traefik and what agents record when they add a service.</p></div>
<div class="dinav"><input id="ipq" type="search" placeholder="Search IP, name, URL…" aria-label="Search the IP map"><select id="ipsort" aria-label="Sort the IP map" title="Sort (or click the IP / Service column header; click again to reverse)"><option value="ip">Sort: IP address</option><option value="name">Sort: name</option></select><button class="back" id="iprefresh" title="Re-read every source">↻</button></div></div>
<div id="ipsum"></div><div class="ipuse" id="ipuse"></div><div class="ipf" id="ipvlans"></div><div id="iplist"></div></section>
<section id="v-profile" hidden><div id="profile"></div></section>
<section id="v-runs" hidden><div class="filters"><select id="fAgent"><option value="">All agents</option></select>
<select id="fStatus"><option value="">Any status</option><option>ok</option><option>error</option><option>skipped</option><option>running</option></select></div>
<div style="overflow:auto;max-height:75vh"><table><thead><tr><th>#</th><th>Agent</th><th>Task</th><th>Trigger</th><th>Started</th><th>Took</th><th>Status</th><th>Model</th><th class="num">In</th><th class="num">Cached</th><th class="num">Out</th><th class="num">Web</th><th class="num">Cost</th></tr></thead><tbody id="runs"></tbody></table></div></section>
<section id="v-approvals" hidden><p id="apmsg" class="mut" role="status">Approve or reject here, or with ✅/❌ in Discord. Gideon applies a portal decision within about 15 seconds and notes it in the approval's Discord thread.</p><table><thead><tr><th>#</th><th>Agent</th><th>Title</th><th>Severity</th><th>Status</th><th>Window</th><th>Created</th><th>Finished</th><th>Decide</th></tr></thead><tbody id="aps"></tbody></table></section>
<section id="v-cost" hidden><div class="chart"><b>Daily cost · last 30 days</b><div id="chart"></div><div class="legend" id="legend"></div></div>
<table><thead><tr><th>Agent</th><th class="num">Runs (30d)</th><th class="num">Errors</th><th class="num">Input tok</th><th class="num">Cached</th><th class="num">Output tok</th><th class="num">Reasoning</th><th class="num">Cost (30d)</th><th class="num">Avg / run</th></tr></thead><tbody id="costtbl"></tbody></table></section>
</main>
<div id="modal"><div class="box" id="mbox"></div></div>
<dialog id="vaudio" aria-labelledby="vaudiotitle"><div class="audio-head"><h2 id="vaudiotitle">Microphone settings</h2><button id="vaudioclose" aria-label="Close microphone settings">Close</button></div>
<label for="vmicsel">Input on this device</label><select id="vmicsel"><option value="">System default</option></select>
<p class="mut" style="font-size:12px">System default follows your device's configured input. A specific choice is remembered only in this browser or companion. Avoid loopback or mix inputs that include the agent's audio.</p>
<div class="audio-actions"><button id="vmictest">Test microphone</button><button id="vmicrefresh">Refresh inputs</button></div>
<meter id="vmictestlevel" min="0" max="100" value="0" aria-label="Microphone input level"></meter>
<p id="vmicnote" class="mut" role="status">Testing stays on this device. No audio is uploaded and no paid call is started.</p></dialog>
<div id="wall" role="dialog" aria-label="Homelab team" hidden><button id="wallx" type="button" onclick="wallHide()">✖ Dismiss</button><h2>🛡️ The team is here</h2><div class="wsub">Hover or use ←/→ to bring an agent to the front, then click the front agent to talk to them. Say "dismiss", or press Esc, to close.</div><div id="wgrid"></div><div id="wlabel" aria-live="polite"></div></div>
<div id="buddy" hidden><div class="bwrap" id="bwrap"><div class="bubble" id="bbub"></div><div class="aura"></div><div class="ring"></div>
<div class="bdepth"><div class="bbody"><img id="bimg" alt="" draggable="false"></div></div><div class="bname" id="bname"></div>
<div class="btools"><button id="dtalk" class="donly" title="Talk (or double-click me · Ctrl+Alt+T)">📞</button><button id="dagents" class="donly" title="Switch agent">👥</button><button id="dteam" class="donly" title="Summon the team: roll call (Ctrl+Alt+G)">🛡️</button><button id="dmic" class="donly" title="Microphone settings">🎙️</button><button id="bdepthtoggle" aria-label="Avatar depth and motion" title="Toggle 2.5D avatar depth and motion">✨</button><button id="bcut" title="Stop talking (or just talk over it)">✋</button><button id="bmute" title="Mute / unmute mic">🎙️</button><button id="bfull" title="Full-body / compact">🧍</button><button id="bpip" title="Browser pop-out (Chrome/Edge; closes with this tab)">📌</button><button id="bmin" title="Shrink / expand">➖</button><button id="bend" title="Hang up">📴</button><button id="dclose" class="donly" title="Hide (Ctrl+Alt+A or the tray icon brings me back)">✖</button></div></div>
<div class="shadow"></div></div><div id="dpanel"></div><div id="dnote" role="alert"></div>
<footer>Read-only · data from ~/.olympus/olympus.db on codex-agent-lxc · refreshes every 30 s</footer>
<script>
const DESK=new URLSearchParams(location.search).has('desk');if(DESK)document.documentElement.classList.add('desk');
const $=s=>document.querySelector(s), esc=s=>(s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const DEMO=localStorage.getItem('persona')==='demo';
// Demo mode swaps the Elden Ring art for the realistic demo personas wherever an agent carries avatar_demo.
function persona(d){if(!DEMO)return d;(function w(x){if(Array.isArray(x))x.forEach(w);else if(x&&typeof x=='object'){
 if(x.avatar_demo){x.avatar=x.avatar_demo;if(x.avatar_full_demo)x.avatar_full=x.avatar_full_demo;else delete x.avatar_full;if(x.anim_demo)x.anim=x.anim_demo;else delete x.anim}
 for(const k in x)if(x[k]&&typeof x[k]=='object')w(x[k])}})(d);return d}
const J=u=>fetch(u).then(r=>r.json()).then(persona);
$('#persona').textContent=DEMO?'🎭 Demo':'🎭 Daily';$('#persona').setAttribute('aria-pressed',DEMO);
$('#persona').onclick=()=>{localStorage.setItem('persona',DEMO?'daily':'demo');location.reload()};
const money=x=>x==null?'—':(x<0.1?'$'+x.toFixed(4):'$'+x.toFixed(2));
const num=x=>(x||0).toLocaleString();
const hex=c=>'#'+(c||0x888888).toString(16).padStart(6,'0');
function ago(t){if(!t)return 'never';const s=(Date.now()-Date.parse(t))/1000,f=Math.abs(s);let v=f<60?Math.round(f)+'s':f<3600?Math.round(f/60)+'m':f<86400?(f/3600).toFixed(1)+'h':(f/86400).toFixed(1)+'d';return s>=0?v+' ago':'in '+v}
const fmt=t=>t?new Date(t).toLocaleString('en-PH',{timeZone:'Asia/Manila',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
let AG=[];
let VIEW='agents',PID=null;
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{if(b.dataset.v=='office'){location.href='/office';return}location.hash=b.dataset.v});
function route(){const h=location.hash.slice(1)||'overview';if(h.startsWith('agent/')){VIEW='profile';PID=h.slice(6)}else if(h.startsWith('voice')){VIEW='voice';PID=h.slice(6)||null}else{VIEW=h;PID=null}
 if(!document.getElementById('v-'+VIEW))VIEW='agents';
 document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x.dataset.v==VIEW||(VIEW=='profile'&&x.dataset.v==grpView(PID))));
 document.querySelectorAll('main section').forEach(s=>s.hidden=s.id!='v-'+VIEW);window.scrollTo(0,0);load()}
window.addEventListener('hashchange',route);
// every agent in this repo is a homelab agent
const GV=g=>'agents',GSEL={agents:['#slegend','#agents','homelab']};
const grpView=id=>GV((AG.find(a=>a.id==id)||{}).group);
$('#modal').onclick=e=>{if(e.target.id=='modal')$('#modal').style.display='none'};
async function showRun(id){const r=await J('/api/runs/'+id);$('#mbox').innerHTML=`<h3>Run #${r.id} · ${esc(r.agent)}</h3>
<dl><dt>Task</dt><dd>${esc(r.task)}</dd><dt>Trigger</dt><dd>${esc(r.trigger)}</dd><dt>Started</dt><dd>${fmt(r.started)}</dd><dt>Ended</dt><dd>${fmt(r.ended)}</dd>
<dt>Status</dt><dd class="${r.status}">${r.status}</dd><dt>Model</dt><dd>${esc(r.model)||'no LLM'}</dd><dt>Tokens</dt><dd>${num(r.input_tokens)} in (${num(r.cached_tokens)} cached) · ${num(r.output_tokens)} out (${num(r.reasoning_tokens)} reasoning) · ${r.calls} calls · ${r.web_calls} web</dd>
<dt>Cost</dt><dd>${money(r.cost_usd)}</dd></dl><h4>Summary</h4><pre>${esc(r.summary)||'—'}</pre>${r.detail?'<h4>Detail</h4><pre>'+esc(r.detail)+'</pre>':''}`;$('#modal').style.display='flex'}
async function loadSummary(){const s=await J('/api/summary');$('#team').textContent=s.team;$('#host').textContent=s.host||'';
 const pct=Math.min(100,s.spent/s.cap*100),col=pct>=100?'var(--bad)':pct>=80?'var(--warn)':'var(--ok)';
 $('#kpis').innerHTML=`<div class="kpi"><b>${money(s.spent)} / $${s.cap}</b><span title="${s.bill?`OpenAI bill ${money(s.bill.usd)} (${esc(s.bill.source)}, ${fmt(s.bill.at)}) plus later ledger runs. Ledger alone: ${money(s.ledger)}`:'Recorded ledger only (no OpenAI bill figure yet)'}. Forecast = spend so far + ${money(s.run_rate_day)}/day (last 7 days, one-off manual jobs left out).">${esc(s.month)} ${s.bill?'OpenAI bill':'ledger spend'} · forecast ${money(s.forecast)}</span><div class="bar"><i style="width:${pct}%;background:${col}"></i></div></div>
 <div class="kpi"><b>${num(s.month_runs)}</b><span>runs this month</span></div><div class="kpi"><b>${num(s.month_tokens)}</b><span>tokens this month</span></div>
 <div class="kpi"><b>${s.open_approvals}</b><span>open approvals</span></div>`;$('#apcount').textContent=s.open_approvals||'';return s}
const SLBL={running:'Running',idle:'Idle',stopped:'Stopped',issue:'Issue'};
const sdot=(a,big)=>{const s=a.state||'idle';return `<span class="sd s-${s}${big?' big':''}" title="${SLBL[s]}: ${esc(a.state_why||'')}">${s=='issue'?'!':''}</span>`};
const spill=a=>{const s=a.state||'idle';return `<span class="pill s-${s}" title="${esc(a.state_why||'')}">${SLBL[s]}</span>`};
// Homelab agents page: one box per function (agents.json "function"), names A-Z inside each box
const FUNCS=[['leadership','🧭 Leadership'],['security','🛡️ Security'],['governance','📋 Governance, compliance & FinOps'],['docs','📚 Documentation & research'],['ops','⚙️ Infrastructure & operations'],['network','🌐 Network & addressing']];
function agentBoxes(L,card){const by=n=>L.filter(a=>(FUNCS.some(f=>f[0]==a.function)?a.function:'other')==n).sort((x,y)=>x.name.localeCompare(y.name,undefined,{sensitivity:'base'}));
 return [...FUNCS,['other','Other']].map(([k,lb])=>{const g=by(k);return g.length?`<section class="fbox"><h3>${lb} <span class="mut">· ${g.length}</span></h3><div class="grid">${g.map(card).join('')}</div></section>`:''}).join('')}
async function loadAgents(){AG=await J('/api/agents');const GQ=GV(VIEW),L=AG.filter(a=>GV(a.group)==GQ);
 const cnt=s=>L.filter(a=>(a.state||'idle')==s).length;
 $(GSEL[GQ][0]).innerHTML=['running','idle','stopped','issue'].map(s=>`<span>${sdot({state:s,state_why:''})} ${SLBL[s]} · ${cnt(s)}</span>`).join('');
 const card=a=>`<div class="card" style="--c:${hex(a.color)}" onclick="openProfile('${a.id}')" title="Open ${esc(a.name)}'s profile">
 <div class="top"><div class="who"><span class="avw"><img class="av" src="${a.avatar||''}" alt="" loading="lazy">${sdot(a)}</span><div><div class="name">${a.emoji} ${esc(a.name)}</div><div class="role">${esc(a.role)}</div></div></div>${spill(a)}</div>
 ${a.state=='issue'||a.state=='stopped'?`<div class="does" style="color:var(--bad)">${a.state=='issue'?'⚠️':'⏹️'} ${esc(a.state_why)}</div>`:''}
 <div class="char">${esc(a.character)}</div><div class="does">${esc(a.does)}</div>
 <div class="chips">${(a.skills||[]).map(s=>`<span class="chip">${esc(s)}</span>`).join('')}</div>
 <dl><dt>Schedule</dt><dd>${esc(a.schedule)}</dd><dt>Model</dt><dd title="${esc(a.models)}">${esc(a.model)} <span class="mut">ⓘ</span></dd>
 <dt>Last ran</dt><dd>${a.last_run?ago(a.last_run)+' · '+fmt(a.last_run):'never'}${a.last_status?` <span class="${a.last_status}">(${a.last_status})</span>`:''}</dd>
 <dt>Next run</dt><dd>${a.next_run?ago(a.next_run)+' · '+fmt(a.next_run):(a.on_demand?'on demand':'—')}</dd>
 <dt>30 days</dt><dd>${a.stats30.runs} runs · ${a.stats30.errors} errors · ${num(a.stats30.input+a.stats30.output)} tok · ${money(a.stats30.cost)}</dd>
 <dt>Channel</dt><dd>#${esc(a.channel)}</dd><dt>Units</dt><dd>${Object.entries(a.units_state).map(([n,u])=>`<span class="chip" title="${esc(u.active)}/${esc(u.sub)}">${esc(n)} <span class="${u.active=='failed'?'failed':u.active=='active'?'ok':'idle'}">●</span></span>`).join(' ')||'—'}</dd></dl>
 ${a.last_summary?`<div class="last" ${a.last_run_id?`onclick="event.stopPropagation();showRun(${a.last_run_id})" title="open run"`:''}>${esc(a.last_summary)}</div>`:''}</div>`;
 $(GSEL[GQ][1]).innerHTML=GQ=='agents'?agentBoxes(L,card):L.map(card).join('');
 const f=$('#fAgent');if(f.options.length<2)AG.forEach(a=>f.add(new Option(a.emoji+' '+a.name,a.id)))}
async function loadRuns(){const rs=await J(`/api/runs?limit=300&agent=${$('#fAgent').value}&status=${$('#fStatus').value}`);
 $('#runs').innerHTML=rs.map(r=>{const took=r.ended?Math.round((Date.parse(r.ended)-Date.parse(r.started))/1000):null;
 return `<tr class="click" onclick="showRun(${r.id})"><td>${r.id}</td><td>${esc(r.agent)}</td><td title="${esc(r.summary)}">${esc(r.task).slice(0,90)}</td><td>${esc(r.trigger)}</td><td>${fmt(r.started)}</td><td>${took==null?'…':took<120?took+'s':Math.round(took/60)+'m'}</td><td class="${r.status}">${r.status}</td><td>${esc(r.model)||'—'}</td><td class="num">${num(r.input_tokens)}</td><td class="num">${num(r.cached_tokens)}</td><td class="num">${num(r.output_tokens)}</td><td class="num">${r.web_calls||''}</td><td class="num">${money(r.cost_usd)}</td></tr>`}).join('')||'<tr><td colspan=13 class="mut">No runs</td></tr>'}
function apActions(a){if(a.queued)return '<span class="mut">⏳ queued for Gideon</span>';
 if(a.status=='pending')return `<button type="button" class="apb ok" onclick="apDecide(${a.id},'approve')">✅ Approve</button> <button type="button" class="apb" onclick="apDecide(${a.id},'reject')">❌ Reject</button>`;
 if(a.status=='scheduled')return `<button type="button" class="apb" onclick="apDecide(${a.id},'run_now')" title="Skip the maintenance window">▶ Run now</button>`;return '—'}
async function loadAps(){const as=await J('/api/approvals');APS=as;$('#aps').innerHTML=as.map(a=>`<tr title="${esc(a.result)}"><td>${a.id}</td><td>${esc(a.agent)}</td><td>${esc(a.title)}</td><td>${esc(a.severity)}</td><td class="${a.status=='done'?'ok':a.status=='failed'?'error':a.status=='running'?'running':'idle'}">${a.status}</td><td>${a.not_before?fmt(a.not_before):'—'}</td><td>${fmt(a.created)}</td><td>${fmt(a.finished)}</td><td>${apActions(a)}</td></tr>`).join('')||'<tr><td colspan=9 class="mut">No approvals yet</td></tr>'}
let APS=[];
// Owner approval from the portal: queued server-side (SSO approvers only); Gideon runs it like a Discord ✅
async function apDecide(id,decision){const a=APS.find(x=>x.id==id)||{},verb={approve:'Approve',reject:'Reject',run_now:'Run now'}[decision];
 if(!confirm(`${verb} approval #${id}?\n\n${a.title||''}\n\n${decision=='reject'?'Nothing will be changed.':'The agent will make this change.'}`))return;
 const r=await post('/api/approvals/decide',{id,decision}).catch(e=>({_s:0,error:e.message}));
 $('#apmsg').textContent=r._s==202?`#${id}: ${verb.toLowerCase()} sent. ${r.note}.`:`⚠️ #${id}: ${r.error||'could not send the decision'}`;
 await loadAps();if(r._s==202)setTimeout(()=>{if(!$('#v-approvals').hidden)loadAps()},17000)}
async function loadCost(s){if(!AG.length)await loadAgents();const col={};AG.forEach(a=>col[a.id]=hex(a.color));
 const days=[];for(let i=29;i>=0;i--){const d=new Date(Date.now()+8*3600e3-i*864e5);days.push(d.toISOString().slice(0,10))}
 const by={};s.daily.forEach(r=>{(by[r.d]=by[r.d]||{})[r.agent]=r.c});const mx=Math.max(0.0001,...days.map(d=>Object.values(by[d]||{}).reduce((a,b)=>a+b,0)));
 const W=900,H=180,bw=W/30;let svg=`<svg viewBox="0 0 ${W} ${H+20}" width="100%" style="max-height:240px">`;
 days.forEach((d,i)=>{let y=H;Object.entries(by[d]||{}).forEach(([ag,c])=>{const h=c/mx*(H-10);y-=h;svg+=`<rect x="${i*bw+2}" y="${y}" width="${bw-4}" height="${h}" fill="${col[ag]||'#888'}"><title>${d} ${ag}: $${c.toFixed(4)}</title></rect>`});
 if(i%5==0)svg+=`<text x="${i*bw+2}" y="${H+14}" style="fill:var(--mut)" font-size="10">${d.slice(5)}</text>`});
 svg+=`<text x="${W-4}" y="10" style="fill:var(--mut)" font-size="10" text-anchor="end">max ${money(mx)}/day</text></svg>`;$('#chart').innerHTML=svg;
 $('#legend').innerHTML=AG.map(a=>`<span><i style="background:${hex(a.color)}"></i>${esc(a.name)}</span>`).join('');
 $('#costtbl').innerHTML=AG.map(a=>{const s=a.stats30;return `<tr><td>${a.emoji} ${esc(a.name)}</td><td class="num">${s.runs}</td><td class="num">${s.errors}</td><td class="num">${num(s.input)}</td><td class="num">${num(s.cached)}</td><td class="num">${num(s.output)}</td><td class="num">${num(s.reasoning)}</td><td class="num">${money(s.cost)}</td><td class="num">${s.runs?money(s.cost/s.runs):'—'}</td></tr>`}).join('')}
async function load(){const s=await loadSummary();const v=VIEW;
 if(v=='agents')loadAgents();else if(v=='runs')loadRuns();else if(v=='approvals')loadAps();else if(v=='map')loadMap();
 else if(v=='hierarchy')await loadHierarchy();else if(v=='ipmap')loadIpmap();else if(v=='profile')loadProfile(PID);else if(v=='voice')loadVoice(PID);else if(v=='overview')loadOverview();else{AG=await J('/api/agents');loadCost(s)}}
// ---------------------------------------------------------------- Overview: Atlas, Glance and every lab site
const OV={d:null,q:'',dns:null};
async function loadOverview(){if(!OV.d)$('#ovlist').innerHTML='<p class="diwait"><i></i>Checking every site…</p>';
 fetch('/api/dnschecks').then(r=>r.json()).then(d=>{OV.dns=d;ovDns()}).catch(e=>{OV.dns={ok:false,error:e.message,checks:[]};ovDns()});
 try{const r=await fetch('/api/links');OV.d=await r.json();if(!r.ok)throw new Error(OV.d.error||r.status)}catch(e){if(!OV.d)$('#ovlist').innerHTML=`<div class="chart">⚠️ Could not load the links: ${esc(e.message)}</div>`;return}
 ovDraw()}
function ovDraw(){const d=OV.d;if(!d)return;const q=OV.q.toLowerCase();const ST={up:'answering',down:'not answering',check:'answers 404',cert:'answering, certificate not trusted'};
 const SL={up:'Online',down:'Offline',check:'Check',cert:'Cert issue'};
 const ext=u=>u.startsWith('/')?'':' target="_blank" rel="noopener"';
 const host=u=>u.startsWith('/')?'roundtable'+u:u.replace(/^https?:\/\//,'').replace(/\.hrmsmrflrii\.xyz$/,'');
 const dot=l=>`<i class="ovd ${esc(l.status)}" title="${esc(ST[l.status]||l.status)}"></i>`;
 const hit=l=>!q||(l.name+' '+l.desc+' '+l.url).toLowerCase().includes(q);
 const all=d.groups.flatMap(g=>g.links),n=s=>all.filter(l=>l.status==s).length,up=n('up'),warn=n('cert')+n('check'),down=n('down');
 const ago=Math.max(0,Math.round(Date.now()/1000-d.checked));
 $('#ovstrip').innerHTML=`<div><small>Sites online</small><b>${up}<span class="mut" style="font-size:13px">/${all.length}</span></b><div class="ovmeter">${all.map(l=>`<i class="${l.status=='up'?'':l.status=='down'?'x':'w'}" title="${esc(l.name)}"></i>`).join('')}</div></div>
  <div><small>Warnings</small><b class="${warn?'w':''}">${warn}</b></div><div><small>Offline</small><b class="${down?'x':''}">${down}</b></div>
  <div><small>Last checked</small><b style="font-size:18px">${ago<60?ago+'s':Math.round(ago/60)+'m'} ago</b></div>`;
 let h='',k=0;d.groups.forEach((g,gi)=>{const ls=g.links.filter(hit);if(!ls.length)return;
  if(gi==0&&!q){h+=`<div class="ovhero">${ls.map((l,i)=>`<a href="${esc(l.url)}"${ext(l.url)}>${dot(l)}<span class="ovc">${String(i+1).padStart(2,'0')} · ${SL[l.status]||''}</span><span class="ovi">${l.icon}</span><b>${esc(l.name)}</b><small>${esc(l.desc)}</small><span class="ovu">↗ ${esc(host(l.url))}</span></a>`).join('')}</div>`;return}
  h+=`<div class="ovg"><h3>${esc(g.name)} <em>${ls.filter(l=>l.status=='up').length}/${ls.length}</em></h3><div class="ovgrid">${ls.map(l=>`<a href="${esc(l.url)}"${ext(l.url)} title="${esc(l.url)}"><span class="ovi">${l.icon}</span><span><b>${esc(l.name)}</b><small>${esc(l.desc)} · ${esc(host(l.url))}</small></span><span class="ovst">${SL[l.status]||''}</span>${dot(l)}</a>`).join('')}</div></div>`});
 $('#ovlist').innerHTML=h||'<p class="mut">No site matches that filter.</p>'}
function ovDns(){const d=OV.dns;if(!d)return;const ST={1:'up',0:'down',2:'check',3:'check'},SN={1:'OK',0:'Failing',2:'Pending',3:'Maintenance'};
 if(!d.checks.length){$('#ovdns').innerHTML=`<div class="ovdns"><h3>🧭 DNS checks · Uptime Kuma</h3><p class="mut" style="margin:0">${d.ok?'No DNS checks in Uptime Kuma yet.':'Could not read the checks: '+esc(d.error||'Uptime Kuma is not being scraped')}</p></div>`;return}
 const by={};d.checks.forEach(c=>(by[c.server]=by[c.server]||[]).push(c));const bad=d.checks.filter(c=>c.status==0).length;
 const role=s=>/technitium/i.test(s)?'primary · 192.168.90.60':/pi-hole/i.test(s)?'secondary · 192.168.90.53':'';
 $('#ovdns').innerHTML=`<div class="ovdns"><h3>🧭 DNS checks · Uptime Kuma <em class="mut" style="font-style:normal">${d.checks.length-bad}/${d.checks.length} passing</em><a href="${esc(d.url)}" target="_blank" rel="noopener">Open Uptime Kuma ↗</a></h3><div class="dg">${Object.entries(by).map(([s,cs])=>`<div class="ds"><b>${esc(s)} <small class="mut" style="font-weight:400">${esc(role(s))}</small></b>${cs.map(c=>`<div class="dr"><i class="ovd ${ST[c.status]||'check'}" title="${SN[c.status]||c.status}"></i>${esc(c.what)}<small>${c.status==1&&c.ms!=null?Math.round(c.ms)+' ms':SN[c.status]||''}</small></div>`).join('')}</div>`).join('')}</div></div>`}
$('#ovq').oninput=e=>{OV.q=e.target.value.trim();ovDraw()};
document.addEventListener('keydown',e=>{if(e.key=='/'&&VIEW=='overview'&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){e.preventDefault();$('#ovq').focus()}});
// Theme picker: handled by /theme-kit.js (shared with /office and /atlas)
// ---------------------------------------------------------------- IP map (Diallos: every assigned address and its service)
const IPM={d:null,vlan:'all',st:'',q:'',sort:'ip',dir:1};
try{const s=(localStorage.getItem('rt-ipsort')||'').split(':');if(s[0]=='ip'||s[0]=='name'){IPM.sort=s[0];IPM.dir=s[1]=='-1'?-1:1}}catch(e){}
const ipn=ip=>String(ip).split('.').reduce((a,x)=>a*256+(+x||0),0);
function ipSort(rows){const dr=IPM.dir;return rows.slice().sort((a,b)=>{if(IPM.sort=='name'){const x=(a.service||'').trim(),y=(b.service||'').trim();if(!x!=!y)return x?-1:1;return (x.localeCompare(y,undefined,{numeric:true,sensitivity:'base'})||ipn(a.ip)-ipn(b.ip))*dr}return (ipn(a.ip)-ipn(b.ip))*dr})}
function ipSetSort(k,toggle){if(toggle&&IPM.sort==k)IPM.dir=-IPM.dir;else{IPM.sort=k;IPM.dir=1}try{localStorage.setItem('rt-ipsort',IPM.sort+':'+IPM.dir)}catch(e){}ipDraw()}
const ipth=(k,label)=>`<th data-sort="${k}" class="ipsorth${IPM.sort==k?' on':''}" title="Sort by ${k=='ip'?'IP address':'name'}">${label}${IPM.sort==k?(IPM.dir>0?' ▲':' ▼'):''}</th>`;
const IPST={ok:['documented','var(--ok)'],undocumented:['undocumented','var(--warn)'],reserved:['reserved','#5dade2'],'not-seen':['not seen','#8a90a2'],retired:['retired','#8a90a2'],'retired-but-seen':['retired but seen','var(--bad)']};
const ipst=s=>{const [t,c]=IPST[s]||[s,'#8a90a2'];return `<span class="ipst" style="color:${c}">${esc(t)}</span>`};
setInterval(()=>{if(VIEW=='ipmap'&&!document.hidden)loadIpmap(false,true)},60000);   // usage follows the 15-min live re-read
async function loadIpmap(refresh,quiet){if(!quiet&&(refresh||!IPM.d))$('#iplist').innerHTML='<p class="diwait"><i></i>Reading Proxmox, Omada, IPAM, Traefik and the vault…</p>';
 try{const r=await fetch('/api/ipmap'+(refresh?'?refresh=1':''));const d=await r.json();if(!r.ok)throw new Error(d.error||r.status);IPM.d=d}catch(e){if(!quiet)$('#iplist').innerHTML=`<div class="chart">⚠️ Could not load the IP map: ${esc(e.message)}</div>`;return}
 ipDraw()}
function ipDraw(){const d=IPM.d;if(!d)return;const c=d.counts||{};
 const k=(st,label,n)=>`<button class="dikpi${IPM.st==st?' on':''}" data-st="${st}"><div class="mut" style="font-size:11.5px">${label}</div><b style="font-size:20px">${n||0}</b></button>`;
 $('#ipsum').innerHTML=`<div class="ipkpis">${k('','All addresses',d.total)}${k('ok','Documented',c.ok)}${k('undocumented','Undocumented',c.undocumented)}${k('reserved','Reserved',c.reserved)}${k('not-seen','Not seen live',c['not-seen'])}</div>
  <p class="mut" style="font-size:12px;margin:-4px 0 10px">As of ${esc(new Date(d.as_of).toLocaleString())} · used/free per VLAN counts what live sources and connected clients show; re-read every 15 min${(d.errors||[]).length?` · ⚠️ ${d.errors.map(esc).join(' · ')}`:''}</p>`;
 const pc=(n,t)=>t?Math.round(n*100/t):0;
 $('#ipuse').innerHTML=(d.usage||[]).map(u=>{const p=pc(u.used,u.size),col=p>=90?'var(--bad)':p>=75?'var(--warn)':'var(--ok)';
  return `<button class="${IPM.vlan==u.vlan?'on':''}" data-vl="${u.vlan}" title="Show VLAN ${u.vlan} addresses"><div class="mut" style="font-size:11.5px">VLAN ${u.vlan} · ${esc(u.name||'')} · ${esc(u.subnet)}</div>
  <div><b style="font-size:20px">${u.used}</b> <span class="mut">used</span> · <b style="font-size:20px">${u.free}</b> <span class="mut">free of ${u.size}</span></div>
  <div class="ub" title="${p}% used"><i style="width:${pc(u.static.used,u.size)}%;background:${col}"></i><i style="width:${pc(u.pool.used,u.size)}%;background:#5dade2"></i></div>
  <div class="un mut">Static: ${u.static.used} used, <b>${u.static.free}</b> free of ${u.static.size}${u.pool.size?`<br>DHCP pool ${esc(u.pool.range)}: ${u.pool.used} leased/reserved, ${u.pool.free} free`:'<br>No DHCP pool'}</div></button>`}).join('');
 $('#ipvlans').innerHTML=['all',...d.vlans].map(v=>`<button class="${IPM.vlan==v?'on':''}" data-vl="${v}">${v=='all'?'All VLANs':'VLAN '+v}</button>`).join('');
 const so=$('#ipsort');if(so&&so.value!=IPM.sort)so.value=IPM.sort;
 const q=IPM.q.toLowerCase();
 const rows=ipSort(d.rows).filter(r=>(IPM.vlan=='all'||r.vlan==IPM.vlan)&&(!IPM.st||r.status==IPM.st)&&(IPM.st||r.status!='retired'||q)&&
  (!q||[r.ip,r.service,r.purpose,...r.names,...r.urls].join(' ').toLowerCase().includes(q)));
 $('#iplist').innerHTML=rows.length?`<div class="chart" style="padding:4px 6px"><table class="iptab"><thead><tr>${ipth('ip','IP')}${ipth('name','Service / app')}<th>Purpose</th><th>URLs</th><th>Seen in</th><th>Status</th></tr></thead><tbody>${rows.map(r=>{const other=r.names.filter(n=>n!=r.service);return `<tr>
  <td>${esc(r.ip)}</td><td data-l="Service"><b>${esc(r.service||'—')}</b>${other.length?`<div class="ipnote">${other.slice(0,3).map(esc).join(' · ')}</div>`:''}</td>
  <td data-l="Purpose">${esc(r.purpose||'—')}${r.purpose_by=='AI suggestion'?' <span class="ipai">✨ AI suggestion</span>':r.purpose_by&&r.purpose_by!='vault'?` <span class="ipai">· recorded by ${esc(r.purpose_by)}</span>`:''}${r.notes.length?`<div class="ipnote">${r.notes.slice(0,2).map(esc).join(' · ')}</div>`:''}</td>
  <td data-l="URLs">${r.urls.map(u=>/^https:\/\/[\w.-]+$/.test(u)?`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace('https://',''))}</a>`:esc(u)).join('<br>')||'<span class="mut">—</span>'}</td>
  <td data-l="Seen in"><div class="ipsrc">${r.sources.map(s=>`<span>${esc(s)}</span>`).join('')}</div></td><td>${ipst(r.status)}</td></tr>`}).join('')}</tbody></table></div>`
  :'<div class="chart mut">No addresses match.</div>'}
document.addEventListener('click',e=>{if(e.target.id=='iprefresh')return loadIpmap(true);const th=e.target.closest('#iplist th[data-sort]');if(th)return ipSetSort(th.dataset.sort,true);const b=e.target.closest('#ipvlans button,#ipuse button,#ipsum button[data-st]');if(!b)return;
 if(b.dataset.vl)IPM.vlan=b.dataset.vl=='all'?'all':+b.dataset.vl;else IPM.st=b.dataset.st;ipDraw()});
document.addEventListener('input',e=>{if(e.target.id=='ipq'){IPM.q=e.target.value;ipDraw()}});
document.addEventListener('change',e=>{if(e.target.id=='ipsort')ipSetSort(e.target.value,false)});
// ---------------------------------------------------------------- reporting hierarchy (registry metadata only)
let HIERARCHY_SIGNATURE='';
function hierarchyTree(agents){
 if(!Array.isArray(agents)||!agents.length)throw new Error('The configured agent roster is empty or unavailable.');
 const nodes=new Map();
 for(const a of agents){
  if(!a.id||nodes.has(a.id))throw new Error('The registry contains a missing or duplicate agent ID.');
  nodes.set(a.id,{id:a.id,name:a.name,role:a.role,emoji:a.emoji,avatar:a.avatar,color:a.color,
   reports_to:a.reports_to,is_lead:!!a.is_lead,children:[]});
 }
 const roots=[];
 for(const a of nodes.values()){
  if(a.reports_to=='hermes')roots.push(a);
  else if(nodes.has(a.reports_to))nodes.get(a.reports_to).children.push(a);
  else throw new Error(`${a.name||a.id} has an unknown or missing reporting parent: ${a.reports_to||'none'}.`);
 }
 const visited=new Set(),active=new Set();
 function visit(a){
  if(active.has(a.id))throw new Error(`Reporting cycle detected at ${a.name||a.id}.`);
  if(visited.has(a.id))return;
  active.add(a.id);a.children.forEach(visit);active.delete(a.id);visited.add(a.id);
 }
 nodes.forEach(visit);
 return {roots,nodes};
}
function renderHierarchy(agents,status,activeIds){
 if(!['configured','pending'].includes(status)||!Array.isArray(activeIds))throw new Error('Invalid hierarchy rollout metadata.');
 const active=new Set(activeIds),{roots,nodes}=hierarchyTree(agents),signature=JSON.stringify([roots,status,activeIds]);
 if(signature==HIERARCHY_SIGNATURE)return;
 const closed=new Set(Array.from(document.querySelectorAll('#hierarchy-tree details:not([open])'),d=>d.dataset.agent));
 const focused=document.activeElement.closest('#hierarchy-tree .hprofile')?.dataset.agent;
 function branch(a){
  const leads=a.children.filter(n=>n.is_lead),others=a.children.filter(n=>!n.is_lead);
  const children=leads.length?`<ul class="hbranches${leads.length>1?' hlead-branches':''}">${leads.map(node).join('')}</ul>${others.length?
   `<div class="hother"><h3>Other direct reports</h3><p>Report directly to ${esc(a.name)}; not additional leads.</p><ul class="hbranches hadvisers">${others.map(node).join('')}</ul></div>`:''}`:
   `<ul class="hbranches">${a.children.map(node).join('')}</ul>`;
  return `<details class="hbranch" data-agent="${esc(a.id)}"${closed.has(a.id)?'':' open'}><summary>Direct reports · ${a.children.length} to ${esc(a.name)}</summary>${children}</details>`;
 }
 function node(a){
  const parent=a.reports_to=='hermes'?'you':nodes.get(a.reports_to).name,live=active.has(a.id);
  const identity=`<span class="hportrait">${a.avatar?`<img src="${esc(a.avatar)}" alt="" loading="lazy">`:esc(a.emoji||'●')}</span><span><span class="name">${esc(a.name)}</span><br><span class="role">${esc(a.role)}</span></span>`;
  return `<li class="hitem" data-agent="${esc(a.id)}" data-parent="${esc(a.reports_to)}"><div class="hnode" style="--c:${hex(a.color)}">
   <div class="hidentity">${live?`<a class="hprofile" data-agent="${esc(a.id)}" href="#agent/${encodeURIComponent(a.id)}" title="Open ${esc(a.name)}'s current runtime profile">${identity}</a>`:
   `<span class="hprofile" data-agent="${esc(a.id)}">${identity}</span><span class="chip hpending" title="This agent is not activated in the current runtime">Pending</span>`}
   ${a.is_lead?'<span class="chip hlead">Lead</span>':''}</div><div class="hparent">Reports to <b>${esc(parent)}</b></div></div>${a.children.length?branch(a):''}</li>`;
 }
 $('#hierarchy-tree').innerHTML=`<ul class="hbranches hroots">${roots.map(node).join('')}</ul>`;
 document.querySelectorAll('#hierarchy-tree .hportrait img').forEach(img=>{
  const emoji=nodes.get(img.closest('.hitem').dataset.agent).emoji||'●';
  img.onerror=()=>{img.onerror=null;img.replaceWith(document.createTextNode(emoji))};
 });
 $('#hierarchy-count').textContent=`${nodes.size} agents · ${Array.from(nodes.values()).filter(a=>a.is_lead).length} leads · ${status=='pending'?'requested':'configured'} roster`;
 $('#hierarchy-stage').textContent=status=='pending'?'Requested hierarchy — activation pending. Running agents and routing remain unchanged.':'';
 $('#hierarchy-stage').hidden=status!='pending';
 HIERARCHY_SIGNATURE=signature;
 if(focused)Array.from(document.querySelectorAll('#hierarchy-tree .hprofile')).find(a=>a.dataset.agent==focused)?.focus();
}
async function loadHierarchy(){
 try{const [data,agents]=await Promise.all([J('/api/hierarchy'),J('/api/agents')]);
  renderHierarchy(data.agents,data.status,data.active_ids);AG=agents;$('#hierarchy-error').hidden=true}
 catch(e){console.error('Reporting hierarchy unavailable',e);HIERARCHY_SIGNATURE='';$('#hierarchy-tree').innerHTML='';
  $('#hierarchy-stage').hidden=true;$('#hierarchy-count').textContent='Reporting structure unavailable';
  $('#hierarchy-error').textContent='Could not load the reporting hierarchy: '+e.message;$('#hierarchy-error').hidden=false}
}
function hierarchyExpand(open){document.querySelectorAll('#hierarchy-tree details').forEach(d=>d.open=open)}
$('#hierarchy-expand').onclick=()=>hierarchyExpand(true);$('#hierarchy-collapse').onclick=()=>hierarchyExpand(false);
// ---------------------------------------------------------------- map (Atlas engine at /depmap)
const KC={command:'#ffd84d',post:'#5865F2',approval:'#b07cf0',trigger:'#e74c3c',data:'#7f8c8d',report:'#2ecc71'};  // also used by agent profiles
const KN={command:'Command / routing',post:'Posts to Discord (via Sentinel)',approval:'Asks for your ✅ (Gideon executes)',trigger:'Triggers another agent',data:'Reads / writes a system',report:'Reporting hierarchy (leads → Gideon)'};
function loadMap(){const u='/depmap'+($('#map-reporting').checked?'?reporting=1':'');$('#map-open').href=u;
 const f=$('#depmap');f.src=u+(u.includes('?')?'&':'?')+'t='+Date.now()}
$('#map-refresh').onclick=loadMap;$('#map-reporting').onchange=loadMap;
// ---------------------------------------------------------------- profile
function openProfile(id){location.hash='agent/'+id}
function bars(rows,key,color,fv){const days=[];for(let i=29;i>=0;i--)days.push(new Date(Date.now()+8*3600e3-i*864e5).toISOString().slice(0,10));
 const by={};rows.forEach(r=>by[r.d]=r[key]||0);const mx=Math.max(1e-9,...days.map(d=>by[d]||0));const W=600,H=110,bw=W/30;
 return `<svg viewBox="0 0 ${W} ${H+16}" width="100%">${days.map((d,i)=>{const v=by[d]||0,h=v/mx*(H-8);return `<rect x="${i*bw+1.5}" y="${H-h}" width="${bw-3}" height="${Math.max(h,v?1:0)}" fill="${color}" rx="2"><title>${d}: ${fv(v)}</title></rect>`+(i%6==0?`<text x="${i*bw}" y="${H+12}" style="fill:var(--mut)" font-size="9">${d.slice(5)}</text>`:'')}).join('')}<text x="${W}" y="9" style="fill:var(--mut)" font-size="9" text-anchor="end">max ${fv(mx)}</text></svg>`}
async function loadProfile(id){const p=await J('/api/agents/'+encodeURIComponent(id));if(p.error){$('#profile').innerHTML='<p>Unknown agent.</p>';return}
 const a=p.agent,L=p.lifetime,s=a.stats30,c=hex(a.color),k=(v,l)=>`<div class="kpi"><b>${v}</b><span>${l}</span></div>`;
 const totTok=p.by_model.reduce((t,m)=>t+(m.input||0)+(m.output||0),0),totCost=p.by_model.reduce((t,m)=>t+(m.cost||0),0);
 $('#profile').innerHTML=`<button class="back" onclick="location.hash='${GV(a.group)}'">← All ${GSEL[GV(a.group)][2]} agents</button><button class="back" onclick="location.hash='hierarchy'">Reporting hierarchy</button><button class="back" onclick="location.hash='map';setTimeout(()=>{SEL=null;selNode('${a.id}')},400)">Show on map</button>
 <div class="phead" style="--c:${c}">${a.avatar?`<span class="avw"><img class="avbig" src="${a.avatar}" alt="">${sdot(a,1)}</span>`:`<div class="big">${a.emoji}</div>`}<div style="flex:1"><h2>${a.emoji} ${esc(a.name)} ${spill(a).replace('class="pill','style="font-size:12px;vertical-align:middle" class="pill')} <span class="mut" style="font-size:12px;font-weight:400">${esc(a.state_why||'')}</span> <button class="back" style="margin:0 0 0 8px;vertical-align:middle" onclick="location.hash='voice/${a.id}'">🎙️ Talk to ${esc(a.name)}</button></h2><div class="role">${esc(a.role)}${a.voice?' · voice: '+esc(a.voice):''}</div>
 <div class="char" style="margin-top:6px">${esc(a.character)}</div><div class="does" style="margin-top:6px">${esc(a.does)}</div>
 ${a.personality?`<div class="does" style="margin-top:8px"><b>Personality:</b> ${esc(a.personality.temperament)}<br><b>Quirks:</b> ${esc(a.personality.quirks)}<br><b>Conversation:</b> ${esc(a.personality.delivery)}<br><small class="mut">Fictional character traits; never operational mistakes or real human experiences.</small></div>`:''}
 <div class="chips" style="margin-top:8px">${(a.skills||[]).map(x=>`<span class="chip">${esc(x)}</span>`).join('')}</div>
 <div class="does" style="margin-top:8px">🧭 <b>Boundary:</b> ${a.boundary?esc(a.boundary):a.scope&&a.scope.length?(a.scope.length>=4?'all layers (application · compute · network · storage)':a.scope.map(esc).join(' · ')):'no infrastructure layers'} · ${a.reports_to=='hermes'?'overall assistant: reports to you':a.reports_to?'reports to '+esc(ag(a.reports_to).name):'no reporting parent'}${a.is_lead?' · <b>Lead</b>':''}</div></div></div>
 <div class="pgrid">${k(a.last_run?ago(a.last_run):'never','last ran'+(a.last_run?' · '+fmt(a.last_run):''))}${k(a.next_run?ago(a.next_run):(a.on_demand?'on demand':'—'),'next run')}
 ${k(num(L.runs),'runs (lifetime)')}${k(L.runs?Math.round(L.ok/L.runs*100)+'%':'—','success rate · '+L.errors+' errors')}${k(L.avg_seconds?(L.avg_seconds<120?L.avg_seconds+'s':(L.avg_seconds/60).toFixed(1)+'m'):'—','avg run time')}
 ${k(num(s.runs),'runs (30 d)')}${k(num(s.input+s.output),'tokens (30 d)')}${k(money(s.cost),'cost (30 d)')}${k(s.runs?money(s.cost/s.runs):'—','avg cost / run')}${k(money(L.cost),'cost (lifetime)')}</div>
 <div class="two"><div class="chart"><h3 class="sec">Runs per day · 30 d</h3>${bars(p.daily,'runs',c,v=>v+' runs')}</div><div class="chart"><h3 class="sec">Cost per day · 30 d</h3>${bars(p.daily,'cost','#2ecc71',v=>money(v))}</div></div>
 <div class="chart"><h3 class="sec">Models &amp; token usage · 30 d</h3><dl style="margin-bottom:10px"><dt>Primary</dt><dd><b>${esc(a.model)}</b></dd><dt>Policy</dt><dd>${esc(a.models)}</dd></dl>
 <table><thead><tr><th>Model</th><th class="num">Runs</th><th class="num">Calls</th><th class="num">Input</th><th class="num">Cached</th><th class="num">Output</th><th class="num">Reasoning</th><th class="num">Web</th><th class="num">Cost</th><th>Share</th></tr></thead><tbody>
 ${p.by_model.map(m=>`<tr><td>${esc(m.model)}</td><td class="num">${m.runs}</td><td class="num">${num(m.calls)}</td><td class="num">${num(m.input)}</td><td class="num">${num(m.cached)}${m.input?' <span class="mut">('+Math.round(m.cached/m.input*100)+'%)</span>':''}</td><td class="num">${num(m.output)}</td><td class="num">${num(m.reasoning)}</td><td class="num">${m.web||''}</td><td class="num">${money(m.cost||0)}</td><td style="width:120px"><div class="bar"><i style="width:${totCost?(m.cost/totCost*100):0}%;background:${c}"></i></div></td></tr>`).join('')||'<tr><td colspan=10 class="mut">No runs in the last 30 days</td></tr>'}</tbody></table></div>
 <div class="two"><div class="chart"><h3 class="sec">What triggers ${esc(a.name)} · 30 d</h3><table><thead><tr><th>Trigger</th><th class="num">Runs</th><th class="num">Cost</th></tr></thead><tbody>${p.by_trigger.map(t=>`<tr><td>${esc(t.trigger)}</td><td class="num">${t.runs}</td><td class="num">${money(t.cost||0)}</td></tr>`).join('')||'<tr><td colspan=3 class="mut">—</td></tr>'}</tbody></table>
 <h3 class="sec" style="margin-top:14px">Schedule &amp; units</h3><dl><dt>Schedule</dt><dd>${esc(a.schedule)}</dd><dt>Channel</dt><dd>#${esc(a.channel)}</dd><dt>Code</dt><dd><code>${esc(a.code)}</code></dd>
 ${Object.entries(a.units_state).map(([n,u])=>`<dt>${esc(n)}</dt><dd><span class="${u.active=='failed'?'failed':u.active=='active'?'ok':'idle'}">${esc(u.active||'?')}</span>${u.next?' · next '+fmt(u.next):''}${u.last?' · last '+fmt(u.last):''}</dd>`).join('')}</dl></div>
 <div class="chart"><h3 class="sec">Connections</h3>${p.connections.map(e=>`<div class="conn side" style="--k:${KC[e.kind]};width:auto;border-left:3px solid ${KC[e.kind]};padding:4px 8px;margin:6px 0;background:var(--inset,#12151c);border-radius:4px">${e.dir=='out'?'→':'←'} <b>${esc(e.other.label||e.other.id||'')}</b> <span class="mut">· ${esc(KN[e.kind])}</span><br>${esc(e.label)}</div>`).join('')||'<span class="mut">none</span>'}</div></div>
 <div class="chart"><h3 class="sec">Recent runs</h3><div style="overflow:auto;max-height:420px"><table><thead><tr><th>#</th><th>Task</th><th>Trigger</th><th>Started</th><th>Took</th><th>Status</th><th>Model</th><th class="num">In</th><th class="num">Out</th><th class="num">Cost</th></tr></thead><tbody>
 ${p.recent.map(r=>{const t=r.ended?Math.round((Date.parse(r.ended)-Date.parse(r.started))/1000):null;return `<tr class="click" onclick="showRun(${r.id})" title="${esc(r.summary)}"><td>${r.id}</td><td>${esc(r.task).slice(0,80)}</td><td>${esc(r.trigger)}</td><td>${fmt(r.started)}</td><td>${t==null?'…':t<120?t+'s':Math.round(t/60)+'m'}</td><td class="${r.status}">${r.status}</td><td>${esc(r.model)||'—'}</td><td class="num">${num(r.input_tokens)}</td><td class="num">${num(r.output_tokens)}</td><td class="num">${money(r.cost_usd)}</td></tr>`}).join('')||'<tr><td colspan=10 class="mut">No runs yet</td></tr>'}</tbody></table></div></div>
 ${p.approvals.length?`<div class="chart"><h3 class="sec">Approvals requested</h3><table><thead><tr><th>#</th><th>Title</th><th>Severity</th><th>Status</th><th>Created</th><th>Finished</th></tr></thead><tbody>${p.approvals.map(x=>`<tr><td>${x.id}</td><td>${esc(x.title)}</td><td>${esc(x.severity)}</td><td>${esc(x.status)}</td><td>${fmt(x.created)}</td><td>${fmt(x.finished)}</td></tr>`).join('')}</tbody></table></div>`:''}`}
// ---------------------------------------------------------------- voice room (OpenAI Realtime over WebRTC)
const V={pc:null,dc:null,stream:null,run:null,agent:localStorage.getItem('vagent')||'boss',t0:0,seg0:0,last:0,seg:[],muted:false,endAfter:false,handoff:null,
 timer:null,info:null,callCost:0,live:null,speaking:false,responding:false,suspect:{},ending:false,actx:null,starting:false,micRevision:0,
 engine:localStorage.getItem('voiceEngine')=='openai'?'openai':'local',local:false};  // local PC voice by default (new key: older 'vengine' picks are ignored)
const MIC={selected:localStorage.getItem('roundtable.mic')||'',devices:[],testing:false,busy:false,pending:false,refresh:0};
const AUD=new Audio();AUD.autoplay=true;
const ag=id=>AG.find(a=>a.id==id)||{id,name:id,emoji:'',color:0x888888};
// voice picker: one group of homelab agents
let VG=null;const vgOf=a=>GV(a.group);
function vgrp(g){VG=g;$('#vsearch').value='';vpickRender()}
function vpickRender(){document.querySelectorAll('#vgrp button').forEach(b=>{b.classList.toggle('on',b.dataset.g==VG);b.setAttribute('aria-pressed',b.dataset.g==VG)});
 const query=$('#vsearch').value.trim().toLowerCase(),group=AG.filter(a=>vgOf(a)==VG);
 const list=group.filter(a=>[a.name,a.role,...(a.skills||[])].join(' ').toLowerCase().includes(query));
 $('#vcount').textContent=`${list.length} of ${group.length} agents`;
 $('#vpick').innerHTML=list.map(a=>`<button type="button" data-id="${a.id}" style="--c:${hex(a.color)}" onclick="vpick('${a.id}')" aria-pressed="${a.id==V.agent}"><span class="avw"><img src="${a.avatar||''}" alt="">${sdot(a)}</span><b>${a.emoji} ${esc(a.name)}</b><span class="mut">${esc(a.role.split('·')[0])}</span></button>`).join('')||'<p class="empty">No matching agents in this group.</p>';
 document.querySelectorAll('#vpick button').forEach(d=>d.classList.toggle('on',d.dataset.id==V.agent))}
function vstatus(t){$('#vstatus').innerHTML=t;if(DESK&&t)deskSay(t)}
function vline(cls,who,text){const L=$('#vlog');if(L.querySelector('.sys:only-child'))L.innerHTML='';const p=document.createElement('p');p.className=cls;
 p.innerHTML=who?`<b>${esc(who)}:</b> ${esc(text)}`:esc(text);L.appendChild(p);L.scrollTop=L.scrollHeight;return p}
function vwho(){const a=ag(V.agent);$('#vstage').style.setProperty('--c',hex(a.color));$('#vav').src=a.avatar||'';$('#vname').textContent=`${a.emoji} ${a.name}`;
 $('#vrole').textContent=`${a.role||''}${a.voice?' · voice '+a.voice:''}`;document.querySelectorAll('#vpick button').forEach(d=>{d.classList.toggle('on',d.dataset.id==V.agent);d.setAttribute('aria-pressed',d.dataset.id==V.agent)})}
function vbtns(){const on=!!V.pc;$('#vtalk').disabled=on||V.starting||MIC.busy||!(V.info&&V.info.ok&&V.info.authed&&window.isSecureContext);$('#vmute').disabled=!on;$('#vend').disabled=!on||V.starting;
 $('#vmicresume').disabled=!on||V.starting||MIC.busy;
 $('#vmute').textContent=V.muted?'🎙️ Unmute':'🔇 Mute mic';$('#vmute').setAttribute('aria-pressed',V.muted);
 $('#vmicsel').disabled=MIC.busy||V.starting;$('#vmictest').disabled=on||V.starting||MIC.busy;$('#vmictest').textContent=MIC.testing?'Stop test':'Test microphone';
 B.el.querySelector('#dmic').disabled=V.starting;B.el.querySelector('#dtalk').disabled=V.starting||MIC.busy;$('#vpreview').disabled=on||V.starting;
 vstopbtn();try{cBtns()}catch(e){}if(DESK)B.el.classList.toggle('oncall',on)}
function vcost(){if(!V.info)return;$('#vcost').textContent=`${V.info.model} · this call ${money(V.callCost)} · voice this month ${money(V.info.spend)} / $${V.info.cap}`}
async function loadVoice(id){if(!AG.length)AG=await J('/api/agents');if(id&&!V.pc&&AG.some(a=>a.id==id))V.agent=id;
 V.info=await J('/api/voice/info');
 if(!V.pc){AG=await J('/api/agents');const requested=id||V.agent;
  if(!AG.some(a=>a.id==requested)){const fallback=AG.find(a=>a.id=='boss')||AG[0];
   if(!fallback)throw new Error('No active agents are available. Refresh the portal or check the agent service.');
   V.agent=fallback.id;VG=null;localStorage.setItem(DESK?'deskAgent':'vagent',V.agent);
   vmicnotice('The saved/requested agent is no longer available. Switched to '+fallback.name+' for a fresh conversation; no private history was imported.')}
  if(!VG||(id&&id!=V.vgId)){VG=vgOf(ag(V.agent));V.vgId=id}vpickRender()}
 const w=[];if(!window.isSecureContext){const msg='This HTTP/IP address is view-only: browser security blocks the microphone. Open the HTTPS portal and sign in.';
  w.push(msg+' <a href="https://roundtable.hrmsmrflrii.xyz/#voice">Open secure voice portal</a>');vmicnotice(msg,true)}
 else if(!V.info.authed)w.push('Sign in through the <a href="https://roundtable.hrmsmrflrii.xyz/#voice">protected HTTPS portal</a> to enable calls. A local microphone test is still available.');
 if(!V.info.ok)w.push(esc(V.info.why));$('#vwarn').innerHTML=w.length?`<div class="warnbox">${w.join('<br>')}</div>`:'';
 vwho();vbtns();vcost()}
function vpick(id){if(id==V.agent)return;if(V.pc){vhandover(id,'Hermes picked you on the portal to continue the conversation.');return}V.agent=id;vwho();if(!DESK)localStorage.setItem('vagent',id)
 if(DESK){localStorage.setItem('deskAgent',id);bSwap(ag(id));deskPanel()}else if(!B.el.hidden)bSwap(ag(id))}
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b),keepalive:true}).then(r=>r.json().then(j=>(j._s=r.status,j)));
const send=m=>{if(V.dc&&V.dc.readyState=='open')V.dc.send(JSON.stringify(m))};
// level meter on the audio thread (AudioWorklet): keeps barge-in and lip-sync working while the browser is
// minimised or the tab is hidden (requestAnimationFrame freezes there). Posts the peak (0-128) every ~20 ms.
const VMETER=`class M extends AudioWorkletProcessor{constructor(){super();this.n=0;this.m=0}
process(i){const c=i[0]&&i[0][0];if(c)for(let k=0;k<c.length;k++){const v=Math.abs(c[k]);if(v>this.m)this.m=v}
if(++this.n>=8){this.port.postMessage(this.m*128);this.n=0;this.m=0}return true}}registerProcessor('vmeter',M)`;
async function vmeter(stream,cb){const ac=V.actx;
 if(!ac.audioWorklet)throw new Error('no AudioWorklet');
 if(!ac._vm){const url=URL.createObjectURL(new Blob([VMETER],{type:'text/javascript'}));ac._vm=ac.audioWorklet.addModule(url).finally(()=>URL.revokeObjectURL(url))}
 await ac._vm;const src=ac.createMediaStreamSource(stream),node=new AudioWorkletNode(ac,'vmeter'),g=ac.createGain();g.gain.value=0;
 src.connect(node);node.connect(g);g.connect(ac.destination);node.port.onmessage=e=>cb(e.data);
 return {stop(){try{node.port.onmessage=null;src.disconnect();node.disconnect();g.disconnect()}catch(e){}}}}
function vmicLevel(m){V.micPeak=Math.max(V.micPeak||0,m);const level=V.muted&&V.pc?0:Math.min(100,m/64*100);
 $('#vlvl').style.width=level+'%';$('#vmictestlevel').value=level;vlevel(m)}
// silent-mic watchdog: a dead/wrong input device otherwise looks like "the agent doesn't hear me"
function vmiccheck(){if(!V.pc||V.muted||V.speaking)return;const t=V.stream&&V.stream.getAudioTracks()[0],lbl=t?t.label:'none';
 if(!t||t.readyState=='ended')vmicnotice(`Your microphone disconnected (${lbl}). Open Microphone settings to choose another input.`,true);
 else if(!t.enabled)vmicnotice('Your input is paused. Press Listen now to stop playback and restore the microphone.',true);
 else if((V.micPeak||0)<4)vmicnotice(`No sound from ${lbl}. Check this device's sound/input settings, input volume, hardware mute switch and the browser's microphone permission.`,true)}
function vmicnotice(text,error=false){$('#vaudiostatus').textContent=text;$('#vmicnote').textContent=text;
 for(const id of ['#vaudiostatus','#vmicnote'])$(id).className=error?'error':'mut';
 if(V.pc)vline('sys','',text);if(DESK&&error)deskSay(text)}
function vmicerror(e){if(e.name=='NotAllowedError'||e.name=='SecurityError')return 'Microphone access denied. Allow microphone access for this site/app and in this device\'s privacy settings.';
 if(e.name=='NotFoundError')return 'No microphone is available. Connect or enable an input, then refresh and try again.';
 if(e.name=='NotReadableError')return 'The microphone could not be opened. Check whether another app is using it and whether the input is enabled.';
 return 'Microphone error: '+e.message}
function vmicremember(id){MIC.selected=id;localStorage.setItem('roundtable.mic',id)}
function vmicrender(){const select=$('#vmicsel');select.replaceChildren(new Option('System default', ''));
 MIC.devices.filter(d=>d.deviceId&&d.deviceId!='default'&&d.deviceId!='communications').forEach((d,i)=>select.add(new Option(d.label||`Microphone ${i+1}`,d.deviceId)));
 if(MIC.selected&&!Array.from(select.options).some(o=>o.value==MIC.selected))select.add(new Option('Saved microphone (currently unavailable)',MIC.selected));
 select.value=MIC.selected;const t=V.stream&&V.stream.getAudioTracks()[0],d=MIC.devices.find(d=>d.deviceId==MIC.selected);
 $('#vmiclabel').textContent=t&&t.readyState=='live'?(t.label||'Active microphone'):(MIC.selected?(d&&d.label||'Saved microphone'):'System default');vbtns()}
async function vmics(){const revision=++MIC.refresh;
 if(!navigator.mediaDevices||!navigator.mediaDevices.enumerateDevices){vmicnotice('Microphone access requires a supported browser and HTTPS (or the configured companion).',true);return []}
 try{const ds=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind=='audioinput');
  if(revision==MIC.refresh){MIC.devices=ds;vmicrender()}return ds}
 catch(e){console.error('microphone enumeration failed',e);vmicnotice('Could not list microphones: '+e.message,true);return null}}
async function vmicacquire(id){if(!window.isSecureContext)throw new Error('Browser security blocks microphones on HTTP/IP addresses. Open https://roundtable.hrmsmrflrii.xyz/#voice and sign in.');
 if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia)throw new Error('Use HTTPS and a browser with microphone support.');
 const base={echoCancellation:true,noiseSuppression:true,autoGainControl:true};
 try{return {stream:await navigator.mediaDevices.getUserMedia({audio:id?{...base,deviceId:{exact:id}}:base}),id}}
 catch(e){if(!id||!(e.name=='NotFoundError'||e.name=='OverconstrainedError'&&(!e.constraint||e.constraint=='deviceId')))throw e;
  vmicnotice('The saved microphone is unavailable on this device. Switching to the system default.');
  return {stream:await navigator.mediaDevices.getUserMedia({audio:base}),id:''}}}
async function vmicmeter(stream){if(V.meter){V.meter.stop();V.meter=null}
 try{if(!V.actx)V.actx=new AudioContext();const ac=V.actx;await ac.resume();if(V.stream!==stream)return;let meter;
  try{meter=await vmeter(stream,vmicLevel)}
  catch(e){console.warn('worklet meter unavailable, using animation-frame meter',e);
   const an=ac.createAnalyser(),src=ac.createMediaStreamSource(stream);an.fftSize=512;src.connect(an);const buf=new Uint8Array(an.fftSize);let frame;
   const tick=()=>{if(V.stream!==stream)return;an.getByteTimeDomainData(buf);let m=0;for(const v of buf)m=Math.max(m,Math.abs(v-128));vmicLevel(m);frame=requestAnimationFrame(tick)};tick();
   meter={stop(){cancelAnimationFrame(frame);src.disconnect();an.disconnect()}}}
  if(V.stream===stream)V.meter=meter;else meter.stop()}
 catch(e){if(V.stream!==stream){console.debug('microphone meter setup cancelled');return}console.error('microphone meter failed',e);vmicnotice('Microphone opened, but its level meter is unavailable: '+e.message,true)}}
function vmicwatch(stream){const t=stream.getAudioTracks()[0];t.onended=()=>{if(V.stream===stream)vmicdeviceschanged(true)};
 V.micPeak=0;V.hot=0;V.echo=null;clearTimeout(V.micT);V.micT=setTimeout(vmiccheck,15000)}
async function vmic(){if(V.stream&&V.stream.getAudioTracks()[0].readyState=='live')return V.stream;
 const revision=V.micRevision,{stream,id}=await vmicacquire(MIC.selected);
 if(revision!=V.micRevision){stream.getTracks().forEach(t=>t.stop());throw new DOMException('Microphone request cancelled','AbortError')}
 V.stream=stream;vmicremember(id);vmicwatch(stream);await vmicmeter(stream);await vmics();
 if(revision!=V.micRevision)throw new DOMException('Microphone request cancelled','AbortError');return stream}
function vmicrelease(){V.micRevision++;clearTimeout(V.micT);clearTimeout(V.gateT);if(V.tx){V.tx.stop();V.tx=null}
 if(V.meter){V.meter.stop();V.meter=null}if(V.stream){V.stream.getTracks().forEach(t=>t.stop());V.stream=null}
 if(V.actx){V.actx.close().catch(e=>console.warn('audio context close failed',e));V.actx=null}
 $('#vlvl').style.width='0';$('#vmictestlevel').value=0;vmicrender()}
async function vmicchange(id){if(MIC.busy||V.starting){MIC.pending=true;return}
 if(!V.stream){vmicremember(id);vmicrender();return}
 MIC.busy=true;vbtns();const revision=V.micRevision,old=V.stream,pc=V.pc,previous=MIC.selected;let stream;
 try{const result=await vmicacquire(id);stream=result.stream;
  if(revision!=V.micRevision){stream.getTracks().forEach(t=>t.stop());return}
  const track=stream.getAudioTracks()[0];track.enabled=!V.pc||!V.muted;
  if(pc){const sender=pc.getSenders().find(s=>s.track&&s.track.kind=='audio');if(!sender)throw new Error('The call has no audio sender. Hang up and try again.');
   const tx=track.clone();tx.enabled=vsendopen();await sender.replaceTrack(tx);if(V.tx)V.tx.stop();V.tx=tx}
  if(revision!=V.micRevision){stream.getTracks().forEach(t=>t.stop());return}
  V.stream=stream;vmicremember(result.id);old.getTracks().forEach(t=>t.stop());vmicwatch(stream);await vmicmeter(stream);await vmics();
  vmicnotice('Using microphone: '+(track.label||'System default'))}
 catch(e){if(stream&&V.stream!==stream)stream.getTracks().forEach(t=>t.stop());vmicremember(previous);vmicrender();console.error('microphone switch failed',e);vmicnotice(vmicerror(e),true)}
 finally{MIC.busy=false;vbtns();if(MIC.pending){MIC.pending=false;vmicdeviceschanged()}}}
async function vmicdeviceschanged(ended=false){if(MIC.busy||V.starting){MIC.pending=true;return}
 const ds=await vmics();if(!ds)return;const t=V.stream&&V.stream.getAudioTracks()[0],settings=t&&t.getSettings();
 const known=ds.some(d=>d.deviceId),gone=MIC.selected&&known&&!ds.some(d=>d.deviceId==MIC.selected);
 const def=ds.find(d=>d.deviceId=='default'),changed=!MIC.selected&&def&&settings&&def.groupId&&settings.groupId&&def.groupId!=settings.groupId;
 const lost=t&&(ended||t.readyState=='ended'||known&&settings.deviceId&&!ds.some(d=>d.deviceId==settings.deviceId));
 if(gone||lost||changed){vmicnotice('Microphone configuration changed. Using the system default.');await vmicchange('')}}
function vmicteststop(){MIC.testing=false;if(!V.pc)vmicrelease();vbtns()}
async function vmictest(){if(V.pc||V.starting||MIC.busy)return;if(MIC.testing){vmicteststop();vmicnotice('Microphone test stopped.');return}
 MIC.testing=true;MIC.busy=true;vbtns();
 try{await vmic();if(MIC.testing)vmicnotice('Speak now and watch the input meter. Test audio stays on this device.')}
 catch(e){MIC.testing=false;if(e.name!='AbortError'){console.error('microphone test failed',e);vmicnotice(vmicerror(e),true)}vmicrelease()}
 finally{MIC.busy=false;vbtns();if(MIC.pending){MIC.pending=false;vmicdeviceschanged()}}}
async function vstart(context){rcStop();
 if(V.pc||V.starting||MIC.busy)return;if(MIC.testing)vmicteststop();V.starting=true;vbtns();vstatus('Opening microphone…');
 try{
 if(!window.isSecureContext)throw new Error('Microphone calls require the HTTPS portal, not the HTTP/IP address.');
 if(!V.info||!V.info.ok||!V.info.authed)throw new Error('Voice is unavailable. Sign in through the protected HTTPS portal and check the voice budget.');
 try{await vmic()}catch(e){vmicnotice(vmicerror(e),true);throw e}
 vstatus('Summoning…');
 if(C.want&&!C.on)await cOn();
 await vengineSync();   // pick up a change made on another device (e.g. the portal) before every call
 if(V.engine=='local'){await lvStart(context);return}
 const s=await post('/api/voice/session',{agent:V.agent,context:context||''});
 if(s._s!=200)throw new Error(s.error||'Could not start the voice session');
 V.run=s.run_id;V.seg=[];V.seg0=Date.now();V.t0=V.t0||Date.now();V.last=Date.now();V.endAfter=false;V.handoff=null;V.ending=false;
 const pc=new RTCPeerConnection();V.pc=pc;
 pc.ontrack=e=>{AUD.srcObject=e.streams[0];bAudioReset();bAudio()};
 V.stream.getAudioTracks().forEach(t=>{t.enabled=!V.muted;V.tx=t.clone();V.tx.enabled=!V.muted;pc.addTrack(V.tx,V.stream)});vmicwatch(V.stream);
 vmicnotice('Using microphone: '+((V.stream.getAudioTracks()[0]||{}).label||'System default'));
 vline('sys','',V.duplex?'Headphones mode: talk to interrupt.':'Speakers mode: just talk (clearly) to interrupt, or use Stop / Esc.');
 const dc=pc.createDataChannel('oai-events');V.dc=dc;
 dc.onmessage=e=>{try{vevent(JSON.parse(e.data))}catch(err){console.error(err)}};
 dc.onopen=()=>{send({type:'conversation.item.create',item:{type:'message',role:'system',content:[{type:'input_text',text:context?
   'You have just been handed this live voice conversation. Greet Hermes in one short in-character sentence and continue from the context.':
   'Hermes has just summoned you on a live voice call. Greet him in one short in-character sentence.'}]}});cBeforeReply();send({type:'response.create'});
  cBtns();
  vstatus('🟢 Connected — speak whenever you like');vline('sys','',`${ag(V.agent).name} joined`);if(B.el.hidden)bShow(ag(V.agent));else bAudio()};
 pc.onconnectionstatechange=()=>{if(['failed','disconnected'].includes(pc.connectionState)&&V.pc===pc&&!V.ending){vstatus('Connection lost');vstop('connection lost')}};
 const offer=await pc.createOffer();await pc.setLocalDescription(offer);
  const a=await fetch('https://api.openai.com/v1/realtime/calls',{method:'POST',body:offer.sdp,headers:{Authorization:'Bearer '+s.value,'Content-Type':'application/sdp'}});
  if(!a.ok)throw new Error('OpenAI '+a.status+': '+(await a.text()).slice(0,200));
  await pc.setRemoteDescription({type:'answer',sdp:await a.text()});
 if(!V.timer)V.timer=setInterval(vtick,1000);vcost()
 }catch(e){console.error('voice connection failed',e);vstop('failed to connect');vstatus('⚠️ '+esc(e.message))}
 finally{V.starting=false;vbtns();if(MIC.pending){MIC.pending=false;vmicdeviceschanged()}}}
function vtick(){if(!V.pc||!V.info)return;const now=Date.now(),idle=(now-V.last)/1000,tot=(now-V.t0)/1000;
 const mm=s=>Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0');
 if(!V.ending)$('#vstatus').dataset.t=mm(tot);
 if(!V.speaking&&idle>V.info.idle_seconds){vline('sys','','Hung up after '+V.info.idle_seconds+' s of silence');vstop('idle')}
 else if(tot>V.info.max_seconds){vline('sys','','Hung up: '+V.info.max_seconds/60+' minute limit per call');vstop('time limit')}
 else if(V.info.max_seconds-tot<60&&!V.warned){V.warned=true;vline('sys','','One minute left on this call')}}
// transcripts the model hallucinates from silence, echo or room noise; these are dropped instead of answered
const VNOISE=/^(you|amazing|chatgpt|chat gpt|thanks for watching|thank you for watching|om det|hmm+|uh+|um+|mm+|ah+|music|applause|subtitles? by.*|please subscribe.*)$/i;
function vnoise(t){const s=(t||'').replace(/[.!?,…"'“”\s]+$/,'').replace(/^[\s"'“”]+/,'');
 if(!s||!/[a-z]/i.test(s))return true;
 if(VNOISE.test(s))return true;
 // the transcriber echoes its own hint prompt back on silence/noise ("Master Hermes talking to his homelab AI agents: ...")
 if(/talking to his homelab|homelab AI agents|Terms:\s*Proxmox|Jellyseerr, Bazarr|Filipino-English\) conversation/i.test(s))return true;
 return s.length<3&&!/^(no|ok|hi)$/i.test(s)}
// barge-in: the mic stays open while the agent talks. Browser echo-cancellation removes most of the agent's own
// voice; a sustained level clearly above the remaining echo floor means Master Hermes is talking -> cut the agent off.
// Speech the server hears during/just after agent audio without that local confirmation is only "suspect": if its
// transcript mostly repeats what the agent just said it is echo and dropped, otherwise it still counts as him.
// Without 🎧 headphones the copy of the mic SENT to the agent (V.tx) is closed while its voice plays (+0.5 s echo tail):
// speakers otherwise feed the agent's own voice back and it cuts itself off. The local mic track (V.stream) stays open
// so the level detector above can still hear him talk over the agent (stricter threshold in speakers mode); a
// sustained voice cuts the agent off and reopens V.tx at once. ✋ Stop / Esc also work. Nothing local is sent.
function vsendopen(){return !V.muted&&(V.duplex||!V.speaking||V.duck)}
// soft barge-in: a loud local sound only DUCKS the agent (quiet playback, sent mic opened) for up to 1.5 s; the agent is
// cut off only when the server's voice detector confirms real speech (input_audio_buffer.speech_started). Coughs,
// keyboard clicks and bumps fade the agent briefly and then it carries on.
function vduck(){if(V.duck||!V.pc||(!V.speaking&&!V.responding))return;V.duck=true;V.duckVol=AUD.volume;AUD.volume=Math.min(AUD.volume,.15);
 vgate();clearTimeout(V.duckT);V.duckT=setTimeout(vunduck,1500)}
function vunduck(){clearTimeout(V.duckT);if(!V.duck)return;V.duck=false;AUD.volume=V.duckVol==null?1:V.duckVol;V.hot=0;vgate()}
function vgate(){if(!V.stream)return;V.stream.getAudioTracks().forEach(t=>t.enabled=!V.muted);if(V.tx)V.tx.enabled=vsendopen()}
async function vmicresume(){if(!V.pc||V.starting||MIC.busy)return;
 vinterrupt('button');clearTimeout(V.gateT);V.muted=false;V.speaking=false;V.hot=0;vgate();
 const track=V.stream&&V.stream.getAudioTracks()[0];
 if(!track){const pc=V.pc,stream=await vmic();if(V.pc!==pc)return;
  const sender=pc.getSenders().find(s=>s.track&&s.track.kind=='audio');if(!sender)throw new Error('The call has no audio sender. Hang up and try again.');const tx=stream.getAudioTracks()[0].clone();tx.enabled=vsendopen();await sender.replaceTrack(tx);if(V.tx)V.tx.stop();V.tx=tx}
 else if(track.readyState=='ended'){await vmicdeviceschanged(true);if(!V.stream||V.stream.getAudioTracks()[0].readyState=='ended')return}
 if(V.actx)await V.actx.resume();vgate();vbtns();vmicwatch(V.stream);vmicnotice('Microphone restored. Speak now; the agent will listen.');vstatus('🟢 Your turn')}
$('#vmicresume').onclick=()=>vmicresume().catch(e=>{console.error('microphone recovery failed',e);vmicnotice(vmicerror(e),true)});
function vlevel(m){const now=performance.now(),dt=Math.min(100,now-(V.lt||now));V.lt=now;
 if(!V.speaking||V.muted||!V.pc){V.hot=0;V.echo=null;return}
 if(V.echo==null)V.echo=Math.min(m,20);
 const thr=Math.max(V.duplex?12:V.local?17:20,V.echo*(V.duplex?1.8:V.local?2.2:2.4));
 // the echo floor is learned only from levels below the threshold, so his own rising voice is never mistaken for echo
 if(m<=thr)V.echo=V.echo*.97+Math.min(m,V.echo*3+5)*.03;
 V.hot=m>thr?(V.hot||0)+dt:Math.max(0,(V.hot||0)-dt*.5);
 if(V.local){if(V.hot>220)vinterrupt('voice')}
 else{if(V.hot>(V.duplex?180:250))vduck();if(V.duck&&V.hot>700)vinterrupt('voice')}}
function vechoish(tx){const w=s=>(s.toLowerCase().match(/[a-z']+/g)||[]),said=new Set(w(V.said||'')),t=w(tx);
 if(!t.length)return true;let h=0;for(const x of t)if(said.has(x))h++;return t.length>=2&&h/t.length>=.6}
function vinterrupt(why){vunduck();const now=Date.now();if(!V.pc||(!V.speaking&&!V.responding)||now-(V.cutAt||0)<1000)return;V.cutAt=now;
 if(V.local){lvCancel();lvFlush()}
 if(V.responding)send({type:'response.cancel'});
 send({type:'output_audio_buffer.clear'});
 if(V.outItem&&V.spk0)send({type:'conversation.item.truncate',item_id:V.outItem,content_index:0,audio_end_ms:Math.max(0,now-V.spk0)});
 send({type:'conversation.item.create',item:{type:'message',role:'system',content:[{type:'input_text',text:
  '[interrupted] Master Hermes cut you off. Drop the unfinished point; answer his next words directly.'}]}});
 for(const k in V.suspect)if(now-V.suspect[k]<3000)delete V.suspect[k];
 if(V.handoff){vline('sys','','Hand-over cancelled');V.handoff=null}
 if(V.endAfter&&V.info.spend<V.info.cap)V.endAfter=false;
 V.speaking=false;V.hot=0;V.spkEnd=now;V.last=now;clearTimeout(V.gateT);vgate();
 if(V.live){V.live.lastChild.textContent+=' —';V.live.classList.add('cut');V.live=null}
 V.cutCur=true;
 vline('sys','',why=='button'?'✋ You stopped '+ag(V.agent).name:'✋ You cut in — '+ag(V.agent).name+' stopped');
 $('#vav').className='listening';vstatus(why=='button'?'🟢 Your turn':'👂 Listening…');bState(why=='button'?'idle':'listening');vstopbtn()}
function vstopbtn(){const b=$('#vcut');if(b)b.disabled=!(V.pc&&(V.speaking||V.responding))}
async function vevent(ev){const t=ev.type,a=ag(V.agent);
 if(t=='input_audio_buffer.speech_started'){const now=Date.now();
  if(V.speaking||V.responding||now-(V.spkEnd||0)<1200){
   if(V.duck||V.duplex&&(V.speaking||V.responding)||now-(V.cutAt||0)<3000)vinterrupt('voice');
   else{V.suspect[ev.item_id]=now;return}}
  V.last=now;$('#vav').className='listening';vstatus('👂 Listening…');bState('listening');bSay('👂 …','me')}
 else if(t=='input_audio_buffer.speech_stopped'){if(V.suspect[ev.item_id])return;V.last=Date.now();$('#vav').className='';vstatus('💭 Thinking…');bState('thinking')}
 else if(t=='conversation.item.input_audio_transcription.completed'){const tx=(ev.transcript||'').trim();if(tx&&!vnoise(tx)&&!wallPhrase(tx)&&wallRoute(tx))return;
  if(ev.usage)post('/api/voice/usage',{run_id:V.run,usage:ev.usage,kind:'transcribe'}).then(r=>{V.callCost+=r.cost||0;V.info.spend=r.spend;vcost()});
  if(V.suspect[ev.item_id]){delete V.suspect[ev.item_id];
   if(vnoise(tx)||vechoish(tx)){send({type:'conversation.item.delete',item_id:ev.item_id});console.debug('echo dropped:',tx);return}
   vinterrupt('voice')}
  if(vnoise(tx)){send({type:'conversation.item.delete',item_id:ev.item_id});if(tx)vline('sys','',`(ignored background sound: “${esc(tx)}”)`);
   $('#vav').className='';bState('idle');vstatus('🟢 Your turn');return}
  vline('me','You',tx);V.seg.push({who:'Hermes',text:tx});V.last=Date.now();cBeforeReply();send({type:'response.create'})}
 else if(t=='conversation.item.input_audio_transcription.failed'){send({type:'conversation.item.delete',item_id:ev.item_id});vstatus('🟢 Your turn')}
 else if(t=='response.created'){V.responding=true;V.cutCur=false;V.outItem=null;vstopbtn()}
 else if(t=='response.output_item.added'){if(ev.item&&ev.item.type=='message')V.outItem=ev.item.id}
 else if(t=='output_audio_buffer.started'){V.speaking=true;V.spk0=Date.now();V.echo=null;V.hot=0;clearTimeout(V.gateT);vgate();V.last=Date.now();$('#vav').className='talking';vstatus(`🗣️ ${esc(a.name)} is speaking — `+(V.duplex?'just talk to cut in':'talk to cut in, or ✋ Stop / Esc'));bState('talking');vstopbtn()}
 else if(t=='output_audio_buffer.stopped'||t=='output_audio_buffer.cleared'){const was=V.speaking;V.speaking=false;vunduck();V.spkEnd=Date.now();V.last=Date.now();vstopbtn();clearTimeout(V.gateT);V.gateT=setTimeout(vgate,t=='output_audio_buffer.cleared'?0:500);
  if(t=='output_audio_buffer.cleared'&&!was)return;$('#vav').className='';bState('idle');
  if(V.handoff)vdohandover();else if(V.endAfter)vstop('goodbye');else vstatus('🟢 Your turn')}
 else if(t=='response.output_audio_transcript.delta'){if(V.cutCur)return;if(!V.live){V.live=vline('ag',a.name,'');bSay('','')}V.live.lastChild.textContent+=ev.delta;V.said=((V.said||'')+ev.delta).slice(-800);bAppend(ev.delta)}
 else if(t=='response.output_audio_transcript.done'){if(V.cutCur){if(ev.transcript)V.seg.push({who:a.name,text:ev.transcript+' [interrupted]'});return}
  if(V.live)V.live.lastChild.textContent=' '+ev.transcript;else vline('ag',a.name,ev.transcript);V.live=null;
  if(ev.transcript)V.seg.push({who:a.name,text:ev.transcript})}
 else if(t=='response.done'){const r=ev.response||{};V.responding=false;vstopbtn();if(r.usage)post('/api/voice/usage',{run_id:V.run,usage:r.usage}).then(x=>{V.callCost+=x.cost||0;V.info.spend=x.spend;vcost();
   if(x.spend>=x.cap){vline('sys','','Voice cap reached — hanging up');V.endAfter=true}});
  const calls=(r.output||[]).filter(i=>i.type=='function_call');
  if(r.status=='cancelled'){for(const c of calls)send({type:'conversation.item.create',item:{type:'function_call_output',call_id:c.call_id,output:JSON.stringify({cancelled:true,note:'Master Hermes interrupted before this ran'})}});return}
  for(const c of calls)await vtool(c);
  // quiet tools (remember) after the agent already spoke must not trigger a second, unprompted reply
  const spoke=(r.output||[]).some(i=>i.type=='message'),quiet=calls.length&&calls.every(c=>['remember'].includes(c.name));
  if(calls.length&&!V.handoff&&!(spoke&&quiet))send({type:'response.create'});
  if(!calls.length&&(V.handoff||V.endAfter)&&!V.speaking)setTimeout(()=>{if(!V.speaking){if(V.handoff)vdohandover();else if(V.endAfter)vstop('goodbye')}},1500)}
 else if(t=='error'){console.warn(ev);vline('sys','','⚠️ '+((ev.error||{}).message||'error'))}}
async function vtool(c){let args={};try{args=JSON.parse(c.arguments||'{}')}catch(e){}
 const out=o=>send({type:'conversation.item.create',item:{type:'function_call_output',call_id:c.call_id,output:JSON.stringify(o)}});
 if(c.name=='hand_over'){if(!AG.some(x=>x.id==args.agent)||args.agent==V.agent){out({error:'unknown agent id'});return}
  vline('sys','',`${ag(V.agent).name} is handing you to ${ag(args.agent).name}…`);V.handoff={agent:args.agent,reason:args.reason||''};
  out({ok:true,note:'Say one short line telling Hermes who is taking over; then stop talking.'});send({type:'response.create'});return}
 if(c.name=='summon_team'){out(wallAction(args));send({type:'response.create'});return}
 if(c.name=='end_call'){V.endAfter=true;out({ok:true,note:'Say a one-line farewell.'});return}
 if(c.name=='look_at_camera'){if(!C.on){out({error:'The webcam is off. Ask Master Hermes to press the 📷 Camera button on the portal.'});return}
  B.txt='taking a look…';bState('tool');
  out({ok:true,note:'The photo follows in the next message. Answer what he asked; be kind about how he looks.'});
  cSnap('Webcam snapshot of Master Hermes right now'+(args.reason?' ('+args.reason+')':'')+'.');return}
 vline('sys','',`⚙️ ${ag(V.agent).name} → ${c.name}${args.agent?' ('+ag(args.agent).name+')':''}`);B.txt=TOOL_LABEL[c.name]||'working…';bState('tool');
 const r=await post('/api/voice/tool',{run_id:V.run,agent:V.agent,name:c.name,arguments:args});delete r._s;
 out(r)}
function vend(reason){if(!V.run)return;const body={run_id:V.run,transcript:V.seg,seconds:Math.round((Date.now()-V.seg0)/1000),reason};V.run=null;
 navigator.sendBeacon?navigator.sendBeacon('/api/voice/end',new Blob([JSON.stringify(body)],{type:'application/json'})):post('/api/voice/end',body)}
// ---------------------------------------------------------------- local voice: speech runs on Hermes's PC, only text goes to the brain
// mic -> 16 kHz PCM -> PC server (Whisper) -> transcript -> /api/voice/local/turn (text brain + tools) -> PC server (Kokoro) -> PCM playback
const LV={ws:null,ctx:null,node:null,an:null,next:0,srcs:[],turn:0,msgs:[],poll:null,tts:{},ttsOpen:0,actions:[],sayId:0,tail:0,gain:null,mic:null};
const LV_WORKLET=['class P extends AudioWorkletProcessor{constructor(){super();this.o=[];this.p=0;this.a=0;this.c=0;this.r=sampleRate/16000}',
 'process(i){const x=i[0]&&i[0][0];if(!x)return true;for(let k=0;k<x.length;k++){this.a+=x[k];this.c++;this.p+=1;',
 'if(this.p>=this.r){this.o.push(this.a/this.c);this.a=0;this.c=0;this.p-=this.r;',
 'if(this.o.length>=320){const b=new Int16Array(320);for(let j=0;j<320;j++){const v=Math.max(-1,Math.min(1,this.o[j]));b[j]=v<0?v*32768:v*32767}',
 'this.port.postMessage(b.buffer,[b.buffer]);this.o=[]}}}return true}}registerProcessor("lv-pcm",P)'].join(' ');
function lvPlaying(){return LV.ctx&&LV.next>LV.ctx.currentTime+.02}
async function lvStart(context){
 const s=await post('/api/voice/local/session',{agent:V.agent,context:context||''});
 if(s._s!=200)throw new Error(s.error||'Could not start the local voice session');
 const cfg=await J('/api/voice/local/info');
 if(!cfg.enabled)throw new Error('Local voice is not configured on the server (no LOCAL_VOICE_TOKEN set). Switch the engine back to OpenAI.');
 V.run=s.run_id;V.seg=[];V.seg0=Date.now();V.t0=V.t0||Date.now();V.last=Date.now();V.endAfter=false;V.handoff=null;V.ending=false;V.local=true;
 LV.msgs=[];LV.turn=0;LV.actions=[];LV.tts={};LV.ttsOpen=0;LV.tail=0;LV.ring=[];
 V.pc={close(){}};
 if(!V.timer)V.timer=setInterval(vtick,1000);
 const ws=new WebSocket(cfg.url.replace(/[/]+$/,'')+'/ws?t='+encodeURIComponent(cfg.token));ws.binaryType='arraybuffer';LV.ws=ws;
 await new Promise((res,rej)=>{ws.onopen=res;ws.onerror=()=>rej(new Error('Cannot reach the voice server on your PC ('+cfg.url+'). Is it running? From another device you need its https/wss address.'))});
 ws.onerror=null;
 ws.onclose=()=>{if(LV.ws===ws&&V.local&&!V.ending){vstatus('Local voice connection lost');vstop('connection lost')}};
 ws.onmessage=e=>{if(typeof e.data=='string'){try{lvEvent(JSON.parse(e.data))}catch(err){console.error(err)}}else lvAudio(e.data)};
 const ctx=new AudioContext({latencyHint:'interactive'});LV.ctx=ctx;await ctx.resume();
 const url=URL.createObjectURL(new Blob([LV_WORKLET],{type:'application/javascript'}));
 try{await ctx.audioWorklet.addModule(url)}finally{URL.revokeObjectURL(url)}
 LV.mic=ctx.createMediaStreamSource(V.stream);LV.node=new AudioWorkletNode(ctx,'lv-pcm');
 LV.node.port.onmessage=e=>{if(!ws||ws.readyState!=1||V.muted)return;
  if(!V.duplex&&(lvPlaying()||V.responding||Date.now()<LV.tail)){const n=performance.now();LV.ring.push([n,e.data]);while(LV.ring.length&&n-LV.ring[0][0]>650)LV.ring.shift();return}ws.send(e.data)};
 const zero=ctx.createGain();zero.gain.value=0;LV.mic.connect(LV.node);LV.node.connect(zero);zero.connect(ctx.destination);LV.gain=zero;
 LV.an=ctx.createAnalyser();LV.an.fftSize=512;LV.an.connect(ctx.destination);
 LV.poll=setInterval(lvTick,60);
 vmicnotice('Using microphone: '+((V.stream.getAudioTracks()[0]||{}).label||'System default'));
 vline('sys','',V.duplex?'Headphones mode: talk to interrupt.':'Speakers mode: the mic pauses while the agent talks; use Stop / Esc to interrupt.');
 vline('sys','',`${ag(V.agent).name} joined · local voice (${s.model})`);
 vstatus('🟢 Connected (local voice) — speak whenever you like');cBtns();
 if(B.el.hidden)bShow(ag(V.agent));
 V.info={...V.info,model:s.model+' (local)'};vcost();
 lvAsk(true);
}
function lvEvent(m){
 if(m.type=='speech_start'){V.last=Date.now();if(V.duplex&&(lvPlaying()||V.responding))vinterrupt('voice');bState('listening')}
 else if(m.type=='transcript'){const t=(m.text||'').trim();V.last=Date.now();if(vnoise(t))return;if(!wallPhrase(t)&&wallRoute(t))return;
  if(lvPlaying()||V.responding)lvCancel();
  vline('me','You',t);V.seg.push({who:'You',text:t});LV.msgs.push({role:'user',content:t});bSay(t,'me');lvAsk(false)}
 else if(m.type=='tts_start'){LV.tts[m.id]={done:false};LV.ttsOpen++;
  const g=LV.greet&&LV.greet[m.id];if(g){delete LV.greet[m.id];const wait=LV.ctx?Math.max(0,LV.next-LV.ctx.currentTime)*1000:0;setTimeout(()=>wallBubble(g.id,g.text),wait)}}
 else if(m.type=='tts_end'){if(LV.tts[m.id])LV.tts[m.id].done=true}
 else if(m.type=='error'){vline('sys','','Local voice: '+(m.message||m.error||'error'))}}
async function lvAsk(greet){
 const id=++LV.turn;V.responding=true;bState('thinking');vstopbtn();
 let r;try{r=await post('/api/voice/local/turn',{run_id:V.run,agent:V.agent,messages:LV.msgs.slice(-20),greet:!!greet})}
 catch(e){r={_s:0,error:e.message}}
 if(id!=LV.turn||!V.local||V.ending)return;
 V.responding=false;
 if(r._s!=200){vline('sys','',r.error||'The local brain did not answer');bState('idle');vstopbtn();return}
 V.callCost+=r.cost||0;if(V.info){V.info.spend=r.spend;V.info.cap=r.cap}vcost();
 const text=(r.text||'').trim();LV.actions=r.actions||[];
 if(text){vline('ag',ag(V.agent).name,text);V.seg.push({who:ag(V.agent).name,text});LV.msgs.push({role:'assistant',content:text});
  bSay(text,'');const sid=++LV.sayId;LV.pendingSay=sid;
  if(LV.ws&&LV.ws.readyState==1)LV.ws.send(JSON.stringify({type:'say',id:sid,text,agent:V.agent}))}
 else{bState('idle');lvActions()}
 V.last=Date.now();vstopbtn()}
function lvAudio(buf){if(!LV.ctx||!(buf.byteLength>=2))return;
 const i16=new Int16Array(buf.slice(0,buf.byteLength&~1)),f=new Float32Array(i16.length);for(let k=0;k<f.length;k++)f[k]=i16[k]/32768;
 const ab=LV.ctx.createBuffer(1,f.length,24000);ab.copyToChannel(f,0);const src=LV.ctx.createBufferSource();src.buffer=ab;src.connect(LV.an);
 const at=Math.max(LV.ctx.currentTime+.04,LV.next);src.start(at);LV.next=at+ab.duration;LV.srcs.push(src);src.onended=()=>{LV.srcs=LV.srcs.filter(x=>x!==src)};
 if(!V.speaking){V.speaking=true;bState('talking');vstopbtn()}}
function lvTick(){if(!V.local||!LV.ctx)return;const play=lvPlaying();
 if(play){V.last=Date.now();const d=new Uint8Array(LV.an.fftSize);LV.an.getByteTimeDomainData(d);let s=0;for(const v of d)s+=(v-128)*(v-128);bAmp(Math.sqrt(s/d.length)*3)}
 else if(V.speaking){V.speaking=false;LV.tail=Date.now()+500;bAmp(0);vstopbtn();
  const open=Object.keys(LV.tts).some(k=>LV.tts[k]&&LV.tts[k].done===false);
  if(!open&&!V.responding){bState('idle');lvActions()}}}
function lvActions(){const a=LV.actions;LV.actions=[];
 for(const x of a){if(x.name=='hand_over'){const id=(x.args||{}).agent;if(AG.some(g=>g.id==id)&&id!=V.agent){
   vline('sys','',`${ag(V.agent).name} is handing you to ${ag(id).name}…`);V.handoff={agent:id,reason:(x.args||{}).reason||''};vdohandover();return}}
  else if(x.name=='summon_team'){wallAction(x.args)}
  else if(x.name=='end_call'){vstop('goodbye');return}}}
function lvFlush(){const r=LV.ring||[];LV.ring=[];if(LV.ws&&LV.ws.readyState==1)for(const x of r)LV.ws.send(x[1])}
function lvCancel(){LV.turn++;LV.actions=[];LV.tts={};if(LV.ws&&LV.ws.readyState==1)LV.ws.send(JSON.stringify({type:'cancel'}));
 LV.srcs.forEach(s=>{try{s.stop()}catch(e){}});LV.srcs=[];if(LV.ctx)LV.next=LV.ctx.currentTime;V.speaking=false;V.responding=false;bAmp(0)}
function lvClose(){if(!V.local&&!LV.ws&&!LV.ctx)return;V.local=false;clearInterval(LV.poll);LV.poll=null;
 const ws=LV.ws;LV.ws=null;if(ws){ws.onclose=null;try{ws.close()}catch(e){}}
 LV.srcs.forEach(s=>{try{s.stop()}catch(e){}});LV.srcs=[];
 try{LV.mic&&LV.mic.disconnect()}catch(e){}try{LV.node&&LV.node.disconnect()}catch(e){}
 const c=LV.ctx;LV.ctx=null;LV.mic=LV.node=LV.an=LV.gain=null;LV.next=0;LV.tts={};LV.actions=[];if(c)c.close().catch(()=>{})}
// Team wall: the summon_team tool shows the leads (Gideon, Loretta, Ranni, Corhyn) at once (current Daily/Demo art, idle animation when present).
function wallHide(){rcStop();const w=$('#wall');w.hidden=true;$('#wgrid').innerHTML='';W.list=[];if(W.desk){W.desk=false;document.documentElement.classList.remove('team');window.desk&&window.desk.team&&window.desk.team(false)}}
const W={list:[],f:0,lock:0};
function wallFocus(i,force){const n=W.list.length;if(!n)return;i=((i%n)+n)%n;if(i==W.f&&!force)return;W.f=i;W.lock=Date.now()+650;const step=360/n,R=W.R;
 if(W.desk){document.querySelectorAll('#wring .wt').forEach((t,k)=>t.classList.toggle('front',k==i));return}
 $('#wring').style.transform=`translateZ(${-R}px) rotateY(${-(W.turn=nearestTurn(W.turn||0,i*step))}deg)`;
 document.querySelectorAll('#wring .wt').forEach((t,k)=>{let d=Math.abs(k-i);d=Math.min(d,n-d);const front=k==i,v=t.querySelector('video');
  t.classList.toggle('front',front);t.style.transform=`rotateY(${k*step}deg) translateZ(${R+(front?90:0)}px) scale(${front?1.3:1})`;
  t.style.opacity=d>4?0:front?1:Math.max(.25,.85-d*.17);t.style.filter=front?'drop-shadow(0 0 18px var(--c)) drop-shadow(0 8px 12px #000a)':`brightness(${Math.max(.35,.85-d*.14)})`;t.style.pointerEvents=d>4?'none':'auto';t.style.zIndex=front?9:Math.max(1,8-d);
  if(v){if(d<=2){v.play().catch(()=>{})}else v.pause()}});
 const a=W.list[i];$('#wlabel').innerHTML=`${esc(a.emoji||'')} <b>${esc(a.name)}</b><small>${esc((a.role||'').split('·')[0])} · click to talk · ←/→ to browse</small>`}
function nearestTurn(cur,target){let t=target;while(t-cur>180)t-=360;while(t-cur<-180)t+=360;return t}
const LEADS=['boss','security','governance','medic'];
function wallShow(focus,who){W.who=who=='all'?'all':'leads';const list=W.who=='all'?AG.filter(a=>GV(a.group)=='agents'):AG.filter(a=>a.is_lead).sort((a,b)=>LEADS.indexOf(a.id)-LEADS.indexOf(b.id)),g=$('#wgrid');W.list=list;W.f=-1;W.turn=0;
 if(DESK){deskTeam(list,focus);return list.length}
 W.R=Math.max(520,Math.round(list.length*230/(2*Math.PI)));
 g.innerHTML='<div id="wring">'+list.map(a=>{const iv=a.anim&&a.anim.idle,src=Array.isArray(iv)?iv[0]:iv,
  still=(a.anim&&a.anim.poster)||a.avatar_full||a.avatar||'',
  media=src?`<video src="${esc(src)}" poster="${esc(still)}" loop muted playsinline preload="none"></video>`:`<img src="${esc(still)}" alt="">`;
  return `<button type="button" class="wt" data-id="${esc(a.id)}" aria-label="${esc(a.name)}" style="--c:${hex(a.color)}">${media}<b>${a.emoji||''} ${esc(a.name)}</b><small>${esc((a.role||'').split('·')[0])}</small></button>`}).join('')+'</div>';
 $('#wall').hidden=false;
 g.querySelectorAll('.wt').forEach((b,k)=>{
  b.onpointerenter=()=>{if(Date.now()>W.lock&&k!=W.f)wallFocus(k)};
  b.onclick=()=>{if(k!=W.f){wallFocus(k);return}if(!V.pc&&!LV.ws)return;const id=b.dataset.id;if(id==V.agent)return;V.handoff={agent:id,keepWall:true,ctx:'Hermes picked you from his team wall. Greet him briefly and ask what he needs.',reason:''};vdohandover()}});
 const fi=Math.max(0,list.findIndex(a=>a.id==focus));requestAnimationFrame(()=>wallFocus(fi,true));wallLive();setTimeout(wallGreet,1500);return list.length}
function wallLive(){document.querySelectorAll('#wring .wt').forEach(t=>t.classList.toggle('live',t.dataset.id==V.agent))}
function wallBubble(id,text){const i=W.list.findIndex(a=>a.id==id);if(i<0||$('#wall').hidden)return;wallFocus(i);
 const t=document.querySelector(`#wring .wt[data-id="${CSS.escape(id)}"]`);if(!t)return;t.querySelectorAll('.wsay').forEach(x=>x.remove());
 const s=document.createElement('span');s.className='wsay';s.textContent=text;t.appendChild(s);setTimeout(()=>s.remove(),3200)}
// A few random agents say hi when the wall appears: spoken through the PC voice server in local mode, text bubbles only otherwise.
function wallGreet(){if($('#wall').hidden||!W.list.length)return;const L=['Hi Hermes!','Hey Hermes, good to see you!','Hello Hermes!','Hi Hermes, reporting for duty!','Hermes! Hi!'];
 const pick=W.list.filter(a=>a.id!=V.agent).sort(()=>Math.random()-.5).slice(0,Math.min(3,W.list.length));
 const ws=V.local&&LV.ws&&LV.ws.readyState==1?LV.ws:null;LV.greet=LV.greet||{};
 pick.forEach((a,k)=>{const text=L[Math.floor(Math.random()*L.length)];
  if(ws){const sid=++LV.sayId;LV.greet[sid]={id:a.id,text};ws.send(JSON.stringify({type:'say',id:sid,text,agent:a.id}))}
  else setTimeout(()=>wallBubble(a.id,text),k*2200)})}
function wallRoute(t){if(W.desk||$('#wall').hidden||!W.list.length||!(V.pc||V.local))return false;const a=W.list[W.f];if(!a||a.id==V.agent)return false;
 vline('sys','',`Hermes is talking to ${a.name} from the team wall…`);
 V.handoff={agent:a.id,keepWall:true,ctx:`Hermes is speaking to you from his team wall of agents. He just said: "${t.slice(0,400)}". Answer him directly and briefly, in character.`,reason:''};vdohandover();return true}
document.addEventListener('keydown',e=>{if($('#wall').hidden)return;if(e.key=='Escape')wallHide();else if(e.key=='ArrowRight')wallFocus(W.f+1);else if(e.key=='ArrowLeft')wallFocus(W.f-1)});
$('#wall').addEventListener('wheel',e=>{e.preventDefault();if(Date.now()>W.lock-350)wallFocus(W.f+(e.deltaY>0||e.deltaX>0?1:-1))},{passive:false});
function wallPhrase(t){if(!t)return false;
 const ask=/\b(call|bring|summon|show|get|gather)\b/i.test(t);
 if(ask&&(/\b(all|every|whole|entire)\b.{0,25}\b(agents?|team|crew)\b/i.test(t)||/\beveryone\b/i.test(t))){if(!$('#wall').hidden)wallHide();wallShow(undefined,'all');return true}
 if(ask&&/\bleads?\b/i.test(t)||/\b(team|agent) wall\b/i.test(t)){if(!$('#wall').hidden)wallHide();wallShow(undefined,'leads');return true}
 if(/\b(hide|dismiss|close|send away|put away)\b.{0,20}\b(agents?|team|wall)\b/i.test(t)){wallHide();return true}
 return false}
function wallAction(args){args=args||{};if(args.action=='hide'){wallHide();return {ok:true,note:'The team wall is closed. Say a short line.'}}
 if(args.action=='meeting'||args.action=='adjourn'){const on=args.action=='meeting';fetch('/api/office/meeting',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on,by:'boss'})}).catch(e=>console.warn('office meeting',e));
  return {ok:true,note:on?'The board meeting is called: every agent is heading to the boardroom table in the Roundtable Office (/office) with their laptop. Say one short line.':'The board meeting is adjourned. Say one short line.'}}
 const who=args.who=='all'?'all':'leads';if(!$('#wall').hidden&&W.who!=who)wallHide();
 if($('#wall').hidden){const n=wallShow(args.focus,who);return {ok:true,agents:n,note:(who=='all'?'His whole team of '+n+' agents is':'His '+n+' leads are')+' now on his screen '+(DESK?'standing on his desktop next to you'+(V.local?', and right after your line they will introduce themselves one by one before you close the roll call':''):'in a rotating carousel')+'. Say one short welcoming line; do not list them. If he asks one of them something, use hand_over.'}}
 const i=W.list.findIndex(a=>a.id==args.focus);if(i>=0)wallFocus(i);return {ok:true,note:i>=0?W.list[i].name+' is now in front. Say one short line.':'The wall is already open.'}}
function vclose(){V.ending=true;lvClose();if(V.keepWall)V.keepWall=false;else wallHide();try{V.dc&&V.dc.close()}catch(e){}try{V.pc&&V.pc.close()}catch(e){}V.pc=V.dc=null;V.speaking=false;V.responding=false;V.suspect={};V.live=null;$('#vav').className='';bAudioReset()}
function vstop(reason){vend(reason);vclose();bHide();cOff();MIC.testing=false;vmicrelease();
 if(DESK&&V.agent!='boss')setTimeout(()=>{if(!V.pc&&!V.local&&V.agent!='boss')vpick('boss')},2500);
 clearInterval(V.timer);V.timer=null;V.t0=0;V.warned=false;$('#vlvl').style.width='0';vstatus(`📴 Call ended (${esc(reason||'hung up')}) · ${money(V.callCost)}. Transcript saved; Discord delivery may take a moment.`);V.callCost=0;vbtns();loadVoice().catch(e=>{console.error('voice availability refresh failed',e);vmicnotice('Could not refresh voice availability: '+e.message,true)})}
function vhandover(id,reason){V.handoff={agent:id,reason};return vdohandover()}
async function vdohandover(){const h=V.handoff;if(!h)return;V.handoff=null;const privateCall=false;
 V.keepWall=!!h.keepWall;const ctx=h.ctx?h.ctx:privateCall?'Hermes requested a fresh conversation. No private context has been shared.':`${ag(V.agent).name} handed Hermes over to you: ${h.reason}\nRecent conversation:\n`+
 V.seg.slice(-8).map(l=>l.who+': '+l.text).join('\n');vend('handed over');vclose();V.agent=h.agent;vwho();bSwap(ag(h.agent));vline('sys','',`Handing over to ${ag(h.agent).name}${privateCall?' · private history not shared':''}…`);wallLive();await vstart(ctx.slice(0,1900))}
// ---------------------------------------------------------------- webcam: (A) expressions read locally, (B) snapshots on request
// A: face-api runs in this browser; only a short text cue ("smiling") reaches the agent. B: one 512px frame, only when asked.
const C={on:false,want:localStorage.getItem('vcam')=='1',stream:null,fa:null,timer:null,hist:[],mood:'',seen:0,away:false,awayT:0,sent:'',sentAt:0,busy:false};
const MOOD={happy:['is smiling and looks happy','😊'],sad:['looks sad or down','💛'],angry:['looks frustrated or tense','🫶'],
 fearful:['looks worried or anxious','🤍'],disgusted:['looks displeased','🤔'],surprised:['looks surprised','😮'],neutral:['looks calm (neutral expression)','']};
async function cLoad(){if(C.fa)return C.fa;const fa=await import('/vendor/faceapi/face-api.esm.js');const M='/vendor/faceapi/';
 try{await fa.tf.setBackend('webgl');await fa.tf.ready()}catch(e){}
 await fa.nets.tinyFaceDetector.loadFromUri(M);await fa.nets.faceExpressionNet.loadFromUri(M);C.fa=fa;return fa}
async function cOn(){try{C.stream=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:640},height:{ideal:480},facingMode:'user'}})}
 catch(e){vline('sys','','📷 Camera blocked: '+esc(e.message)+'. Allow camera access for this site.');return false}
 const v=$('#vcamv');v.srcObject=C.stream;await v.play().catch(()=>{});C.on=true;$('#vcamw').hidden=false;$('#vmood').textContent='loading expression model…';cBtns();
 try{await cLoad()}catch(e){console.warn('face-api',e);$('#vmood').textContent='expression model failed to load (📸 snapshots still work)';return true}
 C.hist=[];C.seen=Date.now();$('#vmood').textContent='watching for your expression…';clearInterval(C.timer);C.timer=setInterval(cTick,700);return true}
function cOff(){clearInterval(C.timer);C.timer=null;if(C.stream)C.stream.getTracks().forEach(t=>t.stop());C.stream=null;C.on=false;
 $('#vcamv').srcObject=null;$('#vcamw').hidden=true;C.mood='';C.sent='';C.away=false;cBtns()}
function cBtns(){$('#vcam').textContent=`📷 Camera: ${C.on?'on':'off'}`;$('#vcam').classList.toggle('go',C.on);$('#vshow').disabled=!(C.on&&V.pc)}
async function cToggle(){C.want=!C.on;localStorage.setItem('vcam',C.want?'1':'0');if(C.on)cOff();else await cOn()}
async function cTick(){if(C.busy||!C.on||!C.fa)return;const v=$('#vcamv');if(v.readyState<2)return;C.busy=true;
 try{const d=await C.fa.detectSingleFace(v,new C.fa.TinyFaceDetectorOptions({inputSize:224,scoreThreshold:.5})).withFaceExpressions(),now=Date.now();
  if(d){if(C.away&&now-C.awayT>45000)cNote(`Master Hermes is back in front of the screen after about ${Math.max(1,Math.round((now-C.awayT)/60000))} min away.`,'👋');
   C.away=false;C.seen=now;C.hist.push(d.expressions);if(C.hist.length>8)C.hist.shift();
   const avg={};for(const h of C.hist)for(const k in MOOD)avg[k]=(avg[k]||0)+(h[k]||0)/C.hist.length;
   let top=Object.keys(avg).sort((a,b)=>avg[b]-avg[a])[0];
   if(top!='neutral'&&avg[top]<.45)top='neutral';  // a mood must hold for a few seconds before it counts
   if(top!=C.mood){const prev=C.mood;C.mood=top;$('#vmood').textContent='You '+MOOD[top][0].replace(/^is /,'are ').replace(/^looks/,'look');
    if(prev&&MOOD[top][1]&&V.pc)bReact(MOOD[top][1])}}
  else if(!C.away&&now-C.seen>12000){C.away=true;C.awayT=C.seen;C.mood='';C.hist=[];$('#vmood').textContent='You are not in view'}
 }catch(e){console.warn('face',e)}finally{C.busy=false}}
function cCue(){if(!C.on)return '';if(C.away)return 'Master Hermes has stepped away from the screen (he may still hear you).';return C.mood?`Master Hermes ${MOOD[C.mood][0]}.`:''}
function cNote(text,emoji){if(emoji&&V.pc)bReact(emoji);if(!V.pc)return;
 send({type:'conversation.item.create',item:{type:'message',role:'system',content:[{type:'input_text',text:'[camera] '+text}]}});C.sent=text;C.sentAt=Date.now()}
// before each reply: pass his current expression (only when it changed, or every 2 minutes)
function cBeforeReply(){const c=cCue();if(c&&(c!=C.sent||Date.now()-C.sentAt>120000))cNote(c)}
function cFrame(){const v=$('#vcamv'),w=512,h=Math.round(w*(v.videoHeight||480)/(v.videoWidth||640)),cv=document.createElement('canvas');cv.width=w;cv.height=h;
 cv.getContext('2d').drawImage(v,0,0,w,h);return cv.toDataURL('image/jpeg',.72)}
function cSnap(note){send({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:note},{type:'input_image',image_url:cFrame()}]}});
 V.seg.push({who:'Hermes',text:'📸 (shared a webcam snapshot)'});vline('sys','','📸 Snapshot sent to '+esc(ag(V.agent).name))}
function bReact(e){if(B.el.hidden||reduce)return;const r=document.createElement('span');r.className='react';r.textContent=e;
 B.el.querySelector('.bwrap').appendChild(r);setTimeout(()=>r.remove(),2300)}
$('#vcam').onclick=cToggle;
$('#vshow').onclick=()=>{if(V.pc&&C.on){cSnap('Master Hermes is showing you this through his webcam right now. React to what you see.');send({type:'response.create'})}};
// ---------------------------------------------------------------- companion overlay (Clippy-style floating agent)
const B={el:$('#buddy'),img:$('#bimg'),bub:$('#bbub'),name:$('#bname'),x:0,y:0,state:'',on:localStorage.getItem('buddy')!=='off',
 wanderT:null,hideT:null,walkT:null,drag:null,pip:null,an:null,amp:0,lastRune:0,agent:null,txt:'',full:localStorage.getItem('buddyfull')!=='off'};
const BW=150,BH=190,reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
const DEPTH={enabled:localStorage.getItem('buddydepth')!=='off',motion:matchMedia('(prefers-reduced-motion: reduce)')};
function bDepthRender(){const on=DEPTH.enabled&&!DEPTH.motion.matches,button=B.el.querySelector('#bdepthtoggle');B.el.classList.toggle('dimensional',on);
 button.setAttribute('aria-pressed',DEPTH.enabled);button.title=DEPTH.motion.matches?'Depth motion disabled by your reduced-motion preference':'2.5D depth: '+(DEPTH.enabled?'on':'off')}
function bDepthToggle(){DEPTH.enabled=!DEPTH.enabled;localStorage.setItem('buddydepth',DEPTH.enabled?'on':'off');bDepthRender()}
function bDepthPointer(x,y){if(!DEPTH.enabled||DEPTH.motion.matches||B.el.hidden)return;
 const rect=B.img.getBoundingClientRect();if(!rect.width||!rect.height)return;
 const dx=Math.max(-1,Math.min(1,(x-rect.left-rect.width/2)/rect.width)),dy=Math.max(-1,Math.min(1,(y-rect.top-rect.height/2)/rect.height));
 B.el.style.setProperty('--depth-x',(-dy*3).toFixed(2)+'deg');B.el.style.setProperty('--depth-y',(dx*5).toFixed(2)+'deg');
 B.el.style.setProperty('--depth-light',(dx*.05).toFixed(3));B.el.style.setProperty('--depth-shadow',(-dx*8).toFixed(2)+'px')}
$('#bdepthtoggle').onclick=bDepthToggle;DEPTH.motion.addEventListener('change',bDepthRender);bDepthRender();
$('#vpreview').onclick=()=>{if(V.pc||V.starting)return;if(!B.on)bToggle();bShow(ag(V.agent))};
addEventListener('pointermove',e=>bDepthPointer(e.clientX,e.clientY));
function bWin(){return B.pip||window}
function bIsFull(){return B.el.classList.contains('full')}
// full-body when enabled and the agent has <id>-full.png; falls back to the round portrait otherwise
function bMode(){const f=B.full&&!B.pip&&!B.el.classList.contains('mini')&&B.fullOk!==false;B.el.classList.toggle('full',f);
 if(f)B.el.classList.remove('below','edge-l','edge-r');else if(!B.pip&&!B.el.hidden)bHome();B.el.querySelector('#bfull').textContent=f?'⚪':'🧍'}
function bPlace(x,y,walk){if(B.pip||bIsFull())return;const w=innerWidth,h=innerHeight;B.x=Math.max(8,Math.min(w-BW-8,x));B.y=Math.max(150,Math.min(h-BH-8,y));
 const dx=B.x-(B._px||B.x);B._px=B.x;
 if(walk&&!reduce&&Math.abs(dx)>4){B.el.classList.add('walking');B.el.style.setProperty('--tilt',(dx>0?6:-6)+'deg');clearTimeout(B.walkT);
  B.walkT=setTimeout(()=>{B.el.classList.remove('walking');B.el.style.setProperty('--tilt','0deg')},2400)}
 B.el.style.transform=`translate(${B.x}px,${B.y}px)`;
 B.el.classList.toggle('below',B.y<330);B.el.classList.toggle('edge-l',B.x<80);B.el.classList.toggle('edge-r',B.x>w-BW-90)}
function bHome(){bPlace(innerWidth-BW-28,innerHeight-BH-24,true)}
function bWander(){clearTimeout(B.wanderT);B.wanderT=setTimeout(()=>{
  if(!B.el.hidden&&!B.drag&&!B.pip&&!bIsFull()&&!B.el.classList.contains('mini')&&['','idle'].includes(B.state)&&!reduce){
   const w=innerWidth,h=innerHeight,r=Math.random();
   // mostly stroll along the bottom/right edges so it never sits on what you are reading for long
   if(r<.45)bPlace(Math.random()*(w-BW),h-BH-20,true);else if(r<.75)bPlace(w-BW-24,200+Math.random()*(h-BH-220),true);
   else bPlace(Math.random()*(w-BW),180+Math.random()*(h-BH-200),true)}
  bWander()},9000+Math.random()*9000)}
// animated companion: looping transparent clips (idle / listen / talk) generated for each agent; every clip starts and
// ends on the same pose (the poster image), so crossfading between them never jumps. The poster keeps the desktop
// app's alpha hit-test aligned with the video. Without clips, in the round portrait or with reduced motion: the still image.
function bAnimSet(a){const box=B.img.parentElement;box.querySelectorAll('video').forEach(v=>{v.pause();v.remove()});box.classList.remove('anim');B.vids=null;
 if(!a.anim||!a.anim.idle||DEPTH.motion.matches||B.pip||!B.full)return;const doc=B.el.ownerDocument,vids={};B.vids=vids;
 // every state (idle / listen / talk) may be one clip or a list of variants; with several, each plays to its end and
 // then another variant of the same state is picked at random (all start and end on the same pose)
 for(const k of ['idle','listen','talk']){const srcs=[].concat(a.anim[k]||[]).filter(Boolean);
  vids[k]=srcs.map(src=>{const v=doc.createElement('video');v.muted=true;v.loop=srcs.length<2;v.playsInline=true;v.preload=k=='idle'?'auto':'metadata';
   v.setAttribute('aria-hidden','true');v.src=src;v.addEventListener('ended',()=>{if(B.vids===vids&&v.classList.contains('on'))bAnimNext(k,v)});box.appendChild(v);return v})}
 vids.idle[0].addEventListener('canplay',()=>{if(B.vids===vids){box.classList.add('anim');bAnimState()}},{once:true})}
function bAnimAll(){const v=B.vids;return v?[...v.idle,...v.listen,...v.talk]:[]}
function bAnimShow(v){for(const x of bAnimAll()){if(x===v){if(!x.classList.contains('on')){try{x.currentTime=0}catch(e){}x.play().catch(()=>{});x.classList.add('on')}}
  else if(x.classList.contains('on')){x.classList.remove('on');setTimeout(()=>{if(!x.classList.contains('on'))x.pause()},300)}}}
function bAnimNext(k,prev){const t=B.vids[k],others=t.filter(x=>x!==prev),pool=others.length?others:t,pick=pool[Math.floor(Math.random()*pool.length)];
 if(pick===prev){try{pick.currentTime=0}catch(e){}pick.play().catch(()=>{});return}bAnimShow(pick)}
function bAnimTalk(prev){bAnimNext('talk',prev)}
function bAnimState(){if(!B.vids)return;const s=B.state,k=s=='talking'&&B.vids.talk.length?'talk':['listening','thinking','tool'].includes(s)&&B.vids.listen.length?'listen':'idle';
 if(!B.vids[k].some(v=>v.classList.contains('on')))bAnimNext(k,null)}
function bState(s){B.state=s;['idle','listening','thinking','talking','tool'].forEach(c=>B.el.classList.toggle(c,c==s));bAnimState();
 if(s=='thinking'||s=='tool')bSay(`<span class="dots"><i></i><i></i><i></i></span>${s=='tool'&&B.txt?' '+B.txt:''}`,'',true);
 if(s=='idle'){clearTimeout(B.hideT);B.hideT=setTimeout(()=>B.bub.classList.remove('on'),7000)}}
function bSay(html,cls,raw){clearTimeout(B.hideT);B.bub.className='bubble on '+(cls||'');B.bub.innerHTML=raw?html:`<span>${esc(html)}</span>`}
function bAppend(t){if(!B.bub.classList.contains('on')||B.bub.classList.contains('me')||B.bub.querySelector('.dots'))bSay('','');
 const s=B.bub.firstChild||B.bub.appendChild(document.createElement('span'));s.textContent+=t;
 if(s.textContent.length>260)s.textContent='…'+s.textContent.slice(-240)}
function bLook(a){B.agent=a.id;B.el.style.setProperty('--c',hex(a.color));B.name.textContent=`${a.emoji} ${a.name}`;
 const animated=a.anim&&a.anim.poster&&!DEPTH.motion.matches,full=animated?a.anim.poster:a.avatar_full||(a.avatar||'').replace(/\.jpg$/,'-full.png'),want=B.full&&!B.pip&&full!=a.avatar;
 B.fullOk=want?undefined:false;B.img.onerror=null;
 if(want){B.img.onerror=()=>{B.img.onerror=null;B.fullOk=false;B.img.src=a.avatar||'';bAnimSet({});bMode()};B.img.src=full}else B.img.src=a.avatar||'';
 bAnimSet(want?a:{});bMode()}
function bShow(a){if(!B.on&&!DESK)return;const first=B.el.hidden;B.el.hidden=false;bLook(a);if(first&&!B.pip&&!bIsFull()){B._px=null;bHome()}
 B.el.classList.remove('exit');B.el.classList.add('enter');setTimeout(()=>B.el.classList.remove('enter'),750);bState('idle');bWander();bAudio()}
function bHide(){clearTimeout(B.wanderT);if(DESK){bState('idle');return}if(B.el.hidden)return;B.el.classList.add('exit');
 setTimeout(()=>{B.el.hidden=true;B.el.classList.remove('exit');B.bub.classList.remove('on');bState('');if(B.pip)B.pip.close()},560)}
function bSwap(a){if(B.el.hidden){bShow(a);return}B.bub.classList.remove('on');B.txt='';B.el.classList.add('exit');setTimeout(()=>{B.el.classList.remove('exit');bShow(a)},560)}
// lip-sync-ish: drive size/glow/runes from the agent's real voice amplitude
function bAmp(m){const a=V.speaking?Math.min(1,m/70):0;B.amp=B.amp*.6+a*.4;B.el.style.setProperty('--amp',B.amp.toFixed(3));
 const now=Date.now();if(B.amp>.35&&now-B.lastRune>140&&!B.el.hidden&&!reduce){B.lastRune=now;const r=B.el.ownerDocument.createElement('span');
  r.className='rune';r.textContent='✦✧⟡᛫'[Math.floor(Math.random()*4)];r.style.setProperty('--dx',(Math.random()*80-40)+'px');
  B.el.querySelector('.bwrap').appendChild(r);setTimeout(()=>r.remove(),1450)}}
function bAudio(){if(B.an||!V.actx||!AUD.srcObject)return;B.an={stop(){}};const stream=AUD.srcObject;
 vmeter(stream,bAmp).then(h=>{if(B.an&&AUD.srcObject===stream)B.an=h;else h.stop()}).catch(e=>{
  try{const an=V.actx.createAnalyser();an.fftSize=256;V.actx.createMediaStreamSource(stream).connect(an);const buf=new Uint8Array(an.fftSize),me={stop(){me.off=true}};B.an=me;
   (function tick(){if(me.off)return;an.getByteTimeDomainData(buf);let m=0;for(const v of buf)m=Math.max(m,Math.abs(v-128));bAmp(m);
    bWin().requestAnimationFrame(tick)})()}catch(e2){console.warn('buddy audio',e2)}})}
function bAudioReset(){if(B.an)B.an.stop();B.an=null;B.amp=0;B.el.style.setProperty('--amp',0)}
// drag (click without moving = toggle the speech bubble)
B.el.querySelector('.bwrap').addEventListener('pointerdown',e=>{if(e.target.closest('.btools')||B.pip)return;
 B.drag={sx:e.clientX,sy:e.clientY,x:B.x,y:B.y,moved:false};B.el.classList.add('drag');e.target.setPointerCapture&&e.target.setPointerCapture(e.pointerId)});
addEventListener('pointermove',e=>{if(!B.drag)return;const dx=e.clientX-B.drag.sx,dy=e.clientY-B.drag.sy;if(Math.abs(dx)+Math.abs(dy)>4)B.drag.moved=true;
 if(B.drag.moved){B._px=null;bPlace(B.drag.x+dx,B.drag.y+dy,false)}});
addEventListener('pointerup',()=>{if(!B.drag)return;const moved=B.drag.moved;B.drag=null;B.el.classList.remove('drag');
 if(!moved)B.bub.classList.toggle('on');bWander()});
addEventListener('resize',()=>{if(!B.el.hidden&&!B.pip)bPlace(B.x,B.y,false)});
$('#bmute').onclick=()=>$('#vmute').click();
$('#bcut').onclick=()=>vinterrupt('button');
$('#bend').onclick=()=>{if(V.pc)vstop('hung up')};
const BP=$('#bpip'),BM=$('#bmin');
$('#bmin').onclick=()=>{B.el.classList.toggle('mini');BM.textContent=B.el.classList.contains('mini')?'➕':'➖';bMode();if(!B.pip&&!bIsFull())bHome()};
$('#bfull').onclick=()=>{B.full=!bIsFull();localStorage.setItem('buddyfull',B.full?'on':'off');B.el.classList.remove('mini');BM.textContent='➖';
 if(B.agent)bLook(ag(B.agent));if(!bIsFull()&&!B.pip)bHome()};
// pop-out: Document Picture-in-Picture = an always-on-top mini window over every app (Chrome/Edge 116+)
BP.hidden=!('documentPictureInPicture' in window);
BP.onclick=async()=>{if(B.pip){B.pip.close();return}
 try{const w=await documentPictureInPicture.requestWindow({width:330,height:340});B.pip=w;
  [...document.styleSheets].forEach(ss=>{try{const st=w.document.createElement('style');st.textContent=[...ss.cssRules].map(r=>r.cssText).join('\n');w.document.head.appendChild(st)}catch(e){}});
  w.document.title='Roundtable companion';w.document.body.style.cssText='margin:0;background:var(--wallbg,radial-gradient(circle at 50% 70%,#1b2130,#0b0d12));overflow:hidden;height:100vh';
  B.el.classList.add('inpip');B.el.classList.remove('below','edge-l','edge-r','full');w.document.body.appendChild(B.el);BP.textContent='↩️';if(B.agent)bLook(ag(B.agent));
  w.document.addEventListener('keydown',e=>{if(e.key=='Escape'&&V.pc&&(V.speaking||V.responding)){e.preventDefault();vinterrupt('button')}});
  w.document.addEventListener('pointermove',e=>bDepthPointer(e.clientX,e.clientY));
  w.addEventListener('pagehide',()=>{B.pip=null;B.el.classList.remove('inpip');document.body.appendChild(B.el);BP.textContent='📌';if(!B.el.hidden){if(B.agent)bLook(ag(B.agent));if(!bIsFull())bHome()}})}
 catch(e){console.warn('pip',e)}};
// Minimized/hidden browser -> the Roundtable Companion app (if running) shows the agent picked here as a transparent full-body
// figure on the desktop: live-mirrored during a call, idle otherwise. The boxed Picture-in-Picture pop-out is the in-call fallback.
const MIR={t:null,was:false,desk:false};
function mirPush(now){if(DESK)return;clearTimeout(MIR.t);MIR.t=setTimeout(mirSend,now?0:200)}
async function mirSend(){if(DESK)return;const on=!!V.pc,show=on||document.hidden;if(!show&&!MIR.was)return;MIR.was=show;
 const s={agent:V.agent,oncall:on,hidden:document.hidden,st:on?B.state:'idle',muted:!!V.muted,me:B.bub.classList.contains('me'),
  text:B.bub.classList.contains('on')&&!B.bub.querySelector('.dots')?B.bub.textContent.slice(-400):''};
 try{const d=await(await fetch('/api/mirror',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(s)})).json();MIR.desk=!!d.desk;
  for(const c of d.cmds||[]){if(!V.pc)break;if(c=='cut')vinterrupt('button');else if(c=='mute')$('#vmute').click();else if(c=='end')vstop('hung up')}}catch(e){}}
if(!DESK){for(const n of ['bState','bSay','bAppend']){const f=window[n];window[n]=function(...a){const r=f.apply(this,a);mirPush();return r}}
 document.addEventListener('visibilitychange',()=>mirPush(true));
 setInterval(()=>{if(document.hidden||(MIR.was&&!V.pc))mirSend()},1000);
 addEventListener('pagehide',()=>{try{navigator.sendBeacon('/api/mirror',new Blob([JSON.stringify({agent:V.agent,oncall:false,hidden:false})],{type:'application/json'}))}catch(e){}})}
// Chrome 134+: while on a call, switching away from this tab pops the companion out (skipped when the desktop app mirrors it)
try{navigator.mediaSession.setActionHandler('enterpictureinpicture',()=>{if(V.pc&&B.on&&!B.pip&&!MIR.desk)BP.onclick()})}catch(e){}
function bToggle(){B.on=!B.on;localStorage.setItem('buddy',B.on?'on':'off');$('#vbuddy').textContent=`🧚 Companion: ${B.on?'on':'off'}`;
 if(!B.on)bHide();else if(V.pc)bShow(ag(V.agent))}
$('#vbuddy').textContent=`🧚 Companion: ${B.on?'on':'off'}`;$('#vbuddy').onclick=bToggle;
const TOOL_LABEL={team_status:'checking the team…',agent_activity:'reading the run log…',delegate:'sending a task…',check_task:'checking on the task…',
 homelab_inventory:'scanning the homelab…',security_findings:'reviewing security findings…',grafana_dashboard:'reading Grafana…',
 glance_dashboard:'looking at Glance…',report_access_gap:'logging an access gap…',backup_status:'checking the backups…',look_at_camera:'taking a look…',
 remember:'holding on to that…',recall_memories:'remembering…',search_vault:'looking through your notes…',read_note:'reading your note…',recent_notes:'catching up on your week…',
  forge_status:'checking the forge…',compute_audit:'auditing compute & storage…',verify_host:'sniffing out the host…',recent_verdicts:'reading the verdicts…',
  search_homelab_docs:'searching the records…',docs_status:'checking the ledger…',scout_report:'unrolling the scouting report…',mirror_status:'checking the mirror…',
  blog_status:'checking the blog studio…',blog_questions:'gathering her questions…',blog_note:'taking notes…',
  consult:'checking with a colleague…'};
$('#vtalk').onclick=()=>vstart('');$('#vend').onclick=()=>vstop('hung up');
$('#vmute').onclick=()=>{V.muted=!V.muted;vgate();vbtns()};
$('#vcut').onclick=()=>vinterrupt('button');
$('#vsearch').oninput=vpickRender;
const audioDialog=$('#vaudio');
function vmicsettings(){audioDialog.showModal();vmics();if(DESK)deskCapture(true)}
function vmicsettingsclose(){if(MIC.testing)vmicteststop();audioDialog.close()}
$('#vaudiosettings').onclick=vmicsettings;$('#dmic').onclick=vmicsettings;
$('#vaudioclose').onclick=vmicsettingsclose;
audioDialog.addEventListener('cancel',()=>{if(MIC.testing)vmicteststop()});
audioDialog.addEventListener('close',()=>{if(!audioDialog.open){if(MIC.testing)vmicteststop();if(DESK)deskCapture(false)}});
$('#vmicsel').onchange=e=>vmicchange(e.target.value);$('#vmictest').onclick=vmictest;$('#vmicrefresh').onclick=()=>vmicdeviceschanged();
vmics();navigator.mediaDevices&&navigator.mediaDevices.addEventListener&&navigator.mediaDevices.addEventListener('devicechange',()=>vmicdeviceschanged());
document.addEventListener('keydown',e=>{if(e.key=='Escape'&&!audioDialog.open&&V.pc&&(V.speaking||V.responding)){e.preventDefault();vinterrupt('button')}});
V.duplex=localStorage.getItem('vduplex')=='1';$('#vduplex').checked=V.duplex;
// The engine choice is kept on the server so the web portal and the desktop app use the same one
// (each has its own browser storage); localStorage only seeds the first paint.
function vengineSet(e){if(e!='openai'&&e!='local')return;V.engine=e;$('#vengine').value=e;try{localStorage.setItem('voiceEngine',e)}catch(x){}}
async function vengineSync(){try{const r=await fetch('/api/voice/engine');if(r.ok)vengineSet((await r.json()).engine)}catch(e){}}
$('#vengine').value=V.engine;$('#vengine').onchange=e=>{vengineSet(e.target.value);post('/api/voice/engine',{engine:V.engine}).catch(()=>{})};
vengineSync();
$('#vwall').onclick=()=>wallShow();
$('#vduplex').onchange=e=>{V.duplex=e.target.checked;localStorage.setItem('vduplex',V.duplex?'1':'0');vgate()};
window.addEventListener('pagehide',()=>{if(V.run)vend('page closed')});
// ---------------------------------------------------------------- desktop companion (Roundtable Companion app)
// The app window is transparent and click-through; it only catches the mouse over the character's visible pixels
// (alpha test on the full-body PNG) and over the buttons, so everything around the outline stays clickable.
const D={cap:null,hovT:null,a:{src:'',d:null}};
function deskSay(t){const s=String(t).replace(/<[^>]+>/g,'').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>');
 bSay(s,'');B.hideT=setTimeout(()=>B.bub.classList.remove('on'),9000)}
function deskAlpha(px,py){const img=B.img,A=D.a;
 if(A.src!=img.src){A.src=img.src;A.d=null;try{const k=256/img.naturalWidth,c=document.createElement('canvas');c.width=256;c.height=Math.round(img.naturalHeight*k);
  const x=c.getContext('2d',{willReadFrequently:true});x.drawImage(img,0,0,c.width,c.height);Object.assign(A,{d:x.getImageData(0,0,c.width,c.height).data,w:c.width,h:c.height,k})}catch(e){}}
 if(!A.d)return 255;const X=Math.min(A.w-1,Math.floor(px*A.k)),Y=Math.min(A.h-1,Math.floor(py*A.k));return A.d[(Y*A.w+X)*4+3]}
function deskHit(x,y){if(audioDialog.open)return 'ui';const el=document.elementFromPoint(x,y);if(el&&el.closest('.btools button,#dpanel,#dnote.on,#wallx'))return 'ui';if(deskTeamAt(x,y))return 'team';
 const img=B.img;if(B.el.hidden||!img.naturalWidth)return '';const r=img.getBoundingClientRect();
 const s=Math.min(r.width/img.naturalWidth,r.height/img.naturalHeight),ox=r.left+(r.width-img.naturalWidth*s)/2,oy=r.bottom-img.naturalHeight*s;
 const px=(x-ox)/s,py=(y-oy)/s;if(px<0||py<0||px>=img.naturalWidth||py>=img.naturalHeight)return '';
 return deskAlpha(px,py)>40?'body':''}
// summon_team on the desktop: the window grows to the whole work area and every agent pops up as a full-body figure
// in a two-row group to Gideon's left; figures are click-through except their visible pixels, like Gideon himself.
async function deskTeam(list,focus){W.desk=true;document.documentElement.classList.add('team');$('#wall').hidden=false;$('#wgrid').innerHTML='<div id="wring"></div>';
 if(window.desk&&window.desk.team){window.desk.team(true);await new Promise(r=>{addEventListener('resize',r,{once:true});setTimeout(r,500)})}
 if(!W.desk)return;deskTeamLayout();const fi=list.findIndex(a=>a.id==focus);if(fi>=0)wallFocus(fi,true);wallLive();
 if(V.pc&&!V.local)setTimeout(wallGreet,1500);else setTimeout(rollCall,1100)}  // OpenAI calls keep the short hellos
function deskTeamLayout(){if(!W.desk)return;const n=W.list.length,vw=innerWidth,vh=innerHeight,aw=B.el.getBoundingClientRect().width||Math.min(vh,852)*.667;
 const left=24,right=Math.max(left+200,vw-aw*.78),rows=n>10?2:1,per=Math.ceil(n/rows),slot=(right-left)/per;
 const h=Math.max(150,Math.min(vh*(rows==2?.5:.66),slot*2.8));  // cutouts are ~half transparent margin, so neighbours may overlap
 $('#wring').innerHTML=W.list.map((a,k)=>{const r=rows==2?(k%2?0:1):1,i=rows==2?Math.floor(k/2):k,back=rows==2&&r==0,hh=Math.round(h*(back?.86:1));
  const x=Math.round(left+slot*(i+(back?1:.5))),y=back?Math.round(h*.30):6;
  return `<button type="button" class="wt dt" data-id="${esc(a.id)}" aria-label="${esc(a.name)}" style="--c:${hex(a.color)};--d:${k*45}ms;--b:-${(k*.37%4).toFixed(2)}s;left:${Math.min(x,right)}px;bottom:${y}px;height:${hh}px;z-index:${back?1:2}">`+
   `<img src="${esc(dtPoster(a))}" alt="" draggable="false">${dtClips(a)}<b>${a.emoji||''} ${esc(a.name)}</b><small>${esc((a.role||'').split('·')[0])}</small></button>`}).join('');
 document.querySelectorAll('#wring .wt.dt').forEach(t=>{const v=t.querySelector('video[data-k=idle]');if(v)v.addEventListener('canplay',()=>{if(!t.isConnected)return;t.classList.add('anim');dtAnim(t,t.classList.contains('speak')?'talk':'idle')},{once:true})})}
// summoned figures move like Gideon: the same transparent idle / listen / talk clips over a matching poster frame
const dtHasClips=a=>!!(a.anim&&a.anim.idle&&a.anim.poster&&!DEPTH.motion.matches);
function dtPoster(a){return dtHasClips(a)?a.anim.poster:a.avatar_full||a.avatar||''}
function dtClips(a){if(!dtHasClips(a))return '';return ['idle','listen','talk'].map(k=>[].concat(a.anim[k]||[]).filter(Boolean).map(src=>
  `<video data-k="${k}" src="${esc(src)}" muted playsinline loop preload="${k=='idle'?'auto':'metadata'}" aria-hidden="true"></video>`).join('')).join('')}
function dtAnim(t,k){if(!t||!t.classList.contains('anim'))return;const vs=[...t.querySelectorAll('video')],pool=vs.filter(v=>v.dataset.k==k),use=pool.length?pool:vs.filter(v=>v.dataset.k=='idle');
 if(!use.length||use.some(v=>v.classList.contains('on')))return;const pick=use[Math.floor(Math.random()*use.length)];
 for(const v of vs){if(v===pick){try{v.currentTime=0}catch(e){}v.play().catch(()=>{});v.classList.add('on')}else if(v.classList.contains('on')){v.classList.remove('on');setTimeout(()=>{if(!v.classList.contains('on'))v.pause()},300)}}}
addEventListener('resize',()=>{if(W.desk&&innerWidth>900)deskTeamLayout()});
const DTA={};
function imgAlpha(img,x,y){if(!img||!img.naturalWidth)return 0;const r=img.getBoundingClientRect(),s=Math.min(r.width/img.naturalWidth,r.height/img.naturalHeight);
 const px=(x-(r.left+(r.width-img.naturalWidth*s)/2))/s,py=(y-(r.bottom-img.naturalHeight*s))/s;if(px<0||py<0||px>=img.naturalWidth||py>=img.naturalHeight)return 0;
 let A=DTA[img.src];if(!A){A=DTA[img.src]={d:null};try{const k=128/img.naturalWidth,c=document.createElement('canvas');c.width=128;c.height=Math.round(img.naturalHeight*k);
  const g=c.getContext('2d',{willReadFrequently:true});g.drawImage(img,0,0,c.width,c.height);Object.assign(A,{d:g.getImageData(0,0,c.width,c.height).data,w:c.width,h:c.height,k})}catch(e){}}
 if(!A.d)return 255;return A.d[(Math.min(A.h-1,Math.floor(py*A.k))*A.w+Math.min(A.w-1,Math.floor(px*A.k)))*4+3]}
function deskTeamAt(x,y){if(!W.desk)return null;const ts=[...document.querySelectorAll('#wring .wt.dt')].sort((a,b)=>b.style.zIndex-a.style.zIndex);
 for(const t of ts){const r=t.getBoundingClientRect();if(x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom&&imgAlpha(t.querySelector('img'),x,y)>40)return t.dataset.id}return null}
function deskTeamPick(id){const i=W.list.findIndex(a=>a.id==id);if(i<0||id==V.agent)return;rcStop();wallFocus(i,true);
 if(V.pc||LV.ws){V.handoff={agent:id,keepWall:true,ctx:'Hermes picked you from his team on the desktop. Greet him briefly and ask what he needs.',reason:''};vdohandover()}
 else{vpick(id);vstart('Hermes picked you from his team on the desktop. Greet him briefly and ask what he needs.')}}
// Roll call: each lead introduces themself in turn (own Kokoro voice from the PC speech server, highlighted
// while speaking), then Gideon closes. TTS only: no call, no OpenAI. Captions alone when the voice server is unreachable.
const RC={on:false,ws:null,ctx:null,t:0,wait:null,ampT:null,muted:null};
// in a local call: let Gideon finish his line, then mute the call's mic server-side so the intros are not heard as Hermes
function rcCallMute(on){const w=on?LV.ws:RC.muted;if(w&&w.readyState==1)w.send(JSON.stringify({type:'mute',on}));RC.muted=on?w:null}
function rcIntro(a){const say=x=>String(x||'').replace(/&/g,'and').replace(/\s*\/\s*/g,' and ').replace(/→/g,'to').trim(),
  [main,...rest]=(a.role||'').split('·'),detail=say(rest.join(', '));
 return `Hi Hermes, I'm ${a.name}, your ${say(main)}.`+(detail?` I handle ${detail.charAt(0).toLowerCase()+detail.slice(1)}.`:'')}
const RC_HOME=()=>"And I'm Gideon, your chief of staff. "+(W.who=='all'?"That's the whole team":'Those are your leads')+", Hermes. Just tell me what you need and I'll bring in the right one.";
async function rollCall(){if(RC.on||!W.desk||(V.pc&&!V.local))return;RC.on=true;
 if(V.local){for(let q=0,i=0;i<150&&q<6&&RC.on;i++){await new Promise(x=>setTimeout(x,100));q=V.speaking||V.responding||lvPlaying()?0:q+1}
  if(!RC.on)return;rcCallMute(true)}
 $('#wring').classList.add('rc');
 try{const cfg=await J('/api/voice/local/info');if(cfg&&cfg.enabled&&RC.on){const ws=new WebSocket(cfg.url.replace(/[/]+$/,'')+'/ws?t='+encodeURIComponent(cfg.token));
   ws.binaryType='arraybuffer';await new Promise((res,rej)=>{ws.onopen=res;ws.onerror=rej;setTimeout(rej,4000)});
   RC.ws=ws;RC.ctx=new AudioContext({latencyHint:'interactive'});await RC.ctx.resume();RC.t=0;
   ws.onmessage=e=>{if(typeof e.data!='string'){rcAudio(e.data);return}let m;try{m=JSON.parse(e.data)}catch(x){return}if(m.type=='tts_end'&&RC.wait)RC.wait(m.id)}}}
 catch(e){RC.ws=null}  // no voice server: captions only
 for(const a of W.list){if(!RC.on)break;const t=document.querySelector(`#wring .wt[data-id="${CSS.escape(a.id)}"]`);if(!t)continue;
  t.classList.add('speak');dtAnim(t,'talk');const s=document.createElement('span');s.className='wsay';s.textContent=rcIntro(a);t.appendChild(s);
  await rcSay(a.id,s.textContent);t.classList.remove('speak');dtAnim(t,'idle');s.remove()}
 if(RC.on){$('#wring').classList.remove('rc');bState('talking');bSay(RC_HOME(),'');RC.ampT=setInterval(()=>{B.amp=B.amp*.45+Math.random()*.3;B.el.style.setProperty('--amp',B.amp.toFixed(3))},90);
  await rcSay('boss',RC_HOME());clearInterval(RC.ampT);B.amp=0;B.el.style.setProperty('--amp',0);bState('idle');B.hideT=setTimeout(()=>B.bub.classList.remove('on'),5000)}
 rcStop()}
function rcAudio(buf){const c=RC.ctx;if(!c)return;const i16=new Int16Array(buf),f=new Float32Array(i16.length);for(let k=0;k<i16.length;k++)f[k]=i16[k]/32768;
 const b=c.createBuffer(1,f.length,24000);b.copyToChannel(f,0);const src=c.createBufferSource();src.buffer=b;src.connect(c.destination);
 const at=Math.max(c.currentTime+.05,RC.t);src.start(at);RC.t=at+b.duration}
// speak one line and resolve when its audio has finished playing (or after reading time without a voice server)
function rcSay(agent,text){return new Promise(done=>{if(!RC.on)return done();const read=Math.max(2500,text.length*55);V.last=Date.now();  // counts as call activity
 if(!RC.ws||RC.ws.readyState!=1){const tm=setTimeout(done,read);RC.wait=()=>{clearTimeout(tm);done()};return}
 const id='rc'+Date.now()+agent,guard=setTimeout(()=>{RC.wait=null;done()},read+15000);
 RC.wait=x=>{if(x!=id&&x!==undefined)return;RC.wait=null;clearTimeout(guard);const left=RC.ctx?Math.max(0,RC.t-RC.ctx.currentTime):0;setTimeout(done,left*1000+350)};
 RC.ws.send(JSON.stringify({type:'say',id,text,agent}))})}
function rcStop(){rcCallMute(false);if(!RC.on&&!RC.ws)return;RC.on=false;const w=RC.wait;RC.wait=null;if(w)w();clearInterval(RC.ampT);
 try{RC.ws&&RC.ws.close()}catch(e){}RC.ws=null;try{RC.ctx&&RC.ctx.close()}catch(e){}RC.ctx=null;RC.t=0;
 const r=$('#wring');if(r)r.classList.remove('rc');document.querySelectorAll('#wring .wt.speak').forEach(t=>{t.classList.remove('speak');t.querySelectorAll('.wsay').forEach(x=>x.remove())})}
function deskCapture(on){if(D.cap===on)return;D.cap=on;window.desk&&window.desk.clickThrough(!on)}
function deskHover(on){clearTimeout(D.hovT);if(on)B.el.classList.add('hov');
 else D.hovT=setTimeout(()=>{if(!$('#dpanel').classList.contains('open'))B.el.classList.remove('hov')},1600)}
function deskPanel(){$('#dpanel').innerHTML=AG.map(a=>`<button data-id="${a.id}" class="${a.id==V.agent?'on':''}" style="--c:${hex(a.color)}" title="${esc(a.name)} · ${esc(a.role.split('·')[0])}">`+
  `<span class="avw"><img src="${a.avatar||''}" alt="">${sdot(a)}</span>${esc(a.name.split(' ')[0])}</button>`).join('')}
// tray / global-shortcut commands from the Electron app; 'home' (Ctrl+Alt+A) brings Gideon in and starts listening
// Ctrl+Alt+A is a toggle: while a call, the team or the roll call is running it stops everything and hides the figure
function deskCmd(c){if(MV.on&&c!='agents'){mirCmd(c=='talk'||c=='home'?'end':c);if(c=='home')window.desk&&window.desk.hide();return}if(c=='home'){if(V.pc||V.local||LV.ws||W.desk||RC.on){if(V.pc||V.local||LV.ws)vstop('hung up');else wallHide();window.desk&&window.desk.hide()}else{vpick('boss');vstart('')}}else if(c=='talk'){if(V.pc)vstop('hung up');else vstart('')}else if(c=='cut')vinterrupt('button');
  else if(c=='mute'&&V.pc)$('#vmute').click();else if(c=='notetalk')nAct('talk');else if(c=='team'){if(W.desk)wallHide();else wallShow()}else if(c=='agents'){$('#dpanel').classList.add('open');deskPanel();deskCapture(true);B.el.classList.add('hov')}}
async function deskInit(){B.on=true;B.full=true;
 // Gideon is the desktop's home figure; others step in only for a call
 await loadVoice('boss');bShow(ag(V.agent));deskPanel();
 const a=ag(V.agent);if(V.info&&!(V.info.ok&&V.info.authed))deskSay(V.info.authed?V.info.why:'Not signed in to Roundtable: use the tray icon → Sign in.');
 else deskSay(`${a.name} is here, Master Hermes. Press Ctrl+Alt+A (or double-click me) to talk.`);
 addEventListener('mousemove',e=>{const h=deskHit(e.clientX,e.clientY);if(W.desk){const id=h=='team'&&deskTeamAt(e.clientX,e.clientY),i=W.list.findIndex(a=>a.id==id);if(i!=W.f){W.f=i;document.querySelectorAll('#wring .wt').forEach((t,k)=>{t.classList.toggle('front',k==i);if(!t.classList.contains('speak'))dtAnim(t,k==i?'listen':'idle')})}}deskCapture(!!h||$('#dpanel').classList.contains('open'));deskHover(!!h)});
 document.addEventListener('mouseleave',()=>deskHover(false));
 addEventListener('click',e=>{const id=deskTeamAt(e.clientX,e.clientY);if(id)deskTeamPick(id)});
 addEventListener('pointerdown',e=>{if(e.button==0&&!W.desk&&deskHit(e.clientX,e.clientY)=='body'){D.drag=true;window.desk&&window.desk.drag(true)}});
 addEventListener('pointerup',()=>{if(D.drag){D.drag=false;window.desk&&window.desk.drag(false)}});
 addEventListener('blur',()=>{if(D.drag){D.drag=false;window.desk&&window.desk.drag(false)}});
 addEventListener('dblclick',e=>{if(deskHit(e.clientX,e.clientY)=='body'&&!V.pc&&!MV.on)vstart('')});
 $('#dtalk').onclick=()=>{if(!MV.on)vstart('')};$('#dclose').onclick=()=>window.desk&&window.desk.hide();
 $('#dagents').onclick=()=>{$('#dpanel').classList.toggle('open');deskPanel()};
 $('#dteam').onclick=()=>{if(W.desk)wallHide();else wallShow()};
 $('#dpanel').onclick=e=>{const b=e.target.closest('button');if(!b)return;$('#dpanel').classList.remove('open');vpick(b.dataset.id)};
 window.desk&&window.desk.onCmd(deskCmd);
 setInterval(async()=>{if(!V.pc){try{AG=await J('/api/agents');deskPanel()}catch(e){}}},60000);
 B.el.addEventListener('click',e=>{const b=MV.on&&e.target.closest('#bcut,#bmute,#bend');if(!b)return;e.stopImmediatePropagation();e.preventDefault();
  mirCmd({bcut:'cut',bmute:'mute',bend:'end'}[b.id])},true);
 setInterval(mirPoll,400)}
// mirror of a browser call (browser minimized): show that agent, its state and words; buttons go back to the browser
const MV={on:false,shown:false,agent:null,text:'',ampT:null};
function mirCmd(c){fetch('/api/mirror/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c})}).catch(()=>{})}
function mirShow(on){if(MV.shown==on)return;MV.shown=on;window.desk&&window.desk.mirror&&window.desk.mirror(on)}
async function mirPoll(){if(V.pc){if(MV.on)mirExit(true);return}
 let d;try{d=await(await fetch('/api/mirror')).json()}catch(e){return}
 const s=d.state||{},known=!!(s.hidden&&AG.some(a=>a.id==s.agent)),live=known&&s.oncall&&d.age<8;
 // browser hidden, no call: show the agent picked there (Chrome throttles hidden tabs to ~1 push/min, hence the long window)
 if(known&&!s.oncall&&d.age<75){if(MV.on)mirExit(true);mirShow(true);return}
 if(!live){if(MV.on)mirExit();else mirShow(false);return}
 if(!MV.on){MV.on=true;MV.agent=null;MV.text='';mirShow(true);B.el.classList.add('oncall','mirror')}
 if(MV.agent!=s.agent){MV.agent=s.agent;bLook(ag(s.agent));B.el.classList.remove('exit');B.el.classList.add('enter');setTimeout(()=>B.el.classList.remove('enter'),750)}
 if(B.state!=(s.st||'idle'))bState(s.st||'idle');
 if(s.text&&s.text!=MV.text){MV.text=s.text;bSay(s.text,s.me?'me':'')}
 $('#bmute').textContent=s.muted?'🎙️':'🔇';
 const talk=s.st=='talking';if(talk&&!MV.ampT)MV.ampT=setInterval(()=>{B.amp=B.amp*.45+Math.random()*.55*.55;B.el.style.setProperty('--amp',B.amp.toFixed(3))},90);
 if(!talk&&MV.ampT){clearInterval(MV.ampT);MV.ampT=null;B.amp=0;B.el.style.setProperty('--amp',0)}}
function mirExit(keep){MV.on=false;MV.text='';clearInterval(MV.ampT);MV.ampT=null;B.amp=0;B.el.style.setProperty('--amp',0);
 B.el.classList.remove('mirror');if(!V.pc)B.el.classList.remove('oncall');bLook(ag(V.agent));bState('idle');B.bub.classList.remove('on');
 if(!keep)mirShow(false)}
// desk pop-ups: Gideon comes forward when something needs Hermes (/api/desk/events) and can bring in the agent who owns it.
// Seen ids live in localStorage; the first run only shows open urgent items, never the whole backlog.
const N={q:[],cur:null,t:null},NRANK={urgent:0,approval:1,nudge:2};
async function nPoll(){let ev;try{ev=await J('/api/desk/events')}catch(e){return}if(!Array.isArray(ev))return;
 let S;const first=localStorage.getItem('deskSeen')===null;try{S=new Set(JSON.parse(localStorage.getItem('deskSeen')||'[]'))}catch(e){S=new Set()}
 const ids=new Set(ev.map(e=>e.id));for(const e of ev)if(!S.has(e.id)){S.add(e.id);if(!first||e.kind=='urgent')N.q.push(e)}
 for(const i of [...S])if(!ids.has(i))S.delete(i);localStorage.setItem('deskSeen',JSON.stringify([...S]));nNext()}
async function nNext(){if(N.cur||V.pc||MV.on||!N.q.length)return;N.q.sort((a,b)=>NRANK[a.kind]-NRANK[b.kind]);const e=N.q.shift();
 const ok=window.desk&&window.desk.popup?await window.desk.popup({kind:e.kind,title:e.title}):true;if(!ok){nNext();return}
 if(V.agent!='boss')vpick('boss');N.cur=e;const owner=ag(e.agent),who=owner.id!='boss'&&AG.some(a=>a.id==owner.id)?owner:null,first=who&&esc(who.name.split(' ')[0]);
 const el=$('#dnote');el.style.setProperty('--nc',e.kind=='urgent'?'#f87171':e.kind=='approval'?'#fbbf24':hex(ag('boss').color));
 el.innerHTML=`<b>${e.kind=='urgent'?'🚨 Hermes, something urgent':e.kind=='approval'?'✅ Hermes, this needs your approval':'🔔 Hermes, something new for you'}</b>${esc(e.title)}`+
  `<div class="dnm">${who?esc(who.name)+' · ':''}${e.at?fmt(e.at):''}</div><div class="dnb"><button class="go" data-a="talk">📞 Talk to Gideon</button>`+
  `${who?`<button data-a="call">Call ${first}</button>`:''}${e.kind=='approval'?'<button data-a="open">Open</button>':''}<button data-a="later">Later</button></div>`;
 el.classList.add('on');B.el.classList.add('hov');deskCapture(true);clearTimeout(N.t);if(e.kind=='nudge')N.t=setTimeout(nClose,120000)}
function nClose(){clearTimeout(N.t);N.cur=null;$('#dnote').classList.remove('on');setTimeout(nNext,1500)}
function nAct(a){const e=N.cur;if(!e)return;const who=ag(e.agent),what={urgent:'An urgent homelab issue',approval:'An approval waiting for his ✅',nudge:'Something new'}[e.kind];nClose();
 if(a=='open')window.open('https://roundtable.hrmsmrflrii.xyz/#approvals');
 else if(a=='talk')vstart(`Hermes opened you from a desktop pop-up. ${what}: "${e.title}" (owner: ${who.name}). You have not seen any private content behind it. `+
  `Explain it briefly in simple terms and offer to hand him over to ${who.name} for details.`);
 else if(a=='call'){vpick(e.agent);vstart(`Gideon brought Hermes to you from a desktop pop-up about: "${e.title}". Greet him briefly and help him with it.`)}}
if(DESK){$('#dnote').onclick=ev=>{const b=ev.target.closest('button');if(b)nAct(b.dataset.a)};setTimeout(nPoll,8000);setInterval(nPoll,30000)}
$('#fAgent').onchange=loadRuns;$('#fStatus').onchange=loadRuns;
if(DESK)deskInit();else{route();setInterval(()=>{if((!$('#modal').style.display||$('#modal').style.display=='none')&&VIEW!='voice')load()},30000)}
</script></body></html>"""


def _warm_inventory():
    import inventory
    while True:
        try:
            inventory.warm()
        except Exception as e:  # noqa: BLE001
            o.log("portal", f"inventory warm: {e!r}")
        time.sleep(110)


def main():
    o.log("portal", f"listening on :{PORT}")
    threading.Thread(target=_warm_inventory, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
