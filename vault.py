#!/usr/bin/env python3
"""Shared Obsidian vault plumbing for the Roundtable agents.

* A read-only Markdown mirror of the vault (rclone sync with filters), searched by Miriel (scribe.py).
* Section-level writes into individual notes (ipmap.py keeps its registry section current) that never overwrite
  a note somebody edited in Obsidian meanwhile.

Private folders, transcripts, session logs, attachments and anything that looks like a credential never enter the
mirror, and every excerpt is passed through olympus_core.strip_secrets.
"""
import contextlib
import datetime as dt
import fcntl
import os
import re
import subprocess
import tempfile
from pathlib import Path

import olympus_core as o

AGENT = "vault"
REMOTE = os.environ.get("DOC_AGENT_REMOTE", "onedrive:Obsidian Vault")
MIRROR = o.DATA_DIR / "vault"
LOCK = o.DATA_DIR / "vault.lock"
# Read-only vault mirror: Markdown only, never credentials, secrets, attachments, private notes or tool folders.
FILTERS = """- .obsidian/**
- .trash/**
- .git/**
- .claude/**
- 999 Attachments/**
- **/node_modules/**
- **/agents/**
- **/Roundtable Transcripts/**
- AI/Personal/**
- AI/Transcripts/**
- AI/sessions/**
- *[Cc]redential*
- *[Pp]assword*
- *[Ss]ecret*
- *[Ll]icense [Kk]ey*
- *[Aa][Pp][Ii] [Kk]ey*
- *[Tt]oken*
- *.env*
+ *.md
- **
"""
STOP = set("""a an and are as at be but by can could did do does for from had has have he her his how i i'm im if in
into is it its just me my of on or our she so than that the their them then there they this to was we were what when
where which who why will with would you your about been more some also really like know think feel get got going want
""".split())


@contextlib.contextmanager
def _lock():
    o.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _rclone(*args, timeout=900):
    return subprocess.run(["rclone", "--retries", "3", *args], capture_output=True, text=True, timeout=timeout)


def _terms(text):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'+-]{2,}", (text or "").lower()) if w not in STOP][:30]


# --------------------------------------------------------------------------- read-only mirror

def sync_vault():
    MIRROR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".filter", delete=False) as f:
        f.write(FILTERS)
        flt = f.name
    try:
        r = _rclone("sync", REMOTE, str(MIRROR), "--filter-from", flt, "--delete-excluded", "--fast-list",
                    "--transfers", "8")
    finally:
        os.unlink(flt)
    if r.returncode != 0:
        o.log(AGENT, f"vault sync failed: {r.stderr[-400:]}")
        return False
    n = sum(1 for _ in MIRROR.rglob("*.md"))
    o.kv_set("vault_mirror", {"synced": o.iso(), "notes": n})
    return True


def _rel(p):
    return str(p.relative_to(MIRROR)).replace("\\", "/")


def _notes():
    def permitted(p):
        rel = _rel(p)
        return (not p.is_symlink() and p.is_file() and
                not any(rel.startswith(x) for x in ("AI/Personal/", "AI/Transcripts/", "AI/sessions/")) and
                not any(x in p.parts for x in ("agents", ".git", ".obsidian", "Roundtable Transcripts")) and
                not re.search(r"credential|password|secret|license.key|api.key|token", rel, re.I))
    return [p for p in MIRROR.rglob("*.md") if permitted(p)]


def _clean(text, n):
    return o.strip_secrets(text or "")[:n]


def search_vault(query, k=5):
    terms = _terms(query)
    if not terms or not MIRROR.exists():
        return []
    now = dt.datetime.now().timestamp()
    hits = []
    for p in _notes():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low, title = txt.lower(), p.stem.lower()
        score = sum(min(low.count(t), 6) + 4 * (t in title) for t in terms)
        if not score:
            continue
        matched = sum(1 for t in terms if t in low or t in title)
        score *= 1 + matched / len(terms)
        age_days = (now - p.stat().st_mtime) / 86400
        score *= 1.3 if age_days < 30 else 1.0
        i = min((low.find(t) for t in terms if t in low), default=0)
        snip = re.sub(r"\s+", " ", txt[max(0, i - 200): i + 500]).strip()
        hits.append((score, _rel(p), snip))
    hits.sort(reverse=True)
    return [{"note": r, "excerpt": _clean(s, 700)} for _, r, s in hits[:k]]


# --------------------------------------------------------------------------- single-note writes

def frontmatter(title, kind="note", tags=(), created=None, status="growing"):
    day = o.local().strftime("%Y-%m-%d")
    tags = ", ".join(tags)
    return (f"---\ncreated: {created or day}\nupdated: {day}\ntype: {kind}\nstatus: {status}\ntags: [{tags}]\n---\n\n"
            f"# {title}\n")


def vault_read(rel):
    """Current text of a vault note, or None when it does not exist."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "note.md"
        r = _rclone("copyto", f"{REMOTE}/{rel}", str(p), timeout=180)
        return p.read_text(encoding="utf-8") if r.returncode == 0 and p.exists() else None


def vault_update(rel, change, title, kind="note", tags=()):
    """change(current_text_or_None) -> new text; uploads only if nobody edited the note meanwhile. Returns True/False."""
    with _lock(), tempfile.TemporaryDirectory() as td:
        cur = vault_read(rel)
        base = cur if cur is not None else frontmatter(title, kind, tags)
        new = change(base)
        new = re.sub(r"(?m)^updated: .*$", f"updated: {o.local().strftime('%Y-%m-%d')}", o.strip_secrets(new), count=1)
        if new == cur:
            return True
        if vault_read(rel) != cur:  # edited in Obsidian while we worked: never overwrite, retry next time
            o.log(AGENT, f"{rel} changed concurrently; skipped")
            return False
        p = Path(td) / "new.md"
        p.write_text(new, encoding="utf-8")
        r = _rclone("copyto", str(p), f"{REMOTE}/{rel}", timeout=180)
        if r.returncode != 0:
            o.log(AGENT, f"vault upload of {rel} failed: {r.stderr[-200:]}")
            return False
        return True


def replace_section(text, heading, body):
    """Replace (or append) a '## heading' section; everything else in the note stays as the owner left it."""
    block = f"## {heading}\n\n{body.strip()}\n"
    m = re.search(rf"(?ms)^## {re.escape(heading)}\n.*?(?=^## |\Z)", text)
    if m:
        return text[:m.start()] + block + "\n" + text[m.end():].lstrip("\n")
    return text.rstrip("\n") + "\n\n" + block
