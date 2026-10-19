"""Roundtable reporting, governance evidence, SLA objectives and API FinOps.

No personal conversation content is included in operational reports or GitLab issues.
"""
import calendar
import contextlib
import datetime as dt
import json
import os
import threading
import fcntl

import olympus_core as o

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items(
  key TEXT PRIMARY KEY, agent TEXT NOT NULL, title TEXT NOT NULL, severity TEXT NOT NULL,
  created TEXT NOT NULL, due TEXT NOT NULL, status TEXT NOT NULL, discord_id TEXT,
  channel TEXT, gitlab_iid INTEGER, gitlab_url TEXT, evidence TEXT, sync_error TEXT,
  acknowledged TEXT, resolution_discord_id TEXT, last_synced TEXT);
"""
RESOLUTION_HOURS = {"critical": 4, "high": 24, "medium": 72, "low": 168, "info": 168}
ACK_MINUTES = {"critical": 15, "high": 60, "medium": 480, "low": 1440, "info": 1440}
GITLAB = os.environ.get("SRE_GITLAB_BASE", "http://192.168.40.23/api/v4")
PROJECT = int(os.environ.get("SRE_GITLAB_PROJECT", "6"))
TERMINAL = {"done", "fixed", "resolved", "ok", "rejected", "expired"}
_SYNC_LOCK = threading.RLock()


def private(aid):
    a = o.agent_info(aid)
    return a.get("group") in ("personal", "specialized")


def participates(aid):
    a = o.agent_info(aid)
    return a.get("participates_in_hierarchy") is not False and a.get("homelab_visibility") is not False


def chain(aid):
    agents = {a["id"]: a for a in o.registry()["agents"]}
    result, seen = [], {aid}
    parent = agents.get(aid, {}).get("reports_to")
    while parent and parent != "hermes":
        if parent in seen or parent not in agents:
            raise ValueError(f"Invalid reporting chain for {aid}: {parent}")
        result.append(parent)
        seen.add(parent)
        parent = agents[parent].get("reports_to")
    return result


def policy_text(aid):
    if not participates(aid):
        names = [o.agent_info(x)["name"] for x in chain(aid)]
        return (f"Personal reporting chain: {' -> '.join(names) or 'Hermes directly'}; coordination only, "
                "without sharing private memories. Personal consultant outside the homelab reporting hierarchy. No homelab inventory, operational "
                "status, lead briefings, audit/access records or infrastructure tools. Consult using public expertise "
                "and your own conversations only. Homelab questions need a fresh handover to Gideon; never receive "
                "or forward automatic technical handover context. You do not approve or execute changes.")
    names = [o.agent_info(x)["name"] for x in chain(aid)]
    return (
        f"Reporting chain: {' -> '.join(names) or 'Hermes directly'}. "
        "Ranni and her governance team enforce approval, audit, SLA and cost policies. "
        "Every infrastructure change needs an owner-approved exact plan, rollback, verification and "
        "Miriel's documentation. Every new service needs Diallos/IPAM feedback before allocation. "
        "Do not infer access or authority from a persona. Certifications describe modeled expertise, "
        "not qualifications held by software. Report missing evidence and manual steps explicitly. "
        "Private personal conversations are not operational summaries; never disclose them to other agents."
    )


def report(aid, status, summary="", task=""):
    if private(aid):
        return {"agent": aid, "status": status, "summary": "Private activity; content withheld"}
    return {"agent": aid, "status": status, "summary": o.strip_secrets(summary)[:700],
            "task": o.strip_secrets(task)[:200]}


def brief(aid="boss", limit=40):
    if not participates(aid):
        raise PermissionError("Personal consultants cannot read homelab reporting or lead briefings")
    agents = {a["id"]: a for a in o.registry()["agents"]}
    allowed = {x for x in agents if participates(x) and x != aid and (aid in chain(x) or aid == "boss")}
    since = o.iso(o.utcnow() - dt.timedelta(days=2))
    with contextlib.closing(o.db()) as con:
        rows = con.execute(
            "SELECT id,agent,status,summary,task,started,ended FROM runs WHERE started>=? "
            "ORDER BY id DESC LIMIT 250", (since,),
        ).fetchall()
    lines = []
    for r in rows:
        if r["agent"] not in allowed:
            continue
        item = report(r["agent"], r["status"], r["summary"], r["task"])
        item.update(run_id=r["id"], at=r["ended"] or r["started"],
                    reports_via=chain(r["agent"]))
        lines.append(item)
        if len(lines) >= limit:
            break
    latest = {}
    for a in agents.values():
        if a["id"] not in allowed:
            continue
        hb = o.kv_get(f"hb:{a['id']}")
        if hb:
            latest[a["id"]] = {"at": hb["t"], **report(a["id"], "ok", hb.get("summary", ""))}
    urgent = urgent_items()
    lead_reports = {}
    for lead in ("security", "governance", "medic"):
        lead_reports[lead] = {"lead": agents[lead]["name"],
                             "recent": [r for r in lines if r["agent"] == lead or lead in r["reports_via"]][:8],
                             "urgent": [r for r in urgent if r["owner"] == lead or lead in chain(r["owner"])]}
    return {"as_of": o.iso(), "recent": lines, "heartbeats": latest, "urgent": urgent,
            "lead_reports": lead_reports, "consolidated_by": "Gideon",
            "note": "Cached evidence, not an all-clear or fresh probe. Private activity is metadata only.",
            "governance": compliance(), "finops": finops(),
            "patch_delivery": {"accountable_lead": "medic", "delivery_owner": "builder",
                               "executor": "sre-agent.service", "approval": "owner only"}}


def urgent_items():
    """Open critical/high alerts, incidents and security findings (secret-stripped titles only)."""
    urgent = []
    for key, field, owner in (("watcher_state", "alerts", "watcher"),
                              ("medic", "incidents", "medic"), ("security", "findings", "security")):
        state = o.kv_get(key, {}) or {}
        for item in (state.get(field) or {}).values():
            if field == "alerts" and not item.get("fired"):
                continue
            if field != "alerts" and item.get("state") in ("resolved", "fixed", "closed", "superseded", "muted"):
                continue
            diag = item.get("diag") or item.get("expl") or {}
            severity = diag.get("severity") or item.get("severity") or item.get("sev")
            if severity in ("critical", "high"):
                urgent.append({"owner": owner, "severity": severity,
                               "title": o.strip_secrets(diag.get("title") or item.get("title") or
                                                       (item.get("finding") or {}).get("summary") or "Open issue"),
                               "first_seen": item.get("first") or item.get("first_seen"),
                               "state": item.get("state") or "firing"})
    return urgent


def _db():
    con = o.db()
    con.executescript(SCHEMA)
    con.execute("BEGIN IMMEDIATE")
    columns = {r["name"] for r in con.execute("PRAGMA table_info(work_items)")}
    for name in ("acknowledged", "resolution_discord_id", "last_synced"):
        if name not in columns:
            con.execute(f"ALTER TABLE work_items ADD COLUMN {name} TEXT")
    con.commit()
    return con


def _gl(method, path, data=None):
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        raise RuntimeError("GitLab audit sync requires GITLAB_TOKEN; no work item was published")
    code, result = o.http(method, f"{GITLAB}/projects/{PROJECT}{path}",
                          {"PRIVATE-TOKEN": token}, data, timeout=30, retries=1)
    if code not in (200, 201) or not isinstance(result, (dict, list)):
        raise RuntimeError(f"GitLab audit HTTP {code}: {o.strip_secrets(str(result))[:200]}")
    return result


def track(key, aid, title, severity="medium", channel="roundtable-hold", message=None, created=None, acknowledged=None):
    if private(aid):
        raise PermissionError("Private requests cannot enter shared operational audit records")
    severity = severity if severity in RESOLUTION_HOURS else "medium"
    title = "Private agent request (content withheld)" if private(aid) else o.strip_secrets(title)[:240]
    now = created or o.iso()
    due = o.iso(o.parse_iso(now) + dt.timedelta(hours=RESOLUTION_HOURS[severity]))
    with contextlib.closing(_db()) as con, con:
        con.execute("INSERT OR IGNORE INTO work_items(key,agent,title,severity,created,due,status,channel,discord_id,acknowledged) "
                    "VALUES(?,?,?,?,?,?,'open',?,?,?)", (key, aid, title, severity, now, due, channel, message, acknowledged))
        if acknowledged:
            con.execute("UPDATE work_items SET acknowledged=COALESCE(acknowledged,?) WHERE key=?", (acknowledged, key))
    return key


def sync_item(key):
    with _SYNC_LOCK:
        o.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (o.DATA_DIR / "governance-sync.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return _sync_item(key)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _sync_item(key):
    with contextlib.closing(_db()) as con:
        row = con.execute("SELECT * FROM work_items WHERE key=?", (key,)).fetchone()
    if not row:
        raise ValueError(f"Unknown audit item {key}")
    item = dict(row)
    try:
        if not item["discord_id"]:
            mid = o.post(item["channel"], embeds=[o.embed(
                "compliance", f"Audit {key}", f"{item['title']}\nOwner: {item['agent']}\n"
                f"Resolution objective: {item['due']} UTC; acknowledgement "
                f"{ACK_MINUTES[item['severity']]} min. Objective, not a guaranteed contractual SLA.")])
            if not mid:
                raise RuntimeError("Discord request audit could not be posted")
            item["discord_id"] = mid
        # Persist each successful side effect before the next network request, to avoid duplicates on retry.
        with contextlib.closing(_db()) as con, con:
            con.execute("UPDATE work_items SET discord_id=? WHERE key=?", (item["discord_id"], key))
        if not item["gitlab_iid"]:
            existing = _gl("GET", "/issues?labels=roundtable-governance&search=" +
                           __import__("urllib.parse", fromlist=["quote"]).quote(f"[{key}]") + "&per_page=100")
            found = next((x for x in existing if x.get("title", "").startswith(f"[{key}] ")), None)
            issue = found or _gl("POST", "/issues", {
                "title": f"[{key}] {item['title']}"[:255],
                "description": f"Agent: {item['agent']}\nDiscord message: {item['discord_id']}\n"
                               f"Created: {item['created']}\nResolution objective: {item['due']}\n"
                               "Closed only after recorded resolution/cancellation, never merely because "
                               "someone closed this issue. Private conversation content is not collected.",
                "labels": f"roundtable-governance,agent::{item['agent']},severity::{item['severity']}",
            })
            item["gitlab_iid"], item["gitlab_url"] = issue["iid"], issue["web_url"]
            with contextlib.closing(_db()) as con, con:
                con.execute("UPDATE work_items SET gitlab_iid=?,gitlab_url=? WHERE key=?",
                            (item["gitlab_iid"], item["gitlab_url"], key))
        issue = _gl("GET", f"/issues/{item['gitlab_iid']}")
        if issue.get("web_url"):
            item["gitlab_url"] = issue["web_url"]
            with contextlib.closing(_db()) as con, con:
                con.execute("UPDATE work_items SET gitlab_url=? WHERE key=?", (item["gitlab_url"], key))
        if item["status"] in TERMINAL and item["evidence"]:
            with contextlib.closing(_db()) as con:
                outstanding = con.execute(
                    "SELECT key FROM work_items WHERE gitlab_iid=? AND key!=? "
                    "AND status NOT IN ('done','fixed','resolved','ok','rejected','expired') LIMIT 1",
                    (item["gitlab_iid"], key)).fetchone()
            if issue["state"] != "closed" and not outstanding:
                _gl("POST", f"/issues/{item['gitlab_iid']}/notes",
                    {"body": f"Recorded outcome: {item['status']}\n{o.strip_secrets(item['evidence'])}"})
                _gl("PUT", f"/issues/{item['gitlab_iid']}", {"state_event": "close", "add_labels": "resolved"})
            if not item["resolution_discord_id"]:
                mid = o.post(item["channel"], f"✅ Audit {key} closed: {item['status']} · {item['gitlab_url']}",
                             reply_to=item["discord_id"])
                if not mid:
                    raise RuntimeError("Resolution was not logged to Discord")
                with contextlib.closing(_db()) as con, con:
                    con.execute("UPDATE work_items SET resolution_discord_id=? WHERE key=?", (mid, key))
        elif issue["state"] == "closed":
            _gl("PUT", f"/issues/{item['gitlab_iid']}", {"state_event": "reopen"})
            raise RuntimeError("GitLab issue was closed without resolution evidence; reopened for audit")
        with contextlib.closing(_db()) as con, con:
            con.execute("UPDATE work_items SET sync_error=NULL,last_synced=? WHERE key=?", (o.iso(), key))
        return item
    except (RuntimeError, KeyError, OSError) as error:
        with contextlib.closing(_db()) as con, con:
            con.execute("UPDATE work_items SET sync_error=? WHERE key=?",
                        (o.strip_secrets(str(error))[:500], key))
        o.log("compliance", f"{key}: {o.strip_secrets(str(error))}")
        raise


def resolve(key, status, evidence):
    if not evidence:
        raise ValueError("Resolution requires recorded evidence")
    with contextlib.closing(_db()) as con, con:
        con.execute("UPDATE work_items SET status=?,evidence=? WHERE key=?",
                    (status, o.strip_secrets(evidence)[:2000], key))


def source_key(namespace, key, item):
    return f"{namespace}:{key}:{item.get('first') or item.get('first_seen') or item.get('id') or 'initial'}"


def track_source(namespace, sid, item, aid, channel):
    diag = item.get("diag") or item.get("expl") or {}
    key = source_key(namespace, sid, item)
    title = diag.get("title") or item.get("title") or item.get("resource") or (
        item.get("finding") or {}).get("summary") or "Recorded operational issue"
    track(key, aid, title, diag.get("severity") or item.get("severity") or item.get("sev") or "medium",
          channel, item.get("msg"), created=item.get("first") or item.get("first_seen"),
          acknowledged=item.get("notified") or item.get("first_seen") or item.get("first"))
    iid = item.get("gitlab_iid") or item.get("issue") or item.get("iid")
    if iid:
        with contextlib.closing(_db()) as con, con:
            con.execute("UPDATE work_items SET gitlab_iid=?,gitlab_url=? WHERE key=? AND gitlab_iid IS NULL",
                        (iid, item.get("url"), key))
    return key


def sync():
    reconcile()
    with contextlib.closing(_db()) as con:
        keys = [r["key"] for r in con.execute(
            "SELECT key FROM work_items ORDER BY COALESCE(last_synced,'') ASC,created ASC LIMIT 100")]
    failures = []
    for key in keys:
        try:
            sync_item(key)
        except (RuntimeError, KeyError, OSError) as error:
            failures.append(f"{key}: {error}")
    if failures:
        raise RuntimeError("Audit sync incomplete: " + "; ".join(failures)[:2000])
    return len(keys)


def _later_incident_evidence(item, incidents):
    if item.get("state") != "resolved" or (item.get("finding") or {}).get("type") != "patch-failed":
        return None
    for candidate in incidents.values():
        fix = candidate.get("fix") or {}
        if (candidate.get("state") == "fixed" and fix.get("status") == "fixed" and fix.get("verification")
                and item.get("gitlab_iid") and candidate.get("gitlab_iid") == item["gitlab_iid"]
                and item.get("first") and candidate.get("first")
                and o.parse_iso(candidate["first"]) >= o.parse_iso(item["first"])
                and (item.get("finding") or {}).get("host")
                and (candidate.get("finding") or {}).get("host") == item["finding"]["host"]):
            return f"Corroborated by later verified incident #{candidate['id']}: " + json.dumps(fix)
    return None


def reconcile():
    with contextlib.closing(o.db()) as con:
        approvals = [dict(r) for r in con.execute("SELECT * FROM approvals")]
    for ap in approvals:
        if private(ap["agent"]):
            continue
        key = f"approval:{ap['id']}"
        track(key, ap["agent"], ap["title"], ap["severity"], ap["channel_id"], ap["message_id"],
              created=ap["created"], acknowledged=ap["decided"])
        if ap["status"] in TERMINAL or ap["status"] in ("failed", "partial"):
            resolve(key, ap["status"], ap["result"] or f"Queue recorded {ap['status']} at {ap['finished'] or ap['decided']}")
    sources = (("watcher_state", "alerts", "watcher", "infrastructure-alerts"),
               ("medic", "incidents", "medic", "infrastructure-alerts"),
               ("security", "findings", "security", "security-alerts"),
               ("access_gaps", "gaps", "iam", "security-alerts"))
    for namespace, field, aid, channel in sources:
        items = o.kv_get(namespace, {}).get(field) or {}
        for sid, item in items.items():
            if field == "alerts" and not item.get("fired") and not item.get("msg"):
                continue
            key = track_source(namespace, sid, item, aid, channel)
            state = item.get("state")
            if (state in ("fixed", "resolved") and item.get("fix")) or (state == "resolved" and item.get("resolution")):
                resolve(key, "resolved", json.dumps(item.get("fix") or item.get("resolution")))
            elif state == "resolved" and (item.get("resolved") or item.get("resolution_evidence")):
                resolve(key, "resolved", f"Condition cleared: {item.get('resolution_evidence') or item['resolved']}")
            elif field == "incidents":
                evidence = _later_incident_evidence(item, items)
                if evidence:
                    resolve(key, "resolved", evidence)


def compliance():
    with contextlib.closing(_db()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT key,agent,title,severity,created,due,status,discord_id,gitlab_iid,sync_error,acknowledged FROM work_items "
            "WHERE status NOT IN ('done','fixed','resolved','ok','rejected','expired') OR sync_error IS NOT NULL")]
    issues = []
    for r in rows:
        reasons = []
        if not r["discord_id"]:
            reasons.append("missing Discord evidence")
        if not r["gitlab_iid"]:
            reasons.append("missing GitLab work item")
        if r["sync_error"]:
            reasons.append(r["sync_error"])
        if r["status"] not in TERMINAL and o.parse_iso(r["due"]) < o.utcnow():
            reasons.append("resolution objective overdue")
        ack_due = o.parse_iso(r["created"]) + dt.timedelta(minutes=ACK_MINUTES[r["severity"]])
        if (not r["acknowledged"] and ack_due < o.utcnow()) or (r["acknowledged"] and o.parse_iso(r["acknowledged"]) > ack_due):
            reasons.append("acknowledgement objective missed")
        if reasons:
            issues.append({**r, "problems": reasons})
    issues.sort(key=lambda x: (list(RESOLUTION_HOURS).index(x["severity"]), x["due"], x["key"]))
    return {"top5": issues[:5], "total": len(issues), "resolution_hours": RESOLUTION_HOURS,
            "acknowledgement_minutes": ACK_MINUTES, "policy": "Default homelab objectives; no automatic escalation of privileges."}


def delivery():
    with contextlib.closing(_db()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT key,agent,title,severity,status,created,due,gitlab_iid,gitlab_url,sync_error "
            "FROM work_items WHERE status NOT IN ('done','fixed','resolved','ok','rejected','expired') "
            "ORDER BY due,key")]
    rows = [r for r in rows if not private(r["agent"]) and participates(r["agent"])]
    return {"open_count": len(rows), "work_items": rows[:100], "lead_status": brief("boss"),
            "patch_delivery": {"accountable_lead": "medic", "delivery_owner": "builder",
                               "executor": "sre-agent.service", "approval": "owner only"},
            "scope": "Recorded Roundtable work items, not a complete project catalog or a fresh SRE backlog probe. "
                     "Due dates are service objectives, not project delivery commitments. Propose milestones and "
                     "dependencies for owner confirmation; never invent unrecorded progress or approvals."}


def finops():
    with contextlib.closing(o.db()) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT COALESCE(NULLIF(model,''),'deterministic') model, COUNT(*) runs,SUM(calls) calls,"
            "SUM(input_tokens) input,SUM(cached_tokens) cached,SUM(output_tokens) output,"
            "SUM(cost_usd) cost FROM runs WHERE started>=? GROUP BY model ORDER BY cost DESC",
            (o.month_start_utc(),))]
        agents = [dict(r) for r in con.execute(
            "SELECT agent,COUNT(*) runs,SUM(cost_usd) cost FROM runs WHERE started>=? "
            "GROUP BY agent ORDER BY cost DESC LIMIT 10", (o.month_start_utc(),))]
    spent, cap = o.month_spend(), o.budget_limit()
    now = o.local()
    elapsed = max((now - now.replace(day=1, hour=0, minute=0, second=0)).total_seconds() / 86400, 0.5)
    forecast = spent / elapsed * calendar.monthrange(now.year, now.month)[1]
    recommendations = ["Keep deterministic checks deterministic; use smaller models for routing/summary "
                       "and larger models only for complex diagnosis or reviewed plans.",
                       "Evaluate quality before model changes; never change budgets/models automatically."]
    if forecast > cap:
        recommendations.insert(0, "Current linear run rate exceeds the cap; review optional schedules and long prompts.")
    return {"as_of": o.iso(), "spend": spent, "cap": cap, "forecast": round(forecast, 2),
            "models": rows, "top_agents": agents, "recommendations": recommendations,
            "scope": "Recorded Roundtable API ledger only; not utility bills, cloud invoices or unrecorded API clients. "
                     "Mixed-model runs are labeled as recorded, not apportioned per model."}


def require_approval(agent=None):
    raw = os.environ.get("OLYMPUS_APPROVAL_ID", "")
    if not raw.isdigit():
        raise PermissionError("This change must be launched by the owner-approved queue")
    approval = o.get_approval(int(raw))
    if not approval or approval["status"] != "running" or (agent and approval["agent"] != agent):
        raise PermissionError("Approval is not running for this agent")
    item = sync_item(f"approval:{approval['id']}")
    if not item["discord_id"] or not item["gitlab_iid"]:
        raise PermissionError("Change blocked: Discord/GitLab audit is incomplete")
    current = o.get_approval(approval["id"])
    if current["status"] != "running" or current["payload"] != approval["payload"] or current["command"] != approval["command"]:
        raise PermissionError("Approval changed while audit evidence was being verified")
    return current
