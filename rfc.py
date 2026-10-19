#!/usr/bin/env python3
"""Ranni's RFC register (ITIL change management for the Roundtable).

Normal changes (major or risky) need a Request for Change before the owner's ✅: the performing agent files what will
be done, what will change and the recovery plan (Ranni drafts it from the agent's request when the agent does not).
The approval embed carries the RFC; ✅ approves it, ❌ rejects it, and Ranni closes it automatically when the
approved task ends (Implemented / Failed / Rolled back). Standard changes (pre-approved, low risk) skip the RFC.

  rfc.py list            # open and recent RFCs
  rfc.py show RFC-2026-001

Vault: 07 HomeLab Things/Claude Managed Homelab/RFCs/<id> - <title>.md and RFCs/RFC Register.md (via rclone).
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import olympus_core as o

AGENT = "governance"  # Ranni
REMOTE = os.environ.get("DOC_AGENT_REMOTE", "onedrive:Obsidian Vault/Hermes's Life Knowledge Base")
DIR = "07 HomeLab Things/Claude Managed Homelab/RFCs"
REGISTER = f"{DIR}/RFC Register.md"
LOCK = o.DATA_DIR / "rfc.lock"
KV = "rfc:"
DRAFT = o.S(what=o.STR, change=o.STR, recovery=o.STR, risk=o.STR)
OPEN = ("In review", "Approved — scheduled", "Approved — implementing")
FINAL = ("Implemented", "Failed", "Rolled back", "Rejected", "Expired", "Interrupted")
# Standard changes: routine, low-risk, pre-approved by governance; they keep the plain approval.
STANDARD = (("media", "restart"), ("backup", ""), ("netops", "probe"))


def required(agent, severity, command, rfc=None):
    if rfc:
        return True
    cmd = " ".join(str(c) for c in command or [])
    if any(agent == a and verb in cmd for a, verb in STANDARD):
        return False
    if agent == "netops" and " acl apply " in f" {cmd} ":
        return True
    if agent == "devops" and re.search(r"\b(forge|destroy|build|apply)\b", cmd):
        return True
    return severity in ("high", "critical")


# --------------------------------------------------------------------------- vault I/O

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


def _read(rel):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "n.md"
        r = _rclone("copyto", f"{REMOTE}/{rel}", str(p))
        return p.read_text(encoding="utf-8") if r.returncode == 0 and p.exists() else None


def _write(rel, text):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "n.md"
        p.write_text(text, encoding="utf-8")
        r = _rclone("copyto", str(p), f"{REMOTE}/{rel}")
        if r.returncode != 0:
            raise RuntimeError(f"upload {rel} failed: {r.stderr[-300:]}")


def _slug(title):
    return re.sub(r"[^\w .()-]+", "", title).strip()[:70] or "change"


# --------------------------------------------------------------------------- filing

def next_id():
    year = o.local().strftime("%Y")
    with _lock():
        n = int(o.kv_get(f"{KV}seq:{year}") or 0) + 1
        o.kv_set(f"{KV}seq:{year}", n)
    return f"RFC-{year}-{n:03d}"


def draft(agent, title, description, command, payload):
    """Ranni drafts the three RFC sections from the performing agent's own request (never inventing scope)."""
    a = o.agent_info(agent)
    with o.Run(AGENT, f"RFC draft for {a['name']}: {title[:120]}", "agent") as run:
        d = o.llm(run, f"""Draft a Request for Change for this change, which {a['name']} ({a.get('role', '')}) will perform.
Use ONLY the information below; where something is unknown, say so plainly instead of guessing.

Title: {title}
Request: {o.strip_secrets(description)[:4000]}
Command the approval will run: {json.dumps(command)[:600]}
Payload: {o.strip_secrets(json.dumps(payload or {}, default=str))[:1500]}

`what`: what will be done, step by step (short). `change`: what will change and who/what is affected.
`recovery`: how to undo it or recover if it goes wrong (backups, rollback commands, who acts).
`risk`: the main risk in one or two sentences. Markdown allowed, no headings.""",
                  model="gpt-6-luna", schema=DRAFT, max_output_tokens=1200, owner_style=False,
                  instructions="You are Ranni, the Roundtable's governance lead, writing a precise ITIL RFC.")
        run.summary = f"drafted RFC for {title[:120]}"
    d["drafted_by"] = "Ranni (from the agent's request)"
    return d


def note_text(rid, info):
    a = o.agent_info(info["agent"])
    d = info["created"][:10]
    s = info["sections"]
    return (f"---\ncreated: {d}\nupdated: {d}\ntype: project\nstatus: seed\nproject-status: active\n"
            f"tags: [rfc, change-management, {info['agent']}]\nrfc-status: \"{info['status']}\"\n"
            f"approval: {info.get('approval') or ''}\n---\n# {rid} · {info['title']}\n\n"
            f"| | |\n|---|---|\n| **Status** | {info['status']} |\n| **Performed by** | {a.get('emoji', '')} {a['name']} "
            f"({a.get('role', '')}) |\n| **Governance** | Ranni |\n| **Approver** | Master Hermes (✅ on Roundtable approval "
            f"#{info.get('approval') or '?'}) |\n| **Filed** | {o.local(o.parse_iso(info['created'])).strftime('%Y-%m-%d %H:%M')} |\n"
            + (f"| **Drafted by** | {s['drafted_by']} |\n" if s.get("drafted_by") else "")
            + f"\n## What will be done\n\n{s['what'].strip()}\n\n## What will change\n\n{s['change'].strip()}\n\n"
            f"## Recovery plan\n\n{s['recovery'].strip()}\n\n## Risk\n\n{(s.get('risk') or 'Not stated.').strip()}\n\n"
            f"## Closure\n\n_Open — Ranni closes this automatically when the approved task ends._\n\n"
            f"Register: [[{REGISTER[:-3]}|RFC Register]]\n")


def _register_line(rid, info):
    a = o.agent_info(info["agent"])
    return (f"| [[{info['rel'][:-3]}|{rid}]] | {info['title'][:80]} | {a['name']} | {info['status']} | "
            f"{info['created'][:10]} | {info.get('closed', '')[:10]} |")


def _sync_vault(rid):
    info = o.kv_get(KV + rid)
    with _lock():
        cur = _read(info["rel"])
        if cur is None:
            cur = note_text(rid, info)
        cur = re.sub(r'(?m)^rfc-status: .*$', f'rfc-status: "{info["status"]}"', cur, count=1)
        cur = re.sub(r"(?m)^\| \*\*Status\*\* \| .* \|$", f"| **Status** | {info['status']} |", cur, count=1)
        cur = re.sub(r"(?m)^updated: .*$", f"updated: {o.local().strftime('%Y-%m-%d')}", cur, count=1)
        if info["status"] in FINAL:
            cur = re.sub(r"(?m)^project-status: .*$", "project-status: done", cur, count=1)
            closure = (f"**{info['status']}** — closed by Ranni on "
                       f"{o.local(o.parse_iso(info['closed'])).strftime('%Y-%m-%d %H:%M')}.\n\n"
                       + (f"```\n{o.strip_secrets(info.get('result') or '')[-1500:]}\n```\n" if info.get("result") else ""))
            cur = re.sub(r"(?s)## Closure\n\n.*?\n\nRegister:", f"## Closure\n\n{closure}\nRegister:", cur, count=1)
        _write(info["rel"], cur)
        reg = _read(REGISTER) or ("---\ncreated: {d}\nupdated: {d}\ntype: moc\nstatus: growing\ntags: [rfc, change-management]\n"
                                  "---\n# RFC Register\n\nRequests for Change filed by Roundtable agents and governed by Ranni. "
                                  "Approve or reject on the Discord approval; Ranni closes each RFC when the task ends.\n\n"
                                  "| RFC | Title | Performed by | Status | Filed | Closed |\n|---|---|---|---|---|---|\n"
                                  ).replace("{d}", o.local().strftime("%Y-%m-%d"))
        line = _register_line(rid, info)
        reg = re.sub(rf"(?m)^\| \[\[[^\]]*\|{re.escape(rid)}\]\] .*$", lambda _: line, reg) if f"|{rid}]]" in reg \
            else reg.rstrip("\n") + "\n" + line + "\n"
        reg = re.sub(r"(?m)^updated: .*$", f"updated: {o.local().strftime('%Y-%m-%d')}", reg, count=1)
        _write(REGISTER, reg)


def sync_vault_async(rid):
    def run():
        try:
            _sync_vault(rid)
        except Exception as error:  # noqa: BLE001
            o.log(AGENT, f"{rid} vault sync failed: {type(error).__name__}: {error}")
    threading.Thread(target=run, daemon=True).start()


def file(agent, title, description, command, payload=None, sections=None):
    """Create the RFC (Ranni drafts missing sections). Returns (rid, info); the vault note is written in the background."""
    sections = dict(sections) if sections else draft(agent, title, description, command, payload)
    rid = next_id()
    info = {"agent": agent, "title": title, "status": "In review", "created": o.iso(), "sections": sections,
            "rel": f"{DIR}/{rid} - {_slug(title)}.md"}
    o.kv_set(KV + rid, info)
    return rid, info


def write_now(rid):
    """Vault write for short-lived agent processes: a detached `rfc.py sync` (a daemon thread would die with the
    agent, and a slow OneDrive upload must never hold up posting the approval)."""
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "sync", rid],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     cwd=str(o.CODE_DIR), start_new_session=True)


def embed_fields(rid, info):
    s = info["sections"]
    cut = lambda t: ((t or "").strip()[:700] + ("…" if len((t or "").strip()) > 700 else "")) or "—"  # noqa: E731
    return [(f"📜 {rid} · Request for Change", f"Filed by {o.agent_info(info['agent'])['name']}"
             + (" (drafted by Ranni)" if s.get("drafted_by") else "") + " · governed by Ranni · ✅ approves this RFC", False),
            ("What will be done", cut(s["what"]), False), ("What will change", cut(s["change"]), False),
            ("Recovery plan", cut(s["recovery"]), False)]


def link_approval(rid, aid):
    info = o.kv_get(KV + rid)
    if info:
        info["approval"] = aid
        o.kv_set(KV + rid, info)
        write_now(rid)


def set_status(rid, status, result=None, channel=None, reply_to=None):
    info = o.kv_get(KV + (rid or ""))
    if not info or info["status"] in FINAL:
        return
    info["status"] = status
    if status in FINAL:
        info["closed"] = o.iso()
        info["result"] = result or ""
    o.kv_set(KV + rid, info)
    sync_vault_async(rid)
    if status in FINAL and channel:
        icon = {"Implemented": "✅", "Rolled back": "↩️", "Rejected": "❌"}.get(status, "⚠️")
        o.post(channel, embeds=[o.embed(AGENT, f"📜 {rid} closed · {status} {icon}",
                                        f"**{info['title']}** ({o.agent_info(info['agent'])['name']})\n"
                                        f"Closed by Ranni. Vault: `{info['rel']}`", color=o.SEV_COLORS.get(
                                            "ok" if status == "Implemented" else "medium"))], reply_to=reply_to)


def status_for_result(approval_status, result):
    if approval_status == "done":
        return "Implemented"
    return "Rolled back" if re.search(r"roll(ed|ing)? ?back", result or "", re.I) else "Failed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "show", "sync"])
    ap.add_argument("rid", nargs="?")
    a = ap.parse_args()
    if a.cmd == "sync":
        try:
            _sync_vault(a.rid)
        except Exception as error:  # noqa: BLE001
            o.log(AGENT, f"{a.rid} vault write failed (retried on the next status change): {error}")
        return
    if a.cmd == "show":
        print(json.dumps(o.kv_get(KV + (a.rid or "")), indent=1, ensure_ascii=False))
        return
    with contextlib.closing(o.db()) as c:
        keys = [r["k"] for r in c.execute("SELECT k FROM kv WHERE k LIKE 'rfc:RFC-%' ORDER BY k DESC LIMIT 30")]
    for k in keys:
        i = o.kv_get(k)
        print(f"{k[4:]} | {i['status']} | {o.agent_info(i['agent'])['name']} | {i['title']}")


if __name__ == "__main__":
    main()
