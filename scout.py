#!/usr/bin/env python3
"""Kalé - Roundtable scout: weekly homelab ideas that actually fit Master Hermes's lab.

  scout.py run                       # weekly (Sun 09:00): web research -> ranked report in #ideas-and-todos
  scout.py ask "<text>" [--reply-to MID] [--channel CID]  # "scout now", "is X worth self-hosting?", ...
  scout.py last                      # print the last report

Remembers every past pick (kv scout:seen) so nothing is suggested twice within 6 months. Never installs anything:
a pick he likes can be handed to Hewg ("Hewg, build an LXC for <app>").
"""
import argparse
import time

import olympus_core as o

AGENT = "scout"
CHANNEL = "ideas-and-todos"  # was #homelab-scout (archived 2026-10-18)
TOPIC = "🔔 Kalé's weekly scouting: self-hosted apps, releases and ideas that fit the lab (Sun 09:00)."
MODEL = "gpt-6.1-sol"
KV_SEEN, KV_LAST = "scout:seen", "scout:last"
REPEAT_DAYS = 180
STACK_HINTS = ("Proxmox VE 3-node cluster + PBS, Synology NAS (NFS), Kubernetes (3 controllers + 6 workers), "
               "Traefik + Authentik SSO, Technitium DNS (primary) + Pi-hole/Unbound (secondary), Omada SDN, GitLab CE + runner, Prometheus/Grafana/Loki, "
               "Uptime Kuma, Glance dashboard, Immich, Jellyfin + *arr stack, Home Assistant, n8n, Paperless-ngx, "
               "Karakeep, OpenAI-powered agent team (the Roundtable), Obsidian vault, Ansible/Terraform. "
               "Owner: solution architect studying CCSP/CISSP; values stability, low maintenance, privacy, learning.")

REPORT = o.S(
    headline=o.STR,
    picks=o.A(o.S(name=o.STR, kind=o.E("new_app", "release", "idea", "security"), category=o.STR, what=o.STR,
                  why_fit=o.STR, effort=o.E("low", "medium", "high"), where=o.STR, resources=o.STR, url=o.STR,
                  score={"type": "integer"})),
    skip=o.A(o.S(name=o.STR, why=o.STR)),
)
EFFORT = {"low": "🟢 low", "medium": "🟡 medium", "high": "🔴 high"}
KIND = {"new_app": "🆕", "release": "⬆️", "idea": "💡", "security": "🛡️"}


def _seen():
    s = o.kv_get(KV_SEEN, {}) or {}
    cut = time.time() - REPEAT_DAYS * 86400
    return {k: v for k, v in s.items() if v.get("t", 0) > cut}


def context():
    import inventory
    try:
        ctr = inventory.text("containers", limit=4000)[:6000]
    except Exception as e:  # noqa: BLE001
        ctr = f"(unavailable: {e})"
    try:
        comp = inventory.get("compute")
        cap = "; ".join(f"{n['node']}: {n['ram_max_gb'] - n['ram_gb']:.0f} GB RAM free of {n['ram_max_gb']:.0f}, CPU "
                        f"{n['cpu_pct']}%" for n in comp.get("nodes", []))
        st = "; ".join(f"{s['storage']}({s['node']}): {s['size_gb'] - s['used_gb']:.0f} GB free"
                       for s in comp.get("storage", []) if s["size_gb"])
    except Exception as e:  # noqa: BLE001
        cap, st = f"(unavailable: {e})", ""
    return ctr, cap, st


def scout(reply_to=None, channel=None, focus=""):
    channel = channel or CHANNEL
    cid = o.channel_id(channel, TOPIC if channel == CHANNEL else None)
    seen = _seen()
    ctr, cap, st = context()
    with o.Run(AGENT, "weekly scout" + (f": {focus[:100]}" if focus else ""), "chat" if reply_to else "schedule") as run:
        r = o.llm(run, f"""You are Kalé, the Roundtable's wandering merchant-scout. Search the web for what is NEW in the
last ~2 weeks for self-hosters (new or trending self-hosted apps, notable releases of software he already runs,
security advisories for his stack, and homelab ideas). Then filter hard against his ACTUAL lab and spare capacity.
Return 5-8 picks, best first. For each: what it is, why it fits HIS lab specifically (what it replaces or adds),
effort, where it would run (existing Docker host / new LXC / Kubernetes), resources (RAM/disk), a real URL
(project or release notes), score 1-10. Include releases/advisories only for software he runs. Put things you
considered but rejected in skip (short reason). Do not repeat anything in ALREADY SUGGESTED. Never invent projects
or versions; only cite what you found.
{('FOCUS FROM MASTER HERMES: ' + focus) if focus else ''}

STACK: {STACK_HINTS}
RUNNING CONTAINERS:
{ctr}
SPARE CAPACITY: {cap}
STORAGE FREE: {st}
ALREADY SUGGESTED (skip): {', '.join(sorted(seen)) or 'none'}""", schema=REPORT, model=MODEL, effort="low", web=True,
                  timeout=600)
        picks = sorted(r["picks"], key=lambda p: -p["score"])
        now = time.time()
        for p in picks:
            seen[p["name"].lower()] = {"t": now, "kind": p["kind"]}
        o.kv_set(KV_SEEN, seen)
        lines = [f"**{i + 1}. {KIND.get(p['kind'], '•')} [{p['name']}]({p['url']})** · {p['category']} · "
                 f"effort {EFFORT[p['effort']]} · {p['score']}/10\n{p['what']}\n↳ _Fit:_ {p['why_fit']}\n"
                 f"↳ _Where:_ {p['where']} · {p['resources']}" for i, p in enumerate(picks)]
        body = f"_{r['headline']}_\n\n" + "\n\n".join(lines)
        if r["skip"]:
            body += "\n\n**Looked at, passed on:** " + "; ".join(f"{s['name']} ({s['why']})" for s in r["skip"][:6])
        body += "\n\nLike one? Say `Hewg, build an LXC for <app>` and he will draw up the blueprint for your ✅."
        o.kv_set(KV_LAST, {"t": o.iso(), "headline": r["headline"], "picks": picks, "skip": r["skip"]})
        run.summary = f"{len(picks)} picks: " + ", ".join(p["name"] for p in picks)[:250]
        o.respond(AGENT, cid, "🔔 Kalé's scouting report", body, reply_to=reply_to, run=run,
                  filename=f"scout-{o.local().strftime('%Y%m%d')}.md")
        return picks


INTENT = o.S(action=o.E("scout_now", "last_report", "answer"), focus=o.STR)


def last_text():
    last = o.kv_get(KV_LAST, {}) or {}
    if not last:
        return "No scouting report yet."
    return (f"Report {last['t'][:10]}: {last['headline']}\n" +
            "\n".join(f"- {p['name']} ({p['kind']}, effort {p['effort']}, {p['score']}/10): {p['what'][:160]}"
                      for p in last["picks"]))


def ask(text, reply_to=None, channel=None):
    channel = channel or "roundtable-hold"
    o.transcript(AGENT, "Request", text, "discord", channel, reply_to or "")
    with o.Run(AGENT, f"ask: {text[:200]}", "chat") as run:
        it = o.llm(run, f"""Classify Master Hermes's request to Kalé, the homelab scout.
scout_now: he wants a fresh scouting round now (focus = any topic he named, e.g. "monitoring", "photo apps", else "").
last_report: he wants to hear the last report again.
answer: a question (is X worth self-hosting, alternatives to Y, compare A vs B). focus = "".
Request: {text}""", schema=INTENT, model="gpt-6-luna")
        if it["action"] == "last_report":
            o.respond(AGENT, channel, "🔔 Kalé's last report", last_text(), reply_to=reply_to, run=run)
            return
        if it["action"] == "answer":
            ctr, cap, _ = context()
            ans = o.llm(run, f"""You are Kalé, the Roundtable's merchant-scout: friendly, sharp-eyed, honest about trade-offs.
Answer Master Hermes in Discord Markdown, concise, with links to what you found on the web. Judge fit against HIS lab.
STACK: {STACK_HINTS}
RUNNING CONTAINERS (excerpt): {ctr[:3000]}
SPARE CAPACITY: {cap}
QUESTION: {text}""", model="gpt-6-luna", web=True, max_output_tokens=1500)
            run.summary = (ans or "")[:300]
            o.respond(AGENT, channel, "🔔 Kalé answers", ans or "No answer.", reply_to=reply_to, run=run)
            return
    scout(reply_to, channel, it["focus"])


VOICE_TOOLS = [
    {"type": "function", "name": "scout_report",
     "description": "Kalé's latest weekly scouting report: new self-hosted apps, releases, advisories and ideas that "
                    "fit the lab, with effort, where it would run and score.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def voice_tool(name, args):
    if name == "scout_report":
        return {"report": last_text(), "note": "For a fresh round (about a minute, posted to Discord #ideas-and-todos), "
                                               "delegate to agent 'scout'."}
    return {"error": f"unknown tool {name}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "ask", "last"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "run":
        scout()
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    else:
        print(last_text())


if __name__ == "__main__":
    main()
