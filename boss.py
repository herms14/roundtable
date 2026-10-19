#!/usr/bin/env python3
"""Gideon - Roundtable COO / enterprise architect / orchestrator (systemd: boss.service, always on).

- Polls #roundtable-hold (through the Sentinel bot token) for the owner's messages and routes each one:
  quick commands answered deterministically, everything else routed by gpt-6-luna to an agent
  (`<agent>.py ask "<task>" --reply-to <msg>`), or planned across several agents with gpt-6.1-sol.
- Runs the approval queue: ✅/❌ reactions (owner only) on approval embeds -> executes the stored command,
  immediately or at the approval's not_before (maintenance window).
- Budget guard (warn at 80 % / 100 % of the monthly cap) and a 21:00 daily digest.
"""
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

import olympus_core as o

AGENT = "boss"
CHAT_POLL = 5
APPROVAL_POLL = 15
DIGEST_HOUR = 21
APPROVAL_TTL_DAYS = 7
MAX_CHAT_WORKERS = 3
PY = sys.executable
HOME = Path.home()

# How each agent is started for an on-demand task.
ASK = {
    "boss": [PY, str(o.CODE_DIR / "council.py"), "ask", "--agent", "boss"],
    **{aid: [PY, str(o.CODE_DIR / "council.py"), "ask", "--agent", aid]
       for aid in ("governance", "project_manager", "security_engineer", "iam", "compliance", "cost", "ipam", "devops")},
    "medic": [PY, str(o.CODE_DIR / "medic.py"), "ask"],
    "watcher": [PY, str(o.CODE_DIR / "watcher.py"), "ask"],
    "netops": [PY, str(o.CODE_DIR / "netops.py"), "ask"],
    "builder": [PY, str(o.CODE_DIR / "builder.py"), "ask"],
    "writer": [PY, str(o.CODE_DIR / "writer.py"), "ask"],
    "scout": [PY, str(o.CODE_DIR / "scout.py"), "ask"],
    "mirror": [PY, str(o.CODE_DIR / "mirror.py"), "ask"],
    "scribe": [PY, str(o.CODE_DIR / "scribe.py"), "ask"],
    "checker": [PY, str(o.CODE_DIR / "checker.py"), "ask"],
    "security": [PY, str(o.CODE_DIR / "security.py"), "ask"],
    "backup": [PY, str(o.CODE_DIR / "backup.py"), "ask"],
    "media": [PY, str(o.CODE_DIR / "media.py"), "ask"],
}

HELP = """**Talk to Roundtable** — Gideon is your COO and Enterprise Architect.
Four leads: Gideon (COO/EA), Loretta (Security), Ranni (Governance), Corhyn (ITOps).
Morgott is the project manager under Ranni, not a lead.
Specialists report to their lead; leads brief Gideon, who reports to the owner.
`brief` -> Gideon's consolidated report · `governance` -> Ranni's audit/SLA picture · `finops` -> Boc's API ledger.
🎯 Talk to an agent directly: start with their name, e.g. `@iji did last night's backups fail?` or `@torrent @loretta check VLAN 30`.
Your exact words go straight to them (no routing by Gideon). Without an @name, Gideon picks the agent.
Examples:
• `why is jellyfin slow?` → Sellen · `VLAN 30 clients can't reach pihole` → Torrent
• `how many VMs and LXCs do I have?` / `how many VLANs?` → Gideon answers from the live inventory
• `is anything exposed to the internet?` / `explain security finding 3` → Loretta 🏹
• `back up 205 now` / `did last night's backups fail?` / `how much backup space is left?` → Iji 🛡️ (backups run immediately)
• `what's downloading?` / `any media errors?` / `recommend a movie for tonight` / `download Dune Part Two` → Nepheli 🎬
• `create a debian LXC with 2 cores and 2GB on VLAN 40 for uptime-kuma` → Hewg 🔨 (Terraform + Ansible, needs ✅) · `audit compute and storage` → Hewg
• `draft a blog post about the Roundtable agents` → Melina · `scout for new apps now` / `is X worth self-hosting?` → Kalé 🔔
• `what does my Glance compute page show?` / `explain the Proxmox Grafana dashboard` → Sellen 🔮
• `sync the docs to GitHub` → Radagon 🌗 · `immich keeps restarting, diagnose it` → Corhyn
• `verify gitlab-vm01 is healthy` / `did last night's fixes hold?` → Blaidd 🐺 · `how is Traefik set up?` / `log in the changelog that…` → Miriel 🐢
🎬 Movies & series: talk film with Nepheli in #cinema-lounge (or by voice on the portal)
🪶 Blog: Melina pitches weekly and drafts on request in #blog-studio (`draft post 2`, `what do you need from me?`)
Quick commands (free, no LLM): `status`, `cost`, `approvals`, `access`, `agents`, `help`
🔓 Access gaps: agents log missing access to #security-alerts + GitLab issues + Obsidian `61 - Roundtable Access Backlog`
Approvals: react ✅/❌, or `approve N` / `reject N` · `run now N` skips the maintenance window
Budget: `budget 65` (monthly total) · `voice budget 20` (voice cap)
🎙️ Voice: talk to any agent with your mic at https://roundtable.hrmsmrflrii.xyz/#voice (Discord voice channel coming soon)
Portal (Hermes Roundtable): https://roundtable.hrmsmrflrii.xyz"""

ROUTE_SCHEMA = o.S(
    action=o.E("answer", "delegate", "plan"),
    agent=o.E("none", *ASK.keys()),
    task=o.STR,
    reply=o.STR,
)
PLAN_SCHEMA = o.S(summary=o.STR, steps=o.A(o.S(agent=o.E(*ASK.keys()), task=o.STR)))

state = {"chat_threads": 0, "running": {}}
lock = threading.Lock()


# --------------------------------------------------------------------------- context for the LLM

def built(aid):
    return aid in ASK and os.path.exists(ASK[aid][1])


def team_brief():
    lines = []
    for a in o.registry()["agents"]:
        if built(a["id"]):
            lines.append(f"- {a['id']}: {a['name']} — {a['role']}. Layers: {', '.join(a.get('scope') or []) or 'none'}. "
                         f"{a['does']}")
    missing = [a["name"] for a in o.registry()["agents"] if a["id"] in ASK and not built(a["id"])]
    if missing:
        lines.append(f"(Not built yet, never delegate to them: {', '.join(missing)}. Answer for them yourself.)")
    return "\n".join(lines)


def inventory_context():
    try:
        import inventory
        return inventory.text("all", limit=9000)[:16000]
    except Exception as e:  # noqa: BLE001
        return f"(inventory unavailable: {e})"


def recent_activity(limit=15):
    import hierarchy
    with contextlib.closing(o.db()) as con:
        rows = con.execute("SELECT agent, task, status, started, cost_usd, summary FROM runs "
                           "WHERE agent!='boss' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return "\n".join(f"- {o.local(o.parse_iso(r['started'])).strftime('%m-%d %H:%M')} {r['agent']} [{r['status']}] "
                     f"{hierarchy.report(r['agent'], r['status'], r['summary'], r['task'])['summary'][:160]}"
                     for r in rows) or "(none)"


def pending_approvals():
    with contextlib.closing(o.db()) as con:
        return [dict(r) for r in con.execute(
            "SELECT id, agent, title, severity, status, not_before, created FROM approvals "
            "WHERE status IN ('pending','scheduled','running') ORDER BY id").fetchall()]


# --------------------------------------------------------------------------- quick (deterministic) commands

def status_text():
    with contextlib.closing(o.db()) as con:
        rows = con.execute("""SELECT agent, MAX(started) last, COUNT(*) n, SUM(cost_usd) c,
                              SUM(status IN ('error','failed')) errs FROM runs
                              WHERE started>=? GROUP BY agent""", (o.iso(o.utcnow() - dt.timedelta(days=1)),)).fetchall()
        last = {r["agent"]: r for r in con.execute(
            "SELECT agent, MAX(started) started, status FROM runs GROUP BY agent").fetchall()}
    day = {r["agent"]: r for r in rows}
    states = _states()
    out = []
    for a in o.registry()["agents"]:
        lr = last.get(a["id"])
        when = o.local(o.parse_iso(lr["started"])).strftime("%d %b %H:%M") if lr and lr["started"] else "never"
        d = day.get(a["id"])
        extra = f" · 24h: {d['n']} runs, {o.money(d['c'] or 0)}" + (f", ⚠️ {d['errs']} errors" if d["errs"] else "") if d else ""
        st = states.get(a["id"])
        light = (STATE_ICON[st[0]] + " ") if st else ""
        why = f" · _{st[1]}_" if st and st[0] in ("stopped", "issue") else ""
        out.append(f"{light}{a['emoji']} **{a['name']}** — last {when}{extra}{why}")
    return "\n".join(out) + ("\n\n🟢 running · 🟡 idle · 🔴 stopped · ❗ issue" if states else "")


STATE_ICON = {"running": "🟢", "idle": "🟡", "stopped": "🔴", "issue": "❗"}


def _states():
    """Same status lights as the portal: {agent id: (state, why)}."""
    try:
        import portal
        return {a["id"]: (a["state"], a["state_why"]) for a in portal.agents_payload()}
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"agent states unavailable: {e}")
        return {}


def cost_text():
    with contextlib.closing(o.db()) as con:
        rows = con.execute("SELECT agent, COUNT(*) n, SUM(cost_usd) c, SUM(input_tokens) i, SUM(output_tokens) t "
                           "FROM runs WHERE started>=? GROUP BY agent ORDER BY c DESC", (o.month_start_utc(),)).fetchall()
    spent, cap = o.month_spend(), o.budget_limit()
    lines = [f"**{o.local().strftime('%B')}: {o.money(spent)} of ${cap:.0f}** ({spent / cap * 100:.1f} %)"]
    for r in rows:
        a = o.agent_info(r["agent"])
        lines.append(f"{a['emoji']} {a['name']}: {o.money(r['c'] or 0)} · {r['n']} runs · "
                     f"{(r['i'] or 0) / 1000:.0f}k in / {(r['t'] or 0) / 1000:.0f}k out")
    return "\n".join(lines)


def approvals_text():
    rows = pending_approvals()
    if not rows:
        return "No open approvals."
    return "\n".join(f"#{r['id']} {o.agent_info(r['agent'])['emoji']} {r['title']} — **{r['status']}**"
                     + (f" (window {o.local(o.parse_iso(r['not_before'])).strftime('%d %b %H:%M')})"
                        if r["status"] == "scheduled" and r["not_before"] else "") for r in rows)


def agents_text():
    states = _states()
    return "\n".join(f"{STATE_ICON[states[a['id']][0]] + ' ' if a['id'] in states else ''}{a['emoji']} **{a['name']}** — "
                     f"{a['role']} · {a['schedule']} · `{a['model']}`" for a in o.registry()["agents"])


def access_text():
    try:
        import access
        gaps = access.brief()
    except Exception as e:  # noqa: BLE001
        return f"Access backlog unavailable: {e}"
    if not gaps:
        return "No open access gaps. 🎉"
    rows = [f"**{g['id']}** {o.agent_info(g['agent'])['emoji']} {g['resource']} — needs {g['needed']} (×{g['hits']})"
            + (f" · [GitLab]({g['gitlab']})" if g.get("gitlab") else "") for g in gaps[:25]]
    return "\n".join(rows) + "\nObsidian: `61 - Roundtable Access Backlog` · close with `access.py resolve AG-n`"


QUICK = {"status": ("Team status", status_text), "cost": ("Spend this month", cost_text),
         "budget": ("Spend this month", cost_text), "approvals": ("Open approvals", approvals_text),
         "access": ("Access backlog", access_text),
         "agents": ("The Roundtable team", agents_text), "help": ("How to use Roundtable", lambda: HELP)}
QUICK.update({
    "brief": ("Gideon · consolidated lead brief", lambda: json.dumps(
        __import__("hierarchy").brief("boss"), indent=2)),
    "governance": ("Ranni · audit and service objectives", lambda: json.dumps(
        __import__("hierarchy").compliance(), indent=2)),
    "finops": ("Boc · API FinOps", lambda: json.dumps(__import__("hierarchy").finops(), indent=2)),
})


# --------------------------------------------------------------------------- chat handling

def spawn_agent(agent, task, reply_to, cid, wait=False):
    if o.agent_info(agent).get("homelab_visibility") is False:
        raise PermissionError("Personal consultants do not receive automatic homelab tasks or handover context; "
                              "start a fresh consultation in their own channel or the voice portal")
    import hierarchy
    cmd = ASK[agent] + [task, "--reply-to", reply_to or "", "--channel", cid]
    o.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(o.LOG_DIR / f"{agent}-ask.log", "a")
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         cwd=str(o.CODE_DIR), start_new_session=True)
    o.log(AGENT, f"delegated to {agent} (pid {p.pid}): " +
          ("(private)" if hierarchy.private(agent) else task[:120]))
    show_working(p, cid, reply_to)
    if wait:
        p.wait()
    return p


WORKING, DONE, FAILED = "⏳", "✅", "⚠️"
TYPING_EVERY, WORKING_MAX = 8, 45 * 60


def show_working(p, channel, mid=None):
    """While an agent process runs: Discord's typing indicator in its channel and ⏳ on the owner's message; when it
    exits, ⏳ becomes ✅ (finished) or ⚠️ (failed). Best-effort: Discord errors never affect the agent."""
    def run():
        cid = o.channel_id(channel) if not str(channel).isdigit() else str(channel)
        try:
            if mid:
                o.react(cid, mid, WORKING)
            end = time.time() + WORKING_MAX
            while p.poll() is None and time.time() < end:
                o.dc("POST", f"/channels/{cid}/typing")
                for _ in range(TYPING_EVERY):
                    if p.poll() is not None:
                        break
                    time.sleep(1)
            if mid:
                o.dc("DELETE", f"/channels/{cid}/messages/{mid}/reactions/{urllib.parse.quote(WORKING)}/@me")
                if p.poll() is not None:
                    o.react(cid, mid, DONE if p.returncode == 0 else FAILED)
        except Exception as error:  # noqa: BLE001
            o.log(AGENT, f"working indicator: {type(error).__name__}: {error}")
    threading.Thread(target=run, daemon=True).start()


def last_summary(agent, since):
    import hierarchy
    with contextlib.closing(o.db()) as con:
        r = con.execute("SELECT status, summary FROM runs WHERE agent=? AND started>=? ORDER BY id DESC LIMIT 1",
                        (agent, since)).fetchone()
    return f"[{r['status']}] {hierarchy.report(agent, r['status'], r['summary'])['summary']}" if r else "(no result recorded)"


MENTION = re.compile(r"(?<![\w<@])@([^\W\d_][\w.\-]*)")
NAME_PREFIXES = {"ms", "sir", "lady"}


def _fold(word):
    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c)).lower().strip(".,:;!?")


def mention_targets(text, agents):
    """`@iji back up 205` -> (["backup"], "back up 205"): agents the owner names directly, plus the message without
    the @names. Names, aliases and ids match case- and accent-insensitively; unknown @words stay in the text."""
    names = {}
    for a in agents:
        words = [w for w in a["name"].split() if _fold(w) not in NAME_PREFIXES and _fold(w) != "ai"]
        for key in [a["id"], *(words[:1]), *(a.get("aliases") or [])]:
            names.setdefault(_fold(key), a["id"])
    found, keep = [], []
    pos = 0
    for m in MENTION.finditer(text):
        aid = names.get(_fold(m.group(1)))
        if aid:
            if aid not in found:
                found.append(aid)
            keep.append(text[pos:m.start()])
            pos = m.end()
    keep.append(text[pos:])
    task = re.sub(r"\s{2,}", " ", "".join(keep)).strip(" ,:;-") if found else text
    return found, task


def handle_direct(text, mid, cid):
    """Owner talked to agents by name: hand them the exact words, no Gideon routing or rewrite (no LLM cost here)."""
    targets, task = mention_targets(text, o.registry()["agents"])
    if not targets:
        return False
    with o.Run(AGENT, f"direct: {', '.join(targets)}", "chat") as run:
        run.summary = "direct to " + ", ".join(targets)
        if not task:
            o.post(cid, "📖 What should " + " and ".join(o.agent_info(t)["name"] for t in targets) + " do, Master Hermes?",
                   reply_to=mid)
            return True
        for aid in targets:
            a = o.agent_info(aid)
            if not built(aid) or a.get("homelab_visibility") is False:
                o.post(cid, f"{a.get('emoji', '')} **{a['name']}** isn't reachable from #roundtable-hold — talk to them in "
                            f"#{a.get('channel') or 'their own channel'} or by voice on the portal.", reply_to=mid)
                continue
            o.post(cid, f"→ {a.get('emoji', '')} **{a['name']}** (direct)", reply_to=mid)
            o.transcript(AGENT, "Gideon", f"direct to {a['name']}: {task}", "discord", cid, mid)
            spawn_agent(aid, task, mid, cid)
    return True


def handle(msg, cid):
    text = (msg.get("content") or "").strip()
    mid = msg["id"]
    o.transcript(AGENT, "Master Hermes", text, "discord", cid, mid)
    key = text.lower().strip("!/ ?.")
    if key in QUICK:
        title, fn = QUICK[key]
        with o.Run(AGENT, f"quick: {key}", "chat") as run:
            result = fn()
            o.respond(AGENT, cid, title, result, reply_to=mid, run=run)
            run.summary = f"{key} answered"
        return
    if not text:
        return
    if handle_direct(text, mid, cid):
        return
    try:
        o.dc("POST", f"/channels/{cid}/typing")  # Gideon is reading and routing (a few seconds of LLM time)
    except Exception:  # noqa: BLE001
        pass
    steps = []
    with o.Run(AGENT, f"chat: {text[:200]}", "chat") as run:
        try:
            r = o.llm(run, f"""Owner's message in #roundtable-hold:
\"\"\"{text}\"\"\"

Agents you can delegate to (each reports through its designated lead and only sees its own layers):
{team_brief()}

Live homelab inventory (all layers; you and Loretta see everything):
{inventory_context()}

Recent agent activity:
{recent_activity()}

Open approvals: {json.dumps(pending_approvals())}
Spend this month: {o.money(o.month_spend())} of ${o.budget_limit():.0f}.

Decide:
- action "answer" when you can answer from the context above (status, what happened, who does what, costs, and ANY inventory question: how many VMs/LXCs/containers/VLANs/clients, what runs where, IPs, storage) or it is small talk. Put the answer in `reply` (Discord markdown, <=900 chars), agent "none", task "".
- action "delegate" when ONE agent should do the work (diagnosis, investigation, security review, changes). Pick the agent whose layer it is (security posture / vulnerabilities / attacks → security), write a precise self-contained `task` for it (include every detail from the message), and a one-line `reply` acknowledging it.
- Patch management/status/delivery → builder (Hewg), never a new patch executor. Project plans, milestones, dependencies and delivery risks → project_manager (Morgott). Cross-team priorities and architectural decisions stay with Gideon.
- action "plan" only when the request clearly needs 2+ agents in sequence (e.g. create an LXC then blog about it). agent "none"; `task` = the full request restated; `reply` one line.
Hostnames/IPs/VLANs must be copied exactly from the message.""",
                      schema=ROUTE_SCHEMA,
                      instructions="You are Gideon, the orchestrator of the Roundtable homelab agent team for Master "
                                   "Hermes. You are the COO and Enterprise Architect, using ITIL and "
                                   "TOGAF-informed practices (not real software certifications). Loretta, Ranni "
                                   "and Corhyn lead Security, Governance and ITOps. Morgott under Ranni owns project "
                                   "plans, milestones and delivery tracking; you resolve cross-team priorities. "
                                   "Leads consolidate specialist reports for you; you brief the owner. Prioritize "
                                   "impact, urgency, dependencies, accountable owners and deadlines. Never invent "
                                   "facts or claim omniscience; cite evidence age and missing coverage.")
        except o.BudgetExceeded as e:
            o.post(cid, f"📖 Monthly budget reached ({e}). Non-critical agent work is paused; "
                        f"raise it with `budget <usd>` or wait for next month.", reply_to=mid)
            raise
        run.summary = f"{r['action']} -> {r['agent']}: {r['task'][:200] or r['reply'][:200]}"
        if r["action"] == "answer" or r["agent"] == "none" and r["action"] != "plan":
            o.respond(AGENT, cid, "📖 Gideon", r["reply"], reply_to=mid, run=run)
            return
        if r["action"] == "delegate":
            if not built(r["agent"]):
                o.respond(AGENT, cid, "📖 Gideon", r["reply"] + f"\n_({o.agent_info(r['agent'])['name']} is not "
                          "built yet, so I cannot hand this over. Ask me to answer it directly, Master Hermes.)_",
                          reply_to=mid, run=run)
                return
            a = o.agent_info(r["agent"])
            o.post(cid, f"📖 {r['reply']}\n→ {a['emoji']} **{a['name']}**: _{r['task'][:300]}_", reply_to=mid)
            o.transcript(AGENT, "Gideon", f"{r['reply']}\n→ delegated to {a['name']}: {r['task']}", "discord", cid, mid)
            spawn_agent(r["agent"], r["task"], mid, cid)
            return
        plan = o.llm(run, f"""Break this request into the minimal ordered steps, each handled by exactly one agent.
Request: {r['task'] or text}
Agents:
{team_brief()}
Each step's task must be self-contained (the agent sees only its own task plus the previous step's result).""",
                     model="gpt-6.1-sol", effort="medium", schema=PLAN_SCHEMA)
        steps = [s for s in plan["steps"] if built(s["agent"])][:5]
        o.respond(AGENT, cid, "📖 Plan", plan["summary"] + "\n\n" + "\n".join(
            f"{i + 1}. {o.agent_info(s['agent'])['emoji']} **{o.agent_info(s['agent'])['name']}** — {s['task']}"
            for i, s in enumerate(steps)), reply_to=mid, run=run)
    prev = ""
    for s in steps:
        since = o.iso()
        task = s["task"] + (f"\n\nResult of the previous step: {prev}" if prev else "")
        spawn_agent(s["agent"], task, mid, cid, wait=True)
        prev = last_summary(s["agent"], since)[:1500]


def handle_safe(msg, cid):
    try:
        handle(msg, cid)
    except o.BudgetExceeded:
        pass
    except Exception as e:  # never let one message kill the service
        o.log(AGENT, f"chat error: {e!r}")
        o.post(cid, f"📖 Something went wrong handling that: `{str(e)[:300]}`", reply_to=msg["id"])
    finally:
        with lock:
            state["chat_threads"] -= 1


def admin_command(text, cid, mid):
    """Owner-only control words that never touch the LLM."""
    parts = text.split()
    if parts[0].lower() == "budget" and len(parts) == 2:
        try:
            v = float(parts[1].lstrip("$"))
        except ValueError:
            return False
        o.kv_set("budget_usd", v)
        o.post(cid, f"📖 Monthly budget set to ${v:.0f}.", reply_to=mid)
        return True
    if parts[0].lower() == "voice" and len(parts) == 3 and parts[1].lower() == "budget":
        try:
            v = float(parts[2].lstrip("$"))
        except ValueError:
            return False
        o.kv_set("voice_budget_usd", v)
        o.post(cid, f"📖 Voice cap set to ${v:.0f} a month (inside the ${o.budget_limit():.0f} total).", reply_to=mid)
        return True
    if parts[0].lower() == "run" and len(parts) == 3 and parts[1].lower() == "now" and parts[2].lstrip("#").isdigit():
        aid = int(parts[2].lstrip("#"))
        ap = o.get_approval(aid)
        if not ap or ap["status"] != "scheduled":
            o.post(cid, f"📖 Approval #{aid} is not waiting for a maintenance window.", reply_to=mid)
        else:
            o.set_approval(aid, not_before=o.iso(), status="approved", decided=o.iso())
            o.post(cid, f"📖 Running approval #{aid} now (window skipped).", reply_to=mid)
            execute(o.get_approval(aid))
        return True
    if parts[0].lower() in ("approve", "reject") and len(parts) == 2 and parts[1].lstrip("#").isdigit():
        aid = int(parts[1].lstrip("#"))
        ap = o.get_approval(aid)
        if not ap or ap["status"] != "pending":
            o.post(cid, f"📖 Approval #{aid} is not pending.", reply_to=mid)
        else:
            decide(ap, parts[0].lower() == "approve")
        return True
    return False


def poll_chat(cid):
    last = o.kv_get("boss_last_msg")
    if not last:
        code, msgs = o.dc("GET", f"/channels/{cid}/messages?limit=1")
        last = msgs[0]["id"] if code == 200 and msgs else "0"
        o.kv_set("boss_last_msg", last)
        return
    code, msgs = o.dc("GET", f"/channels/{cid}/messages?after={last}&limit=50")
    if code != 200 or not msgs:
        return
    for m in sorted(msgs, key=lambda m: int(m["id"])):
        o.kv_set("boss_last_msg", m["id"])
        if m["author"].get("bot") or m["author"]["id"] != o.OWNER_ID or m.get("type") not in (0, 19):
            continue
        text = (m.get("content") or "").strip()
        if text and admin_command(text, cid, m["id"]):
            continue
        with lock:
            if state["chat_threads"] >= MAX_CHAT_WORKERS:
                o.post(cid, "📖 Busy with other requests — try again in a minute.", reply_to=m["id"])
                continue
            state["chat_threads"] += 1
        threading.Thread(target=handle_safe, args=(m, cid), daemon=True).start()


# --------------------------------------------------------------------------- approvals

def rfc_status(ap, status, result=None):
    """Ranni keeps the approval's RFC in step with the approval (best-effort; never blocks the queue)."""
    rid = (ap.get("payload") or {}).get("rfc")
    if not rid:
        return
    try:
        import rfc
        rfc.set_status(rid, status, result, ap["channel_id"], ap["message_id"])
    except Exception as error:  # noqa: BLE001
        o.log(AGENT, f"{rid} status {status} not recorded: {type(error).__name__}: {error}")


def decide(ap, approved):
    aid = ap["id"]
    if not approved:
        o.set_approval(aid, status="rejected", decided=o.iso())
        o.post(ap["channel_id"], f"❌ Approval #{aid} rejected — nothing will be changed.", reply_to=ap["message_id"])
        rfc_status(ap, "Rejected", "Rejected by Master Hermes on the approval.")
        o.log(AGENT, f"approval #{aid} rejected")
        return
    nb = ap.get("not_before")
    if nb and o.parse_iso(nb) > o.utcnow():
        rfc_status(ap, "Approved — scheduled")
        o.set_approval(aid, status="scheduled", decided=o.iso())
        o.post(ap["channel_id"], f"✅ Approval #{aid} accepted — scheduled for "
                                 f"{o.local(o.parse_iso(nb)).strftime('%a %d %b %H:%M')} (maintenance window).",
               reply_to=ap["message_id"])
        return
    o.set_approval(aid, status="approved", decided=o.iso())
    execute(ap)


def execute(ap):
    import hierarchy
    aid = ap["id"]
    key = f"approval:{aid}"
    hierarchy.track(key, ap["agent"], ap["title"], ap["severity"], ap["channel_id"], ap["message_id"])
    try:
        hierarchy.sync_item(key)
    except RuntimeError as error:
        o.set_approval(aid, status="failed", result=f"Governance blocked execution: {error}")
        o.post(ap["channel_id"], f"⚠️ Approval #{aid} blocked: audit sync incomplete.", reply_to=ap["message_id"])
        return
    o.set_approval(aid, status="running")
    rfc_status(ap, "Approved — implementing")
    o.post(ap["channel_id"], f"⚙️ Approval #{aid} — {o.agent_info(ap['agent'])['name']} is on it.",
           reply_to=ap["message_id"])
    env = dict(os.environ, OLYMPUS_APPROVAL_ID=str(aid))
    logp = o.LOG_DIR / f"approval-{aid}.log"
    p = subprocess.Popen(ap["command"], stdout=open(logp, "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=env, cwd=str(o.CODE_DIR), start_new_session=True)
    state["running"][aid] = (p, logp)
    show_working(p, ap["channel_id"], ap["message_id"])
    o.log(AGENT, f"approval #{aid} executing (pid {p.pid}): {ap['command']}")


def reap():
    import hierarchy
    for aid, (p, logp) in list(state["running"].items()):
        rc = p.poll()
        if rc is None:
            continue
        del state["running"][aid]
        tail = logp.read_text(errors="replace")[-1500:] if logp.exists() else ""
        ap = o.get_approval(aid)
        if ap and ap["status"] == "running":  # the executor may have set its own final status
            o.set_approval(aid, status="done" if rc == 0 else "failed", finished=o.iso(), result=tail)
        current = o.get_approval(aid)
        if current:
            hierarchy.resolve(f"approval:{aid}", current["status"], current.get("result") or f"Executor exit {rc}")
            import rfc
            rfc_status(current, rfc.status_for_result(current["status"], current.get("result") or tail), tail)
        if rc != 0:
            o.post(ap["channel_id"], f"⚠️ Approval #{aid} executor exited with code {rc}.\n```\n{tail[-900:]}\n```",
                   reply_to=ap["message_id"])
        o.log(AGENT, f"approval #{aid} finished rc={rc}")


def portal_decisions():
    """Owner decisions made on the Roundtable portal's Approvals page (portal.py queues kv portal_decision:<id>).
    Each is taken off the queue once, re-checked against the approval's current state, noted in its Discord thread
    with who decided, then applied through the same path as a Discord ✅/❌ or `run now`."""
    with contextlib.closing(o.db()) as con, con:
        rows = [dict(r) for r in con.execute("SELECT k, v FROM kv WHERE k LIKE 'portal_decision:%'").fetchall()]
        for r in rows:
            con.execute("DELETE FROM kv WHERE k=?", (r["k"],))
    for r in rows:
        try:
            aid, d = int(r["k"].split(":", 1)[1]), json.loads(r["v"] or "{}")
        except (ValueError, TypeError):
            continue
        decision, by = d.get("decision"), str(d.get("by") or "portal")[:40]
        ap = o.get_approval(aid)
        want = "scheduled" if decision == "run_now" else "pending"
        if decision not in ("approve", "reject", "run_now") or not ap or ap["status"] != want:
            o.log(AGENT, f"portal decision {decision} for approval #{aid} ignored (now {ap and ap['status']})")
            continue
        verb = {"approve": "approved", "reject": "rejected", "run_now": "set to run now"}[decision]
        o.post(ap["channel_id"], f"🖥️ Approval #{aid} {verb} on the Roundtable portal by {by}.", reply_to=ap["message_id"])
        o.log(AGENT, f"approval #{aid} {verb} on the portal by {by}")
        if decision == "run_now":
            o.set_approval(aid, not_before=o.iso(), status="approved", decided=o.iso())
            execute(o.get_approval(aid))
        else:
            decide(ap, decision == "approve")


def poll_approvals():
    reap()
    portal_decisions()
    with contextlib.closing(o.db()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM approvals WHERE status IN ('pending','scheduled') ORDER BY id").fetchall()]
    for r in rows:
        ap = o.get_approval(r["id"])
        if r["status"] == "scheduled":
            if not r["not_before"] or o.parse_iso(r["not_before"]) <= o.utcnow():
                execute(ap)
            continue
        if not r["message_id"]:
            continue
        if o.parse_iso(r["created"]) < o.utcnow() - dt.timedelta(days=APPROVAL_TTL_DAYS):
            o.set_approval(r["id"], status="expired")
            o.post(r["channel_id"], f"⌛ Approval #{r['id']} expired after {APPROVAL_TTL_DAYS} days.",
                   reply_to=r["message_id"])
            rfc_status(ap, "Expired", f"Not decided within {APPROVAL_TTL_DAYS} days.")
            continue
        if o.OWNER_ID in o.reactors(r["channel_id"], r["message_id"], "✅"):
            decide(ap, True)
        elif o.OWNER_ID in o.reactors(r["channel_id"], r["message_id"], "❌"):
            decide(ap, False)
        time.sleep(0.3)


def recover():
    with contextlib.closing(o.db()) as con, con:
        stale = con.execute("SELECT id, channel_id, message_id FROM approvals WHERE status='running'").fetchall()
        con.execute("UPDATE approvals SET status='interrupted' WHERE status='running'")
        con.execute("UPDATE runs SET status='interrupted', ended=? WHERE status='running' AND started<?",
                    (o.iso(), o.iso(o.utcnow() - dt.timedelta(hours=6))))
    for r in stale:
        o.post(r["channel_id"], f"⚠️ Approval #{r['id']} was interrupted (Gideon restarted while it ran). "
                                f"Check the result before re-running.", reply_to=r["message_id"])
        rfc_status(o.get_approval(r["id"]), "Interrupted", "Gideon restarted while the change was running.")


# --------------------------------------------------------------------------- budget + digest

def budget_watch(cid):
    spent, cap = o.month_spend(), o.budget_limit()
    month = o.local().strftime("%Y-%m")
    flags = o.kv_get("budget_flags", {})
    if flags.get("month") != month:
        flags = {"month": month}
    for pct in (80, 100):
        if spent >= cap * pct / 100 and not flags.get(str(pct)):
            flags[str(pct)] = True
            o.post(cid, f"💰 **Budget {pct} %** — {o.money(spent)} of ${cap:.0f} spent in {o.local().strftime('%B')}."
                        + (" Non-critical LLM work is now paused (incident fixes and approved actions still run)."
                           if pct == 100 else ""), mention_owner=True)
    o.kv_set("budget_flags", flags)


def digest(cid):
    today = o.local().strftime("%Y-%m-%d")
    if o.local().hour < DIGEST_HOUR or o.kv_get("boss_digest") == today:
        return
    o.kv_set("boss_digest", today)
    since = o.iso(o.utcnow() - dt.timedelta(days=1))
    with contextlib.closing(o.db()) as con:
        rows = con.execute("""SELECT agent, COUNT(*) n, SUM(cost_usd) c, SUM(status IN ('error','failed')) e,
                              GROUP_CONCAT(CASE WHEN status IN ('error','failed') THEN task END, ' | ') et
                              FROM runs WHERE started>=? GROUP BY agent ORDER BY n DESC""", (since,)).fetchall()
    total = sum(r["c"] or 0 for r in rows)
    lines = []
    for r in rows:
        a = o.agent_info(r["agent"])
        lines.append(f"{a['emoji']} **{a['name']}** — {r['n']} runs · {o.money(r['c'] or 0)}"
                     + (f" · ⚠️ {r['e']} failed: {(r['et'] or '')[:150]}" if r["e"] else ""))
    fields = [("Today", o.money(total), True),
              ("Month to date", f"{o.money(o.month_spend())} / ${o.budget_limit():.0f}", True),
              ("Open approvals", str(len(pending_approvals())), True)]
    o.post(cid, embeds=[o.embed(AGENT, f"Daily digest · {o.local().strftime('%a %d %b')}",
                                "\n".join(lines) or "Quiet day on Roundtable.", fields)])


# --------------------------------------------------------------------------- main loop

def main():
    cid = o.channel_id("roundtable-hold", create_topic="Talk to the Roundtable agent team — Gideon routes your requests. "
                                               "Type `help`. Approvals: ✅/❌ (owner only).")
    if cid == o.CHANNELS["homelab-infrastructure"]:
        o.log(AGENT, "#roundtable-hold not available (missing Manage Channels?) - chat disabled, approvals still run")
        cid = None
    elif not o.kv_get("boss_welcomed"):
        o.post(cid, embeds=[o.embed(AGENT, "Roundtable is online", HELP)])
        o.kv_set("boss_welcomed", True)
    o.log(AGENT, f"started; chat channel {cid}")
    recover()
    import media
    import writer
    side = {m: {"cid": None, "tried": False} for m in (media, writer)}
    last_appr = 0
    last_audit = 0
    while True:
        try:
            for mod, sv in side.items():
                if sv["cid"] is None and (int(time.time()) % 300 < CHAT_POLL or not sv["tried"]):
                    sv["tried"] = True
                    c = o.channel_id(mod.CHANNEL, create_topic=mod.TOPIC)
                    if c != o.CHANNELS["homelab-infrastructure"]:
                        sv["cid"] = c
                        o.log(AGENT, f"{mod.AGENT} listening in #{mod.CHANNEL} ({c})")
                if sv["cid"]:
                    try:
                        mod.poll(sv["cid"])
                    except Exception as e:
                        o.log(AGENT, f"{mod.AGENT} poll error: {e!r}")
            if cid is None and int(time.time()) % 300 < CHAT_POLL:
                c = o.channel_id("roundtable-hold", create_topic="Talk to the Roundtable agent team — Gideon routes your requests.")
                if c != o.CHANNELS["homelab-infrastructure"]:
                    cid = c
                    o.post(cid, embeds=[o.embed(AGENT, "Roundtable is online", HELP)])
                    o.kv_set("boss_welcomed", True)
            if cid:
                poll_chat(cid)
            if time.time() - last_appr >= APPROVAL_POLL:
                last_appr = time.time()
                poll_approvals()
                budget_watch(cid or o.CHANNELS["homelab-infrastructure"])
                digest(cid or o.CHANNELS["homelab-infrastructure"])
            if time.time() - last_audit >= 300:
                last_audit = time.time()
                try:
                    __import__("hierarchy").sync()
                except RuntimeError as error:
                    o.log("compliance", str(error))
        except Exception as e:
            o.log(AGENT, f"loop error: {e!r}")
            time.sleep(10)
        time.sleep(CHAT_POLL)


if __name__ == "__main__":
    main()
