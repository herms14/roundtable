#!/usr/bin/env python3
"""Miriel - Roundtable scribe: keeps the Obsidian homelab docs and changelog faithful.

  scribe.py ask "<text>" [--reply-to MID] [--channel CID]   # answer from the homelab docs, log a change, document now
  scribe.py log "<title>" "<detail>" [--agent ID] [--section Added|Changed|Fixed|Removed]
                                    # append an entry to the Homelab Changelog (used by Hewg, Blaidd, Radagon...)
  scribe.py docs [--hours 24]       # run the doc agent now (patch runs, container updates, incidents -> vault)
  scribe.py atlas                   # redraw the Hermes Lab Atlas now (atlas.py; also after every changelog entry)
  scribe.py status

The daily 06:00 documentation pass is doc_agent.py (doc-agent.timer). Edits go through rclone to OneDrive; the
credentials note is never read or written.
"""
import argparse
import contextlib
import fcntl
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import olympus_core as o

AGENT = "scribe"
CHANNEL = "homelab-infrastructure"
REMOTE = os.environ.get("DOC_AGENT_REMOTE", "onedrive:Obsidian Vault/Hermes's Life Knowledge Base")
HL_REL = "07 HomeLab Things/Claude Managed Homelab"
CHANGELOG_REL = f"{HL_REL}/Homelab Changelog.md"
VAULT_CHANGELOG_REL = "00 Landing Zone/Vault Changelog.md"
DOC_AGENT = o.HOME / "doc_agent.py"
LOCK = o.DATA_DIR / "scribe.lock"
SECTIONS = ("Added", "Changed", "Fixed", "Removed", "Security")


@contextlib.contextmanager
def _lock():
    o.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _rclone(*args, timeout=180):
    return subprocess.run(["rclone", "--retries", "3", *args], capture_output=True, text=True, timeout=timeout)


def insert_entry(text, entry, section, today):
    head = f"## [{today}] - Roundtable agent log"
    if head in text:
        start = text.index(head)
        nxt = text.find("\n## ", start + len(head))
        end = len(text) if nxt < 0 else nxt
        block = text[start:end]
        sh = f"\n### {section}\n"
        if sh in block:
            i = start + block.index(sh) + len(sh)
            return text[:i] + entry + "\n" + text[i:]
        return text[:end].rstrip("\n") + f"\n\n### {section}\n{entry}\n" + text[end:]
    new = f"{head}\n\n### {section}\n{entry}\n\n"
    m = re.search(r"(?m)^## \[", text)
    if m:
        return text[:m.start()] + new + text[m.start():]
    return text.rstrip("\n") + "\n\n" + new


def log_change(title, detail, agent="scribe", section="Changed", atlas=True):
    """Append an entry to the Homelab Changelog in the vault, then redraw the Atlas. Returns True on success."""
    section = section if section in SECTIONS else "Changed"
    a = o.agent_info(agent) or {}
    today = o.local().strftime("%Y-%m-%d")
    entry = f"- {a.get('emoji', '📝')} **{title.strip().rstrip('.')}** ({a.get('name', agent)}): " \
            f"{o.strip_secrets(detail).strip()}"
    with _lock(), tempfile.TemporaryDirectory() as td:
        for index, rel in enumerate((CHANGELOG_REL, VAULT_CHANGELOG_REL)):
            p = Path(td) / f"changelog-{index}.md"
            current = Path(td) / f"current-{index}.md"
            r = _rclone("copyto", f"{REMOTE}/{rel}", str(p))
            if r.returncode != 0 or not p.exists():
                raise RuntimeError(f"could not download {rel}: {r.stderr[-300:]}")
            cur = p.read_text(encoding="utf-8")
            new = cur if entry in cur else insert_entry(cur, entry, section, today)
            new = re.sub(r"(?m)^updated: .*$", f"updated: {today}", new, count=1)
            p.write_text(new, encoding="utf-8")
            r = _rclone("copyto", f"{REMOTE}/{rel}", str(current))
            if r.returncode != 0 or not current.exists() or current.read_text(encoding="utf-8") != cur:
                raise RuntimeError(f"{rel} changed concurrently; no overwrite, retry documentation")
            r = _rclone("copyto", str(p), f"{REMOTE}/{rel}")
            if r.returncode != 0:
                raise RuntimeError(f"upload of {rel} failed: {r.stderr[-300:]}")
    o.log(AGENT, f"changelog += [{section}] {title}")
    with o.Run(AGENT, f"changelog: {title[:150]}", "agent" if agent != AGENT else "chat") as run:
        run.summary = f"[{section}] {title} (for {a.get('name', agent)})"
    if atlas:   # a logged change may have added, moved or removed something on the map
        try:
            import atlas as atlas_mod
            atlas_mod.spawn(f"changelog: {title.strip()[:120]}")
        except Exception as e:  # noqa: BLE001 - the changelog entry is already written
            o.log(AGENT, f"atlas refresh not started: {type(e).__name__}")
    return True


def search_docs(query, k=6):
    """Search homelab and relocated operational AI docs in the read-only vault mirror."""
    import vault
    hits = [h for h in vault.search_vault(query, k=40)
            if h["note"].startswith(("07 HomeLab Things/", "AI/Documentation/"))]
    return hits[:k]


def docs_now(hours=24):
    if not DOC_AGENT.exists():
        return False
    subprocess.Popen([sys.executable, str(DOC_AGENT), "run", "--hours", str(int(hours)), "--trigger", "chat"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def status_text():
    with contextlib.closing(o.db()) as con:
        rows = con.execute("SELECT task, status, started, summary FROM runs WHERE agent='scribe' "
                           "ORDER BY id DESC LIMIT 8").fetchall()
    lines = [f"- {o.local(o.parse_iso(r['started'])).strftime('%d %b %H:%M')} [{r['status']}] {r['task'][:60]} — "
             f"{(r['summary'] or '')[:160]}" for r in rows]
    try:
        tail = (o.HOME / ".doc-agent" / "doc-agent.log").read_text(encoding="utf-8").splitlines()[-4:]
    except OSError:
        tail = []
    v = o.kv_get("vault_mirror", {}) or {}
    return ("**Recent scribe runs**\n" + ("\n".join(lines) or "- none recorded yet") +
            "\n\n**Doc agent log**\n" + ("\n".join(f"`{l[:180]}`" for l in tail) or "-") +
            f"\n\nVault mirror: {v.get('notes', '?')} notes, synced {(v.get('synced') or 'never')[:16]}")


INTENT = o.S(action=o.E("answer", "log", "document_now", "status", "atlas"), title=o.STR, detail=o.STR,
             section=o.E(*SECTIONS))


def ask(text, reply_to=None, channel=None):
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", text, "discord", channel, reply_to or "")
    with o.Run(AGENT, f"ask: {text[:200]}", "chat") as run:
        it = o.llm(run, f"""Classify Master Hermes's request to Miriel, the homelab documentation scribe.
log: he wants something recorded in the Homelab Changelog (title = short headline, detail = 1-3 factual sentences
  using only what he said, section = Added/Changed/Fixed/Removed/Security).
document_now: run the documentation pass now (patch runs, updates, incidents into the vault).
status: what was documented recently / is the doc agent working.
atlas: redraw / refresh / update the Hermes Lab Atlas (the lab diagrams), or what changed on it recently.
answer: a question about the homelab that the Obsidian docs can answer (how is X set up, where is Y documented).
Request: {text}""", schema=INTENT, model="gpt-6-luna")
        act = it["action"]
        if act == "log":
            log_change(it["title"], it["detail"], "scribe", it["section"])
            run.summary = f"logged: {it['title']}"
            o.respond(AGENT, channel, "🐢 Written into the record",
                      f"Under **{it['section']}** in `Homelab Changelog.md` for today:\n> **{it['title']}**: "
                      f"{it['detail']}", reply_to=reply_to, run=run)
        elif act == "document_now":
            ok = docs_now()
            run.summary = "doc agent started" if ok else "doc agent missing"
            o.respond(AGENT, channel, "🐢 Documenting now" if ok else "⚠️ Doc agent missing",
                      "I am writing the last day's patch runs, container updates and incidents into the vault "
                      "(changelog, troubleshooting, service docs). It takes a few minutes; ask me for `status` after."
                      if ok else f"`{DOC_AGENT}` not found.", reply_to=reply_to, run=run)
        elif act == "status":
            o.respond(AGENT, channel, "🐢 Miriel's ledger", status_text(), reply_to=reply_to, run=run)
        elif act == "atlas":
            import atlas as atlas_mod
            res = atlas_mod.refresh("asked in Discord", fresh=True, quiet=True)
            if res.get("queued"):
                body = "A redraw is already running; I folded your request into it. The map updates in a minute."
            else:
                lines = "\n".join(f"• {x}" for x in res["changes"][:20])
                body = (lines or "No additions, removals or renames since the last pass.")
                if res["status"]:
                    body += f"\n\nStatus on the map: {'; '.join(res['status'][:8])}"
                body += "\n\n" + atlas_mod.status_text()
            run.summary = f"atlas: {len(res.get('changes', []))} changes"
            o.respond(AGENT, channel, "🗺️ Atlas redrawn", body + f"\n\n{o.PORTAL_URL.rstrip('/')}/atlas",
                      reply_to=reply_to, run=run)
        else:
            hits = search_docs(text)
            ctx = "\n\n".join(f"### {h['note']}\n{h['excerpt']}" for h in hits) or "(no matching notes)"
            ans = o.llm(run, f"""You are Miriel, Pastor of Vows and keeper of records for Master Hermes's homelab. Answer
from the Obsidian homelab docs below only, briefly, in Discord Markdown, and cite the note names as [[Note Name]].
If the docs do not say, say so and offer to have the right agent check live.

DOC EXCERPTS:
{ctx}

QUESTION: {text}""", model="gpt-6-luna", max_output_tokens=1200)
            run.summary = (ans or "")[:300]
            o.respond(AGENT, channel, "🐢 From the records", ans or "No answer.", reply_to=reply_to, run=run)


VOICE_TOOLS = [
    {"type": "function", "name": "search_homelab_docs",
     "description": "Search Master Hermes's Obsidian homelab documentation (runbooks, service docs, troubleshooting, "
                    "changelog) and return matching excerpts with note names.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                    "additionalProperties": False}},
    {"type": "function", "name": "docs_status",
     "description": "Miriel's recent documentation runs and the doc agent's log.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "atlas_status",
     "description": "When the Hermes Lab Atlas diagrams were last redrawn and what changed on them recently.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "redraw_atlas",
     "description": "Start redrawing the Hermes Lab Atlas from the live lab now (takes about a minute).",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def voice_tool(name, args):
    if name == "search_homelab_docs":
        return {"results": search_docs(args.get("query", ""))}
    if name == "docs_status":
        return {"status": status_text().replace("**", "")}
    if name in ("atlas_status", "redraw_atlas"):
        import atlas as atlas_mod
        if name == "redraw_atlas":
            atlas_mod.spawn("asked by voice")
            return {"started": True, "note": "The map updates in about a minute."}
        return {"status": atlas_mod.status_text()}
    return {"error": f"unknown tool {name}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["ask", "log", "docs", "status", "vault-sync", "atlas"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("detail", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    ap.add_argument("--agent", default="scribe")
    ap.add_argument("--section", default="Changed")
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args()
    if a.cmd == "vault-sync":
        import vault
        if not vault.sync_vault():
            raise RuntimeError("Permitted note mirror refresh failed; no personal profile was generated")
        print("permitted note mirror synchronized")
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    elif a.cmd == "log":
        log_change(a.arg, a.detail or "", a.agent, a.section)
        print("logged")
    elif a.cmd == "atlas":
        import atlas as atlas_mod
        print(atlas_mod.status_text() if atlas_mod.refresh("scribe atlas", fresh=True) else "")
    elif a.cmd == "docs":
        print("started" if docs_now(a.hours) else "doc_agent.py missing")
    else:
        print(status_text())


if __name__ == "__main__":
    main()
