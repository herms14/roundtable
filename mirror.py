#!/usr/bin/env python3
"""Radagon - Roundtable mirror: keeps the public homelab-hub repo matching the live lab.

  mirror.py run [--force]            # daily 07:30: live inventory vs docs -> branch + diff -> approval in Discord
  mirror.py push <branch>            # run by Gideon after Master Hermes approves: rebase, re-scan, push to main
  mirror.py ask "<text>" [--reply-to MID] [--channel CID]
  mirror.py status

Only touches docs/*.md, README.md and CHANGELOG.md of github.com/herms14/homelab-hub. Every change is secret-scanned
twice (repo scanner + Roundtable redaction rules) and nothing is pushed without his ✅. The GitHub token is passed to
git through the environment only; it is never written to .git/config.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys

import olympus_core as o

AGENT = "mirror"
CHANNEL = "homelab-infrastructure"
REPO = "herms14/homelab-hub"
WORK = o.DATA_DIR / "mirror" / "homelab-hub"
KV = "mirror:state"
EDITABLE = re.compile(r"^(docs/[\w.-]+\.md|README\.md)$")
AUTHOR = ("Radagon (Roundtable)", "herms14@users.noreply.github.com")


def _token():
    return (os.environ.get("MIRROR_GITHUB_TOKEN") or os.environ.get("HARMONIA_GITHUB_TOKEN") or "").strip()


def git(*args, check=True, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME=AUTHOR[0], GIT_AUTHOR_EMAIL=AUTHOR[1],
               GIT_COMMITTER_NAME=AUTHOR[0], GIT_COMMITTER_EMAIL=AUTHOR[1])
    tok = _token()
    if tok:
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0="Authorization: Basic " +
                   base64.b64encode(f"x-access-token:{tok}".encode()).decode())
    cwd = str(WORK) if WORK.exists() else str(WORK.parent)
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        err = o.strip_secrets((r.stderr or r.stdout)[-600:])
        import access
        access.from_error(AGENT, f"GitHub repo {REPO}", err, f"a GitHub token with contents:write on {REPO} "
                          "(MIRROR_GITHUB_TOKEN in ~/.olympus_env)", "Radagon keeps the public docs in sync")
        raise RuntimeError(f"git {args[0]} failed: {err}")
    return r.stdout


def sync_main():
    WORK.parent.mkdir(parents=True, exist_ok=True)
    if not (WORK / ".git").exists():
        git("clone", "--quiet", f"https://github.com/{REPO}.git", str(WORK), timeout=300)
    git("fetch", "--quiet", "--prune", "origin")
    git("checkout", "--quiet", "-B", "main", "origin/main")
    git("reset", "--quiet", "--hard", "origin/main")
    git("clean", "-fdq")


# --------------------------------------------------------------------------- deterministic inventory tables

def _table(text, header_prefix):
    """Return (start, end, rows) of the Markdown table whose header line starts with header_prefix."""
    lines = text.split("\n")
    for i, l in enumerate(lines):
        if l.startswith(header_prefix):
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in lines[i + 2:j]]
            return i, j, rows
    return None


def _gb(x):
    return f"{round(x)} GB" if x else "-"


def regen_inventory(md, comp):
    lines = md.split("\n")
    out = md
    today = o.local().strftime("%Y-%m-%d")
    out = re.sub(r"taken \*\*\d{4}-\d{2}-\d{2}\*\*", f"taken **{today}**", out, count=1)

    t = _table(out, "| Node |")
    if t:
        s, e, rows = t
        mgmt = {r[0]: r[1] for r in rows if len(r) > 1}
        body = [f"| {n['node']} | {mgmt.get(n['node'], '-')} | {n['cores']} | {_gb(n['ram_max_gb'])} |"
                for n in comp["nodes"]]
        lines = out.split("\n")
        out = "\n".join(lines[:s + 2] + body + lines[e:])

    for kind, prefix, head_rx, head_fmt in (
            ("VM", "| VMID |", r"(?m)^## Virtual Machines.*$", "## Virtual Machines ({run} running + {tpl} templates)"),
            ("LXC", "| CTID |", r"(?m)^## LXC Containers.*$", "## LXC Containers ({n}, {run} running)")):
        t = _table(out, prefix)
        if not t:
            continue
        s, e, rows = t
        roles = {r[0]: re.sub(r"\s*\(\*\*stopped\*\*\)", "", r[5]) for r in rows if len(r) > 5}
        gs = [g for g in comp["guests"] if g["kind"] == kind]
        body = []
        for g in gs:
            role = roles.get(str(g["id"])) or (g.get("note") or g.get("tags") or "_new - role to be described_")
            if not g["template"] and g["status"] != "running":
                role += " (**stopped**)"
            ip = "-" if g["template"] else (g["ip"] or "dhcp")
            body.append(f"| {g['id']} | {g['name']} | {g['node']} | {'-' if g['template'] else _gb(g['ram_max_gb'])} "
                        f"| {ip} | {role} |")
        lines = out.split("\n")
        out = "\n".join(lines[:s + 2] + body + lines[e:])
        real = [g for g in gs if not g["template"]]
        run = sum(g["status"] == "running" for g in real)
        out = re.sub(head_rx, head_fmt.format(run=run, tpl=len(gs) - len(real), n=len(real)), out, count=1)
    return out


def _fingerprint(comp):
    keep = {"nodes": [(n["node"], n["cores"], round(n["ram_max_gb"] or 0)) for n in comp["nodes"]],
            "guests": [(g["id"], g["name"], g["kind"], g["node"], g["status"], g["template"], g["ip"],
                        round(g["ram_max_gb"] or 0)) for g in comp["guests"]]}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- LLM drift check for prose docs

EDITS = o.S(edits=o.A(o.S(file=o.STR, old=o.STR, new=o.STR, why=o.STR)), summary=o.STR)


def drift_edits(run, live):
    docs = {}
    for p in sorted((WORK / "docs").glob("*.md")) + [WORK / "README.md"]:
        rel = p.relative_to(WORK).as_posix()
        if rel != "docs/inventory.md" and p.exists():
            docs[rel] = p.read_text(encoding="utf-8")[:14000]
    blob = "\n\n".join(f"===== FILE {k} =====\n{v}" for k, v in docs.items())
    r = o.llm(run, f"""You are Radagon, keeper of the public homelab-hub repo. Compare the docs with the LIVE lab state and
propose minimal exact-text edits ONLY for facts that are now wrong (IP addresses, guest names, which node something
runs on, counts, removed/added guests, versions shown in the live data). Do not restyle, do not add opinions, do not
add secrets, internal tokens or credentials, do not touch anything you cannot verify from LIVE. `old` must be copied
exactly from the file and be unique in it (include enough context); `new` is its replacement. Return no edits if the
docs are accurate.

LIVE (Proxmox + containers, authoritative):
{live}

DOCS:
{blob}""", schema=EDITS, model="gpt-6-luna", max_output_tokens=4000)
    applied, rejected = [], []
    for ed in r["edits"]:
        f = ed["file"].strip().lstrip("./")
        p = WORK / f
        if not EDITABLE.match(f) or not p.exists():
            rejected.append(f"{f}: not an editable doc")
            continue
        txt = p.read_text(encoding="utf-8")
        if not ed["old"] or txt.count(ed["old"]) != 1 or ed["old"] == ed["new"]:
            rejected.append(f"{f}: text not found exactly once")
            continue
        if o.strip_secrets(ed["new"]) != ed["new"]:
            rejected.append(f"{f}: replacement looked like a secret")
            continue
        p.write_text(txt.replace(ed["old"], ed["new"], 1), encoding="utf-8")
        applied.append(ed)
    return applied, rejected, r["summary"]


def _changelog(items):
    p = WORK / "CHANGELOG.md"
    txt = p.read_text(encoding="utf-8")
    today = o.local().strftime("%Y-%m-%d")
    entry = f"## [docs-sync] - {today}\n\n### Changed\n" + "\n".join(f"- {i}" for i in items) + "\n\n"
    m = re.search(r"(?m)^## \[", txt)
    p.write_text(txt[:m.start()] + entry + txt[m.start():] if m else txt.rstrip() + "\n\n" + entry, encoding="utf-8")


def _scan():
    git("add", "-A")
    r = subprocess.run([sys.executable, "scripts/secret_scan.py"], cwd=str(WORK), capture_output=True, text=True,
                       timeout=120)
    diff = git("diff", "--cached")
    if r.returncode != 0 or o.strip_secrets(diff) != diff:
        return False, (r.stdout or "")[-800:] or "Roundtable redaction rules matched the diff"
    return True, diff


def run(force=False, reply_to=None, channel=None):
    channel = channel or CHANNEL
    if not _token():
        import access
        access.report(AGENT, f"GitHub repo {REPO}", f"a GitHub token with contents:write on {REPO} "
                      "(MIRROR_GITHUB_TOKEN in ~/.olympus_env)", "Radagon keeps the public docs in sync")
        raise SystemExit("no GitHub token")
    import inventory
    with o.Run(AGENT, "mirror homelab-hub", "chat" if reply_to else "schedule") as rn:
        comp = inventory.get("compute", fresh=True)
        if comp.get("error") and not comp.get("guests"):
            raise RuntimeError(f"inventory unavailable: {comp['error']}")
        st = o.kv_get(KV, {}) or {}
        if st.get("pending"):
            ap = o.get_approval(st["pending"]["approval"]) or {}
            if ap.get("status") not in ("pending", "approved", "scheduled", "running", None):
                clear_pending()
                st = o.kv_get(KV, {}) or {}
        if st.get("pending"):
            rn.summary = f"waiting on approval #{st['pending']['approval']}"
            if reply_to:
                o.respond(AGENT, channel, "🌗 Still awaiting your ✅",
                          f"Branch `{st['pending']['branch']}` (approval #{st['pending']['approval']}) is not pushed "
                          "yet. Approve or reject it first.", reply_to=reply_to, run=rn)
            return
        sync_main()
        head = git("rev-parse", "HEAD").strip()
        fp = _fingerprint(comp) + ":" + head[:12]
        if fp == st.get("fingerprint") and not force:
            rn.summary = "no change since last check"
            return
        inv = WORK / "docs" / "inventory.md"
        before = inv.read_text(encoding="utf-8")
        after = regen_inventory(before, comp)
        changes = []
        if re.sub(r"taken \*\*[\d-]+\*\*", "", after) != re.sub(r"taken \*\*[\d-]+\*\*", "", before):
            inv.write_text(after, encoding="utf-8")
            changes.append("`docs/inventory.md`: node, VM and LXC tables refreshed from the live cluster")
        live = inventory.text("compute", limit=12000)
        applied, rejected, summ = drift_edits(rn, live)
        changes += [f"`{e['file']}`: {e['why']}" for e in applied]
        if not changes:
            st.update(fingerprint=fp, checked=o.iso(), result="in sync")
            o.kv_set(KV, st)
            rn.summary = "docs in sync"
            if reply_to:
                o.respond(AGENT, channel, "🌗 The mirror is true", "homelab-hub matches the live lab. Nothing to "
                          "change.", reply_to=reply_to, run=rn)
            return
        _changelog([re.sub(r"`", "", c) for c in changes])
        ok, diff = _scan()
        if not ok:
            git("reset", "--quiet", "--hard", "origin/main")
            rn.status = "failed"
            o.respond(AGENT, channel, "🛑 Mirror blocked by the secret scan",
                      f"I discarded my edits; nothing was committed.\n```\n{o.strip_secrets(diff)[:1200]}\n```",
                      color=o.SEV_COLORS["high"], reply_to=reply_to, run=rn)
            return
        branch = f"radagon/{o.local().strftime('%Y%m%d-%H%M')}"
        git("checkout", "--quiet", "-B", branch)
        git("commit", "--quiet", "-m", "docs: sync with live lab (Radagon)\n\n" + "\n".join(f"- {c}" for c in changes))
        stat = git("show", "--stat", "--format=", "HEAD").strip()
        o.respond(AGENT, channel, "🌗 Proposed homelab-hub changes",
                  f"{summ}\n\n" + "\n".join(f"- {c}" for c in changes) +
                  (f"\n\n_Rejected suggestions:_ " + "; ".join(rejected) if rejected else "") +
                  f"\n\n```\n{stat[-900:]}\n```\nFull diff attached.\n\n```diff\n{diff}\n```",
                  reply_to=reply_to, run=rn, filename=f"homelab-hub-{branch.split('/')[1]}.md")
        aid = o.request_approval(
            AGENT, f"Push docs sync to {REPO}", f"Push commit `{git('rev-parse', '--short', 'HEAD').strip()}` on "
            f"`{branch}` to **main** of the public repo ({len(changes)} change(s), secret scan clean). The diff is "
            "in the message above.", [sys.executable, str(o.CODE_DIR / "mirror.py"), "push", branch], channel,
            severity="low", fields=[("Changes", "\n".join(f"• {c}" for c in changes)[:1000], False)],
            payload={"branch": branch})
        st.update(fingerprint=fp, checked=o.iso(), result="proposed",
                  pending={"branch": branch, "approval": aid, "t": o.iso()})
        o.kv_set(KV, st)
        rn.summary = f"proposed {len(changes)} change(s) on {branch} (approval #{aid})"


def push(branch):
    st = o.kv_get(KV, {}) or {}
    with o.Run(AGENT, f"push {branch}", "approval") as rn:
        git("fetch", "--quiet", "origin")
        git("checkout", "--quiet", branch)
        r = subprocess.run(["git", "rebase", "--quiet", "origin/main"], cwd=str(WORK), capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=str(WORK), capture_output=True)
            st.pop("pending", None)
            st["fingerprint"] = ""
            o.kv_set(KV, st)
            rn.status = "failed"
            o.respond(AGENT, CHANNEL, "⚠️ Mirror push skipped", "main moved and my branch no longer rebases cleanly. "
                      "I dropped it and will propose a fresh sync on the next run.", run=rn)
            return
        diff = git("diff", "origin/main..HEAD")
        if o.strip_secrets(diff) != diff:
            rn.status = "failed"
            o.respond(AGENT, CHANNEL, "🛑 Mirror push blocked", "The re-scan before push matched a secret pattern; "
                      "nothing was pushed.", color=o.SEV_COLORS["high"], run=rn)
            return
        git("push", "--quiet", "origin", f"HEAD:main", timeout=180)
        sha = git("rev-parse", "--short", "HEAD").strip()
        msg = git("log", "-1", "--format=%b").strip()
        st.pop("pending", None)
        st.update(pushed=o.iso(), last_commit=sha, result="pushed")
        o.kv_set(KV, st)
        git("checkout", "--quiet", "-B", "main", "origin/main", check=False)
        git("branch", "-D", branch, check=False)
        rn.summary = f"pushed {sha} to {REPO}"
        o.respond(AGENT, CHANNEL, "🌗 homelab-hub updated",
                  f"Pushed [`{sha}`](https://github.com/{REPO}/commit/{sha}) to `main`.\n{msg}", run=rn,
                  color=o.SEV_COLORS.get("ok"))
        try:
            import scribe
            scribe.log_change("homelab-hub docs synced", f"Commit {sha} on {REPO}: " + msg.replace("\n", " ")[:400],
                              AGENT, "Changed")
        except Exception as e:  # noqa: BLE001
            o.log(AGENT, f"changelog failed: {e}")


def status_text():
    st = o.kv_get(KV, {}) or {}
    p = st.get("pending")
    return (f"Last check: {(st.get('checked') or 'never')[:16]} · result: {st.get('result', '-')}\n"
            f"Last push: {(st.get('pushed') or 'never')[:16]} {st.get('last_commit', '')}\n" +
            (f"Awaiting approval #{p['approval']} for branch `{p['branch']}`" if p else "Nothing pending."))


def clear_pending():
    """Called when an approval is rejected (Gideon) or by hand: drop the local branch so the next run re-proposes."""
    st = o.kv_get(KV, {}) or {}
    p = st.pop("pending", None)
    st["fingerprint"] = ""
    o.kv_set(KV, st)
    if p and WORK.exists():
        git("checkout", "--quiet", "-B", "main", "origin/main", check=False)
        git("branch", "-D", p["branch"], check=False)
    return p


INTENT = o.S(action=o.E("sync_now", "status", "discard_pending", "answer"))


def ask(text, reply_to=None, channel=None):
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", text, "discord", channel, reply_to or "")
    with o.Run(AGENT, f"ask: {text[:200]}", "chat") as rn:
        act = o.llm(rn, f"""Classify Master Hermes's request to Radagon, who keeps the public GitHub repo homelab-hub
in sync with the live lab. sync_now: check/sync now. status: last sync / anything pending. discard_pending: drop the
proposed branch. answer: anything else about the repo or docs.
Request: {text}""", schema=INTENT, model="gpt-6-luna")["action"]
        if act == "status":
            o.respond(AGENT, channel, "🌗 Mirror status", status_text(), reply_to=reply_to, run=rn)
            return
        if act == "discard_pending":
            p = clear_pending()
            o.respond(AGENT, channel, "🌗 Discarded", f"Dropped `{p['branch']}`." if p else "Nothing was pending.",
                      reply_to=reply_to, run=rn)
            return
        if act == "answer":
            o.respond(AGENT, channel, "🌗 Radagon", f"I keep [{REPO}](https://github.com/{REPO}) (`docs/`, README, "
                      "CHANGELOG) true to the live cluster. Daily at 07:30 I compare, propose a branch with the diff, "
                      f"and push only after your ✅.\n\n{status_text()}", reply_to=reply_to, run=rn)
            return
    run(force=True, reply_to=reply_to, channel=channel)


VOICE_TOOLS = [
    {"type": "function", "name": "mirror_status",
     "description": "Radagon's sync status for the public homelab-hub GitHub repo: last check, last push, pending "
                    "approval.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def voice_tool(name, args):
    if name == "mirror_status":
        return {"status": status_text()}
    return {"error": f"unknown tool {name}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "push", "ask", "status", "discard"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a.force)
    elif a.cmd == "push":
        push(a.arg)
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    elif a.cmd == "discard":
        print(clear_pending())
    else:
        print(status_text())


if __name__ == "__main__":
    main()
