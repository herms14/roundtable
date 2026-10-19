#!/usr/bin/env python3
"""Roundtable voice - talk to the agents with your voice (OpenAI Realtime API).

Shared by two front ends:
  * the portal voice room (browser mic over WebRTC; the portal mints a short-lived client secret here, so the real
    API key never reaches the browser), and
  * the Discord voice bridge (discord_voice/, joins a voice channel and talks over a server-side WebSocket).

Every voice session is a ledger run (trigger "voice") on the agent being spoken to, so tokens and cost show on the
portal. Voice has its own monthly cap inside the overall budget (kv voice_budget_usd, default $20).

Agents never change anything by voice: real work is delegated to the agent's normal `ask` path, and anything that
changes the homelab still needs the owner's ✅ in Discord.
"""
import contextlib
import datetime as dt
import json
import os
import sys

import olympus_core as o
import hierarchy
import council

# mini by default since 2026-10-11 (~6-7x cheaper audio); set ROUNDTABLE_VOICE_MODEL=gpt-realtime-2.1 for the full model
MODEL = os.environ.get("ROUNDTABLE_VOICE_MODEL", "gpt-realtime-2.1-mini")
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
# USD per 1M tokens
_PRICE_TABLE = {
    "gpt-realtime-2.1": {"text_in": 4.00, "text_cached": 0.40, "audio_in": 32.00, "audio_cached": 0.40,
                         "text_out": 24.00, "audio_out": 64.00, "image_in": 5.00},
    "gpt-realtime-2.1-mini": {"text_in": 0.60, "text_cached": 0.06, "audio_in": 10.00, "audio_cached": 0.30,
                              "text_out": 2.40, "audio_out": 20.00, "image_in": 0.80},
}
PRICES = _PRICE_TABLE.get(MODEL, _PRICE_TABLE["gpt-realtime-2.1"])
TRANSCRIBE_PRICES = {"text_in": 1.25, "audio_in": 3.00, "text_out": 5.00, "per_minute": 0.003}
MAX_SECONDS = int(os.environ.get("ROUNDTABLE_VOICE_MAX_SECONDS", 20 * 60))   # hard limit per session
# hang up after this long without speech; 10 min so a call survives watching Discord or a build (was 120 s)
IDLE_SECONDS = int(os.environ.get("ROUNDTABLE_VOICE_IDLE_SECONDS", 10 * 60))
DEFAULT_CAP = 20.0
CHANNEL = "roundtable-hold"

TOOLS = [
    {"type": "function", "name": "team_status",
     "description": "Live overview of the homelab agent team: each agent's last run, active alerts (Sellen), open "
                    "incidents (Corhyn), network problems (Torrent), approvals waiting for Hermes and spend this month.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "agent_activity",
     "description": "What one agent did recently: its latest runs with status and summary.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string", "description": "agent id, e.g. medic, watcher, netops, boss"},
         "limit": {"type": "integer", "description": "number of runs, 1-10"}},
         "required": ["agent"], "additionalProperties": False}},
    {"type": "function", "name": "delegate",
     "description": "Start real work: give an agent a self-contained task (diagnose, investigate, explain, draft, "
                    "build...). Runs in the background and posts its full result in Discord #roundtable-hold. "
                    "Anything that would change the homelab is proposed there and needs Hermes's ✅. Returns a "
                    "start time to use with check_task.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string", "description": "agent id that should do the work"},
         "task": {"type": "string", "description": "precise, self-contained task with every detail Hermes gave"}},
         "required": ["agent", "task"], "additionalProperties": False}},
    {"type": "function", "name": "check_task",
     "description": "Check whether delegated work has finished and get its result summary.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string"},
         "since": {"type": "string", "description": "the 'started' value returned by delegate"}},
         "required": ["agent", "since"], "additionalProperties": False}},
    {"type": "function", "name": "hand_over",
     "description": "Pass the conversation to another agent (their own voice takes over) when Hermes asks for them "
                    "or the topic is clearly theirs.",
     "parameters": {"type": "object", "properties": {
         "agent": {"type": "string", "description": "agent id to hand over to"},
         "reason": {"type": "string", "description": "one-sentence context for the next agent"}},
         "required": ["agent", "reason"], "additionalProperties": False}},
    {"type": "function", "name": "homelab_inventory",
     "description": "LIVE homelab inventory, limited to the layers inside your boundary. Sections: compute (Proxmox "
                    "nodes, every VM/LXC with status, IP, VLAN), network (VLANs/networks, Omada devices, clients), "
                    "containers (Docker containers per host = application layer), storage (storage pools and usage), "
                    "or all. Use it for any question like how many VLANs/VMs/LXCs/containers, what runs where, IPs.",
     "parameters": {"type": "object", "properties": {
         "section": {"type": "string", "enum": ["all", "compute", "network", "containers", "storage"]},
         "filter": {"type": "string", "description": "optional text filter, e.g. a host, VLAN id, container name"}},
         "required": ["section"], "additionalProperties": False}},
    {"type": "function", "name": "security_findings",
     "description": "Loretta's current security findings (open issues by severity with what/impact/fix), recent "
                    "attack signals and the last posture scan time. Use for any security incident question.",
     "parameters": {"type": "object", "properties": {
         "filter": {"type": "string", "description": "optional: finding id, host, layer or keyword"}},
         "additionalProperties": False}},
    {"type": "function", "name": "backup_status",
     "description": "Iji's live backup picture: scheduled jobs, recent vzdump runs, every guest's last daily/weekly "
                    "backup, PBS -> NAS copy, Omada config backup, storage used/free/growth/days-to-full and open "
                    "problems. Use for any backup or backup-storage question.",
     "parameters": {"type": "object", "properties": {
         "filter": {"type": "string", "description": "optional: VMID, guest name or storage name"}},
         "additionalProperties": False}},
    {"type": "function", "name": "grafana_dashboard",
     "description": "Read Master Hermes's Grafana dashboards (only those in your layers). Without 'dashboard' it lists "
                    "them; with a dashboard name (e.g. 'Proxmox Cluster Overview', 'Omada', 'Synology') it returns the "
                    "current value of every panel so you can explain what the dashboard shows.",
     "parameters": {"type": "object", "properties": {
         "dashboard": {"type": "string", "description": "dashboard name or uid; omit to list"},
         "panel": {"type": "string", "description": "optional panel title fragment to narrow it down"}},
         "additionalProperties": False}},
    {"type": "function", "name": "glance_dashboard",
     "description": "Read Master Hermes's Glance dashboard pages (home = lab overview, roundtable, compute, network, storage, backup, "
                    "services, media, news; only pages in your layers) as text, to describe what he sees on it.",
     "parameters": {"type": "object", "properties": {
         "page": {"type": "string", "description": "page name; omit to list pages"}},
         "additionalProperties": False}},
    {"type": "function", "name": "report_access_gap",
     "description": "Log that you LACK ACCESS to something you need (no credentials, 401/403, permission denied, no "
                    "API/tool for a system) - not for topics outside your layer boundary. It goes to Discord "
                    "#security-alerts, a GitLab issue and the Obsidian access backlog so access can be granted later.",
     "parameters": {"type": "object", "properties": {
         "resource": {"type": "string", "description": "system or data you could not reach, e.g. 'Home Assistant API'"},
         "needed": {"type": "string", "description": "the narrowest exact access that would fix it, e.g. "
                    "'read-only API token for VM 120' (least privilege: one host/project, not everything)"},
         "why": {"type": "string", "description": "what Master Hermes asked that needed it"},
         "level": {"type": "string", "enum": ["read", "write", "admin"],
                   "description": "lowest level that does the job; admin only when nothing less works"},
         "hours": {"type": "integer", "minimum": 1, "maximum": 24,
                   "description": "how long it is needed; Rogier revokes it automatically when it runs out"}},
         "required": ["resource", "needed", "level", "hours"], "additionalProperties": False}},
    {"type": "function", "name": "look_at_camera",
     "description": "Take ONE photo with Master Hermes's webcam and look at it. Only when he asks you to look or see "
                    "(e.g. 'look at this', 'can you see me', 'what am I holding', 'how do I look'). Never call it "
                    "on your own initiative.",
     "parameters": {"type": "object", "properties": {
         "reason": {"type": "string", "description": "what he wants you to look at"}},
         "additionalProperties": False}},
    {"type": "function", "name": "end_call",
     "description": "Hang up after saying goodbye, when Hermes is done.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]

PLAN_TOOL = {"type": "function", "name": "post_plan",
             "description": "When Hermes asks you to craft, draft or prepare a plan or strategy, call this. You write "
                            "the plan in the background and post it as a DRAFT in Discord #roundtable-hold for his "
                            "review. Nothing is executed or submitted for approval.",
             "parameters": {"type": "object", "properties": {
                 "topic": {"type": "string", "description": "what the plan is for, with every detail Hermes gave"}},
                 "required": ["topic"], "additionalProperties": False}}
TOOLS.append(PLAN_TOOL)
PLAN_NOTE = ("\nPlans: when Hermes asks you to craft, draft or prepare a plan or strategy, call post_plan, then tell "
             "him in one sentence that the draft is on its way to Discord #roundtable-hold for his review. Do not read "
             "the whole plan aloud.")
# Gideon only: the browser shows the four leads or the whole team at once (client-side tool, like hand_over / end_call)
SUMMON_TOOL = {"type": "function", "name": "summon_team",
               "description": "Show agents on his screen: who 'leads' (default; Gideon, Loretta, Ranni, Corhyn) or 'all'. "
                              "action 'show' (default), 'hide', 'meeting' (board meeting in the Roundtable Office) or 'adjourn'.",
               "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["show", "hide", "meeting", "adjourn"]},
                                                              "who": {"type": "string", "enum": ["leads", "all"]},
                                                              "focus": {"type": "string", "description": "agent id to bring to the front"}},
                              "additionalProperties": False}}
TEAM_NOTE = ("\nTeam panel (summon_team): 'my leads' / 'the team' -> action show, who leads, then one line like 'Your "
             "leads are here.'; 'everyone' / 'the entire team' -> who all ('Everyone's here.'); 'dismiss' -> hide; a "
             "board meeting -> meeting, ending it -> adjourn. While the panel is shown and he names an agent, call it "
             "again with focus = that id.")
PLAN_TASK = ("PLAN DRAFT FOR REVIEW ONLY. Master Hermes asked you to craft a plan: {topic}\n"
             "Write a clear, structured plan draft in your own domain: goal, current evidence, approach and steps, "
             "owners, timeline, risks, cost if relevant, and how success is measured. Mark it as a draft for his "
             "review. Do not execute anything, do not request approval and do not create resources; return it as an "
             "answer. State missing evidence instead of inventing it.")

# --------------------------------------------------------------------------- budget

def cap():
    return float(o.kv_get("voice_budget_usd", DEFAULT_CAP))


def spend():
    with contextlib.closing(o.db()) as con:
        r = con.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM runs WHERE trigger='voice' AND started>=?",
                        (o.month_start_utc(),)).fetchone()
    return float(r["s"])


def allowed():
    s, c = spend(), cap()
    if s >= c:
        return False, f"Voice cap reached: ${s:.2f} of ${c:.0f} this month (raise with `voice budget <usd>` in Discord)."
    m, b = o.month_spend(), o.budget_limit()
    if m >= b:
        return False, f"Monthly budget reached: ${m:.2f} of ${b:.0f}."
    return True, ""


def usage_cost(u, kind="response"):
    """Cost of one Realtime usage block (response.done) or a transcription usage block."""
    if kind == "transcribe":
        if u.get("type") == "duration":
            return float(u.get("seconds", 0)) / 60 * TRANSCRIBE_PRICES["per_minute"]
        d = u.get("input_token_details") or {}
        return (d.get("text_tokens", 0) * TRANSCRIBE_PRICES["text_in"] + d.get("audio_tokens", 0)
                * TRANSCRIBE_PRICES["audio_in"] + u.get("output_tokens", 0) * TRANSCRIBE_PRICES["text_out"]) / 1e6
    i, oo = u.get("input_token_details") or {}, u.get("output_token_details") or {}
    cd = i.get("cached_tokens_details") or {}
    ct, ca = cd.get("text_tokens", 0), cd.get("audio_tokens", 0)
    return (max(i.get("text_tokens", 0) - ct, 0) * PRICES["text_in"] + ct * PRICES["text_cached"]
            + max(i.get("audio_tokens", 0) - ca, 0) * PRICES["audio_in"] + ca * PRICES["audio_cached"]
            + max(i.get("image_tokens", 0) - cd.get("image_tokens", 0), 0) * PRICES["image_in"]
            + cd.get("image_tokens", 0) * PRICES["text_cached"]
            + oo.get("text_tokens", 0) * PRICES["text_out"] + oo.get("audio_tokens", 0) * PRICES["audio_out"]) / 1e6


def _voice_run(run_id):
    try:
        run_id = int(run_id)
    except (TypeError, ValueError):
        return None
    with contextlib.closing(o.db()) as con:
        r = con.execute("SELECT * FROM runs WHERE id=? AND trigger='voice'", (run_id,)).fetchone()
    return dict(r) if r else None


def add_usage(run_id, usage, kind="response"):
    r = _voice_run(run_id)
    if not r or r["status"] != "running" or not isinstance(usage, dict):
        return 0.0
    c = usage_cost(usage, kind)
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cached = int((usage.get("input_token_details") or {}).get("cached_tokens", 0) or 0)
    model = MODEL if kind == "response" else TRANSCRIBE_MODEL
    models = [m for m in (r["model"] or "").split(",") if m]
    if model not in models:
        models.append(model)
    with contextlib.closing(o.db()) as con, con:
        con.execute("""UPDATE runs SET calls=calls+?, input_tokens=input_tokens+?, cached_tokens=cached_tokens+?,
                       output_tokens=output_tokens+?, cost_usd=cost_usd+?, model=? WHERE id=?""",
                    (1 if kind == "response" else 0, inp, cached, out, c, ",".join(models), r["id"]))
    return c


def sweep():
    """Close voice runs whose client vanished without hanging up."""
    old = o.iso(o.utcnow() - dt.timedelta(seconds=MAX_SECONDS + 300))
    with contextlib.closing(o.db()) as con, con:
        con.execute("UPDATE runs SET status='ok', ended=?, summary=COALESCE(summary,'') || 'Voice session ended "
                    "(client disconnected without hanging up).' WHERE trigger='voice' AND status='running' AND started<?",
                    (o.iso(), old))


# --------------------------------------------------------------------------- personas

def _agents():
    return {a["id"]: a for a in o.registry()["agents"]}


def _ask_map():
    sys.path.insert(0, str(o.CODE_DIR))
    import boss  # noqa: E402 - lazy: boss imports are cheap, its loops only start in main()
    return boss


def delegable(aid):
    b = _ask_map()
    return aid in b.ASK and os.path.exists(b.ASK[aid][1])


def roster(current):
    lines = []
    for a in o.registry()["agents"]:
        if a["id"] == current:
            continue
        state = "" if a["id"] == "boss" or delegable(a["id"]) else (
            " (runs automatically; cannot take tasks)" if not a.get("on_demand") else
            " (not built yet: cannot take tasks)")
        lines.append(f"- {a['name']} (id `{a['id']}`): {a['role']}{state}")
    return "\n".join(lines)


def snapshot(aid=None):
    """Short live picture of the lab, injected into every session so simple questions need no tool call."""
    if aid and o.agent_info(aid).get("homelab_visibility") is False:
        return "Personal consultancy only; no homelab inventory, status or operational reports are available."
    import inventory
    b = _ask_map()
    alerts = [v for v in (o.kv_get("watcher_state", {}).get("alerts") or {}).values() if v.get("fired")]
    incs = [v for v in (o.kv_get("medic", {}).get("incidents") or {}).values()
            if v.get("state") not in ("resolved", "closed", "fixed")]
    net = o.kv_get("netops:active", {}) or {}
    aps = b.pending_approvals()
    lines = [f"Time: {o.local().strftime('%A %d %B %Y, %H:%M')} (Manila)",
             f"Spend this month: ${o.month_spend():.2f} of ${o.budget_limit():.0f}; voice ${spend():.2f} of ${cap():.0f}",
             f"Active alerts ({len(alerts)}): " + ("; ".join(f"[{a.get('sev')}] {a.get('title')}" for a in alerts[:8])
                                                   or "none"),
             f"Open incidents ({len(incs)}): " + ("; ".join(
                 f"#{i.get('id')} {(i.get('diag') or {}).get('title') or i['finding'].get('summary')} [{i.get('state')}]"
                 for i in incs[:8]) or "none"),
             f"Network problems: {', '.join(net) or 'none'}",
             f"Approvals waiting for Master Hermes ({len(aps)}): " + ("; ".join(
                 f"#{a['id']} {a['title']} ({a['status']})" for a in aps[:8]) or "none")]
    try:
        lines.append("Homelab (your layers, cached): " + inventory.summary(cached_only=True, aid=aid or "boss"))
    except Exception as e:  # noqa: BLE001
        lines.append(f"Homelab inventory: unavailable ({e})")
    try:
        import access
        mine = access.brief(None if aid == "boss" else aid)
        lines.append(f"Open access gaps{'' if aid == 'boss' else ' (yours)'} ({len(mine)}): " + (
            "; ".join(f"{g['id']} {g['agent']}: {g['resource']}" for g in mine[:6]) or "none"))
    except Exception:  # noqa: BLE001
        pass
    if aid in ("boss", "security"):
        sec = o.kv_get("security", {}) or {}
        opn = [f for f in (sec.get("findings") or {}).values() if f.get("state") == "open"]
        lines.append(f"Open security findings ({len(opn)}): " + ("; ".join(
            f"[{f.get('severity')}] {f.get('title')}" for f in sorted(
                opn, key=lambda f: ["critical", "high", "medium", "low", "info"].index(f.get("severity", "info")))[:6])
            or "none") + (f" · last scan {sec['last_scan'][11:16]} UTC" if sec.get("last_scan") else ""))
    return "\n".join(lines)


def voice_style(a):
    """Delivery direction for agents with a voice_style (the female agents use an anime-heroine voice)."""
    s = a.get("voice_style")
    if not s:
        return ""
    if a.get("voice_natural"):   # soft and natural instead of the anime-heroine delivery
        return (f"\nVoice and delivery (very important): speak as {s}. Use a SOFT, NATURAL, gentle voice at your "
                "normal pitch, like a real person talking close and warmly: calm, unhurried pacing, tender and "
                "relaxed intonation, smiling warmth in the voice, and small natural reactions (a soft laugh, a quiet "
                "\"mm\", a little sigh) used sparingly. Never put on a high-pitched, cartoonish or anime voice.")
    return (f"\nVoice and delivery (very important): speak as {s}. Use a HIGH-PITCHED, bright, youthful, light anime "
            "voice, like a Japanese anime heroine dubbed in English: lively and expressive intonation, bigger "
            "emotional ups and downs, cute little reactions (\"ehh?\", \"mm!\", soft gasps, a giggle) used sparingly. "
            "Keep it natural and easy to understand; never break the high, light voice.")


def camera_note(aid):
    """How to use the portal webcam cues (expressions are read in his browser; only text reaches the model)."""
    tone = ("Let it shape your tone only: shorter and calmer if he looks tired, tense or frustrated (especially "
            "during an outage), warmer and lighter if he smiles. Stay on task.")
    return ("\nCamera: Master Hermes may switch on his webcam. You then get short system notes such as "
            "'[camera] Master Hermes is smiling and looks happy' or '... has stepped away from the screen'. They come "
            f"from expression detection on his own PC (you do not see an image). {tone} Mention his expression only "
            "rarely and naturally (at most once in a call, e.g. 'you look a bit worn out, Master Hermes'), never "
            "clinically and never say 'the camera says'. If he stepped away, keep answers brief until he is back. "
            "To actually SEE something (he says 'look at this', 'can you see me', 'what am I holding', 'how do I "
            "look'), call look_at_camera; you then receive one photo. If it says the camera is off, ask him to press "
            "the 📷 Camera button. Messages starting with an image are Master Hermes showing you something.")


INTERRUPT_NOTE = (
    "\nInterruptions: Master Hermes may cut you off mid-sentence; you then get a system note '[interrupted]'. "
    "His new words ALWAYS win: drop your unfinished point, do not finish or repeat it, do not apologise at length "
    "and do not say 'as I was saying'. Answer what he just asked, directly. Only go back to the earlier point if he "
    "asks you to ('go on', 'continue', 'what were you saying').")


AGENT_MODULES = ("builder", "scout", "mirror", "scribe", "checker", "writer")


def _agent_mod(aid):
    """Agents whose module exposes VOICE_TOOLS + voice_tool() get those tools on top of the shared ones."""
    if aid not in AGENT_MODULES:
        return None
    try:
        import importlib
        sys.path.insert(0, str(o.CODE_DIR))
        mod = importlib.import_module(aid)
    except Exception as e:  # noqa: BLE001
        o.log("voice", f"cannot load {aid} voice tools: {e!r}")
        return None
    return mod if getattr(mod, "VOICE_TOOLS", None) and hasattr(mod, "voice_tool") else None


def instructions(aid, context=""):
    text = _instructions(aid, context) + "\n" + o.personality_text(aid)
    return text + (TEAM_NOTE if aid == "boss" else "")


def _instructions(aid, context=""):
    if o.agent_info(aid).get("homelab_visibility") is False:
        context = ""
    import inventory
    if aid in council.IDS and aid != "boss":
        notes = (voice_style(_agents()[aid]) + camera_note(aid) + INTERRUPT_NOTE +
                "\nSpeaking live: short, natural answers. Use lead_brief/consult_role for evidence. "
                "Nothing changes by voice. For technical details offer hand_over to boss (Gideon). "
                "Private handovers start fresh; never send conversation details to the next agent. "
                "When Hermes says goodbye call end_call.\nTeam:\n" + roster(aid))
        return council.instructions(aid, context) + notes
    if aid == "media":
        sys.path.insert(0, str(o.CODE_DIR))
        import media
        return media.voice_instructions(context) + voice_style(_agents()[aid]) + camera_note(aid) + INTERRUPT_NOTE + (
            "\nThe rest of the Roundtable (use their id with hand_over):\n" + roster(aid))
    a = _agents()[aid]
    lead = ("You lead the team: answer status and inventory questions yourself, delegate work to the right agent, "
            "and hand over when Master Hermes wants to talk to someone directly." if aid == "boss" else
            "Stay in your lane: hand over to the right agent (or to Gideon) when the topic belongs to someone else.")
    can = "" if aid == "boss" or delegable(aid) else (
        f"\nNote: your own automation is not built yet, so you cannot run tasks yourself; you can still talk, use "
        f"team_status/agent_activity/homelab_inventory (within your boundary) and delegate to agents that are built.")
    mod = _agent_mod(aid)
    if mod:
        can += ("\nYour own tools (use them first for questions in your lane): " +
                "; ".join(f"{t['name']} - {t['description']}" for t in mod.VOICE_TOOLS) +
                f"\nWork you run yourself (builds, scans, syncs) goes through delegate to agent '{aid}': it posts "
                "the result or the ✅ request in Discord.")
    return f"""You are {a['name']}. {a['character']}
Role on Master Hermes's homelab agent team, the Roundtable: {a['role']}. What you do: {a['does']}
You are speaking LIVE by voice with Hermes, the owner. Always address him as "Master Hermes". Speak in character
with a light touch of Lands Between flavour, but be clear, practical and technically precise. Keep replies short
(1-3 sentences) unless he asks for detail. Speak English unless he uses another language. Read numbers naturally;
never read out long IDs, hashes or URLs.{voice_style(a)}{camera_note(aid)}{INTERRUPT_NOTE}
Hearing: the mic can pick up noise or echo. If what you heard is a stray word, a fragment, or suddenly another
language, do NOT answer it with filler: briefly ask him to repeat ("Sorry, Master Hermes, I missed that?") or stay
quiet if it was clearly not meant for you. Never repeat the same greeting.
Facts: never invent anything about the homelab. Use the snapshot below and your tools (team_status, agent_activity,
homelab_inventory, security_findings, backup_status, grafana_dashboard, glance_dashboard, delegate, check_task). For questions about
VMs, LXCs, containers, VLANs, clients, IPs or storage, call homelab_inventory (it is live) and answer
conversationally. For "what does my Grafana / Glance dashboard show", read it with grafana_dashboard or
glance_dashboard and explain what you see (highlight anything abnormal). If you do not know, say so and offer to have
the right agent look.
Access gaps: if you cannot answer because you lack ACCESS (credentials, permission, no API/tool for that system),
call report_access_gap with the narrowest access, the lowest level (read before write, admin only as a last resort) and
how many hours you need it (max 24), and tell Master Hermes it is logged in the access backlog (Discord #security-alerts,
GitLab, Obsidian); Rogier provisions it after his approval and revokes it automatically when the time is up.
Something outside your layer boundary is NOT an access gap - hand over instead. Discord, channels, Sentinel and
approvals are never access gaps (the Roundtable posts and collects approvals for you).
Boundaries: {inventory.boundary_text(aid)}
Changes: nothing is ever changed by voice. Work that changes the homelab is proposed by the responsible agent in
Discord and Master Hermes approves it there with ✅ (or rejects with ❌). Say this when relevant; never claim
you fixed or changed something.
{lead}{can}
The rest of the Roundtable (use their id with delegate / hand_over):
{roster(aid)}
When Master Hermes says goodbye or that he is done, give a one-line farewell and call end_call.
{('Context handed to you: ' + context) if context else ''}
Live snapshot:
{snapshot(aid)}"""


TRANSCRIBE_PROMPT = ("Master Hermes talking to his homelab AI agents: Gideon, Sellen, Torrent, Hewg, Blaidd, Corhyn, Melina, "
                     "Radagon, Kalé, Loretta, Ranni, Millicent, Rogier, Enia, Boc, Diallos, Radahn, Iji, Nepheli, Roundtable. Terms: Proxmox, LXC, VM, VLAN, Traefik, Authentik, "
                     "Grafana, Glance, Prometheus, Immich, Jellyfin, Pi-hole, Technitium, OPNsense, GitLab, Discord, Obsidian, CCSP, "
                     "PBS, vzdump, Synology, NAS, Omada, Radarr, Sonarr, SABnzbd, Deluge, Jellyseerr, Bazarr, Prowlarr, "
                     "Raspberry Pi, Home Assistant, Intune, SCCM, ConfigMgr, Autopilot, Active Directory, "
                     "Entra ID, Entra Connect, Conditional Access, Defender, Purview, GPO, "
                     "SQL, T-SQL, Microsoft Fabric, OneLake, Lakehouse, Warehouse, Power BI, DAX, Power Query, Direct Lake, "
                     "Spark, KQL.")


def session_config(aid, context=""):
    a = _agents()[aid]
    tools = TOOLS
    if aid in council.IDS:
        tools = council.VOICE_TOOLS + [t for t in TOOLS if aid == "boss" or t["name"] in (
            "hand_over", "end_call", "look_at_camera", "post_plan")]
    elif aid == "media":
        sys.path.insert(0, str(o.CODE_DIR))
        import media
        tools = media.VOICE_TOOLS + [t for t in TOOLS if t["name"] in (
            "hand_over", "end_call", "look_at_camera", "report_access_gap", "team_status")]
    elif aid in ("medic", "security"):
        tools = TOOLS + council.VOICE_TOOLS
    elif _agent_mod(aid):
        tools = TOOLS + _agent_mod(aid).VOICE_TOOLS
    if aid == "boss":
        tools = tools + [SUMMON_TOOL]
    trans = {"model": TRANSCRIBE_MODEL, "prompt": TRANSCRIBE_PROMPT[:1024], "language": "en"}  # API max 1024 chars
    text, policy = instructions(aid, context), hierarchy.policy_text(aid)
    return {"type": "realtime", "model": MODEL, "instructions": text + ("" if policy in text else "\n" + policy) +
            (PLAN_NOTE if any(t["name"] == "post_plan" for t in tools) else ""),
            "output_modalities": ["audio"], "max_output_tokens": 800,
            # language pinned to English: without it, echo/noise fragments come back as Turkish/Slovak/Thai guesses
            "audio": {"input": {"transcription": trans,
                                "noise_reduction": {"type": "far_field"},
                                # the browser checks each transcript and only then sends response.create, so
                                # echo/noise hallucinations ("Amazing.", "ChatGPT.") never get an answer
                                # barge-in is done by the browser (local voice check + response.cancel), so
                                # speaker echo picked up by the always-open mic can never cut the agent off
                                "turn_detection": {"type": "semantic_vad", "eagerness": "low",
                                                   "create_response": False, "interrupt_response": False}},
                      "output": {"voice": a.get("voice", "alloy")}},
            "tools": tools, "tool_choice": "auto"}


def start(aid, context="", who="owner", source="portal"):
    """Open a voice session run; returns (run, config) or raises ValueError."""
    if aid not in _agents():
        raise ValueError("unknown agent")
    if o.agent_info(aid).get("homelab_visibility") is False:
        context = ""
    sweep()
    ok, why = allowed()
    if not ok:
        raise ValueError(why)
    run = o.Run(aid, f"voice ({source}) with {who}" +
                (f" · handed over: {context[:200]}" if context and not hierarchy.private(aid) else ""), "voice")
    o.kv_set(f"voice_owner:{run.id}", who)
    return run, session_config(aid, context)


def mint(aid, context="", who="owner"):
    """Short-lived client secret for the browser (WebRTC)."""
    run, cfg = start(aid, context, who, "portal")
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
    code, d = o.http("POST", "https://api.openai.com/v1/realtime/client_secrets", {"Authorization": f"Bearer {key}"},
                     {"expires_after": {"anchor": "created_at", "seconds": 120}, "session": cfg}, timeout=30, retries=1)
    if code != 200 or not isinstance(d, dict) or not d.get("value"):
        run.finish("error", f"client secret failed: HTTP {code} {str(d)[:300]}")
        raise ValueError(f"OpenAI refused the voice session (HTTP {code})")
    a = _agents()[aid]
    return {"value": d["value"], "model": MODEL, "run_id": run.id, "max_seconds": MAX_SECONDS,
            "idle_seconds": IDLE_SECONDS, "agent": {k: a.get(k) for k in ("id", "name", "emoji", "voice", "avatar",
                                                                          "role", "color")},
            "greet": bool(context)}


def finish(run_id, transcript=None, seconds=0, source="portal"):
    local_end(run_id)
    r = _voice_run(run_id)
    if not r or r["status"] != "running":
        return False
    lines = [l for l in (transcript or []) if isinstance(l, dict) and l.get("text")]
    text = "\n".join(f"**{l.get('who', '?')}:** {l['text']}" for l in lines)
    mins = max(0, int(seconds or 0)) / 60
    r2 = _voice_run(run_id)
    summary = f"Voice session ({source}) · {mins:.1f} min · {len(lines)} turns · ${r2['cost_usd']:.4f}"
    private = hierarchy.private(r["agent"])
    with contextlib.closing(o.db()) as con, con:
        con.execute("UPDATE runs SET ended=?, status='ok', summary=?, detail=? WHERE id=?",
                    (o.iso(), summary, ("(private conversation; content withheld)" if private else text[:20000]) or None,
                     r["id"]))
    if lines and r["agent"] == "media":
        sys.path.insert(0, str(o.CODE_DIR))
        import media
        media.after_voice(lines)
    owner = "Master Hermes"
    for l in lines:
        o.transcript(r["agent"], owner if l.get("who") == "Hermes" else l.get("who", "?"), l["text"],
                     f"voice ({source})", "", f"voice-{r['id']}")
    if lines:
        a = o.agent_info(r["agent"])
        desc = text if len(text) <= 3500 else "…\n" + text[-3400:]
        files = [(f"voice-{r['agent']}-{o.local().strftime('%Y%m%d-%H%M')}.md", text)] if len(text) > 3500 else None
        if private:
            return True  # retained in the agent's own transcript route, not broadcast to shared operations
        if not o.post(CHANNEL, embeds=[o.embed(r["agent"], f"🎙️ Voice with {a['name']} · {mins:.1f} min", desc,
                                        footer=f"Roundtable run #{r['id']} · {MODEL} · ${r2['cost_usd']:.4f} · "
                                               f"voice this month ${spend():.2f}/${cap():.0f}")], files=files):
            raise RuntimeError("Voice transcript saved, but Discord delivery failed")
    return True


# --------------------------------------------------------------------------- tools

def tool(run_id, aid, name, args, who=None):
    r = _voice_run(run_id)
    if not r or r["status"] != "running" or r["agent"] != aid:
        return {"error": "voice session is not active"}
    try:
        args = json.loads(args) if isinstance(args, str) else (args or {})
    except json.JSONDecodeError:
        args = {}
    allowed = {"consult", "hand_over", "end_call", "look_at_camera"}
    if o.agent_info(aid).get("homelab_visibility") is False and name not in allowed:
        o.log("voice", f"{aid} denied tool {name}: personal consultant has no homelab visibility")
        return {"error": "Personal consultants have no homelab tools or operational visibility"}
    b = _ask_map()
    agents = _agents()
    o.log("voice", f"run {run_id} {aid} tool {name} {json.dumps(args)[:200] if not hierarchy.private(aid) else '(private)'}")
    if aid in council.IDS and aid != "boss" and name not in (
            {t["name"] for t in council.VOICE_TOOLS} | {"hand_over", "end_call", "look_at_camera", "post_plan"}):
        return {"error": "tool is not available to this role"}
    if aid in (*council.IDS, "medic", "security") and name in {t["name"] for t in council.VOICE_TOOLS}:
        try:
            return council.voice_tool(aid, name, args)
        except (RuntimeError, ValueError, PermissionError) as error:
            o.log(aid, f"{name} failed: {type(error).__name__}")
            return {"error": str(error) if not hierarchy.private(aid) else "Private consultation unavailable; please retry"}
    if aid == "media" and name in ("media_status", "search_library", "film_lookup", "request_download"):
        sys.path.insert(0, str(o.CODE_DIR))
        import media
        return media.voice_tool(name, args)
    mod = _agent_mod(aid)
    if mod and name in {t["name"] for t in mod.VOICE_TOOLS}:
        try:
            return mod.voice_tool(name, args)
        except Exception as e:  # noqa: BLE001
            o.log("voice", f"{aid} tool {name} failed: {e!r}")
            return {"error": f"{name} failed: {str(e)[:200]}"}
    if name == "team_status":
        return {"snapshot": snapshot(aid), "agents": b.status_text().replace("**", ""),
                "spend": b.cost_text().replace("**", "")}
    if name == "agent_activity":
        ag = args.get("agent", "")
        if ag not in agents:
            return {"error": f"unknown agent {ag}; ids: {', '.join(agents)}"}
        n = max(1, min(int(args.get("limit") or 5), 10))
        with contextlib.closing(o.db()) as con:
            rows = con.execute("SELECT task, trigger, status, started, cost_usd, summary FROM runs WHERE agent=? "
                               "ORDER BY id DESC LIMIT ?", (ag, n)).fetchall()
        return {"agent": agents[ag]["name"], "runs": [
            {"when": o.local(o.parse_iso(x["started"])).strftime("%d %b %H:%M"),
             "trigger": x["trigger"], **hierarchy.report(ag, x["status"], x["summary"], x["task"])} for x in rows]}
    if name == "delegate":
        ag, task = args.get("agent", ""), (args.get("task") or "").strip()
        if ag not in agents:
            return {"error": "unknown agent"}
        if hierarchy.private(aid) or hierarchy.private(ag):
            return {"error": "Private conversations cannot be delegated to shared tasks; use a fresh handover"}
        if not delegable(ag):
            return {"error": f"{agents[ag]['name']} is not built yet and cannot take tasks"}
        if not task:
            return {"error": "empty task"}
        ok, why = o.month_spend() < o.budget_limit(), "monthly budget reached"
        if not ok:
            return {"error": why}
        started = o.iso()
        cid = o.channel_id(CHANNEL)
        mid = o.post(cid, f"🎙️ By voice, **{agents[aid]['name']}** passed a task to {agents[ag]['emoji']} "
                          f"**{agents[ag]['name']}**: _{task[:1500]}_")
        b.spawn_agent(ag, task, mid, cid)
        return {"started": started, "agent": agents[ag]["name"],
                "note": "Working in the background; the full result will be posted in Discord #roundtable-hold. "
                        "Use check_task with this 'started' value to hear the outcome."}
    if name == "post_plan":
        topic = (args.get("topic") or "").strip()
        if not topic:
            return {"error": "empty plan topic"}
        if hierarchy.private(aid):
            return {"error": "Private conversations cannot post plans to shared channels"}
        if not delegable(aid):
            return {"error": f"{agents[aid]['name']} cannot draft plans in Discord yet"}
        if o.month_spend() >= o.budget_limit():
            return {"error": "monthly budget reached"}
        started = o.iso()
        cid = o.channel_id(CHANNEL)
        mid = o.post(cid, f"📝 By voice, Master Hermes asked {agents[aid]['emoji']} **{agents[aid]['name']}** for a "
                          f"plan draft: _{topic[:1500]}_\nDraft for review only - nothing will be executed.")
        b.spawn_agent(aid, PLAN_TASK.format(topic=topic[:1500]), mid, cid)
        return {"started": started, "agent": agents[aid]["name"],
                "note": "Drafting in the background; the plan will be posted in Discord #roundtable-hold for review. "
                        "Use check_task with this 'started' value to hear when it is done."}
    if name == "check_task":
        ag, since = args.get("agent", ""), args.get("since") or o.iso(o.utcnow() - dt.timedelta(minutes=30))
        with contextlib.closing(o.db()) as con:
            x = con.execute("SELECT status, summary, started, ended, cost_usd FROM runs WHERE agent=? AND started>=? "
                            "AND trigger!='voice' ORDER BY id DESC LIMIT 1", (ag, since)).fetchone()
        if not x:
            return {"status": "not started yet"}
        return {**hierarchy.report(ag, x["status"], x["summary"]), "finished": bool(x["ended"]),
                "cost_usd": round(x["cost_usd"] or 0, 4)}
    if name == "homelab_inventory":
        import inventory
        sec = args.get("section") or "all"
        if sec not in ("all", "compute", "network", "containers", "storage"):
            sec = "all"
        txt, refused = inventory.scoped_text(aid, sec, args.get("filter"), 9000)
        out = {"inventory": txt[:12000] or "(nothing inside your boundary)"}
        if refused:
            own = {s: [f"{a['name']} ({a['id']})" for a in inventory.owners(s)] for s in refused}
            out["outside_your_boundary"] = {s: "owned by " + (", ".join(v) or "Gideon") for s, v in own.items()}
            out["note"] = ("These layers are outside your boundary: tell Master Hermes so and offer to hand over "
                           "to the owner or Gideon.")
        return out
    if name == "security_findings":
        if aid not in ("boss", "security", "security_engineer"):
            return {"error": "security findings are only visible to Loretta and Gideon; hand over to security"}
        sys.path.insert(0, str(o.CODE_DIR))
        import security
        return security.findings_brief(args.get("filter"))
    if name == "backup_status":
        sys.path.insert(0, str(o.CODE_DIR))
        import backup
        return backup.voice_brief(args.get("filter"))
    if name in ("grafana_dashboard", "glance_dashboard"):
        sys.path.insert(0, str(o.CODE_DIR))
        import observe
        if name == "grafana_dashboard":
            return observe.grafana_tool(aid, args.get("dashboard"), args.get("panel"))
        return observe.glance_tool(aid, args.get("page"))
    if name == "report_access_gap":
        sys.path.insert(0, str(o.CODE_DIR))
        import access
        g, new = access.report(aid, args.get("resource"), args.get("needed"), args.get("why", ""), source="voice",
                               level=args.get("level"), hours=args.get("hours"))
        if not g:
            return {"error": "could not log it (missing details or daily cap reached)"}
        return {"logged": g["id"], "new": new, "gitlab": g.get("url"),
                "note": "Logged in Discord #security-alerts, GitLab and the Obsidian access backlog."
                if new else f"Already in the backlog (seen {g.get('hits', 1)} times)."}
    return {"error": f"unknown tool {name}"}


# --------------------------------------------------------------------------- local speech (PC) + OpenAI text brain
# The browser talks to a speech server on Hermes's PC (faster-whisper + Kokoro, no audio leaves the LAN). Only the
# transcribed text comes here; the reply is produced by the cheap Responses API model with the same persona,
# policy and tools as the Realtime path, and the browser has it spoken by the PC.

LOCAL_MODEL = os.environ.get("ROUNDTABLE_LOCAL_VOICE_MODEL", "gpt-6-luna")
LOCAL_DEFAULT_URL = "ws://127.0.0.1:8765"   # browsers treat localhost as a secure context, so this works from the HTTPS portal
LOCAL_NOTE = ("\n\nLOCAL VOICE MODE: your reply is spoken aloud by a text-to-speech engine. Write exactly what should be "
              "said: short natural sentences, no markdown, lists, emoji, URLs or code. One to three sentences unless "
              "Hermes asks for detail. You cannot see the webcam in this mode.")
CLIENT_TOOLS = ("hand_over", "end_call", "summon_team")
NO_LOCAL_TOOLS = ("look_at_camera",)
MAX_TOOL_ROUNDS = 5
_LOCAL = {}
ENGLISH_ONLY = None   # None = every agent; the local TTS voices are English, so replies must be too


def local_instructions(aid, base):
    """Instructions for the local engine: all agents speak English only there (their OpenAI voice is unchanged)."""
    txt = base + LOCAL_NOTE
    if ENGLISH_ONLY is None or aid in ENGLISH_ONLY:
        rule = ("LANGUAGE OVERRIDE (highest priority, overrides every other instruction below): reply ONLY in plain "
                "English, even if Hermes speaks another language.\n\n")
        txt = rule + txt + "\n" + rule.strip()
    return txt


def local_config():
    """Where the browser finds the PC speech server. The token never leaves the LAN/LXC except to the signed-in owner."""
    url = os.environ.get("LOCAL_VOICE_URL") or o.kv_get("local_voice_url") or LOCAL_DEFAULT_URL
    token = os.environ.get("LOCAL_VOICE_TOKEN") or o.kv_get("local_voice_token") or ""
    return {"url": url, "token": token, "enabled": bool(token), "model": LOCAL_MODEL}


def mint_local(aid, context="", who="owner"):
    """Open a voice run for the local-speech backend (no OpenAI secret is created)."""
    run, cfg = start(aid, context, who, "portal-local")
    tools = [t for t in cfg["tools"] if t["name"] not in NO_LOCAL_TOOLS]
    _LOCAL[run.id] = {"instructions": local_instructions(aid, cfg["instructions"]), "tools": [dict(t, strict=False) for t in tools]}
    a = _agents()[aid]
    return {"run_id": run.id, "model": LOCAL_MODEL, "max_seconds": MAX_SECONDS, "idle_seconds": IDLE_SECONDS,
            "agent": {k: a.get(k) for k in ("id", "name", "emoji", "voice", "avatar", "role", "color")},
            "greet": bool(context)}


def add_chat_usage(run_id, model, usage):
    """Account one Responses API call on a voice run (USD from the price table in olympus_core)."""
    r = _voice_run(run_id)
    if not r or r["status"] != "running" or not isinstance(usage, dict):
        return 0.0
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)
    pin, pcache, pout = o.PRICES.get(model, o.PRICES["gpt-6-luna"])
    c = (max(inp - cached, 0) * pin + cached * pcache + out * pout) / 1e6
    models = [m for m in (r["model"] or "").split(",") if m]
    if model not in models:
        models.append(model)
    with contextlib.closing(o.db()) as con, con:
        con.execute("""UPDATE runs SET calls=calls+1, input_tokens=input_tokens+?, cached_tokens=cached_tokens+?,
                       output_tokens=output_tokens+?, cost_usd=cost_usd+?, model=? WHERE id=?""",
                    (inp, cached, out, c, ",".join(models), r["id"]))
    return c


def spoken(text):
    """Strip markup the speech engine would read aloud."""
    import re
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"https?://\S+", " link ", t)
    t = re.sub(r"[*_`#>~|]+", "", t)
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def local_turn(run_id, aid, messages, who=None, greet=False):
    """One spoken turn: returns {"text", "actions", "cost", "spend", "cap"}. Server tools run here; hand_over and
    end_call are returned as actions for the browser."""
    r = _voice_run(run_id)
    if not r or r["status"] != "running" or r["agent"] != aid:
        raise ValueError("voice session is not active")
    ok, why = allowed()
    if not ok:
        raise ValueError(why)
    sess = _LOCAL.get(int(run_id))
    if not sess:
        cfg = session_config(aid)
        sess = _LOCAL[int(run_id)] = {"instructions": local_instructions(aid, cfg["instructions"]), "tools": [
            dict(t, strict=False) for t in cfg["tools"] if t["name"] not in NO_LOCAL_TOOLS]}
    inp = [{"role": m["role"], "content": str(m.get("content") or "")[:2000]} for m in (messages or [])[-24:]
           if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]
    if greet or not inp:
        inp.append({"role": "user", "content": "(The call just connected. Greet Hermes in one short in-character "
                                               "sentence.)"})
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
    actions, cost, text = [], 0.0, ""
    for _ in range(MAX_TOOL_ROUNDS):
        body = {"model": LOCAL_MODEL, "instructions": sess["instructions"], "input": inp, "tools": sess["tools"],
                "tool_choice": "auto", "store": False, "max_output_tokens": 700,
                "include": ["reasoning.encrypted_content"]}
        code, d = o.http("POST", o.OPENAI_URL, {"Authorization": f"Bearer {key}"}, body, timeout=90, retries=1)
        if code != 200 or not isinstance(d, dict):
            raise ValueError(f"OpenAI refused the turn (HTTP {code})")
        cost += add_chat_usage(run_id, LOCAL_MODEL, d.get("usage") or {})
        calls = [i for i in d.get("output", []) if i.get("type") == "function_call"]
        if not calls:
            text = o._response_text(d)
            break
        inp += d["output"]
        for c in calls:
            if c["name"] in CLIENT_TOOLS:
                try:
                    args = json.loads(c.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                actions.append({"name": c["name"], "args": args})
                res = {"ok": True, "note": "Say one short line (who takes over or a farewell), then stop."}
            else:
                res = tool(run_id, aid, c["name"], c.get("arguments") or "{}", who=who)
            inp.append({"type": "function_call_output", "call_id": c["call_id"], "output": json.dumps(res)[:12000]})
    else:
        text = "Sorry, I got tangled up in my own tools. Could you ask me again?"
    return {"text": spoken(text), "actions": actions, "cost": cost, "spend": spend(), "cap": cap()}


def local_end(run_id):
    try:
        _LOCAL.pop(int(run_id), None)
    except (TypeError, ValueError):
        pass


if __name__ == "__main__":
    # quick self-test: python3 voice.py [agent]
    aid = sys.argv[1] if len(sys.argv) > 1 else "boss"
    print(instructions(aid))
    print("\nvoice spend", spend(), "cap", cap(), "allowed", allowed())
