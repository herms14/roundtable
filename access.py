#!/usr/bin/env python3
"""Access-gap backlog for the Roundtable.

When any agent cannot see or do something because it lacks ACCESS (no credentials, 401/403, SSH key refused, no API
or tool for a system) - not because of its deliberate layer boundary - it calls `report()`. Each distinct gap is:
  * posted once to Discord #security-alerts (as the reporting agent),
  * opened as a GitLab issue (labels agent-access, agent::<id>) in the homelab GitLab project,
  * listed in the Obsidian backlog note `61 - Roundtable Access Backlog.md` (synced through rclone/OneDrive).
Repeats only bump a hit counter. `access.py resolve AG-n "what was done"` closes all three.

CLI: list | sync | check | migrate | resolve <id> [note] | wontfix <id> [note] | report <agent> <resource> <needed> [why]
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import olympus_core as o

KV = "access_gaps"
CHANNEL = "security-alerts"  # was #agent-access (archived 2026-10-18)
CHANNEL_TOPIC = ("Access gaps reported by Roundtable agents (credentials, API access, permissions). Each one is a "
                 "GitLab issue (label agent-access) and a row in the Obsidian Access Backlog.")
GITLAB = os.environ.get("SRE_GITLAB_BASE", "http://192.168.40.23/api/v4")
PROJECT = int(os.environ.get("SRE_GITLAB_PROJECT", "6"))
GH_REPO = os.environ.get("ACCESS_GH_REPO", "herms14/homelab-hub")  # legacy issues only (see `migrate`)
REMOTE = os.environ.get("DOC_AGENT_REMOTE", "onedrive:Obsidian Vault/Hermes's Life Knowledge Base")
NOTE_REL = "AI/Documentation/61 - Roundtable Access Backlog.md"
LOCK = o.DATA_DIR / "access.lock"
MAX_NEW_PER_DAY = 15
MARK_START, MARK_END = "<!-- roundtable:access-backlog:start -->", "<!-- roundtable:access-backlog:end -->"
AUTH_RE = re.compile(r"(?i)\b(401|403)\b|unauthori[sz]ed|forbidden|permission denied|access denied|"
                     r"authentication (failed|required)|invalid (credentials|token|api key)|not authori[sz]ed|"
                     r"no credentials|publickey")
TIP = ("If you cannot answer or act because you lack ACCESS to a homelab system (no credentials, 401/403, permission denied, "
       "SSH key refused, no API or tool for it) - not because the topic is outside your layer boundary - say so and "
       "add one line per gap at the very end of your output, exactly: "
       "ACCESS_GAP: <system/resource> | <the narrowest exact access, e.g. 'read-only API token for VM 120'> | "
       "<why you need it> | <read/write/admin> | <hours needed, 1-24>. "
       "Least privilege: ask for the lowest level and shortest time that lets you do the one task (read before write, "
       "one host/project/VM rather than all, admin only when nothing less works). Rogier provisions it after Master "
       "Hermes approves and revokes it automatically when the time is up. "
       "Never report Discord, channels, Sentinel or posting/approval as a gap: the framework posts your output and "
       "collects approvals for you.")
GAP_LINE = re.compile(r"ACCESS_GAP:\s*([^|\n]{3,120})\|([^|\n]{3,300})(?:\|([^|\n]{0,400}))?"
                      r"(?:\|\s*(read|write|admin)\s*)?(?:\|\s*(\d{1,3})\s*h?(?:ours?)?\s*)?", re.I)
# Least privilege: every request names a level and a lifetime; nothing is granted for longer than MAX_HOURS
# without a fresh owner approval. Expired grants are revoked by `access.py expire` (access-expire.timer, 5 min).
LEVELS = ("read", "write", "admin")
MAX_HOURS = 24
GRANTS_KV = "access_grants"
REVOKE_ALERT_EVERY = 6   # failed revocations re-alert the owner every 6th attempt (~30 min at the 5-minute timer)
# The framework posts, approves and routes for every agent - these are never real access gaps.
NOT_A_GAP = re.compile(r"discord|sentinel|roundtable-hold|#[a-z][\w-]+|\bchannel\b|approv", re.I)


@contextlib.contextmanager
def _lock():
    o.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _state():
    s = o.kv_get(KV, {}) or {}
    s.setdefault("gaps", {})
    s.setdefault("next_id", 1)
    return s


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def _level(x):
    x = str(x or "").strip().lower()
    return x if x in LEVELS else None


def _hours(x):
    """Requested lifetime in whole hours, capped at MAX_HOURS; None when not stated."""
    try:
        h = int(x)
    except (TypeError, ValueError):
        return None
    return min(max(h, 1), MAX_HOURS) if h > 0 else None


def _span(g):
    lv, hrs = g.get("level"), g.get("hours")
    if not lv or not hrs:
        return "⚠️ not stated - Rogier must confirm the level and hours before provisioning"
    return f"{lv} for {hrs} h (max {MAX_HOURS} h, revoked automatically when it expires)"


def _name(aid):
    try:
        a = o.agent_info(aid)
        return f"{a['emoji']} {a['name']}"
    except Exception:  # noqa: BLE001
        return aid


# --------------------------------------------------------------------------- GitLab

def _gl(method, path, data=None):
    if not os.environ.get("GITLAB_TOKEN"):
        return 0, "no GitLab token"
    try:
        return o.http(method, f"{GITLAB}/projects/{PROJECT}{path}", {"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"]},
                      data, timeout=20, retries=1)
    except Exception as e:  # noqa: BLE001 - a GitLab outage must never break an agent
        return 0, str(e)


def _gl_body(g):
    ev = o.strip_secrets(g.get("evidence") or "")[:1500]
    return f"""**Agent:** {_name(g['agent'])} (`{g['agent']}`)
**Resource:** {g['resource']}
**Access needed:** {g['needed']}
**Level / time span:** {_span(g)}
**Why:** {g.get('why') or '-'}
**Source:** {g.get('source', 'auto')} · first seen {g['first_seen'][:16].replace('T', ' ')} UTC

{('**Evidence**' + chr(10) + '```' + chr(10) + ev + chr(10) + '```') if ev else ''}

---
Logged automatically by the Roundtable agent team (backlog: Obsidian `61 - Roundtable Access Backlog`, Discord
#security-alerts). Resolve with `python3 access.py resolve {g['id']} "what was granted"` on codex-agent-lxc, which
closes this issue; closing it here is picked up on the next `access.py check`."""


def _gl_open(g):
    code, d = _gl("POST", "/issues", {"title": f"[{g['id']}] {o.agent_info(g['agent'])['name']} needs access: "
                                               f"{g['resource']}"[:240],
                                      "description": _gl_body(g), "labels": f"agent-access,agent::{g['agent']}"})
    if code in (200, 201) and isinstance(d, dict):
        g["issue"], g["url"] = d.get("iid"), d.get("web_url")
        g.pop("issue_error", None)
    else:
        g["issue_error"] = f"HTTP {code}: {str(d)[:200]}"


def _gl_note(g, body):
    if g.get("issue"):
        _gl("POST", f"/issues/{g['issue']}/notes", {"body": o.strip_secrets(body)[:60000]})


def _gl_close(g, note, state="resolved"):
    if not g.get("issue"):
        raise RuntimeError("Access request lacks a GitLab work item; cannot record a complete resolution")
    for method, path, payload in (
            ("POST", f"/issues/{g['issue']}/notes", {"body": o.strip_secrets(note)}),
            ("PUT", f"/issues/{g['issue']}", {"state_event": "close", "add_labels": state})):
        code, result = _gl(method, path, payload)
        if code not in (200, 201):
            raise RuntimeError(f"Access resolution audit HTTP {code}; request remains incomplete")


def _gh_retire(g, note):
    """Close a legacy GitHub issue (pre-GitLab) with a pointer to its GitLab replacement."""
    tok = os.environ.get("ACCESS_GH_TOKEN") or os.environ.get("HARMONIA_GITHUB_TOKEN")
    if not (tok and g.get("gh_issue")):
        return
    h = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json", "User-Agent": "roundtable-access"}
    base = f"https://api.github.com/repos/{GH_REPO}/issues/{g['gh_issue']}"
    try:
        o.http("POST", base + "/comments", h, {"body": note}, timeout=30)
        o.http("PATCH", base, h, {"state": "closed", "state_reason": "not_planned"}, timeout=30)
    except Exception as e:  # noqa: BLE001
        o.log("access", f"github retire #{g['gh_issue']} failed: {e!r}")


# --------------------------------------------------------------------------- Obsidian

def _rclone(*args, timeout=180):
    return subprocess.run(["rclone", "--retries", "3", *args], capture_output=True, text=True, timeout=timeout)


def _note_body(s):
    gaps = sorted(s["gaps"].values(), key=lambda g: int(g["id"].split("-")[1]))
    opn = [g for g in gaps if g["state"] == "open"]
    done = [g for g in gaps if g["state"] != "open"]

    def link(g):
        return f"[#{g['issue']}]({g['url']})" if g.get("url") else "-"

    def esc(x):
        return str(x or "-").replace("|", "\\|").replace("\n", " ")[:220]

    out = [MARK_START, f"*Auto-generated by `access.py` · last sync {o.local().strftime('%Y-%m-%d %H:%M')} (Manila) · "
                       f"{len(opn)} open / {len(done)} closed*", "", "## 🔓 Open access gaps", ""]
    if opn:
        out += ["| ID | Agent | Resource | Access needed | Level | Hours | Why | Hits | First seen | GitLab |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        out += [f"| {g['id']} | {esc(_name(g['agent']))} | {esc(g['resource'])} | {esc(g['needed'])} | {g.get('level') or '⚠️'} "
                f"| {g.get('hours') or '⚠️'} | {esc(g.get('why'))} | {g.get('hits', 1)} | {g['first_seen'][:10]} | {link(g)} |"
                for g in opn]
    else:
        out.append("_No open access gaps._ 🎉")
    grants = s.get("grants") or []
    active = [x for x in grants if x["state"] in ("active", "revoke_failed")]
    out += ["", "## ⏳ Active time-limited grants", ""]
    if active:
        out += ["| Grant | Request | Agent | Access | Granted | Expires (Manila) | State |", "|---|---|---|---|---|---|---|"]
        out += [f"| {x['id']} | {x['gap']} | {esc(_name(x['agent']))} | {esc(x['summary'])} | {_when(x['granted'])} "
                f"| {_when(x['expires'])} | {'🚨 revocation failing' if x['state'] == 'revoke_failed' else 'active'} |"
                for x in active]
    else:
        out.append("_No active grants._")
    revoked = [x for x in grants if x["state"] == "revoked"]
    out += ["", "## 🔒 Revoked on expiry", ""]
    if revoked:
        out += ["| Grant | Request | Agent | Access | Expired | Revoked by | Revoked at |", "|---|---|---|---|---|---|---|"]
        out += [f"| {x['id']} | {x['gap']} | {esc(_name(x['agent']))} | {esc(x['summary'])} | {_when(x['expires'])} "
                f"| 🔑 Rogier | {_when(x.get('revoked'))} |" for x in revoked[-60:]]
    else:
        out.append("_None yet._")
    out += ["", "## ✅ Closed", ""]
    if done:
        out += ["| ID | Agent | Resource | Outcome | Closed | GitLab |", "|---|---|---|---|---|---|"]
        out += [f"| {g['id']} | {esc(_name(g['agent']))} | {esc(g['resource'])} | {g['state']}: {esc(g.get('resolution'))} "
                f"| {(g.get('closed') or '')[:10]} | {link(g)} |" for g in done[-60:]]
    else:
        out.append("_None yet._")
    out.append(MARK_END)
    return "\n".join(out)


def _header(today):
    return f"""---
tags: [type/note, domain/homelab, action/todo, source/claude]
created: {today}
updated: {today}
type: note
status: growing
related: ["[[60 - Roundtable Agent Team]]", "[[MOC - Homelab]]"]
---

# 🔓 Roundtable Access Backlog

> [!NOTE]
> Every time a [[60 - Roundtable Agent Team|Roundtable]] agent hits an **access** problem (missing credentials,
> 401/403, SSH key refused, no API for a system) it is logged here, in Discord **#security-alerts** and as a GitLab issue
> (label `agent-access`). Work through the open rows to grant access, then run
> `python3 access.py resolve AG-n "what was granted"` on codex-agent-lxc. Deliberate layer **boundaries** are not gaps.

"""


def sync_obsidian():
    """Rewrite the generated block of the backlog note (anything outside the markers is preserved)."""
    with _lock():
        s = _state()
        s["grants"] = o.kv_get(GRANTS_KV, []) or []
    today = o.local().strftime("%Y-%m-%d")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "note.md"
        r = _rclone("copyto", f"{REMOTE}/{NOTE_REL}", str(p))
        cur = p.read_text(encoding="utf-8") if r.returncode == 0 and p.exists() else ""
        block = _note_body(s)
        if MARK_START in cur and MARK_END in cur:
            new = cur[:cur.index(MARK_START)] + block + cur[cur.index(MARK_END) + len(MARK_END):]
            new = re.sub(r"(?m)^updated: .*$", f"updated: {today}", new, count=1)
        else:
            new = _header(today) + block + "\n\n## 📝 Notes\n\n"
        p.write_text(new, encoding="utf-8")
        r = _rclone("copyto", str(p), f"{REMOTE}/{NOTE_REL}")
        if r.returncode != 0:
            o.log("access", f"obsidian sync failed: {r.stderr[:300]}")
            return False
    return True


def _sync_later():
    """Obsidian upload takes seconds; do it in a detached process so voice/chat callers never wait."""
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "sync"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         cwd=str(o.CODE_DIR))
    except OSError as e:
        o.log("access", f"sync spawn failed: {e}")


# --------------------------------------------------------------------------- report / resolve

def report(agent, resource, needed, why="", evidence="", source="auto", level=None, hours=None):
    """Log an access gap with the requested level (read/write/admin) and lifetime (1-24 h). Returns (gap, is_new).
    Never raises."""
    try:
        return _report(agent, (resource or "").strip()[:150], (needed or "").strip()[:400], (why or "").strip()[:500],
                       evidence, source, _level(level), _hours(hours))
    except Exception as e:  # noqa: BLE001
        o.log("access", f"report failed for {agent} {resource}: {e!r}")
        return None, False


def _report(agent, resource, needed, why, evidence, source, level=None, hours=None):
    if not resource or not needed:
        return None, False
    if NOT_A_GAP.search(resource):
        o.log("access", f"ignored non-gap from {agent}: {resource}")
        return None, False
    key = f"{agent}:{_slug(resource)}"
    now = o.iso()
    with _lock():
        s = _state()
        g = s["gaps"].get(key)
        if g and g["state"] == "open":
            g["hits"] = g.get("hits", 1) + 1
            g["last_seen"] = now
            if why and not g.get("why"):
                g["why"] = why
            if level and hours and not (g.get("level") and g.get("hours")):   # a later report can fill in the span
                g.update(level=level, hours=hours)
            o.kv_set(KV, s)
            return g, False
        if g and g["state"] == "wontfix":
            return g, False
        today = now[:10]
        if sum(1 for x in s["gaps"].values() if x["first_seen"][:10] == today) >= MAX_NEW_PER_DAY:
            o.log("access", f"daily cap reached; dropped gap {key}")
            return None, False
        reopened = bool(g)
        g = {"id": g["id"] if g else f"AG-{s['next_id']}", "key": key, "agent": agent, "resource": resource,
             "needed": needed, "why": why, "evidence": o.strip_secrets(str(evidence or ""))[:3000], "source": source,
             "level": level, "hours": hours,
             "state": "open", "hits": 1, "first_seen": now, "last_seen": now,
             **({k: g[k] for k in ("issue", "url") if g.get(k)} if reopened else {})}
        if not reopened:
            s["next_id"] += 1
        s["gaps"][key] = g
        o.kv_set(KV, s)
    if reopened and g.get("issue"):
        _gl("PUT", f"/issues/{g['issue']}", {"state_event": "reopen"})
        _gl_note(g, f"🔁 Recurred {now[:16]} UTC: {why or needed}")
    else:
        _gl_open(g)
    fields = [("Access needed", needed, False), ("Level / time span", _span(g), False), ("Why", why or "-", False),
              ("Backlog", f"GitLab {'#' + str(g['issue']) if g.get('issue') else '(issue failed)'} · Obsidian "
                          f"`61 - Roundtable Access Backlog`", False)]
    if g.get("evidence"):
        fields.append(("Evidence", f"```{g['evidence'][:900]}```", False))
    try:
        cid = o.channel_id(CHANNEL, create_topic=CHANNEL_TOPIC)
        g["msg"] = o.post(cid, embeds=[o.embed(agent, f"🔓 {'Recurring' if reopened else 'New'} access gap {g['id']}: "
                                                      f"{resource}", f"{o.agent_info(agent)['name']} could not get to "
                                                      f"**{resource}** and logged it in the access backlog.",
                                               fields, color=0x8E44AD, url=g.get("url"),
                                               footer=f"Roundtable access backlog · source: {source}")])
    except Exception as e:  # noqa: BLE001
        o.log("access", f"discord post failed: {e!r}")
    with _lock():
        s = _state()
        s["gaps"][key] = {**s["gaps"].get(key, {}), **{k: g[k] for k in ("issue", "url", "issue_error", "msg")
                                                        if k in g}}
        o.kv_set(KV, s)
    o.log("access", f"{'reopened' if reopened else 'new'} {g['id']} {key} gitlab={g.get('issue')}")
    _sync_later()
    return g, True


def from_error(agent, resource, err, needed=None, why="", source="auto"):
    """Report only if `err` looks like an authentication/authorisation failure."""
    if err and AUTH_RE.search(str(err)):
        return report(agent, resource, needed or f"working credentials / permission for {resource}", why,
                      str(err)[:1500], source)
    return None, False


def scan_text(agent, text, source="codex"):
    """Pick ACCESS_GAP lines out of an agent's output. Returns (text without those lines, [gaps])."""
    if not text or "ACCESS_GAP:" not in text:
        return text, []
    gaps = []
    for m in GAP_LINE.finditer(text):
        res, need, why, level, hours = (x.strip() if x else "" for x in m.groups())
        if "<" in res or "<" in need:  # the instruction template itself
            continue
        g, _ = report(agent, res, need, why, source=source, level=level, hours=hours)
        if g:
            gaps.append(g)
    clean = "\n".join(l for l in text.splitlines() if "ACCESS_GAP:" not in l).rstrip()
    return clean, gaps


def _find(gid):
    s = _state()
    for k, g in s["gaps"].items():
        if g["id"].lower() == gid.lower():
            return s, k, g
    raise SystemExit(f"no gap {gid}")


def close(gid, note="", state="resolved"):
    if not note.strip():
        raise ValueError("Access resolution requires verification evidence or a cancellation reason")
    with _lock():
        s, k, g = _find(gid)
    _gl_close(g, note, state)
    mid = o.post(o.channel_id(CHANNEL, create_topic=CHANNEL_TOPIC),
               embeds=[o.embed(g["agent"], f"{'✅' if state == 'resolved' else '🚫'} {g['id']} {state}: {g['resource']}",
                               note or "-", color=o.SEV_COLORS["ok"] if state == "resolved" else o.SEV_COLORS["info"],
                               url=g.get("url"))])
    if not mid:
        raise RuntimeError("Access resolution was not logged to Discord; request remains incomplete")
    with _lock():
        s = _state()
        g.update(state=state, resolution=o.strip_secrets(note), closed=o.iso())
        s["gaps"][k] = g
        o.kv_set(KV, s)
    if not sync_obsidian():
        raise RuntimeError("Access verified and logged, but Obsidian sync failed; manual follow-up required")
    return g


def check():
    """Reopen unsupported closures, retry failed issue creation, then refresh the note."""
    changed = False
    with _lock():
        s = _state()
        gaps = [g for g in s["gaps"].values() if g["state"] == "open"]
    for g in gaps:
        if not g.get("issue"):
            _gl_open(g)
            changed = True
        else:
            code, d = _gl("GET", f"/issues/{g['issue']}")
            if code == 200 and isinstance(d, dict) and d.get("state") == "closed":
                code, _ = _gl("PUT", f"/issues/{g['issue']}", {"state_event": "reopen"})
                if code != 200:
                    raise RuntimeError(f"Cannot reopen unsupported closure of {g['id']}: HTTP {code}")
                o.log("compliance", f"{g['id']} reopened: GitLab closure alone is not access verification")
        if changed:
            with _lock():
                s = _state()
                s["gaps"][g["key"]] = g
                o.kv_set(KV, s)
    if not sync_obsidian():
        raise RuntimeError("Access audit Obsidian synchronization failed")
    return changed


def migrate():
    """One-off: move gaps from the old GitHub tracker to GitLab and close the GitHub issues."""
    with _lock():
        s = _state()
        gaps = [g for g in s["gaps"].values() if g.get("gh_issue")]
    done = []
    for g in gaps:
        if g["state"] == "open" and not g.get("issue"):
            _gl_open(g)
        dest = f"GitLab #{g['issue']} ({g['url']})" if g.get("issue") else f"closed as {g['state']}"
        _gh_retire(g, f"Moved: Roundtable access gaps are now tracked in the homelab GitLab - {dest}.")
        for k in ("gh_issue", "gh_url", "gh_error"):
            g.pop(k, None)
        with _lock():
            s = _state()
            s["gaps"][g["key"]] = g
            o.kv_set(KV, s)
        done.append(f"{g['id']}->gitlab#{g.get('issue', '-')}")
    sync_obsidian()
    return " ".join(done) or "nothing to migrate"


def brief(agent=None):
    s = _state()
    gaps = [g for g in s["gaps"].values() if g["state"] == "open" and (not agent or g["agent"] == agent)]
    return [{"id": g["id"], "agent": g["agent"], "resource": g["resource"], "needed": g["needed"],
             "level": g.get("level"), "hours": g.get("hours"),
             "hits": g.get("hits", 1), "gitlab": g.get("url")} for g in gaps]


# --------------------------------------------------------------------------- time-limited grants + expiry
# Rogier records every verified grant here with its exact expiry. `expire()` (access-expire.timer, every 5 min)
# revokes each one when its time is up, verifies it is gone, and logs the revocation to the request's GitLab issue,
# Discord #security-alerts and the Obsidian backlog. A revocation that fails stays 'revoke_failed', is retried on
# every run and pages the owner (first failure, then every REVOKE_ALERT_EVERY attempts).

def _when(ts):
    return o.local(o.parse_iso(ts)).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _revoke_gitlab(target):
    import council
    return council._gitlab(["gitlab", "revoke", str(target["project"]), str(target["user"])])


def _revoke_authentik(target):
    import council
    return council._authentik(["authentik", "revoke", target["group"], str(target["user"])])


# Rogier's Authentik service account can only add/remove members of these groups (object permissions in Authentik)
AUTHENTIK_GRANTABLE = ("authentik Agent-Users", "media-users")
REVOKERS = {"gitlab": _revoke_gitlab, "authentik": _revoke_authentik}


def record_grant(gid, system, target, hours, summary):
    """Register a verified grant for automatic revocation. Returns the grant record."""
    if system not in REVOKERS:
        raise PermissionError(f"No automatic revocation for {system}; it cannot be granted automatically")
    hrs = _hours(hours)
    if not hrs:
        raise ValueError(f"Grant lifetime must be 1-{MAX_HOURS} hours")
    with _lock():
        s, _, g = _find(gid)
        grants = o.kv_get(GRANTS_KV, []) or []
        now = o.utcnow()
        x = {"id": f"GR-{len(grants) + 1}", "gap": g["id"], "agent": g["agent"], "system": system, "target": target,
             "summary": o.strip_secrets(summary)[:200], "hours": hrs, "granted": o.iso(now),
             "expires": o.iso(now + dt.timedelta(hours=hrs)), "state": "active", "attempts": 0}
        grants.append(x)
        o.kv_set(GRANTS_KV, grants)
    o.log("access", f"{x['id']} for {x['gap']} {system} {x['summary']} expires {x['expires']}")
    return x


def grants(active_only=False):
    gs = o.kv_get(GRANTS_KV, []) or []
    return [x for x in gs if x["state"] in ("active", "revoke_failed")] if active_only else gs


def _save_grant(x):
    with _lock():
        gs = o.kv_get(GRANTS_KV, []) or []
        for i, y in enumerate(gs):
            if y["id"] == x["id"]:
                gs[i] = x
        o.kv_set(GRANTS_KV, gs)


def expire(now=None):
    """Revoke every grant past its expiry. Returns (revoked ids, failed ids)."""
    now = now or o.utcnow()
    done, failed = [], []
    cid = None
    for x in grants(active_only=True):
        if o.parse_iso(x["expires"]) > now:
            continue
        try:
            g = _find(x["gap"])[2]
        except SystemExit:   # request record gone: still revoke, just without the issue link
            g = {}
        x["attempts"] = x.get("attempts", 0) + 1
        try:
            evidence = REVOKERS[x["system"]](x["target"])
        except Exception as e:  # noqa: BLE001 - one failure must not stop the other revocations
            x["state"], x["last_error"] = "revoke_failed", o.strip_secrets(f"{type(e).__name__}: {e}")[:300]
            _save_grant(x)
            failed.append(x["id"])
            o.log("access", f"revocation of {x['id']} failed (attempt {x['attempts']}): {x['last_error']}")
            if x["attempts"] == 1 or x["attempts"] % REVOKE_ALERT_EVERY == 0:
                with contextlib.suppress(Exception):
                    cid = cid or o.channel_id(CHANNEL, create_topic=CHANNEL_TOPIC)
                    o.post(cid, content=f"<@{o.OWNER_ID}>", embeds=[o.embed(
                        "iam", f"🚨 Expired access NOT revoked: {x['id']} ({x['gap']})",
                        f"Rogier could not revoke **{x['summary']}** for {_name(x['agent'])}, which expired "
                        f"{_when(x['expires'])}. It is retried every 5 minutes; remove it by hand if this persists.",
                        [("Error", x["last_error"], False), ("Attempts", str(x["attempts"]), True)],
                        color=o.SEV_COLORS.get("critical", 0xE74C3C), url=g.get("url"))])
            continue
        x.update(state="revoked", revoked=o.iso(), evidence=o.strip_secrets(str(evidence))[:500])
        x.pop("last_error", None)
        _save_grant(x)
        done.append(x["id"])
        note = (f"🔒 {x['id']} revoked on expiry by Rogier at {_when(x['revoked'])} (Manila). Access: {x['summary']} "
                f"for {_name(x['agent'])}; granted {_when(x['granted'])}, expired {_when(x['expires'])}. "
                f"Verification: {x['evidence']}")
        _gl_note(g, note)
        o.log("access", note)
        with contextlib.suppress(Exception):
            cid = cid or o.channel_id(CHANNEL, create_topic=CHANNEL_TOPIC)
            o.post(cid, embeds=[o.embed("iam", f"🔒 Access expired and revoked: {x['id']} ({x['gap']})",
                                        f"**{x['summary']}** for {_name(x['agent'])} reached the end of its "
                                        f"{x['hours']} h window and was removed and verified.",
                                        [("Granted", _when(x["granted"]), True), ("Expired", _when(x["expires"]), True),
                                         ("Revoked", _when(x["revoked"]), True), ("Verification", x["evidence"], False)],
                                        color=o.SEV_COLORS["info"], url=g.get("url"),
                                        footer="Rogier · least privilege, time-boxed access")])
    if done or failed:
        sync_obsidian()
    return done, failed


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("sync")
    sub.add_parser("check")
    sub.add_parser("migrate")
    sub.add_parser("expire")
    sub.add_parser("grants")
    for n in ("resolve", "wontfix"):
        p = sub.add_parser(n)
        p.add_argument("id")
        p.add_argument("note", nargs="?", default="")
    p = sub.add_parser("report")
    p.add_argument("agent")
    p.add_argument("resource")
    p.add_argument("needed")
    p.add_argument("why", nargs="?", default="")
    a = ap.parse_args()
    if a.cmd == "list":
        for g in sorted(_state()["gaps"].values(), key=lambda g: int(g["id"].split("-")[1])):
            print(f"{g['id']:6} {g['state']:8} {g['agent']:9} hits={g.get('hits', 1):<3} {g['resource'][:50]:50} "
                  f"gitlab#{g.get('issue', '-')}  {g.get('issue_error', '')}")
    elif a.cmd == "migrate":
        print(migrate())
    elif a.cmd == "expire":
        done, failed = expire()
        print(f"revoked {' '.join(done) or '-'}; failed {' '.join(failed) or '-'}")
        if failed:
            raise SystemExit(1)
    elif a.cmd == "grants":
        for x in grants():
            print(f"{x['id']:6} {x['state']:13} {x['gap']:6} {x['agent']:9} expires {_when(x['expires'])}  {x['summary']}")
    elif a.cmd == "sync":
        print("synced" if sync_obsidian() else "sync failed")
    elif a.cmd == "check":
        print("changed" if check() else "no change")
    elif a.cmd in ("resolve", "wontfix"):
        g = close(a.id, a.note, "resolved" if a.cmd == "resolve" else "wontfix")
        print(f"{g['id']} {g['state']}")
    else:
        g, new = report(a.agent, a.resource, a.needed, a.why, source="manual")
        print(g and g["id"], "new" if new else "existing")


if __name__ == "__main__":
    main()
