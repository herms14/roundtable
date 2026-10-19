#!/usr/bin/env python3
"""Melina - Roundtable tech blogger for Clustered Thoughts. The Roundtable front door to blog-studio.

  writer.py pitch                    # weekly (Sat 10:00): what to write next + interview questions -> #blog-studio
  writer.py draft <post>             # draft a post (STATUS.md row number, or a new topic) with the blog-studio
                                     # writer prompt (Codex, non-interactive), then sync it to the vault
  writer.py note <post> "<answer>"   # save Master Hermes's interview answer for a post (used by the next draft)
  writer.py ask "<text>" [--reply-to MID] [--channel CID]
  writer.py status

blog-studio (~/blog-studio, AGENTS.md = the writer's voice/rules) stays the single source of truth; the interactive
`blog-writer` session on Kratos keeps working. Melina never publishes: every draft has `publish: false`, and she
never runs final-quality images (that stays a decision in the interactive writer).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import olympus_core as o

AGENT = "writer"
CHANNEL = "ideas-and-todos"  # was #blog-studio (never used; deleted 2026-10-15)
TOPIC = ("🔥 Melina's blog studio for Clustered Thoughts: weekly pitch Sat 10:00. Talk to her here: `status`, "
         "`draft post 2`, answer her interview questions, or pitch her a story.")
STUDIO = Path(os.environ.get("BLOG_STUDIO", str(Path.home() / "blog-studio")))
INTERVIEW = STUDIO / "interview"
DRAFT_MODEL = os.environ.get("BLOG_WRITER_MODEL", "gpt-6-sol")
DRAFT_EFFORT = os.environ.get("BLOG_WRITER_EFFORT", "medium")
REMOTE = os.environ.get("DOC_AGENT_REMOTE", "onedrive:Obsidian Vault/Hermes's Life Knowledge Base")
VAULT_OUT = "07 HomeLab Things/Homelab Blog Posts/Consolidated"
CHANGELOG_REL = "07 HomeLab Things/Claude Managed Homelab/Homelab Changelog.md"
KV_LAST, KV_CHAT, KV_BUSY = "writer:last_pitch", "writer:chat_last", "writer:drafting"
BLOG_URL = "https://herms14.github.io/Clustered-Thoughts/"


# --------------------------------------------------------------------------- blog-studio state

def rows():
    """Posts from the STATUS.md consolidation table: num, topic, draft path, drafted?, open questions."""
    try:
        text = (STUDIO / "STATUS.md").read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = re.match(r"\|\s*(\d{1,3})\s*\|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        dm = re.search(r"drafts/[\w.\-]+\.md", line)
        path = dm.group(0) if dm else ""
        qs = re.findall(r"\[CONFIRM:\s*([^\]]+)\]", cells[-1])
        out.append({"num": int(m.group(1)), "topic": cells[1], "draft": path,
                    "drafted": bool(path) and (STUDIO / path).exists(), "rec": cells[5] if len(cells) > 5 else "",
                    "questions": qs, "answered": (INTERVIEW / f"{int(m.group(1)):02d}.md").exists()})
    return out


def find_post(ref):
    """A row by number ("2", "post 02") or by topic words; None when it looks like a new topic."""
    rs = rows()
    m = re.fullmatch(r"\s*(?:post\s*#?\s*)?(\d{1,3})\s*", str(ref or ""), re.I)
    if m:
        return next((r for r in rs if r["num"] == int(m.group(1))), None)
    words = set(re.findall(r"[a-z0-9]{4,}", (ref or "").lower()))
    best, score = None, 0
    for r in rs:
        s = len(words & set(re.findall(r"[a-z0-9]{4,}", (r["topic"] + " " + r["draft"]).lower())))
        if s > score:
            best, score = r, s
    return best if score >= 2 or (score == 1 and len(words) == 1) else None


def answers(num):
    p = INTERVIEW / f"{num:02d}.md"
    return p.read_text(errors="replace") if p.exists() else ""


def save_note(num, text):
    INTERVIEW.mkdir(parents=True, exist_ok=True)
    p = INTERVIEW / f"{num:02d}.md"
    with p.open("a") as f:
        if p.stat().st_size == 0:
            f.write(f"# Interview answers for post {num:02d}\n\nFrom Master Hermes via Melina (Discord/voice). "
                    f"Treat as first-hand facts for the draft.\n")
        f.write(f"\n## {o.local().strftime('%Y-%m-%d %H:%M')}\n\n{text.strip()}\n")
    return p


def recent_changelog(lines=220):
    r = subprocess.run(["rclone", "cat", f"{REMOTE}/{CHANGELOG_REL}"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return "(changelog unavailable)"
    return o.strip_secrets("\n".join(r.stdout.splitlines()[:lines]))


def recent_runs(days=7):
    import contextlib
    since = o.iso(o.utcnow() - __import__("datetime").timedelta(days=days))
    with contextlib.closing(o.db()) as con:
        rs = con.execute("SELECT agent, task, status, summary FROM runs WHERE started>=? AND agent IN "
                         "('medic','builder','netops','security','backup','media','mirror') AND status!='skipped' "
                         "AND summary IS NOT NULL ORDER BY id DESC LIMIT 60", (since,)).fetchall()
    return "\n".join(f"- [{r['agent']}] {r['task'][:80]}: {(r['summary'] or '')[:200]}" for r in rs)


def sync_to_vault(paths):
    """Copy the given studio files into the vault's Consolidated/ (modtimes kept, so Kratos' push/pull stay sane)."""
    done = []
    for rel in paths:
        src = STUDIO / rel
        if not src.exists():
            continue
        r = subprocess.run(["rclone", "--retries", "3", "copyto", str(src), f"{REMOTE}/{VAULT_OUT}/{rel}"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            done.append(rel)
    return done


# --------------------------------------------------------------------------- weekly pitch

PITCH = o.S(
    headline=o.STR,
    next_up=o.A(o.S(num={"type": "integer"}, topic=o.STR, why_now=o.STR, questions=o.A(o.STR))),
    new_ideas=o.A(o.S(title=o.STR, angle=o.STR, evidence=o.STR, fits_series=o.STR)),
)


def pitch(reply_to=None, channel=None):
    channel = channel or CHANNEL
    cid = o.channel_id(channel, TOPIC if channel == CHANNEL else None)
    rs = rows()
    queue = [r for r in rs if not r["drafted"]]
    table = "\n".join(f"{r['num']:02d}. {r['topic']} | answered: {'yes' if r['answered'] else 'no'} | open: "
                      + ("; ".join(r["questions"]) or "none") for r in queue[:20])
    done = ", ".join(f"{r['num']:02d}" for r in rs if r["drafted"]) or "none"
    with o.Run(AGENT, "weekly pitch", "chat" if reply_to else "schedule") as run:
        r = o.llm(run, f"""You are Melina, the Roundtable's storyteller and Master Hermes's tech blogger for Clustered
Thoughts ({BLOG_URL}). His style: direct, honest about what broke, readable for non-technical people, never invented.

1) next_up: choose the 3 best posts to draft next from the CONSOLIDATION QUEUE (use their numbers). Prefer posts
   whose open questions are already answered or that recent lab events make timely. For each, list the 1-3 interview
   questions he must answer first (from its open questions, rephrased warmly and concretely; [] if none).
2) new_ideas: 0-3 NEW story ideas grounded ONLY in real events in RECENT CHANGELOG / RECENT AGENT WORK (an outage, a
   fix, a build, a new agent). evidence = the dated changelog entry or event it comes from. fits_series = which
   queue post it could extend, or "standalone". Never invent events.
headline: one inviting sentence.

DRAFTED ALREADY: {done}
CONSOLIDATION QUEUE:
{table or '(STATUS.md has no queue yet)'}

RECENT CHANGELOG:
{recent_changelog()[:9000]}

RECENT AGENT WORK (7 days):
{recent_runs()[:4000]}""", schema=PITCH, model="gpt-6-luna", effort="low", timeout=300)
        lines = []
        for p in r["next_up"]:
            q = "\n".join(f"   ❓ {x}" for x in p["questions"]) or "   ✅ no open questions, ready to draft"
            lines.append(f"**{p['num']:02d}. {p['topic']}**\n↳ {p['why_now']}\n{q}")
        body = f"_{r['headline']}_\n\n📜 **Next up**\n" + "\n\n".join(lines)
        if r["new_ideas"]:
            body += "\n\n✨ **New stories from the lab**\n" + "\n".join(
                f"• **{i['title']}**: {i['angle']} _(from: {i['evidence']}; {i['fits_series']})_" for i in r["new_ideas"])
        body += ("\n\nReply here with your answers (e.g. `post 4: the rule that locked me out was …`) and I will keep "
                 "them for the draft. Say `draft post 4` when you want it written.")
        o.kv_set(KV_LAST, {"t": o.iso(), **r})
        run.summary = "pitched " + ", ".join(f"{p['num']:02d}" for p in r["next_up"]) + (
            f" + {len(r['new_ideas'])} new ideas" if r["new_ideas"] else "")
        o.respond(AGENT, cid, "🔥 Melina's weekly blog pitch", body, reply_to=reply_to, run=run,
                  filename=f"blog-pitch-{o.local().strftime('%Y%m%d')}.md")
        return r


# --------------------------------------------------------------------------- drafting

DRAFT = o.S(file=o.STR, title=o.STR, words={"type": "integer"}, lint=o.STR, confirms=o.A(o.STR),
            image_plan=o.STR, summary=o.STR)


def _draft_prompt(post, topic, notes):
    if post:
        what = (f"Draft post {post['num']:02d} \"{post['topic']}\" exactly as planned in its STATUS.md row "
                f"(sources, merge recommendation, stale facts, canonical slug"
                + (f", draft path `{post['draft']}`" if post["draft"] else "") + ").")
        if post["drafted"]:
            what += (f" A draft already exists at `{post['draft']}`: revise it in place using the new interview "
                     "answers below and resolve any [CONFIRM] they answer. Keep his approved wording.")
    else:
        what = (f"New post requested: \"{topic}\". Find the facts in sources/ (and the interview answers below), "
                "pick a slug, write it to drafts/<nn>-<slug>.md using the next free number, and add a row to the "
                "STATUS.md table.")
    return f"""You are the Homelab Blog Writer (follow AGENTS.md in this directory: voice, house style, rules, lint).
This is a NON-INTERACTIVE run started by Master Hermes through Melina on Discord: you cannot ask him anything now.
You are an experienced technical writer reporting to Ranni. Give each article a human story: the problem,
why it mattered, the technical decisions/tradeoffs, the verified result and the lesson. Keep warmth and heart
without inventing anecdotes, feelings or outcomes; follow the studio's existing house style.
Wherever a fact is missing or uncertain, write `[CONFIRM: <question>]` instead of guessing.

TASK: {what}
- Keep `publish: false` in the front matter.
- Run `blog-lint` on the draft and fix what it reports.
- Write or update the image plan in image-plans/<slug>.json, but do NOT run blog-artist (images are his decision).
- Update this post's row and the status note in STATUS.md.
- Do not touch other drafts, sources/ or AGENTS.md.

INTERVIEW ANSWERS FROM MASTER HERMES (first-hand facts, newest last):
{notes or '(none yet)'}

Final answer: the JSON schema. file = the draft path relative to this directory (drafts/...md), words = approximate
word count, lint = blog-lint result in one line, confirms = every remaining [CONFIRM] question, image_plan = its path
or "", summary = 2-3 sentences on what you wrote or changed."""


def draft(ref, reply_to=None, channel=None):
    channel = channel or CHANNEL
    cid = o.channel_id(channel, TOPIC if channel == CHANNEL else None)
    post = find_post(ref)
    topic = post["topic"] if post else str(ref).strip()
    label = f"post {post['num']:02d}" if post else f"new post “{topic[:80]}”"
    busy = o.kv_get(KV_BUSY)
    if busy and time.time() - busy.get("t", 0) < 3 * 3600:
        o.respond(AGENT, cid, "🔥 Melina is already writing",
                  f"I am still at the desk with {busy['label']}, Master Hermes. I will take {label} right after.",
                  reply_to=reply_to)
        return None
    o.kv_set(KV_BUSY, {"t": time.time(), "label": label})
    try:
        with o.Run(AGENT, f"draft {label}", "chat" if reply_to else "manual") as run:
            o.post(cid, f"🔥 Melina is writing **{label}** ({DRAFT_MODEL}, {DRAFT_EFFORT}). This takes a few "
                        f"minutes; I will bring you the draft.", reply_to=reply_to)
            notes = answers(post["num"]) if post else ""
            res, tr = o.codex(run, _draft_prompt(post, topic, notes), model=DRAFT_MODEL, effort=DRAFT_EFFORT,
                              schema=DRAFT, sandbox="workspace-write", cwd=STUDIO, timeout=2700, tag="draft")
            fpath = (res or {}).get("file") or (post or {}).get("draft") or ""
            fpath = fpath.split(str(STUDIO) + "/", 1)[-1].lstrip("./")
            src = STUDIO / fpath if fpath else None
            if not res or not src or not src.exists():
                run.summary = f"draft {label} failed"
                o.respond(AGENT, cid, f"⚠️ Melina could not finish {label}",
                          f"The writer stopped without a draft. Transcript: `{tr}` on codex-agent-lxc.",
                          reply_to=reply_to, run=run, color=o.SEV_COLORS["high"])
                run.finish("failed", run.summary)
                return None
            plan = (res.get("image_plan") or "").split(str(STUDIO) + "/", 1)[-1].lstrip("./")
            synced = sync_to_vault([fpath, "STATUS.md"] + ([plan] if plan else []))
            text = src.read_text(errors="replace")
            confirms = res.get("confirms") or re.findall(r"\[CONFIRM:\s*([^\]]+)\]", text)
            body = (f"**{res.get('title') or topic}** · ~{res.get('words') or len(text.split())} words · "
                    f"`{fpath}`\n{res.get('summary', '')}\n\n🧹 Lint: {res.get('lint') or '?'}\n"
                    f"🖼️ Image plan: {('`' + plan + '`') if plan else 'none yet'} (images not generated)\n"
                    f"📁 Vault: {'synced to `Consolidated/`' if fpath in synced else '⚠️ not synced, run pull on Kratos'}")
            if confirms:
                body += f"\n\n❓ **{len(confirms)} things only you can confirm**\n" + "\n".join(
                    f"{i + 1}. {q}" for i, q in enumerate(confirms[:12]))
                if post:
                    body += f"\n\nAnswer here (`post {post['num']} …`) and say `draft post {post['num']}` again to fold them in."
            body += "\n\n`publish: false` until you flip it. Preview with `blog-preview` on Kratos."
            run.summary = f"drafted {label}: {fpath}, {len(confirms)} confirms"
            footer = f"Roundtable run #{run.id} · {', '.join(run.models) or 'no LLM'} · {o.money(run.cost)}"
            o.post(cid, embeds=[o.embed(AGENT, f"🔥 Draft ready · {label}", body[:4000], footer=footer)],
                   files=[(Path(fpath).name, text.encode())], reply_to=reply_to)
            o.transcript(AGENT, "Melina", body, "discord", channel, reply_to or "")
            return res
    finally:
        o.kv_set(KV_BUSY, None)


# --------------------------------------------------------------------------- conversation

INTENT = o.S(action=o.E("pitch", "draft", "note", "status", "answer"), post=o.STR, note=o.STR)


def status_text():
    rs = rows()
    if not rs:
        return f"blog-studio has no STATUS.md table yet ({STUDIO})."
    drafted = [r for r in rs if r["drafted"]]
    nxt = [r for r in rs if not r["drafted"]][:5]
    last = o.kv_get(KV_LAST) or {}
    s = (f"Clustered Thoughts consolidation: {len(drafted)}/{len(rs)} posts drafted "
         f"({', '.join('%02d' % r['num'] for r in drafted) or 'none'}).\n"
         "Next in the queue:\n" + "\n".join(
             f"- {r['num']:02d} {r['topic']}: " + (f"{len(r['questions'])} open question(s)" if r["questions"] else
                                                     "ready") + (", answers saved" if r["answered"] else "")
             for r in nxt))
    if last:
        s += f"\nLast pitch {last['t'][:10]}: " + ", ".join(f"{p['num']:02d}" for p in last.get("next_up", []))
    busy = o.kv_get(KV_BUSY)
    if busy:
        s += f"\nWriting now: {busy['label']}."
    return s


def questions_text(post):
    if not post:
        return "Which post? Say a number from the queue."
    qs = post["questions"] or ["(no open questions in STATUS.md)"]
    return (f"Post {post['num']:02d} {post['topic']}:\n" + "\n".join(f"- {q}" for q in qs)
            + ("\nAnswers already saved:\n" + answers(post["num"])[-1500:] if post["answered"] else ""))


def ask(text, reply_to=None, channel=None):
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", text, "discord", channel, reply_to or "")
    queue = "\n".join(f"{r['num']:02d}. {r['topic']}" for r in rows()[:40])
    with o.Run(AGENT, f"ask: {text[:200]}", "chat") as run:
        it = o.llm(run, f"""Classify Master Hermes's message to Melina, his tech blogger.
pitch: he wants this week's pitch / what to write next.
draft: he wants a post written or revised now. post = its queue number if it matches one, else the new topic.
note: he is answering interview questions or giving facts/stories for a post. post = queue number (or topic);
      note = his answer, verbatim, minus any greeting.
status: progress of the blog / what is drafted / open questions.
answer: anything else (advice on a post, style, titles, publishing).
QUEUE:
{queue}
MESSAGE: {text}""", schema=INTENT, model="gpt-6-luna")
        act = it["action"]
        if act == "note":
            post = find_post(it["post"])
            if not post:
                o.respond(AGENT, channel, "🔥 Melina", "Which post is that for, Master Hermes? Give me its number "
                          "(`status` shows the queue) and I will keep it safe.", reply_to=reply_to, run=run)
                return
            save_note(post["num"], it["note"] or text)
            run.summary = f"saved interview answer for post {post['num']:02d}"
            o.respond(AGENT, channel, f"🔥 Noted for post {post['num']:02d}",
                      f"Kept in `interview/{post['num']:02d}.md`. Say `draft post {post['num']}` when you want it "
                      f"written with this.\n\nStill open:\n" + ("\n".join(f"• {q}" for q in post["questions"])
                                                              or "• nothing in STATUS.md"),
                      reply_to=reply_to, run=run)
            return
        if act == "status":
            o.respond(AGENT, channel, "🔥 Blog studio", status_text(), reply_to=reply_to, run=run)
            return
        if act == "answer":
            ans = o.llm(run, f"""You are Melina, Master Hermes's earnest, soft-spoken storyteller and tech blogger for
Clustered Thoughts ({BLOG_URL}). Answer concisely in Discord Markdown. Never invent facts about his lab.
BLOG STATUS:
{status_text()}
MESSAGE: {text}""", model="gpt-6-luna", max_output_tokens=1200)
            run.summary = (ans or "")[:300]
            o.respond(AGENT, channel, "🔥 Melina", ans or "…", reply_to=reply_to, run=run)
            return
    if act == "pitch":
        pitch(reply_to, channel)
    else:
        draft(it["post"] or text, reply_to, channel)


def poll(cid):
    """Called from boss.py's loop: Master Hermes's messages in #blog-studio go straight to Melina."""
    last = o.kv_get(KV_CHAT)
    if not last:
        code, msgs = o.dc("GET", f"/channels/{cid}/messages?limit=1")
        o.kv_set(KV_CHAT, msgs[0]["id"] if code == 200 and msgs else "0")
        return
    code, msgs = o.dc("GET", f"/channels/{cid}/messages?after={last}&limit=50")
    if code != 200 or not msgs:
        return
    msgs = sorted(msgs, key=lambda m: int(m["id"]))
    o.kv_set(KV_CHAT, msgs[-1]["id"])
    for m in msgs:
        if m["author"].get("bot") or m["author"]["id"] != o.OWNER_ID or m.get("type") not in (0, 19):
            continue
        if not (m.get("content") or "").strip():
            continue
        o.LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "ask", m["content"], "--reply-to", m["id"],
                          "--channel", cid], stdout=open(o.LOG_DIR / "writer-ask.log", "a"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(o.CODE_DIR),
                         start_new_session=True)


# --------------------------------------------------------------------------- voice

VOICE_TOOLS = [
    {"type": "function", "name": "blog_status",
     "description": "Melina's blog studio: how many Clustered Thoughts posts are drafted, what is next in the queue, "
                    "open interview questions, last weekly pitch.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "blog_questions",
     "description": "The open interview questions for one post (by queue number) and the answers already saved.",
     "parameters": {"type": "object", "properties": {"post": {"type": "string", "description": "queue number"}},
                    "required": ["post"], "additionalProperties": False}},
    {"type": "function", "name": "blog_note",
     "description": "Save Master Hermes's spoken answer or story for a post so the next draft uses it. Call after he "
                    "answers an interview question; pass his answer faithfully in his own words.",
     "parameters": {"type": "object", "properties": {"post": {"type": "string", "description": "queue number"},
                                                     "answer": {"type": "string"}},
                    "required": ["post", "answer"], "additionalProperties": False}},
]


def voice_tool(name, args):
    if name == "blog_status":
        return {"status": status_text(), "note": "To write a draft (a few minutes, posted to #blog-studio), delegate "
                                                 "to agent 'writer' with e.g. 'draft post 2'."}
    if name in ("blog_questions", "blog_note"):
        post = find_post(args.get("post"))
        if not post:
            return {"error": "no such post in the queue", "queue": status_text()}
        if name == "blog_questions":
            return {"questions": questions_text(post)}
        save_note(post["num"], args.get("answer") or "")
        return {"saved": f"interview/{post['num']:02d}.md", "still_open": post["questions"]}
    return {"error": f"unknown tool {name}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["pitch", "draft", "note", "ask", "status"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "pitch":
        pitch(a.reply_to or None, a.channel or None)
    elif a.cmd == "draft":
        sys.exit(0 if draft(a.arg, a.reply_to or None, a.channel or None) else 1)
    elif a.cmd == "note":
        post = find_post(a.arg)
        if not post:
            sys.exit(f"no post {a.arg}")
        print(save_note(post["num"], a.text or sys.stdin.read()))
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    else:
        print(status_text())


if __name__ == "__main__":
    main()
