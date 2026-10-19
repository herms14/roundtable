#!/usr/bin/env python3
"""Miriel - keeps the Hermes Lab Atlas current (deterministic, no LLM).

  atlas.py refresh [--reason TEXT] [--fresh] [--quiet]   # re-read the lab, rebuild the Atlas, report what changed
  atlas.py status                                        # last build and recent changes
  atlas.py render --out FILE                             # layout only, no live data (local preview / offline build)

atlas_layout.json (hand-edited) holds the diagrams. Nodes with a "live" key are overlaid with Proxmox guests, Docker
containers, Omada devices, systemd units and agents.json; a live value that differs from the node's "base" overrides
its text, removed items drop off, and new guests / containers / agents appear (new containers and unplaceable agents in
a "New - needs placing" group until they are added to the layout).

Triggers: scribe.log_change (every agent's changelog entry), atlas-sweep.timer (every 30 min), Miriel on request and
the portal's refresh button. Output: ~/.olympus/atlas.html (served at /atlas). Additions, removals and renames also
go to #homelab-infrastructure, the Homelab Changelog and the vault copy (Diagrams/Hermes Lab Atlas.html).
Status flips (stopped, exited, offline) only change the map; Sellen alerts on those.
"""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import olympus_core as o

AGENT = "scribe"
LAYOUT = o.CODE_DIR / "atlas_layout.json"
ENGINE = o.CODE_DIR / "atlas_engine.html"
OUT_HTML = o.DATA_DIR / "atlas.html"
STATE = o.DATA_DIR / "atlas_state.json"
LOCK = o.DATA_DIR / "atlas.lock"
DIRTY = o.DATA_DIR / "atlas.dirty"
VAULT_REL = "Diagrams/Hermes Lab Atlas.html"
NEW_GROUP = {"id": "new", "label": "New · needs placing", "c": "#facc15", "cols": 3,
             "desc": "Found by Miriel; add them to atlas_layout.json to give them a home"}
AGENT_GROUPS = {"personal": "pers", "specialized": "spec"}
LEAD_GROUPS = {"governance": "gov", "security": "sec", "medic": "ops"}


# --------------------------------------------------------------------------- facts (what the lab looks like now)

def _size(g):
    return [g["cores"], g["ram_max_gb"], g["disk_max_gb"]]


def collect(fresh=False, prev=None):
    """Current facts. A source that is down keeps its previous facts, so nothing is dropped by an outage."""
    import inventory
    prev = prev or {}
    f, notes = {}, []
    c = inventory.get("compute", fresh)
    if c.get("guests"):
        f["guests"] = {str(g["id"]): {"name": g["name"], "node": g["node"], "kind": g["kind"], "status": g["status"],
                                      "template": bool(g["template"]), "ip": g.get("ip") or "", "size": _size(g)}
                       for g in c["guests"]}
        f["nodes"] = {n["node"]: {"status": n["status"], "ram_max_gb": n["ram_max_gb"]} for n in c.get("nodes", [])}
    else:
        f["guests"], f["nodes"] = prev.get("guests", {}), prev.get("nodes", {})
        notes.append(f"Proxmox unavailable ({c.get('error', 'no data')}); kept the last guests")
    k = inventory.get("containers", fresh)
    hosts = {h: {r["name"]: r["state"] for r in rows} for h, rows in (k.get("hosts") or {}).items()}
    for h in (k.get("unreachable") or {}):
        if h in prev.get("containers", {}):
            hosts[h] = prev["containers"][h]
            notes.append(f"{h} unreachable; kept its last containers")
    f["containers"] = hosts if hosts else prev.get("containers", {})
    n = inventory.get("network", fresh)
    if n.get("devices"):
        f["devices"] = {d["ip"]: {"name": d["name"], "online": bool(d["online"])} for d in n["devices"] if d.get("ip")}
    else:
        f["devices"] = prev.get("devices", {})
        notes.append("Omada unavailable; kept the last devices")
    f["agents"] = {a["id"]: {"name": a.get("name") or a["id"], "model": a.get("model") or "",
                             "channel": a.get("channel") or "", "reports_to": a.get("reports_to") or "",
                             "group": a.get("group") or "", "role": a.get("role") or ""}
                   for a in o.registry()["agents"]}
    f["units"] = {}
    for u in ("boss", "olympus-portal", "labfeed", "sre-agent"):
        r = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True, timeout=10)
        f["units"][u] = r.stdout.strip() or "unknown"
    return f, notes


# --------------------------------------------------------------------------- overlay (layout + facts -> diagrams)

def _ignored(name, state, rx):
    return bool(re.search(rx, name)) or (state != "running" and re.fullmatch(r"[a-z]+_[a-z]+", name) is not None)


def _gb(x):
    return f"{round(x, 1):g}"


def _sizetxt(s, template=False):
    return (f"template · {s[0]} vCPU · {_gb(s[1])} GiB" if template
            else f"{s[0]} vCPU · {_gb(s[1])} GiB · {round(s[2])} GB")


def _sub(text, old, new):
    return re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, text) if old and new and old != new else text


def _mark(n, s, note):
    n["s"], n["note"] = s, note


def overlay(layout, facts, first_seen=None, today=None):
    """Return (atlas, warnings). Pure: the same layout and facts always give the same diagrams."""
    first_seen = first_seen if first_seen is not None else {}
    today = today or time.strftime("%Y-%m-%d")
    G, C, D, A, U = (facts.get(k, {}) for k in ("guests", "containers", "devices", "agents", "units"))
    NODES = facts.get("nodes", {})
    rx = layout.get("ignore_containers") or r"^$"
    hidden = set(layout.get("hidden_containers", []))
    atlas, dropped, placed_guests, placed_containers, placed_agents = copy.deepcopy(layout["atlas"]), set(), set(), set(), set()
    for d in atlas:
        keep = []
        for n in d["nodes"]:
            lv, f = n.pop("live", None), n.get("f") or {}
            if not lv:
                keep.append(n)
                continue
            b = lv.get("base", {})
            if "guest" in lv:
                g = G.get(str(lv["guest"]))
                if not g:
                    dropped.add((d["id"], n["id"]))
                    continue
                placed_guests.add(str(lv["guest"]))
                if g["name"] != b.get("name"):
                    n["n"] = (_sub(n["n"], b.get("name"), g["name"]) if b.get("name") in n["n"]
                              else f"{'VM' if g['kind'] == 'VM' else 'CT'}{lv['guest']} {g['name']}" if d["id"] == "hw"
                              else n["n"])
                for k, v in list(f.items()):
                    if isinstance(v, str):
                        f[k] = _sub(_sub(v, b.get("node"), g["node"]), b.get("ip"), g["ip"])
                if lv.get("spec") and g["size"] != b.get("size"):
                    f["size"] = _sizetxt(g["size"], g["template"])
                if g["status"] != b.get("status") and g["status"] != "running" and not g["template"]:
                    _mark(n, "down", f"{g['status'].capitalize()} in Proxmox")
                if d["id"] == "hw" and g["node"] != b.get("node") and n["g"] in ("h1", "h2", "h3"):
                    n["g"] = "h" + g["node"][-1]
            elif "containers" in lv:
                rows = C.get(lv["host"])
                if rows is None:
                    keep.append(n)
                    continue
                there = [x for x in lv["containers"] if x in rows]
                if not there:
                    dropped.add((d["id"], n["id"]))
                    continue
                placed_containers.update((lv["host"], x) for x in lv["containers"])
                bad = [x for x in there if rows[x] != "running"]
                if bad:
                    _mark(n, "down", f"{', '.join(bad)} {rows[bad[0]]}")
            elif "agent" in lv:
                a = A.get(lv["agent"])
                if not a:
                    dropped.add((d["id"], n["id"]))
                    continue
                placed_agents.add(lv["agent"])
                if a["name"] != b.get("name"):
                    n["n"] = a["name"]
                for k in ("model", "channel"):
                    if a[k] != b.get(k) and k in f:
                        f[k] = a[k] if k == "model" or a[k].startswith("#") else "#" + a[k]
            elif "device" in lv:
                dv = D.get(lv["device"])
                if dv is None:
                    _mark(n, "warn", "Not in Omada any more")
                elif not dv["online"]:
                    _mark(n, "down", "Offline in Omada")
            elif "unit" in lv:
                st = U.get(lv["unit"], "active")
                if st != "active":
                    _mark(n, "down", f"systemd: {st}")
            elif "pve" in lv:
                node, gs = lv["pve"], [g for g in G.values() if g["node"] == lv["pve"]]
                run = [g for g in gs if g["status"] == "running" and not g["template"]]
                if lv.get("guests_field"):
                    lx, vm = sum(g["kind"] == "LXC" for g in run), sum(g["kind"] == "VM" for g in run)
                    f[lv["guests_field"]] = f"{lx} LXC · {vm} VM"
                if lv.get("ram_note") and node in NODES:
                    alloc, mx = sum(g["size"][1] for g in run), NODES[node]["ram_max_gb"]
                    if alloc >= mx * 0.95:
                        _mark(n, "warn", f"Guests allotted {_gb(alloc)} GiB on {int(mx)} GiB" if alloc > mx
                              else f"RAM fully allocated ({_gb(alloc)}/{int(mx)} GiB)")
                    elif re.match(r"(RAM fully allocated|Guests allotted)", n.get("note") or ""):
                        n["s"] = "ok"
                        n.pop("note", None)
                if NODES.get(node, {}).get("status", "online") != "online":
                    _mark(n, "down", "Node offline")
            elif lv.get("team"):
                groups = [a["group"] for a in A.values()]
                n["n"] = f"{len(A)} agents"
                f.update({"homelab": str(sum(not g for g in groups)), "personal": str(groups.count("personal")),
                          "specialists": str(groups.count("specialized"))})
            if f:
                n["f"] = f
            keep.append(n)
        d["nodes"] = keep
    by = {d["id"]: d for d in atlas}
    news = []

    def add_new(did, node):
        d = by[did]
        if not any(g["id"] == "new" for g in d["groups"]):
            d["groups"].append(dict(NEW_GROUP))
            d["layout"].append(["new"])
        d["nodes"].append(node)

    # hardware summaries + new guests (placed on their node)
    if "hw" in by and G:
        for node, gid, sid in (("node01", "h1", "g1s"), ("node02", "h2", "g2s"), ("node03", "h3", "g3s")):
            s = next((n for n in by["hw"]["nodes"] if n["id"] == sid), None)
            run = [g for g in G.values() if g["node"] == node and g["status"] == "running" and not g["template"]]
            if s:
                vm = sum(g["kind"] == "VM" for g in run)
                s["f"]["running"] = f"{len(run)} ({vm} VM · {len(run) - vm} LXC)"
                s["f"]["allocated"] = f"{sum(g['size'][0] for g in run)} vCPU · {_gb(sum(g['size'][1] for g in run))} GiB"
        for vid in sorted(set(G) - placed_guests, key=int):
            g = G[vid]
            key = f"guest:{vid}"
            first_seen.setdefault(key, today)
            node = {"id": f"g{vid}", "g": "h" + g["node"][-1] if g["node"][-1] in "123" else "h1",
                    "t": "vm" if g["kind"] == "VM" else "lxc", "n": f"{'VM' if g['kind'] == 'VM' else 'CT'}{vid} {g['name']}",
                    "f": {"host": g["node"], "size": _sizetxt(g["size"], g["template"])}, "badge": "new",
                    "note": f"New since {first_seen[key]}"}
            by["hw"]["nodes"].append(node)
            news.append({"tab": "hw", "node": node["id"], "t": f"New guest {node['n']}",
                         "d": f"On {g['node']}; add it to the Services map in atlas_layout.json"})
    for d in atlas:   # cluster headline counts
        for gr in d["groups"]:
            if gr.get("desc") and G:
                gr["desc"] = re.sub(r"\d+ guests \(\d+ templates\)",
                                    f"{len(G)} guests ({sum(g['template'] for g in G.values())} templates)", gr["desc"])
    # new containers on the hosts the Services map breaks out
    if "svc" in by:
        for h in layout.get("container_hosts", []):
            for name, state in sorted((C.get(h) or {}).items()):
                if (h, name) in placed_containers or f"{h}/{name}" in hidden or _ignored(name, state, rx):
                    continue
                key = f"container:{h}/{name}"
                first_seen.setdefault(key, today)
                add_new("svc", {"id": "new-" + re.sub(r"\W", "-", f"{h}-{name}"), "g": "new", "t": "container",
                                "n": name, "f": {"host": h, "state": state}, "badge": "new",
                                "s": "ok" if state == "running" else "down", "note": f"Found {first_seen[key]}"})
    # new agents: placed by their group or lead, otherwise parked
    if "agents" in by:
        for aid in sorted(set(A) - placed_agents):
            a = A[aid]
            key = f"agent:{aid}"
            first_seen.setdefault(key, today)
            gid = AGENT_GROUPS.get(a["group"]) or LEAD_GROUPS.get(a["reports_to"])
            node = {"id": aid, "g": gid or "new", "t": "agent", "n": a["name"], "badge": "new",
                    "f": {"role": a["role"][:40], "model": a["model"], "channel": "#" + a["channel"].lstrip("#")},
                    "note": f"New since {first_seen[key]}"}
            if gid:
                by["agents"]["nodes"].append(node)
            else:
                add_new("agents", node)
    for d in atlas:   # edges may join nodes or whole groups
        ids = {n["id"] for n in d["nodes"]} | {n["g"] for n in d["nodes"]}
        d["edges"] = [e for e in d.get("edges", []) if e[0] in ids and e[1] in ids]
    gone = {nid for _, nid in dropped}
    warnings = [w for w in layout.get("warnings", []) if (w["tab"], w["node"]) not in dropped] + news
    return atlas, warnings, sorted(gone)


# --------------------------------------------------------------------------- diff (what changed since last run)

def diff(prev, cur):
    """(structural, status) change lines between two fact sets."""
    if not prev:
        return [], []
    big, small = [], []
    pg, cg = prev.get("guests", {}), cur.get("guests", {})
    for k in sorted(set(cg) - set(pg), key=int):
        g = cg[k]
        big.append(f"Added {g['kind']} {k} {g['name']} on {g['node']} ({_sizetxt(g['size'], g['template'])})")
    for k in sorted(set(pg) - set(cg), key=int):
        big.append(f"Removed {pg[k]['kind']} {k} {pg[k]['name']} (was on {pg[k]['node']})")
    for k in sorted(set(pg) & set(cg), key=int):
        a, b = pg[k], cg[k]
        label = f"{b['kind']} {k} {b['name']}"
        if a["name"] != b["name"]:
            big.append(f"Renamed {b['kind']} {k}: {a['name']} → {b['name']}")
        if a["node"] != b["node"]:
            big.append(f"Moved {label}: {a['node']} → {b['node']}")
        if a["size"] != b["size"]:
            big.append(f"Resized {label}: {_sizetxt(a['size'])} → {_sizetxt(b['size'])}")
        if a["ip"] != b["ip"] and a["ip"] and b["ip"]:
            big.append(f"Re-addressed {label}: {a['ip']} → {b['ip']}")
        if a["status"] != b["status"]:
            small.append(f"{label}: {a['status']} → {b['status']}")
    pc, cc = prev.get("containers", {}), cur.get("containers", {})
    for h in sorted(set(pc) | set(cc)):
        a, b = pc.get(h, {}), cc.get(h, {})
        if not a and b and pc:
            big.append(f"New Docker host {h} ({len(b)} containers)")
            continue
        if a and not b:
            big.append(f"Docker host {h} gone ({len(a)} containers)")
            continue
        real = lambda rows, x: not _ignored(x, rows[x], r"^$")   # noqa: E731 - keep helpers, skip random names
        for x in sorted(set(b) - set(a)):
            if real(b, x):
                big.append(f"Added container {x} on {h}")
        for x in sorted(set(a) - set(b)):
            if real(a, x):
                big.append(f"Removed container {x} from {h}")
        for x in sorted(set(a) & set(b)):
            if a[x] != b[x] and real(b, x):
                small.append(f"{x} on {h}: {a[x]} → {b[x]}")
    pa, ca = prev.get("agents", {}), cur.get("agents", {})
    for k in sorted(set(ca) - set(pa)):
        big.append(f"New agent {ca[k]['name']} ({k}), reports to {ca[k]['reports_to'] or '-'}")
    for k in sorted(set(pa) - set(ca)):
        big.append(f"Agent {pa[k]['name']} ({k}) retired")
    for k in sorted(set(pa) & set(ca)):
        for f in ("name", "model", "channel", "reports_to"):
            if pa[k][f] != ca[k][f]:
                big.append(f"Agent {ca[k]['name']}: {f} {pa[k][f] or '-'} → {ca[k][f] or '-'}")
    pd, cd = prev.get("devices", {}), cur.get("devices", {})
    for ip in sorted(set(cd) - set(pd)):
        big.append(f"New network device {cd[ip]['name']} ({ip})")
    for ip in sorted(set(pd) - set(cd)):
        big.append(f"Network device {pd[ip]['name']} ({ip}) removed from Omada")
    for ip in sorted(set(pd) & set(cd)):
        if pd[ip]["online"] != cd[ip]["online"]:
            small.append(f"{cd[ip]['name']} {'online' if cd[ip]['online'] else 'offline'}")
    pu, cu = prev.get("units", {}), cur.get("units", {})
    for u in sorted(set(pu) & set(cu)):
        if pu[u] != cu[u]:
            small.append(f"{u}.service: {pu[u]} → {cu[u]}")
    return big, small


# --------------------------------------------------------------------------- render + publish

def render(atlas, warnings, snapshot):
    eng = ENGINE.read_text(encoding="utf-8")
    js = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")  # noqa: E731
    data = f"const SNAPSHOT={js(snapshot)};\nconst ATLAS={js(atlas)};\nconst WARNINGS={js(warnings)};\n"
    assert eng.count("/*__DATA__*/") == 1, "atlas_engine.html has no single /*__DATA__*/ slot"
    return eng.replace("/*__DATA__*/", data)


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def publish_vault(html, state):
    """Upload the vault copy unless someone else changed it since Miriel's last upload."""
    import scribe
    rel = f"{scribe.REMOTE}/{scribe.HL_REL}/{VAULT_REL}"
    with tempfile.TemporaryDirectory() as td:
        cur = Path(td) / "current.html"
        r = scribe._rclone("copyto", rel, str(cur))
        if r.returncode == 0 and cur.exists():
            seen = _sha(cur.read_bytes())
            if state.get("vault_sha") and seen != state["vault_sha"]:
                return f"vault copy was edited elsewhere (sha {seen[:8]}); not overwritten"
        new = Path(td) / "atlas.html"
        new.write_text(html, encoding="utf-8")
        r = scribe._rclone("copyto", str(new), rel)
        if r.returncode != 0:
            return f"vault upload failed: {r.stderr[-200:]}"
        state["vault_sha"] = _sha(new.read_bytes())
    return "vault copy updated"


def load_layout():
    return json.loads(LAYOUT.read_text(encoding="utf-8"))


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def _write(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def build(reason="manual", fresh=False, quiet=False):
    state = load_state()
    prev = state.get("facts")
    facts, notes = collect(fresh, prev)
    big, small = diff(prev, facts)
    first_seen = state.get("first_seen", {})
    atlas, warnings, gone = overlay(load_layout(), facts, first_seen, o.local().strftime("%Y-%m-%d"))
    live = {k for k in first_seen if (k.startswith("guest:") and k[6:] in facts["guests"])
            or (k.startswith("container:") and k[10:].split("/", 1)[1] in facts["containers"].get(k[10:].split("/", 1)[0], {}))
            or (k.startswith("agent:") and k[6:] in facts["agents"])}
    first_seen = {k: v for k, v in first_seen.items() if k in live}
    now = o.local()
    html = render(atlas, warnings, now.strftime("%Y-%m-%d %H:%M") + " · live")
    _write(OUT_HTML, html)
    state.update({"facts": facts, "first_seen": first_seen, "generated": o.iso(), "reason": reason, "notes": notes,
                  "dropped": gone})
    if big or small:
        state["history"] = ([{"t": o.iso(), "reason": reason, "changes": big, "status": small}]
                            + state.get("history", []))[:30]
    out = {"changes": big, "status": small, "notes": notes, "vault": None, "first": prev is None}
    if prev is None or big:
        out["vault"] = publish_vault(html, state)
    save_state(state)
    if big and not quiet:
        report(big, small, reason, out["vault"])
    o.log(AGENT, f"atlas: {len(big)} changes, {len(small)} status ({reason})")
    return out


def report(big, small, reason, vault):
    import scribe
    text = "\n".join(f"• {x}" for x in big[:25]) + (f"\n…and {len(big) - 25} more" if len(big) > 25 else "")
    if small:
        text += "\n\n**Status on the map:** " + "; ".join(small[:8])
    text += f"\n\n[Open the Atlas]({o.PORTAL_URL.rstrip('/')}/atlas) · trigger: {reason[:80]} · {vault or ''}"
    try:
        o.respond(AGENT, scribe.CHANNEL, f"🗺️ Atlas redrawn · {len(big)} change{'s' * (len(big) != 1)}", text)
    except Exception as e:  # noqa: BLE001 - the map is updated either way
        o.log(AGENT, f"atlas discord post failed: {type(e).__name__}")
    try:
        scribe.log_change(f"Hermes Lab Atlas redrawn ({len(big)} change{'s' * (len(big) != 1)})",
                          "; ".join(big)[:700], AGENT, "Changed", atlas=False)
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"atlas changelog entry failed: {e}")


def refresh(reason="manual", fresh=False, quiet=False):
    """Build once; a refresh asked for while one runs is folded into one more pass afterwards."""
    o.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            DIRTY.write_text(reason[:200], encoding="utf-8")
            return {"queued": True}
        with o.Run(AGENT, f"atlas: {reason[:150]}", "agent") as run:
            res = build(reason, fresh, quiet)
            for _ in range(3):
                if not DIRTY.exists():
                    break
                again = DIRTY.read_text(encoding="utf-8") or "queued"
                DIRTY.unlink()
                more = build(again, True, quiet)
                res["changes"] += more["changes"]
                res["status"] += more["status"]
            run.summary = f"{len(res['changes'])} changes, {len(res['status'])} status · {reason[:80]}"
        return res


def spawn(reason):
    """Fire-and-forget refresh (used by scribe.log_change and the portal)."""
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "refresh", "--fresh", "--reason", reason[:200]],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=str(o.CODE_DIR))


def status():
    s = load_state()
    busy = False
    with contextlib.suppress(OSError), open(LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            busy = True
    return {"generated": s.get("generated"), "reason": s.get("reason"), "busy": busy, "notes": s.get("notes", []),
            "new": sorted(s.get("first_seen", {})), "history": s.get("history", [])[:10]}


def status_text():
    s = status()
    if not s["generated"]:
        return "The Atlas has not been built by Miriel yet."
    t = o.local(o.parse_iso(s["generated"])).strftime("%d %b %H:%M")
    lines = [f"Atlas last redrawn {t} ({s['reason']}){' · redrawing now' if s['busy'] else ''}."]
    if s["new"]:
        lines.append("New since the layout was last edited (add them to atlas_layout.json): "
                     + ", ".join(k.split(":", 1)[1] for k in s["new"]))
    for h in s["history"][:4]:
        lines.append(f"- {o.local(o.parse_iso(h['t'])).strftime('%d %b %H:%M')}: "
                     + ("; ".join(h["changes"][:4]) or "; ".join(h["status"][:4]))[:300])
    lines += [f"Note: {n}" for n in s["notes"]]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["refresh", "status", "render"])
    ap.add_argument("--reason", default="manual")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.cmd == "refresh":
        print(json.dumps(refresh(a.reason, a.fresh, a.quiet), ensure_ascii=False, indent=1))
    elif a.cmd == "render":
        lay = load_layout()
        for d in lay["atlas"]:
            for n in d["nodes"]:
                n.pop("live", None)
        Path(a.out).write_text(render(lay["atlas"], lay["warnings"], lay.get("base_taken", "")[:10]), encoding="utf-8")
        print(a.out)
    else:
        print(status_text())


if __name__ == "__main__":
    main()
