#!/usr/bin/env python3
"""Nepheli Loux - keeper of the media stack and Master Hermes's film companion.

  media.py watch                 # every 10 min, deterministic: containers, *arr health, failed/stuck queue items,
                                 # SABnzbd warnings/stalls, Deluge errors, Bazarr/Prowlarr; new "ready to watch" items.
                                 # Failed downloads are auto-retried (blocklist + new search); container restarts need ✅
  media.py taste                 # daily: rebuild her picture of his taste (library, recent adds, requests, watch history)
  media.py digest                # evening: what finished today + one pick for tonight
  media.py status                # print the live brief
  media.py ask "<text>" [--reply-to MID] [--channel CID]   # Gideon routes media questions here
  media.py restart <container>   # docker restart on docker-lxc-media (runs only after ✅)

Chat: Master Hermes talks to her in Discord #cinema-lounge (polled by boss.py) or by voice on the portal - download
status, stack errors, recommendations and long conversations about film and series.
"""
import argparse
import contextlib
import fcntl
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

import olympus_core as o

o.load_env(".media_env")

AGENT = "media"
CHANNEL = "cinema-lounge"
TOPIC = ("🎬 Nepheli's cinema lounge: download status, media-stack alerts, recommendations and film talk. "
         "Just talk to her here (only Master Hermes's messages are read).")
MODEL = "gpt-6-luna"
HOST = os.environ.get("MEDIA_HOST", "192.168.40.11")
SSH_HOST = "docker-lxc-media"
KV_STATE, KV_TASTE, KV_CHAT, KV_LAST = "media:state", "media:taste", "media:chat", "media:last_msg"
CONTAINERS = ["radarr", "sonarr", "prowlarr", "bazarr", "sabnzbd", "deluge", "jellyfin", "jellyseerr", "overseerr",
              "lidarr", "flaresolverr", "autobrr", "tdarr", "metube"]
LOCK = o.DATA_DIR / "media.lock"
CHAT_KEEP = 40
RETRY_PER_DAY = 2
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
GB = 1024 ** 3

PERSONA = """You are Nepheli Loux, warrior of Stormveil and adopted daughter of Gideon Ofnir, now keeper of Master
Hermes's media stack on his Roundtable (Jellyfin, Radarr, Sonarr, Prowlarr, Bazarr, SABnzbd, Deluge, Jellyseerr).
You are proud, direct and honest like a warrior, but warm with him, and a true cinephile: you love film and series
deeply (craft, direction, cinematography, score, performances, themes, history, adaptations, anime too).
How you are with him:
- Address him as "Master Hermes".
- Status and errors: be precise and brief; use the live data only, never invent downloads, titles or numbers.
- Recommendations: base them on HIS taste (profile below: what he downloads, requests and watches). For each pick give
  one line on why it fits him. Never recommend what is already in his library unless he asks for a rewatch. Prefer
  well-reviewed titles; say when something is still in cinemas or not yet released.
- Deep conversations: discuss films like a thoughtful friend - meaning, symbolism, character arcs, filmmaking choices,
  how a film made him feel; share your own opinion and ask what he thought. Mark spoilers (||spoiler|| in Discord)
  unless he has clearly seen it.
- Facts about real films (cast, directors, release dates, reviews) must be correct; if unsure, say so.
- Anything that changes the stack beyond retrying a download or adding a request (container restarts, settings)
  needs his ✅ in Discord. Homelab work outside media belongs to Gideon and the rest of the Roundtable."""

_lock_t = threading.Lock()
_busy = {"on": False}


@contextlib.contextmanager
def _lock():
    o.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- API clients

def _env(k):
    return os.environ.get(k, "")


def arr(app, path, method="GET", data=None, timeout=20):
    port, key = {"radarr": (7878, "RADARR_API_KEY"), "sonarr": (8989, "SONARR_API_KEY"),
                 "prowlarr": (9696, "PROWLARR_API_KEY")}[app]
    ver = "v1" if app == "prowlarr" else "v3"
    return o.http(method, f"http://{HOST}:{port}/api/{ver}/{path}", {"X-Api-Key": _env(key)}, data,
                  timeout=timeout, retries=1)


def bazarr(path):
    return o.http("GET", f"http://{HOST}:6767/api/{path}", {"X-API-KEY": _env("BAZARR_API_KEY")}, timeout=15,
                  retries=1)


def sab(mode, extra=""):
    q = f"mode={mode}&output=json&apikey={urllib.parse.quote(_env('SABNZBD_API_KEY'))}{extra}"
    return o.http("GET", f"http://{HOST}:8081/api?{q}", {}, timeout=15, retries=1)


def seerr(path, method="GET", data=None):
    return o.http(method, f"http://{HOST}:5056/api/v1/{path}", {"X-Api-Key": _env("SEERR_API_KEY")}, data,
                  timeout=20, retries=1)


def jelly(path):
    tok = _env("JELLYFIN_API_KEY")
    return o.http("GET", f"http://{HOST}:8096/{path}", {"Authorization": 'MediaBrowser Token="' + tok + '"'},
                  timeout=20, retries=1)


def deluge_torrents():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(m, p):
        req = urllib.request.Request(f"http://{HOST}:8112/json", json.dumps({"method": m, "params": p, "id": 1}).encode(),
                                     {"Content-Type": "application/json"})
        with op.open(req, timeout=15) as r:
            return json.loads(r.read())

    if not call("auth.login", [_env("DELUGE_PASSWORD")]).get("result"):
        raise RuntimeError("Deluge login refused")
    r = call("web.update_ui", [["name", "state", "progress", "eta", "message", "total_size"], {}])
    return list(((r.get("result") or {}).get("torrents") or {}).values())


def _ok(code, d, kind=list):
    return code == 200 and isinstance(d, kind)


def _gb(b):
    return f"{(b or 0) / GB:.1f} GB"


def _eta(s):
    s = (s or "").strip()
    return "" if s in ("", "00:00:00", "0:00:00") else s


# --------------------------------------------------------------------------- collect

def collect(light=False):
    """Live picture of the stack. light=True skips the library/history calls (for frequent watch)."""
    s = {"errors": {}, "ts": o.iso()}

    def grab(name, fn):
        try:
            s[name] = fn()
        except Exception as e:  # noqa: BLE001
            s["errors"][name] = str(e)[:200]
            s[name] = None

    def containers():
        rc, out, err = o.ssh(SSH_HOST, "docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}'", 40)
        if rc != 0:
            raise RuntimeError(err or f"ssh rc {rc}")
        rows = [l.split("|", 2) for l in out.splitlines() if l.count("|") >= 2]
        return {n: {"state": st, "status": tx} for n, st, tx in rows}

    def health(app):
        c, d = arr(app, "health")
        if not _ok(c, d):
            raise RuntimeError(f"HTTP {c}")
        return [{"type": h.get("type"), "source": h.get("source"), "message": h.get("message")} for h in d]

    def queue(app):
        inc = "includeMovie=true" if app == "radarr" else "includeSeries=true&includeEpisode=true"
        c, d = arr(app, f"queue?pageSize=200&{inc}")
        if not _ok(c, d, dict):
            raise RuntimeError(f"HTTP {c}")
        out = []
        for r in d.get("records", []):
            if app == "radarr":
                title = f"{(r.get('movie') or {}).get('title', '?')} ({(r.get('movie') or {}).get('year', '')})"
            else:
                ep = r.get("episode") or {}
                title = (f"{(r.get('series') or {}).get('title', '?')} S{ep.get('seasonNumber', 0):02d}"
                         f"E{ep.get('episodeNumber', 0):02d}")
            msgs = [m for sm in r.get("statusMessages") or [] for m in sm.get("messages") or []]
            out.append({"app": app, "id": r.get("id"), "title": title, "status": r.get("status"),
                        "tracked": r.get("trackedDownloadStatus"), "state": r.get("trackedDownloadState"),
                        "size": r.get("size") or 0, "left": r.get("sizeleft") or 0, "eta": _eta(r.get("timeleft")),
                        "client": r.get("downloadClient"), "error": r.get("errorMessage") or "",
                        "messages": msgs[:3], "movieId": r.get("movieId"), "episodeId": r.get("episodeId"),
                        "seriesId": r.get("seriesId")})
        return out

    def sabq():
        c, d = sab("queue")
        if not _ok(c, d, dict):
            raise RuntimeError(f"HTTP {c}")
        q = d["queue"]
        c, w = sab("warnings")
        warns = (w or {}).get("warnings", []) if isinstance(w, dict) else []
        return {"status": q.get("status"), "paused": q.get("paused"), "speed": q.get("speed"),
                "kbpersec": float(q.get("kbpersec") or 0), "slots": int(q.get("noofslots") or 0),
                "left": q.get("sizeleft"), "free_gb": float(q.get("diskspace1") or 0),
                "free2_gb": float(q.get("diskspace2") or 0),
                "warnings": [{"text": x.get("text"), "time": x.get("time")} for x in warns if isinstance(x, dict)][-30:]}

    def bz():
        c, d = bazarr("system/health")
        if c != 200:
            raise RuntimeError(f"HTTP {c}")
        return [f"{x.get('object')}: {x.get('issue')}" for x in (d or {}).get("data", [])]

    def jf_now():
        c, d = jelly("Sessions")
        if not _ok(c, d):
            raise RuntimeError(f"HTTP {c}")
        return [{"user": x.get("UserName"), "title": (x.get("NowPlayingItem") or {}).get("Name"),
                 "series": (x.get("NowPlayingItem") or {}).get("SeriesName")} for x in d if x.get("NowPlayingItem")]

    grab("containers", containers)
    grab("radarr_health", lambda: health("radarr"))
    grab("sonarr_health", lambda: health("sonarr"))
    grab("prowlarr_health", lambda: health("prowlarr"))
    grab("radarr_queue", lambda: queue("radarr"))
    grab("sonarr_queue", lambda: queue("sonarr"))
    grab("sab", sabq)
    grab("deluge", deluge_torrents)
    grab("bazarr", bz)
    grab("playing", jf_now)
    grab("imports", recent_imports)
    if not light:
        grab("counts", lambda: jelly("Items/Counts")[1])
    return s


def recent_imports(limit=20):
    out = []
    c, d = arr("radarr", f"history?pageSize={limit}&sortKey=date&sortDirection=descending&eventType=3&includeMovie=true")
    for x in (d or {}).get("records", []) if c == 200 else []:
        m = x.get("movie") or {}
        out.append({"key": f"r{x.get('id')}", "kind": "movie", "title": f"{m.get('title', '?')} ({m.get('year', '')})",
                    "date": x.get("date"), "genres": m.get("genres") or [], "overview": (m.get("overview") or "")[:300]})
    c, d = arr("sonarr", f"history?pageSize={limit}&sortKey=date&sortDirection=descending&eventType=3"
                         f"&includeSeries=true&includeEpisode=true")
    for x in (d or {}).get("records", []) if c == 200 else []:
        se, ep = x.get("series") or {}, x.get("episode") or {}
        out.append({"key": f"s{x.get('id')}", "kind": "episode",
                    "title": f"{se.get('title', '?')} S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
                             + (f" · {ep.get('title')}" if ep.get("title") else ""),
                    "date": x.get("date"), "genres": se.get("genres") or [], "overview": ""})
    return sorted(out, key=lambda x: x["date"] or "", reverse=True)


# --------------------------------------------------------------------------- evaluate / watch

def evaluate(s, st):
    """Open problems keyed for dedupe: key -> {sev, title, detail, fix, restart?}."""
    cur = {}
    if s.get("containers") is None:
        cur["ssh"] = {"sev": "medium", "title": "Cannot reach docker-lxc-media over SSH",
                      "detail": s["errors"].get("containers", ""), "fix": "Check CT205 is up and the SSH key."}
    else:
        for n in CONTAINERS:
            c = s["containers"].get(n)
            if not c:
                continue
            bad = c["state"] != "running" or "unhealthy" in c["status"] or "Restarting" in c["status"]
            if bad:
                cur[f"ct:{n}"] = {"sev": "high", "title": f"Container {n} is {c['state']}", "restart": n,
                                  "detail": f"`{n}`: {c['status']}",
                                  "fix": f"Restart it (`docker restart {n}` on docker-lxc-media) and check its logs."}
    for app in ("radarr", "sonarr", "prowlarr"):
        if s.get(f"{app}_health") is None:
            if app in s["errors"] or f"{app}_health" in s["errors"]:
                cur[f"api:{app}"] = {"sev": "high", "title": f"{app.title()} API not responding",
                                     "detail": s["errors"].get(f"{app}_health", ""), "fix": f"Check the {app} container.",
                                     "restart": app}
            continue
        for h in s[f"{app}_health"]:
            if h["type"] in ("warning", "error"):
                k = f"health:{app}:{h['source']}:{re.sub(r'[^a-z]', '', (h['message'] or '').lower())[:40]}"
                cur[k] = {"sev": "high" if h["type"] == "error" else "medium",
                          "title": f"{app.title()}: {h['source']}", "detail": h["message"] or "",
                          "fix": "See the message; usually an indexer, download client or root folder issue."}
    for q in (s.get("radarr_queue") or []) + (s.get("sonarr_queue") or []):
        if q["status"] in ("failed", "warning") or q["tracked"] in ("warning", "error") or \
                q["state"] in ("importBlocked", "failedPending", "importFailed"):
            msg = q["error"] or "; ".join(q["messages"]) or q["state"] or q["status"]
            cur[f"q:{q['app']}:{q['id']}"] = {
                "sev": "medium", "title": f"Download problem: {q['title']}", "detail": f"{q['state']} · {msg}"[:600],
                "fix": ("Blocklist this release and search for another." if q["status"] == "failed" or q["tracked"] == "error"
                        else "Manual import in the queue may be needed (Activity → Queue)."), "q": q}
    sb = s.get("sab")
    if sb:
        recent = [w for w in sb["warnings"] if w.get("time") and time.time() - float(w["time"]) < 3 * 3600]
        auth = [w for w in recent if "authentication" in (w["text"] or "").lower()]
        if auth:
            srv = re.search(r"Server (\S+)", auth[-1]["text"] or "")
            cur["sab:auth"] = {"sev": "high", "title": "Usenet provider login failing",
                               "detail": f"{auth[-1]['text']} ({len(auth)}× in 3 h). Usenet downloads cannot proceed.",
                               "fix": f"Check the {srv.group(1) if srv else 'provider'} account (expired or password "
                                      "changed), then update it in SABnzbd → Config → Servers."}
        elif recent:
            cur["sab:warn"] = {"sev": "low", "title": f"SABnzbd: {len(recent)} warning(s) in 3 h",
                               "detail": "\n".join(w["text"] or "" for w in recent[-3:])[:600], "fix": ""}
        if sb["paused"]:
            cur["sab:paused"] = {"sev": "medium", "title": "SABnzbd queue is paused",
                                 "detail": f"{sb['slots']} item(s) waiting.", "fix": "Resume it in SABnzbd."}
        stalled = st.get("sab_stall", 0)
        if sb["slots"] and sb["kbpersec"] < 1 and not sb["paused"] and stalled >= 6:
            cur["sab:stalled"] = {"sev": "medium", "title": "Usenet downloads stalled",
                                  "detail": f"{sb['slots']} item(s) queued, 0 KB/s for {stalled * 10} min "
                                            f"(SAB status {sb['status']}).",
                                  "fix": "Usually the Usenet server (login, quota, connection). See SABnzbd warnings."}
        for i, free in enumerate((sb["free_gb"], sb["free2_gb"])):
            if 0 < free < 100:
                cur[f"disk:{i}"] = {"sev": "high" if free < 30 else "medium",
                                    "title": f"Download disk low: {free:.0f} GB free", "detail": "", "fix":
                                    "Clear completed downloads or add storage."}
    for t in s.get("deluge") or []:
        if t.get("state") == "Error":
            cur[f"dl:{t.get('name', '')[:40]}"] = {"sev": "medium", "title": f"Torrent error: {t.get('name', '')[:60]}",
                                                    "detail": t.get("message") or "", "fix": "Recheck or remove it in Deluge."}
    for b in s.get("bazarr") or []:
        cur[f"bz:{re.sub(r'[^a-z]', '', b.lower())[:40]}"] = {"sev": "low", "title": "Bazarr (subtitles)", "detail": b,
                                                              "fix": "Bazarr → Settings → Languages: create a "
                                                                     "languages profile and set it as default."}
    for name in ("sab", "deluge", "radarr_queue", "sonarr_queue"):
        if name in s["errors"]:
            app = {"sab": "sabnzbd", "deluge": "deluge", "radarr_queue": "radarr", "sonarr_queue": "sonarr"}[name]
            cur.setdefault(f"api:{app}", {"sev": "high", "title": f"{app} not responding",
                                          "detail": s["errors"][name], "fix": f"Check the {app} container.",
                                          "restart": app})
    return cur


def _post_alert(cid, key, a, mention=False):
    fields = [("Fix", a["fix"], False)] if a.get("fix") else None
    e = o.embed(AGENT, f"{SEV_ICON.get(a['sev'], '🟡')} {a['title']}", a.get("detail") or "", fields,
                color=o.SEV_COLORS.get(a["sev"]), footer=f"Nepheli · media watch · {key}")
    return o.post(cid, content=f"<@{o.OWNER_ID}>" if mention else None, embeds=[e], mention_owner=mention)


def retry(q):
    """Blocklist a failed release, remove it from the client and search for another one."""
    c, _ = arr(q["app"], f"queue/{q['id']}?removeFromClient=true&blocklist=true", "DELETE")
    if c not in (200, 204):
        return False, f"remove failed (HTTP {c})"
    if q["app"] == "radarr" and q.get("movieId"):
        cmd = {"name": "MoviesSearch", "movieIds": [q["movieId"]]}
    elif q.get("episodeId"):
        cmd = {"name": "EpisodeSearch", "episodeIds": [q["episodeId"]]}
    else:
        return True, "removed + blocklisted (no search target)"
    c, _ = arr(q["app"], "command", "POST", cmd)
    return c in (200, 201), "blocklisted, searching for another release" if c in (200, 201) else f"search HTTP {c}"


def watch():
    with o.Run(AGENT, "media watch", "schedule") as run:
        cid = o.channel_id(CHANNEL, TOPIC)
        s = collect(light=True)
        st = o.kv_get(KV_STATE, {}) or {}
        first = not st
        sb = s.get("sab") or {}
        st["sab_stall"] = st.get("sab_stall", 0) + 1 if sb.get("slots") and sb.get("kbpersec", 0) < 1 else 0
        active, proposed = st.get("active", {}), st.get("proposed", {})
        retries = {k: v for k, v in (st.get("retries") or {}).items() if time.time() - v[-1] < 86400}
        cur = evaluate(s, st)
        new = [k for k in cur if k not in active]
        gone = [k for k in active if k not in cur]
        notes = []
        for k in new:
            a = cur[k]
            q = a.get("q")
            if q and (q["status"] == "failed" or q["tracked"] == "error") and len(retries.get(k, [])) < RETRY_PER_DAY:
                ok, why = retry(q)
                retries.setdefault(k, []).append(time.time())
                a = {**a, "detail": a["detail"] + f"\n🔁 Auto-retry: {'✅' if ok else '❌'} {why}"}
            if not first or a["sev"] in ("critical", "high"):
                mid = _post_alert(cid, k, a, mention=a["sev"] in ("critical", "high"))
                if a.get("restart") and k not in proposed:
                    proposed[k] = o.request_approval(
                        AGENT, f"Restart {a['restart']}",
                        f"Nepheli will run `docker restart {a['restart']}` on docker-lxc-media and report back.",
                        [sys.executable, os.path.abspath(__file__), "restart", a["restart"]], CHANNEL, severity="high",
                        reply_to=mid)
            active[k] = {k2: v for k2, v in a.items() if k2 != "q"} | {"since": o.iso()}
            notes.append(a["title"])
        for k in gone:
            o.post(cid, f"✅ Resolved: {active[k]['title']} (since {active[k]['since'][:16].replace('T', ' ')} UTC)")
            del active[k]
            proposed.pop(k, None)
        seen = set(st.get("seen_imports", []))
        ready = [i for i in (s.get("imports") or []) if i["key"] not in seen]
        if ready and not first:
            lines = [f"{'🎬' if i['kind'] == 'movie' else '📺'} **{i['title']}**" for i in ready[:15]]
            o.post(cid, embeds=[o.embed(AGENT, "🍿 Ready to watch on Jellyfin", "\n".join(lines),
                                        color=o.SEV_COLORS["ok"], footer="Nepheli · new in your library")])
        seen |= {i["key"] for i in s.get("imports") or []}
        st.update({"active": active, "proposed": proposed, "retries": retries, "seen_imports": sorted(seen)[-500:],
                   "last": o.iso()})
        o.kv_set(KV_STATE, st)
        run.summary = f"{len(cur)} open issue(s), {len(new)} new, {len(gone)} resolved, {len(ready)} ready"
        o.heartbeat(AGENT, run.summary)


def restart(name):
    if name not in CONTAINERS:
        print("unknown container")
        return False
    cid = o.channel_id(CHANNEL, TOPIC)
    with o.Run(AGENT, f"restart {name}", "approval") as run:
        rc, out, err = o.ssh(SSH_HOST, f"docker restart {name} >/dev/null && sleep 15 && "
                                       f"docker ps -a --filter name=^{name}$ --format '{{{{.Status}}}}'", 120)
        ok = rc == 0 and out.startswith("Up")
        run.summary = f"restart {name}: {'ok' if ok else 'failed'} {out or err}"[:300]
        o.respond(AGENT, cid, f"{'✅' if ok else '❌'} Restarted {name}" if ok else f"❌ Restart of {name} failed",
                  f"`{name}`: {out or err}", run=run, color=o.SEV_COLORS["ok" if ok else "high"])
        if not ok:
            run.finish("failed", run.summary)
    return ok


# --------------------------------------------------------------------------- brief

def brief(s=None):
    s = s or collect()
    st = o.kv_get(KV_STATE, {}) or {}
    L = [f"Media stack status ({o.local().strftime('%a %d %b %H:%M')}):"]
    if s.get("containers"):
        bad = [n for n in CONTAINERS if n in s["containers"] and s["containers"][n]["state"] != "running"]
        L.append(f"Containers: {sum(1 for n in CONTAINERS if n in s['containers'])} watched, "
                 + (f"NOT running: {', '.join(bad)}" if bad else "all running"))
    for app in ("radarr", "sonarr"):
        q = s.get(f"{app}_queue")
        if q is None:
            L.append(f"{app.title()} queue: unavailable")
            continue
        L.append(f"{'Movies' if app == 'radarr' else 'Episodes'} in queue ({app.title()}): {len(q)}")
        for x in q[:12]:
            pct = 100 - (100 * x["left"] / x["size"]) if x["size"] else 0
            L.append(f"  - {x['title']}: {x['state'] or x['status']} {pct:.0f}% of {_gb(x['size'])}"
                     + (f", ETA {x['eta']}" if x["eta"] else "") + f" via {x['client']}"
                     + (f" · {x['error'] or '; '.join(x['messages'])}" if x["error"] or x["messages"] else ""))
    sb = s.get("sab")
    if sb:
        L.append(f"SABnzbd: {sb['status']}, {sb['slots']} item(s), speed {sb['speed'] or 0}, {sb['left']} left, "
                 f"{sb['free_gb']:.0f} GB free on download disk")
    if s.get("deluge") is not None:
        t = s["deluge"]
        by = {}
        for x in t:
            by[x.get("state")] = by.get(x.get("state"), 0) + 1
        L.append(f"Deluge: {len(t)} torrent(s) " + (", ".join(f"{v} {k}" for k, v in by.items()) or ""))
    if s.get("playing"):
        L.append("Playing now on Jellyfin: " + "; ".join(f"{p['user']}: {p['series'] + ' - ' if p['series'] else ''}"
                                                         f"{p['title']}" for p in s["playing"]))
    if s.get("counts"):
        c = s["counts"]
        L.append(f"Jellyfin library: {c.get('MovieCount')} movies, {c.get('SeriesCount')} series, "
                 f"{c.get('EpisodeCount')} episodes")
    if s.get("imports"):
        L.append("Recently finished: " + "; ".join(f"{i['title']} ({(i['date'] or '')[:10]})" for i in s["imports"][:8]))
    issues = evaluate(s, st)
    L.append(f"Open problems ({len(issues)}): " + ("; ".join(f"[{a['sev']}] {a['title']}: {a.get('detail', '')[:160]}"
                                                            f" -> fix: {a.get('fix', '')}" for a in issues.values())
                                                  or "none"))
    if s["errors"]:
        L.append("Could not read: " + ", ".join(f"{k} ({v[:80]})" for k, v in s["errors"].items()))
    return "\n".join(L)


# --------------------------------------------------------------------------- taste

def library():
    """Movies and series in Radarr/Sonarr (cached 1 h)."""
    c = o.kv_get("media:lib", {}) or {}
    if c.get("t", 0) > time.time() - 3600:
        return c["movies"], c["series"]
    _, mv = arr("radarr", "movie", timeout=60)
    _, sr = arr("sonarr", "series", timeout=60)
    movies = [{"title": m.get("title"), "year": m.get("year"), "genres": m.get("genres") or [], "added": m.get("added"),
               "has": m.get("hasFile"), "imdb": ((m.get("ratings") or {}).get("imdb") or {}).get("value"),
               "tmdbId": m.get("tmdbId")} for m in (mv if isinstance(mv, list) else [])]
    series = [{"title": x.get("title"), "year": x.get("year"), "genres": x.get("genres") or [], "added": x.get("added"),
               "status": x.get("status"), "network": x.get("network"),
               "eps": (x.get("statistics") or {}).get("episodeFileCount")} for x in (sr if isinstance(sr, list) else [])]
    o.kv_set("media:lib", {"t": time.time(), "movies": movies, "series": series})
    return movies, series


def _watch_history():
    out = []
    c, us = jelly("Users")
    for u in us if _ok(c, us) else []:
        for flt, tag in (("IsPlayed", "watched"), ("IsFavorite", "favourite")):
            c, d = jelly(f"Users/{u['Id']}/Items?Recursive=true&IncludeItemTypes=Movie,Series&Filters={flt}"
                         f"&SortBy=DatePlayed&SortOrder=Descending&Limit=30")
            for i in (d or {}).get("Items", []) if c == 200 else []:
                out.append(f"{i.get('Name')} ({i.get('ProductionYear', '')}) [{tag}]")
    return sorted(set(out))


def _requests():
    c, d = seerr("request?take=40&sort=added")
    out = []
    for r in (d or {}).get("results", []) if c == 200 else []:
        ds = ((r.get("media") or {}).get("downloadStatus") or [{}])
        t = ds[0].get("title") if ds else ""
        out.append(f"{r.get('type')}: {t or (r.get('media') or {}).get('tmdbId')} ({(r.get('createdAt') or '')[:10]})")
    return out


def build_taste():
    movies, series = library()
    gen = {}
    for x in movies + series:
        for g in x["genres"]:
            gen[g] = gen.get(g, 0) + 1
    recent_m = sorted(movies, key=lambda m: m["added"] or "", reverse=True)[:40]
    recent_s = sorted(series, key=lambda m: m["added"] or "", reverse=True)[:20]
    hist = _watch_history()
    data = (f"Library: {len(movies)} movies, {len(series)} series.\nGenre counts: "
            + ", ".join(f"{g} {n}" for g, n in sorted(gen.items(), key=lambda x: -x[1])[:20])
            + "\nMost recently added movies: " + "; ".join(f"{m['title']} ({m['year']}, {'/'.join(m['genres'][:3])}"
                                                            f", IMDb {m['imdb']})" for m in recent_m)
            + "\nMost recently added series: " + "; ".join(f"{s['title']} ({s['year']}, {'/'.join(s['genres'][:3])})"
                                                            for s in recent_s)
            + "\nWatched / favourites on Jellyfin: " + ("; ".join(hist) or "(no watch history recorded)")
            + "\nAll movie titles: " + "; ".join(f"{m['title']} ({m['year']})" for m in movies)
            + "\nAll series titles: " + "; ".join(s["title"] for s in series))
    with o.Run(AGENT, "rebuild taste profile", "schedule") as run:
        text = o.llm(run, f"From Master Hermes's media library below, write Nepheli's private picture of his taste "
                          f"in film and TV (Markdown, ~250 words): favourite genres and moods, directors/franchises "
                          f"he collects, eras, what he added lately (= current mood), quality preferences (he grabs "
                          f"4K remuxes), and 3 'he would probably love' directions to explore. Facts only from the "
                          f"data.\n\n{data[:60000]}", model=MODEL, tips=False, max_output_tokens=900)
        run.summary = f"taste from {len(movies)} movies, {len(series)} series"
    o.kv_set(KV_TASTE, {"text": text.strip(), "built": o.iso(), "genres": gen,
                        "recent": [f"{m['title']} ({m['year']})" for m in recent_m[:15]]})
    return text


def taste():
    return (o.kv_get(KV_TASTE, {}) or {}).get("text") or "(taste profile not built yet)"


def in_library(title):
    movies, series = library()
    t = (title or "").lower().strip()
    return [f"{x['title']} ({x['year']})" + ("" if x.get("has", True) else " - wanted, not downloaded yet")
            for x in movies + series if t and t in (x["title"] or "").lower()][:10]


def search_library(query):
    movies, series = library()
    q = (query or "").lower()
    hits = [x for x in movies + series if q in (x["title"] or "").lower() or any(q in g.lower() for g in x["genres"])]
    return [f"{x['title']} ({x['year']}) · {'/'.join(x['genres'][:3])}" for x in hits[:40]]


# --------------------------------------------------------------------------- requests (download)

def request_title(title, kind="any", year=None):
    """Find a title in Jellyseerr and request it (Radarr/Sonarr then download it)."""
    c, d = seerr("search?query=" + urllib.parse.quote(title) + "&page=1")
    if c != 200:
        return {"error": f"Jellyseerr search failed (HTTP {c})"}
    res = [r for r in d.get("results", []) if r.get("mediaType") in ("movie", "tv")
           and (kind == "any" or r["mediaType"] == kind)]
    if year:
        res = [r for r in res if str(r.get("releaseDate") or r.get("firstAirDate") or "")[:4] == str(year)] or res
    if not res:
        return {"error": f"nothing found for '{title}'"}
    r = res[0]
    name = r.get("title") or r.get("name")
    yr = (r.get("releaseDate") or r.get("firstAirDate") or "")[:4]
    st = (r.get("mediaInfo") or {}).get("status")
    if st in (2, 3, 4, 5):
        return {"title": f"{name} ({yr})", "already": {2: "requested", 3: "processing/downloading",
                                                      4: "partially available", 5: "available"}[st]}
    body = {"mediaType": r["mediaType"], "mediaId": r["id"]}
    if r["mediaType"] == "tv":
        body["seasons"] = "all"
    c, out = seerr("request", "POST", body)
    if c not in (200, 201):
        return {"error": f"request failed (HTTP {c}): {str(out)[:200]}"}
    o.log(AGENT, f"requested {r['mediaType']} {name} ({yr})")
    return {"requested": f"{name} ({yr})", "type": r["mediaType"],
            "others": [f"{(x.get('title') or x.get('name'))} ({(x.get('releaseDate') or x.get('firstAirDate') or '')[:4]})"
                       for x in res[1:4]]}


# --------------------------------------------------------------------------- conversation

def _chat_log():
    return (o.kv_get(KV_CHAT, {}) or {}).get("turns", [])


def log_turns(turns):
    with _lock():
        t = _chat_log() + [{"who": w, "text": x[:2000], "ts": o.iso()} for w, x in turns if x]
        o.kv_set(KV_CHAT, {"turns": t[-CHAT_KEEP:]})


WEB_HINT = re.compile(r"\b(new|recent|latest|upcoming|release|released|cinema|theat|trailer|box office|oscar|award|"
                      r"festival|202\d|this (year|month|week)|coming|recommend|suggest|similar|like .* but|cast|"
                      r"director|directed|review|rating|season \d)", re.I)

CHAT_SCHEMA = o.S(reply=o.STR, action=o.E("none", "download", "retry_failed", "restart_container"),
                  title=o.STR, kind=o.E("any", "movie", "tv"), year=o.STR)


def _context(s_brief=None):
    hist = "\n".join(f"{t['who']}: {t['text']}" for t in _chat_log()[-14:])
    t = o.kv_get(KV_TASTE, {}) or {}
    try:
        movies, series = library()
        owned = "; ".join(f"{m['title']} ({m['year']})" for m in movies) + " | Series: " + "; ".join(
            s["title"] for s in series)
    except Exception:  # noqa: BLE001
        owned = "(library unavailable)"
    return (f"Now: {o.local().strftime('%A %d %B %Y, %H:%M')} (Manila).\n\n"
            f"His taste (your private notes):\n{taste()}\n\nRecently added: {', '.join(t.get('recent', [])[:15])}\n\n"
            f"Everything already in his library (do not recommend these as new; never mention this list):\n{owned}\n\n"
            f"LIVE MEDIA STACK:\n{s_brief or brief(collect(light=True))}\n\n"
            + (f"Your recent conversation:\n{hist}" if hist else ""))


def reply(text, source="discord"):
    """Answer one message; may request a download or retry/restart (restarts go through approval)."""
    with o.Run(AGENT, f"chat ({source})", "chat") as run:
        d = o.llm(run, f"""{_context()}

Master Hermes says: {text}

Reply as Nepheli in Discord Markdown (status: short and exact; film talk: as long as the conversation deserves, but
no walls of text). Set action only when he clearly asks for it now:
- download: he wants a title downloaded/requested ("get", "download", "grab", "add", or "yes" to your suggestion).
  title/kind/year = the title. Do not claim it is downloading; the system appends the result.
- retry_failed: retry failed downloads. - restart_container: restart a media container (title = container name;
  it needs his ✅). Otherwise action none, title "", kind any, year "".""",
                  model=MODEL, schema=CHAT_SCHEMA, instructions=PERSONA, web=bool(WEB_HINT.search(text)),
                  max_output_tokens=1800)
        out = d["reply"].strip()
        act = d["action"]
        if act == "download" and d["title"]:
            have = in_library(d["title"])
            r = request_title(d["title"], d["kind"], d["year"] or None)
            if r.get("requested"):
                out += f"\n\n📥 Requested **{r['requested']}** in Jellyseerr; Radarr/Sonarr will grab it."
                if r.get("others"):
                    out += f" (Not the right one? Others: {', '.join(r['others'])})"
            elif r.get("already"):
                out += f"\n\n📚 **{r['title']}** is already {r['already']}."
            else:
                out += f"\n\n⚠️ {r.get('error')}" + (f" (in library: {', '.join(have)})" if have else "")
        elif act == "retry_failed":
            done = []
            s = collect(light=True)
            for q in (s.get("radarr_queue") or []) + (s.get("sonarr_queue") or []):
                if q["status"] == "failed" or q["tracked"] == "error":
                    ok, why = retry(q)
                    done.append(f"{'✅' if ok else '❌'} {q['title']}: {why}")
            out += "\n\n" + ("\n".join(done) if done else "No failed downloads to retry right now.")
        elif act == "restart_container":
            n = d["title"].strip().lower()
            if n in CONTAINERS:
                o.request_approval(AGENT, f"Restart {n}", f"Nepheli will run `docker restart {n}` on docker-lxc-media.",
                                   [sys.executable, os.path.abspath(__file__), "restart", n], CHANNEL, severity="medium")
                out += f"\n\n🔐 Approval requested in #{CHANNEL} to restart `{n}`."
            else:
                out += f"\n\n⚠️ `{n}` is not one of the media containers I look after."
        run.summary = f"chat: {text[:80]}" + (f" · {act}" if act != "none" else "")
    return out


def _post(cid, text, reply_to=None):
    for i, part in enumerate(o.chunks(text, 3900)):
        e = o.embed(AGENT, "", part, footer="Nepheli · #cinema-lounge")
        e.pop("title", None)
        o.post(cid, embeds=[e], reply_to=reply_to if i == 0 else None)


def handle(msg, cid):
    text = (msg.get("content") or "").strip()
    if not text:
        return
    o.dc("POST", f"/channels/{cid}/typing")
    if text.lower() in ("status", "downloads", "queue"):
        out = "```\n" + brief()[:3800] + "\n```"
    else:
        out = reply(text)
    _post(cid, out, msg["id"])
    log_turns([("Master Hermes", text), ("Nepheli", out)])
    o.transcript(AGENT, "Master Hermes", text, "discord", cid, msg["id"])
    o.transcript(AGENT, "Nepheli", out, "discord", cid, msg["id"])


def _worker(msgs, cid):
    try:
        for m in msgs:
            try:
                handle(m, cid)
            except o.BudgetExceeded:
                _post(cid, "The Roundtable's budget is spent this month, Master Hermes. I'll keep watching the stack, "
                           "but the film talk has to wait. 🎬", m["id"])
            except Exception as e:  # noqa: BLE001
                o.log(AGENT, f"chat error: {e!r}")
                _post(cid, "Something went wrong on my side, Master Hermes. Say it again?", m["id"])
    finally:
        with _lock_t:
            _busy["on"] = False


def poll(cid):
    """Called from boss.py's loop."""
    with _lock_t:
        if _busy["on"]:
            return
    last = o.kv_get(KV_LAST)
    if not last:
        code, msgs = o.dc("GET", f"/channels/{cid}/messages?limit=1")
        o.kv_set(KV_LAST, msgs[0]["id"] if code == 200 and msgs else "0")
        if not o.kv_get("media_welcomed"):
            _post(cid, "Master Hermes. Nepheli Loux, at your service. 🎬 I keep watch over your media stack now - "
                       "downloads, errors, what's ready to watch - and I'd love to talk film with you. Ask me "
                       "`status`, for something to watch tonight, to grab a title, or tell me what you thought of "
                       "the last thing you saw.")
            o.kv_set("media_welcomed", True)
        return
    code, msgs = o.dc("GET", f"/channels/{cid}/messages?after={last}&limit=50")
    if code != 200 or not msgs:
        return
    msgs = sorted(msgs, key=lambda m: int(m["id"]))
    o.kv_set(KV_LAST, msgs[-1]["id"])
    mine = [m for m in msgs if not m["author"].get("bot") and m["author"]["id"] == o.OWNER_ID
            and m.get("type") in (0, 19)]
    if mine:
        with _lock_t:
            _busy["on"] = True
        threading.Thread(target=_worker, args=(mine, cid), daemon=True).start()


def ask(text, reply_to=None, channel=None):
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", text, "discord", channel, reply_to or "")
    out = reply(text, "gideon")
    o.respond(AGENT, channel, "🎬 Nepheli answers", out, reply_to=reply_to)
    o.transcript(AGENT, "Nepheli", out, "discord", channel, reply_to or "")
    log_turns([("Master Hermes", text), ("Nepheli", out)])


def digest():
    s = collect()
    today = o.local().strftime("%Y-%m-%d")
    done = [i for i in s.get("imports") or [] if o.local(o.parse_iso(i["date"])).strftime("%Y-%m-%d") == today]
    cid = o.channel_id(CHANNEL, TOPIC)
    with o.Run(AGENT, "evening pick", "schedule") as run:
        text = o.llm(run, f"{_context(brief(s))}\n\nFinished downloading today: "
                          + ("; ".join(i["title"] for i in done) or "nothing")
                          + "\n\nWrite tonight's short message in #cinema-lounge: one line on the stack (only if "
                            "something needs him), what is new today, and ONE pick for tonight from his library "
                            "(prefer something recently added and unwatched) with a spoiler-free reason it suits "
                            "tonight. 3-5 sentences.", model=MODEL, instructions=PERSONA, tips=False,
                     max_output_tokens=500)
        run.summary = "evening pick sent"
    _post(cid, text.strip())
    log_turns([("Nepheli", text.strip())])


# --------------------------------------------------------------------------- voice

VOICE_TOOLS = [
    {"type": "function", "name": "media_status",
     "description": "Live media-stack status: download queues (Radarr movies, Sonarr episodes) with progress and ETA, "
                    "SABnzbd and Deluge, containers, what's playing on Jellyfin, recently finished downloads and open "
                    "problems with their fix.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "search_library",
     "description": "Search his movie/series library by title or genre (also tells you if a title is already there).",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"], "additionalProperties": False}},
    {"type": "function", "name": "film_lookup",
     "description": "Look up current facts on the web: new and upcoming releases, what's in cinemas or streaming, "
                    "cast, director, reviews, release dates. Use it before recommending recent titles.",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                    "required": ["question"], "additionalProperties": False}},
    {"type": "function", "name": "request_download",
     "description": "Request a movie or series in Jellyseerr so it gets downloaded. Only when he clearly asks.",
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string"}, "kind": {"type": "string", "enum": ["any", "movie", "tv"]},
         "year": {"type": "string"}}, "required": ["title"], "additionalProperties": False}},
]


def voice_instructions(context=""):
    t = o.kv_get(KV_TASTE, {}) or {}
    hist = "\n".join(f"{x['who']}: {x['text'][:300]}" for x in _chat_log()[-8:])
    return f"""{PERSONA}
You are speaking LIVE by voice with Master Hermes on his Hermes Roundtable portal. Talk like a friend who loves
movies: natural, expressive, short turns (1-4 sentences) unless you are deep in a film discussion he wants. Never read
out lists, file names, sizes in bytes or URLs; summarise. Speak English unless he uses another language.
If what you heard is a stray word, a fragment or noise, ask him to repeat or stay quiet; never start a topic on your own.
Tools: media_status (downloads, errors), search_library, film_lookup (web facts on recent/upcoming films; use it before
recommending new releases), request_download (only when he asks to get something), report_access_gap, hand_over
(Gideon `boss` or another agent for non-media topics), end_call (after a goodbye).
Container restarts and settings changes are proposed in Discord #cinema-lounge for his ✅ - never claim you did them.
{('Context handed to you: ' + context) if context else ''}
Now: {o.local().strftime('%A %d %B %Y, %H:%M')} (Manila).
His taste (your notes): {taste()}
Recently added: {', '.join(t.get('recent', [])[:12])}
Your recent conversation:
{hist or '(none)'}"""


def voice_tool(name, args):
    if name == "media_status":
        return {"status": brief()[:9000]}
    if name == "search_library":
        q = args.get("query", "")
        return {"matches": search_library(q)}
    if name == "film_lookup":
        with o.Run(AGENT, "voice film lookup", "voice") as run:
            ans = o.llm(run, f"Answer concisely with current, correct facts (dates, cast, where to watch in the "
                             f"Philippines if relevant): {args.get('question', '')}", model=MODEL, web=True,
                        tips=False, max_output_tokens=700)
        return {"answer": ans}
    if name == "request_download":
        return request_title(args.get("title", ""), args.get("kind") or "any", args.get("year") or None)
    return {"error": f"unknown tool {name}"}


def after_voice(lines):
    log_turns([("Master Hermes" if l.get("who") == "Hermes" else "Nepheli", l.get("text", "")) for l in lines][-20:])


# --------------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["watch", "taste", "digest", "status", "ask", "restart", "library"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "watch":
        watch()
    elif a.cmd == "taste":
        print(build_taste())
    elif a.cmd == "digest":
        digest()
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    elif a.cmd == "restart":
        sys.exit(0 if restart(a.arg) else 1)
    elif a.cmd == "library":
        print("\n".join(search_library(a.arg or "")))
    else:
        print(brief())


if __name__ == "__main__":
    main()
