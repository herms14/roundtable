#!/usr/bin/env python3
"""Roundtable core - shared library for the Roundtable agent team on codex-agent-lxc.

Agent registry, SQLite run/token ledger, monthly budget guard, OpenAI Responses + Codex wrappers,
Discord (Sentinel bot) helpers and the owner-approval queue. Python stdlib only.

CLI:
  olympus_core.py record --agent scribe --task "..." [--status ok] [--summary ..] [--model m --codex-jsonl FILE]
  olympus_core.py spend            # month-to-date spend
  olympus_core.py init             # create DB / dirs
"""
import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = HOME / ".olympus"
DB_FILE = DATA_DIR / "olympus.db"
RUNS_DIR = DATA_DIR / "runs"
LOG_DIR = DATA_DIR / "logs"
REGISTRY_FILE = CODE_DIR / "agents.json"
HOSTS_FILE = HOME / ".ssh" / "sre-hosts.txt"
TZ = ZoneInfo("Asia/Manila")
OWNER_ID = os.environ.get("DISCORD_OWNER_ID", "000000000000000000")  # owner's Discord user id (host config)
DISCORD_API = "https://discord.com/api/v10"
OPENAI_URL = "https://api.openai.com/v1/responses"
CODEX_SLOTS = 2

# USD per 1M tokens: (input, cached input, output). Output includes reasoning tokens.
PRICES = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-4o-mini-tts": (0.60, 0.60, 12.00),  # recorded speech (output = audio tokens)
}
WEB_SEARCH_FEE = 0.01  # USD per web_search tool call

# Discord channel ids live on the host (~/.olympus/discord_channels.json, {"name": "id", ...}); not committed
CHANNEL_NAMES = ("general", "ideas-and-todos", "announcements", "project-management", "homelab-infrastructure",
                 "infrastructure-alerts", "roundtable-hold")
try:
    CHANNELS = json.loads((DATA_DIR / "discord_channels.json").read_text())
except (OSError, ValueError):
    CHANNELS = {n: "0" for n in CHANNEL_NAMES}
CHANNELS.setdefault("olympus", CHANNELS.get("roundtable-hold", "0"))  # old name of #roundtable-hold
PORTAL_URL = os.environ.get("ROUNDTABLE_PORTAL_URL", "https://roundtable.hrmsmrflrii.xyz")
OWNER_TITLE = "Master Hermes"
OWNER_STYLE = (f'Always address the owner, Hermes, as "{OWNER_TITLE}" (never just "Hermes", "Tarnished" or "user") '
               "whenever you speak to him directly.")


# --------------------------------------------------------------------------- env / time / logging

def load_env(*names):
    """Secrets: this script's scoped values from OpenBao first (Varys's broker), then the env files fill any gap."""
    bao, drift, files = {}, set(), names or (".discord_env", ".codex_env", ".olympus_env", ".gitlab_env")
    if not names:
        with contextlib.suppress(Exception):
            import bao_broker
            bao = bao_broker.load()
            files = bao_broker.env_files(files)
    for n in files:
        p = HOME / n
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            if k.strip() in bao and str(bao[k.strip()]) != v:
                drift.add(k.strip())
            os.environ.setdefault(k.strip(), v)
    if bao:
        with contextlib.suppress(Exception):
            bao_broker.drift(None, drift)


load_env()


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or utcnow()).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def local(t=None):
    return (t or utcnow()).astimezone(TZ)


def log(agent, msg):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{local().strftime('%Y-%m-%d %H:%M:%S')} [{agent}] {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"{agent}.log").open("a") as f:
        f.write(line + "\n")


def S(**props):
    """Strict JSON-schema object (all properties required, no extras) for structured outputs."""
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def A(items):
    return {"type": "array", "items": items}


STR, INT, NUM, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}


def E(*values):
    return {"type": "string", "enum": list(values)}


# --------------------------------------------------------------------------- registry

def registry():
    return json.loads(REGISTRY_FILE.read_text())


def agent_info(agent_id):
    for a in registry()["agents"]:
        if a["id"] == agent_id:
            return a
    return {"id": agent_id, "name": agent_id.title(), "emoji": "🤖", "color": 0x95A5A6, "role": ""}


def load_hosts():
    hosts = {}
    if HOSTS_FILE.exists():
        for line in HOSTS_FILE.read_text().splitlines():
            p = line.strip().split("|")
            if len(p) == 4:
                hosts[p[0]] = {"name": p[0], "ip": p[1], "user": p[2], "type": p[3]}
    return hosts


# --------------------------------------------------------------------------- database / ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, agent TEXT NOT NULL, task TEXT, trigger TEXT, started TEXT, ended TEXT,
  status TEXT, model TEXT, calls INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
  cached_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0,
  web_calls INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, summary TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent, started);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY, agent TEXT, title TEXT, severity TEXT, payload TEXT, command TEXT,
  channel_id TEXT, message_id TEXT, status TEXT, not_before TEXT, created TEXT, decided TEXT,
  finished TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS transcripts(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, agent TEXT NOT NULL, source TEXT, channel TEXT, session TEXT,
  speaker TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS transcripts_ts ON transcripts(ts);
"""


def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def kv_get(k, default=None):
    with contextlib.closing(db()) as con:
        r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(k, v):
    with contextlib.closing(db()) as con, con:
        con.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v)))


def transcript(agent, speaker, text, source="discord", channel="", session=""):
    """Append one conversation turn; transcripts.py exports them to the Obsidian vault. Never raises."""
    if not (text or "").strip():
        return
    try:
        with contextlib.closing(db()) as con, con:
            con.execute("INSERT INTO transcripts(ts, agent, source, channel, session, speaker, text) "
                        "VALUES(?,?,?,?,?,?,?)", (iso(), agent, source, str(channel or ""), str(session or ""),
                                                  speaker, text.strip()[:20000]))
    except Exception as e:  # noqa: BLE001 - logging a transcript must never break a conversation
        log("transcripts", f"write failed: {e!r}")


def price(model, inp, cached, out):
    pin, pc, pout = PRICES.get(model, PRICES["gpt-6.1-sol"])
    return (max(inp - cached, 0) * pin + cached * pc + out * pout) / 1e6


class Run:
    """One unit of agent work in the ledger. Use as a context manager; exceptions mark it 'error'."""

    def __init__(self, agent, task, trigger="schedule"):
        self.agent, self.task, self.trigger = agent, task, trigger
        self.cost, self.models, self.status, self.summary, self.detail = 0.0, [], None, None, None
        with contextlib.closing(db()) as con, con:
            cur = con.execute("INSERT INTO runs(agent, task, trigger, started, status) VALUES(?,?,?,?,?)",
                              (agent, task[:500], trigger, iso(), "running"))
            self.id = cur.lastrowid
        import hierarchy
        if trigger in ("chat", "manual") and not hierarchy.private(agent):
            hierarchy.track(f"run:{self.id}", agent, task, acknowledged=iso())

    def usage(self, model, inp=0, cached=0, out=0, reasoning=0, web_calls=0, calls=1):
        c = price(model, inp, cached, out) + web_calls * WEB_SEARCH_FEE
        self.cost += c
        if model not in self.models:
            self.models.append(model)
        with contextlib.closing(db()) as con, con:
            con.execute("""UPDATE runs SET calls=calls+?, input_tokens=input_tokens+?, cached_tokens=cached_tokens+?,
                           output_tokens=output_tokens+?, reasoning_tokens=reasoning_tokens+?, web_calls=web_calls+?,
                           cost_usd=cost_usd+?, model=? WHERE id=?""",
                        (calls, inp, cached, out, reasoning, web_calls, c, ",".join(self.models), self.id))
        return c

    def finish(self, status="ok", summary=None, detail=None):
        self.status = status
        with contextlib.closing(db()) as con, con:
            con.execute("UPDATE runs SET ended=?, status=?, summary=?, detail=? WHERE id=?",
                        (iso(), status, (summary or "")[:2000], (detail or "")[:20000] or None, self.id))
        import hierarchy
        if not hierarchy.private(self.agent) and self.trigger in ("chat", "manual"):
            hierarchy.resolve(f"run:{self.id}", status, summary or f"Run #{self.id} finished: {status}")

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if self.status is None:
            if et is None:
                self.finish("ok", self.summary, self.detail)
            elif et is BudgetExceeded:
                self.finish("skipped", f"Budget cap reached: {ev}")
                return True
            else:
                self.finish("error", f"{et.__name__}: {ev}")
        return False


def heartbeat(agent, summary=""):
    """Cheap 'last ran' marker for frequent deterministic loops that should not fill the run ledger."""
    kv_set(f"hb:{agent}", {"t": iso(), "summary": summary[:300]})


def record_run(agent, task, status="ok", summary="", model=None, usage=None, trigger="schedule", started=None):
    """One-shot ledger entry for agents that are not built on Run (Blaidd, legacy scripts)."""
    r = Run(agent, task, trigger)
    if started:
        with contextlib.closing(db()) as con, con:
            con.execute("UPDATE runs SET started=? WHERE id=?", (started, r.id))
    if model and usage:
        r.usage(model, usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0),
                usage.get("output_tokens", 0), usage.get("reasoning_output_tokens", 0), calls=usage.get("calls", 1))
    r.finish(status, summary)
    return r


def month_start_utc():
    t = local()
    return iso(t.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def ledger_spend(since=None):
    """USD the run ledger recorded since `since` (default: the start of this month)."""
    with contextlib.closing(db()) as con:
        r = con.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM runs WHERE started>=?",
                        (since or month_start_utc(),)).fetchone()
    return float(r["s"])


def openai_bill():
    """OpenAI's own month-to-date bill {month, usd, at, source} for this month, or None. Written by openai_costs.py
    (Costs API with an admin key, or the figure typed in from the usage page)."""
    b = kv_get("openai_bill") or {}
    return b if b.get("month") == local().strftime("%Y-%m") and b.get("usd") is not None else None


def month_spend():
    """This month's spend: OpenAI's bill plus what the ledger recorded after it was read; the ledger alone (which
    misses Codex CLI use and anything before it started) when no bill is known. Never less than the ledger."""
    ledger = ledger_spend()
    b = openai_bill()
    return max(ledger, float(b["usd"]) + ledger_spend(b["at"])) if b else ledger


def budget_limit():
    return float(os.environ.get("OLYMPUS_BUDGET_USD", kv_get("budget_usd", 70)))


class BudgetExceeded(Exception):
    pass


def check_budget(critical=False):
    spent, cap = month_spend(), budget_limit()
    if spent >= cap and not critical:
        raise BudgetExceeded(f"${spent:.2f} of ${cap:.0f} spent this month")


# --------------------------------------------------------------------------- HTTP

def http(method, url, headers=None, data=None, timeout=30, raw_body=None, retries=3):
    hdrs = dict(headers or {})
    body = raw_body
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                try:
                    return r.status, (json.loads(raw) if raw else None)
                except ValueError:
                    return r.status, raw.decode(errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw.decode(errors="replace")
            if e.code == 429 and attempt < retries - 1:
                wait = payload.get("retry_after", 2) if isinstance(payload, dict) else 2
                time.sleep(float(wait) + 0.5)
                continue
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            return e.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return 0, str(e)
    return 0, "retries exhausted"


def ssh(host, script, timeout=120):
    """Run a bash script on a host alias from ~/.ssh/config via stdin."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "bash -s"],
                           input=script, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


# --------------------------------------------------------------------------- LLM: OpenAI Responses API

def _response_text(d):
    return "".join(c.get("text", "") for o in d.get("output", []) if o.get("type") == "message"
                   for c in o.get("content", []) if c.get("type") == "output_text")


def _access_scan(run, text, source):
    """Strip ACCESS_GAP lines from agent output and log them to the access backlog (access.py)."""
    if not text or "ACCESS_GAP:" not in text:
        return text
    try:
        import access
        return access.scan_text(getattr(run, "agent", "boss"), text, source)[0]
    except Exception as e:  # noqa: BLE001
        log("access", f"scan failed: {e!r}")
        return text


def _access_tip():
    try:
        import access
        return "\n" + access.TIP
    except Exception:  # noqa: BLE001
        return ""


def owner_style_text(agent):
    return OWNER_STYLE


def personality_text(agent):
    a = agent_info(agent)
    p = a.get("personality")
    if not p:
        return ""
    return (f"Stable fictional character texture for {a['name']}: {p['temperament']}. "
            f"Harmless quirks: {p['quirks']}. Delivery: {p['delivery']}\n"
            "These are fictional conversational/design traits, not real human experiences, credentials or memories. "
            "Use a light touch; do not force a joke or quirk into every answer. Never invent off-screen activities. "
            "Clumsiness is cosmetic only: commands, evidence, approvals and access remain precise. "
            "During incidents be calm and direct; skip banter. Follow the agent's configured language and form of address. "
            "Personality grants no tools, permissions or reporting changes. Structured/tool outputs must follow "
            "their schema exactly, with no banter or extra fields.")


def llm(run, prompt, model="gpt-6-luna", schema=None, effort=None, web=False, instructions=None,
        critical=False, timeout=240, max_output_tokens=None, tips=True, owner_style=True, images=None):
    """Single Responses API call. Returns parsed JSON when `schema` is given, else text. Usage goes to `run`.
    Text answers may end with ACCESS_GAP lines; they are logged to the access backlog and removed."""
    check_budget(critical)
    # owner_style=False: personas with their own form of address
    style = (owner_style_text(getattr(run, "agent", None)) if owner_style else "") + ("" if schema or not tips else _access_tip())
    body = {"model": model, "input": prompt, "store": False, "instructions": style}
    if images:  # data: URLs or https URLs of photos
        body["input"] = [{"role": "user", "content": [{"type": "input_text", "text": prompt}] +
                          [{"type": "input_image", "image_url": u} for u in images]}]
    import hierarchy
    if getattr(run, "agent", None) in {a["id"] for a in registry()["agents"]}:
        style += "\n" + personality_text(run.agent) + "\n" + hierarchy.policy_text(run.agent)
    body["instructions"] = style
    if instructions:
        body["instructions"] = instructions + "\n" + style
    if schema:
        body["text"] = {"format": {"type": "json_schema", "name": "result", "strict": True, "schema": schema}}
    if effort:
        body["reasoning"] = {"effort": effort}
    if web:
        body["tools"] = [{"type": "web_search"}]
    if max_output_tokens:
        body["max_output_tokens"] = max_output_tokens
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
    code, d = http("POST", OPENAI_URL, {"Authorization": f"Bearer {key}"}, body, timeout=timeout)
    if code != 200 or not isinstance(d, dict):
        raise RuntimeError(f"OpenAI HTTP {code}: {str(d)[:300]}")
    u = d.get("usage") or {}
    web_calls = sum(1 for o in d.get("output", []) if o.get("type") == "web_search_call")
    run.usage(model, u.get("input_tokens", 0), (u.get("input_tokens_details") or {}).get("cached_tokens", 0),
              u.get("output_tokens", 0), (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0), web_calls)
    text = _response_text(d)
    if schema:
        return json.loads(text)
    return _access_scan(run, text, "llm")


# --------------------------------------------------------------------------- LLM: Codex CLI (tool-using sessions)

@contextlib.contextmanager
def codex_slot(wait=3600):
    """At most CODEX_SLOTS concurrent Codex sessions across all agents on this LXC."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait
    while True:
        for i in range(CODEX_SLOTS):
            f = open(DATA_DIR / f"codex-slot-{i}.lock", "w")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                continue
            try:
                yield i
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()
            return
        if time.time() > deadline:
            raise TimeoutError("no free Codex slot")
        time.sleep(5)


def codex_usage(transcript):
    """Sum token usage from a `codex exec --json` transcript."""
    tot = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "calls": 0}
    try:
        lines = Path(transcript).read_text(errors="replace").splitlines()
    except OSError:
        return tot
    for line in lines:
        if '"turn.completed"' not in line:
            continue
        try:
            u = json.loads(line).get("usage") or {}
        except ValueError:
            continue
        tot["calls"] += 1
        for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
            tot[k] += int(u.get(k) or 0)
    return tot


# Token discipline for Codex sessions: every command's output is re-sent to the model on each later step, so one
# unfiltered API listing used to cost hundreds of thousands of tokens (2026-10-10 sessions reached 0.4-1.3M input).
CHEAP_MODEL, STRONG_MODEL = "gpt-6-luna", "gpt-6.1-sol"
CODEX_TOOL_OUTPUT_TOKENS = int(os.environ.get("OLYMPUS_CODEX_TOOL_OUTPUT_TOKENS", "4000"))
CODEX_COMPACT_TOKENS = int(os.environ.get("OLYMPUS_CODEX_COMPACT_TOKENS", "120000"))
CODEX_TOKEN_BUDGET = int(os.environ.get("OLYMPUS_CODEX_TOKEN_BUDGET", "300000"))
OUTPUT_RULES = """Token discipline (each command's output is re-sent to the model on every later step):
- Filter before printing: grep, jq with only the needed fields, awk, head -n 80 / tail -n 80; docker logs --tail 200;
  journalctl -n 200 --no-pager; for APIs select fields (jq '[.items[] | {name, status}]') instead of whole listings.
- Never cat large files or dump full JSON; check size first (wc -c) and read only the relevant part.
- Stop investigating as soon as the evidence is sufficient."""
BUDGET_STOP_TEXT = ("⚠️ I stopped this investigation: it was about to use more tokens than the per-question budget "
                    "allows. Please ask a narrower question (one host, service or symptom) and I will dig in.")
ESCALATE_RULE = ("\nIf you cannot answer confidently with the evidence you can gather, reply with exactly the single word "
                 "ESCALATE and nothing else.")


def _codex_estimate(transcript, base_tokens):
    """Estimated cumulative input tokens so far: the context (prompt + capped outputs) is re-sent at every step.
    Calibrated on 23 real sessions (median real/estimate 1.14)."""
    ctx = est = base_tokens
    try:
        lines = Path(transcript).read_text(errors="replace").splitlines()
    except OSError:
        return 0
    for line in lines:
        if '"item.completed"' not in line:
            continue
        try:
            it = json.loads(line).get("item") or {}
        except ValueError:
            continue
        if it.get("type") in ("command_execution", "agent_message", "reasoning"):
            ctx += min(len(it.get("aggregated_output") or it.get("text") or ""), CODEX_TOOL_OUTPUT_TOKENS * 4) / 4 + 60
            est += ctx
    return int(est)


def codex(run, prompt, model="gpt-6-luna", effort=None, schema=None, sandbox="read-only", cwd=None,
          timeout=1800, critical=False, tag=None, owner_style=True, token_budget=None):
    """One Codex session. sandbox: read-only | workspace-write | danger (needed for ssh/network).
    Returns (result, transcript_path); result is parsed JSON when `schema` is given.
    token_budget: estimated input-token ceiling; the session is stopped (result None) when exceeded. Defaults to
    OLYMPUS_CODEX_TOKEN_BUDGET (x1.5 when critical). Pass 0 for sessions that CHANGE systems, which must never be
    killed half-way (they still get the output cap and auto-compaction)."""
    check_budget(critical)
    budget = (int(CODEX_TOKEN_BUDGET * (1.5 if critical else 1)) if token_budget is None else token_budget)
    run.budget_stopped = False
    prompt += "\n\n" + OUTPUT_RULES
    import hierarchy
    if run.agent in {a["id"] for a in registry()["agents"]}:
        prompt += "\n\n" + personality_text(run.agent) + "\n" + hierarchy.policy_text(run.agent)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{run.agent}-{tag or 'task'}-{local().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    out_file, transcript = RUNS_DIR / f"{stem}.out", RUNS_DIR / f"{stem}.jsonl"
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "-m", model,
           "-c", f"tool_output_token_limit={CODEX_TOOL_OUTPUT_TOKENS}",
           "-c", f"model_auto_compact_token_limit={CODEX_COMPACT_TOKENS}"]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    if sandbox == "danger":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd += ["--sandbox", sandbox]
    if schema:
        sf = RUNS_DIR / f"{stem}.schema.json"
        sf.write_text(json.dumps(schema))
        cmd += ["--output-schema", str(sf)]
    cmd += ["-o", str(out_file), "-C", str(cwd or RUNS_DIR),
            prompt + ("\n\n" + owner_style_text(run.agent) + _access_tip() if owner_style else "")]
    base_tokens = len(cmd[-1]) // 4 + 4000
    with codex_slot(), transcript.open("w") as tf:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=tf, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while p.poll() is None:
            if time.time() > deadline:
                p.terminate()
                break
            if budget and _codex_estimate(transcript, base_tokens) > budget:
                run.budget_stopped = True
                p.terminate()
                break
            time.sleep(2)
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
    if run.budget_stopped:
        est = _codex_estimate(transcript, base_tokens)
        log(run.agent, f"codex {tag or 'task'} stopped: ~{est:,} input tokens exceeded the {budget:,}-token budget")
        # the killed session never reports usage, but the tokens were spent: record the estimate (~80% cached)
        run.usage(model, est, int(est * .8), 0, calls=1)
        return None, transcript
    u = codex_usage(transcript)
    if u["calls"]:
        run.usage(model, u["input_tokens"], u["cached_input_tokens"], u["output_tokens"],
                  u["reasoning_output_tokens"], calls=u["calls"])
    try:
        text = out_file.read_text()
    except OSError:
        text = None
    if schema:
        _access_scan(run, _transcript_messages(transcript), "codex")
        if text is None:
            return None, transcript
        try:
            return json.loads(text), transcript
        except ValueError:
            return None, transcript
    if text is None:
        return None, transcript
    return _access_scan(run, text.strip(), "codex"), transcript


def _transcript_messages(transcript):
    """Agent message texts from a codex --json transcript (where schema runs put ACCESS_GAP lines)."""
    out = []
    try:
        for line in Path(transcript).read_text(errors="replace").splitlines():
            if "ACCESS_GAP" not in line or '"agent_message"' not in line:
                continue
            try:
                it = json.loads(line).get("item") or {}
            except ValueError:
                continue
            if it.get("type") == "agent_message":
                out.append(it.get("text") or "")
    except OSError:
        pass
    return "\n".join(out)


# --------------------------------------------------------------------------- Discord (as the Sentinel bot)

def dc(method, path, data=None, raw_body=None, headers=None):
    h = {"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}",
         "User-Agent": "DiscordBot (https://hrmsmrflrii.xyz, 1.0) olympus"}
    h.update(headers or {})
    return http(method, DISCORD_API + path, h, data, raw_body=raw_body)


def channel_id(name, create_topic=None):
    """Resolve a text channel by name (cached); optionally create it next to #homelab-infrastructure."""
    if name.isdigit():
        return name
    if name in CHANNELS:
        return CHANNELS[name]
    cache = kv_get("channels", {})
    if name in cache:
        return cache[name]
    gid = os.environ["DISCORD_GUILD_ID"]
    code, chans = dc("GET", f"/guilds/{gid}/channels")
    if code != 200:
        return CHANNELS["homelab-infrastructure"]
    by_name = {c["name"]: c for c in chans if c.get("type") == 0}
    if name in by_name:
        cid = by_name[name]["id"]
    elif create_topic is not None:
        payload = {"name": name, "type": 0, "topic": create_topic}
        parent = next((c.get("parent_id") for c in chans if c["id"] == CHANNELS["homelab-infrastructure"]), None)
        if parent:
            payload["parent_id"] = parent
        code, ch = dc("POST", f"/guilds/{gid}/channels", payload)
        if code not in (200, 201):
            return CHANNELS["homelab-infrastructure"]  # not cached: picked up once the channel exists
        cid = ch["id"]
    else:
        return CHANNELS["homelab-infrastructure"]
    cache[name] = cid
    kv_set("channels", cache)
    return cid


def private_channel(name, topic=None):
    """Resolve/create an owner-and-bot-only channel; never fall back to a shared channel."""
    gid = os.environ["DISCORD_GUILD_ID"]
    code, bot = dc("GET", "/users/@me")
    if code != 200 or not isinstance(bot, dict):
        raise RuntimeError("Cannot verify the bot identity for a private channel")
    code, channels = dc("GET", f"/guilds/{gid}/channels")
    if code != 200 or not isinstance(channels, list):
        raise RuntimeError("Cannot verify private Discord channel permissions")
    ch = next((c for c in channels if c.get("name") == name and c.get("type") == 0), None)
    if not ch:
        code, ch = dc("POST", f"/guilds/{gid}/channels", {
            "name": name, "type": 0, "topic": topic or "Private conversations; owner and bot only.",
            "permission_overwrites": [
                {"id": gid, "type": 0, "deny": "1024", "allow": "0"},
                {"id": OWNER_ID, "type": 1, "allow": "68608", "deny": "0"},
                {"id": bot["id"], "type": 1, "allow": "117760", "deny": "0"},
            ],
        })
        if code not in (200, 201) or not isinstance(ch, dict):
            raise RuntimeError("Private Discord channel creation failed; no shared fallback")
    permissions = ch.get("permission_overwrites", [])
    everyone = next((p for p in permissions if p["id"] == gid and p["type"] == 0), {})
    owner = next((p for p in permissions if p["id"] == OWNER_ID and p["type"] == 1), {})
    if not int(everyone.get("deny", 0)) & 1024 or not int(owner.get("allow", 0)) & 1024:
        raise PermissionError("Private channel lacks explicit owner-only visibility; repair it manually")
    if any(int(p.get("allow", 0)) & 1024 and p["id"] not in (OWNER_ID, bot["id"]) for p in permissions):
        raise PermissionError("Private channel grants other members/roles visibility; no private text will be posted")
    return ch["id"]


def _trim(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def embed(agent_id, title, description="", fields=None, color=None, footer=None, url=None):
    a = agent_info(agent_id)
    e = {"author": {"name": _trim(f"{a['emoji']} {a['name']} · {a.get('role', '')}", 250)},
         "title": _trim(title, 250), "description": _trim(description, 3900),
         "color": color if color is not None else int(a.get("color", 0x95A5A6)),
         "timestamp": iso(), "footer": {"text": _trim(footer or "Roundtable · codex-agent-lxc", 2000)}}
    # Discord can't reach the LAN-only portal, so prefer the avatar uploaded as a guild emoji (avatar_emojis.py)
    em = (kv_get("avatar_emojis", {}) or {}).get(agent_id)
    if em:
        e["author"]["icon_url"] = f"https://cdn.discordapp.com/emojis/{em['id']}.png"
    elif a.get("avatar"):
        e["author"]["icon_url"] = PORTAL_URL + a["avatar"]
    if url:
        e["url"] = url
    if fields:
        e["fields"] = [{"name": _trim(n, 250) or "\u200b", "value": _trim(v, 1020) or "\u200b", "inline": bool(i)}
                       for n, v, i in fields[:25]]
    return e


SEV_COLORS = {"critical": 0xE74C3C, "high": 0xE67E22, "medium": 0xF1C40F, "low": 0x3498DB, "ok": 0x2ECC71,
              "info": 0x95A5A6}


def post(channel, content=None, embeds=None, files=None, reply_to=None, mention_owner=False):
    """Post to a channel (name or id). files: list of (filename, bytes). Returns message id or None."""
    cid = channel_id(channel)
    payload = {"allowed_mentions": {"users": [OWNER_ID] if mention_owner else []}}
    if content:
        payload["content"] = _trim(content, 1990)
    if embeds:
        payload["embeds"] = embeds[:10]
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
    if files:
        boundary = uuid.uuid4().hex
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                 f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n".encode()]
        for i, (fname, data) in enumerate(files[:10]):
            if isinstance(data, str):
                data = data.encode()
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[{i}]\"; "
                         f"filename=\"{fname}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        code, msg = dc("POST", f"/channels/{cid}/messages", raw_body=b"".join(parts),
                       headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        code, msg = dc("POST", f"/channels/{cid}/messages", payload)
    if code not in (200, 201):
        print(f"Discord post failed HTTP {code}: {str(msg)[:200]}", flush=True)
        return None
    return msg["id"]


def edit(channel, mid, content=None, embeds=None):
    payload = {}
    if content is not None:
        payload["content"] = _trim(content, 1990)
    if embeds is not None:
        payload["embeds"] = embeds
    return dc("PATCH", f"/channels/{channel_id(channel)}/messages/{mid}", payload)[0] == 200


def react(channel, mid, emoji):
    dc("PUT", f"/channels/{channel_id(channel)}/messages/{mid}/reactions/{urllib.parse.quote(emoji)}/@me")


def reactors(channel, mid, emoji):
    code, users = dc("GET", f"/channels/{channel_id(channel)}/messages/{mid}/reactions/{urllib.parse.quote(emoji)}?limit=100")
    return {u["id"] for u in users} if code == 200 and isinstance(users, list) else set()


def respond(agent_id, channel, title, text, reply_to=None, fields=None, color=None, run=None, filename=None):
    """Post an agent result as an embed; text longer than an embed is attached in full as Markdown."""
    footer = "Roundtable · codex-agent-lxc"
    if run is not None:
        footer = f"Roundtable run #{run.id} · {', '.join(run.models) or 'no LLM'} · {money(run.cost)}"
    files = None
    desc = text or ""
    if len(desc) > 3800:
        files = [(filename or f"{agent_id}-{local().strftime('%Y%m%d-%H%M')}.md", desc)]
        desc = desc[:3500].rsplit("\n", 1)[0] + "\n\n… *(full report attached)*"
    if reply_to:  # a reply to one of Master Hermes's messages is part of a conversation
        transcript(agent_id, agent_info(agent_id).get("name", agent_id), text, "discord", channel, reply_to)
    return post(channel, embeds=[embed(agent_id, title, desc, fields, color=color, footer=footer)],
                files=files, reply_to=reply_to)


def chunks(text, n=1900):
    out, cur = [], ""
    for line in (text or "").splitlines(keepends=True):
        if len(cur) + len(line) > n:
            out.append(cur)
            cur = ""
        while len(line) > n:
            out.append(line[:n])
            line = line[n:]
        cur += line
    if cur.strip():
        out.append(cur)
    return out


# --------------------------------------------------------------------------- approvals

def next_window(start_h=2, end_h=5):
    """Start of the next local maintenance window (now if inside it)."""
    t = local()
    if start_h <= t.hour < end_h:
        return utcnow()
    nxt = t.replace(hour=start_h, minute=0, second=0, microsecond=0)
    if t.hour >= start_h:
        nxt += dt.timedelta(days=1)
    return nxt.astimezone(dt.timezone.utc)


def request_approval(agent_id, title, description, command, channel, severity="medium", fields=None,
                     payload=None, not_before=None, reply_to=None, rfc=None):
    """Post an approval embed (✅/❌, owner only). Gideon executes `command` (argv list) once approved.
    Normal (major) changes carry an RFC (rfc.py): `rfc` = {what, change, recovery, risk} from the performing agent,
    otherwise Ranni drafts it; ✅ approves the RFC and Ranni closes it when the task ends. Returns the approval id."""
    import rfc as rfc_mod
    rid = None
    if rfc_mod.required(agent_id, severity, command, rfc):
        rid, info = rfc_mod.file(agent_id, title, description, command, payload, rfc)
        payload = {**(payload or {}), "rfc": rid}
        fields = list(fields or []) + rfc_mod.embed_fields(rid, info)
        title = f"{title} · {rid}"
        if len(description) > 1800:  # Discord caps a whole embed at 6000 characters; the full text is in the RFC note
            description = description[:1750].rstrip() + f"\n… (full detail in {rid})"
    with contextlib.closing(db()) as con, con:
        aid = con.execute("""INSERT INTO approvals(agent, title, severity, payload, command, channel_id, status,
                             not_before, created) VALUES(?,?,?,?,?,?,?,?,?)""",
                          (agent_id, title, severity, json.dumps(payload or {}), json.dumps(command),
                           channel_id(channel), "pending", iso(not_before) if not_before else None, iso())).lastrowid
    when = ("immediately after approval" if not not_before or not_before <= utcnow()
            else f"in the maintenance window from {local(not_before).strftime('%a %d %b %H:%M')}")
    e = embed(agent_id, f"Approval #{aid} · {title}", description, fields,
              color=SEV_COLORS.get(severity, SEV_COLORS["medium"]),
              footer=f"React ✅ to approve or ❌ to reject (owner only) · runs {when} · Roundtable approval #{aid}")
    mid = post(channel, content=f"<@{OWNER_ID}> approval needed", embeds=[e], reply_to=reply_to, mention_owner=True)
    if mid:
        react(channel, mid, "✅")
        time.sleep(0.4)
        react(channel, mid, "❌")
    with contextlib.closing(db()) as con, con:
        con.execute("UPDATE approvals SET message_id=?, status=? WHERE id=?",
                    (mid, "pending" if mid else "failed", aid))
    if not mid:
        raise RuntimeError("Approval was not posted to Discord; change remains blocked")
    if rid:
        rfc_mod.link_approval(rid, aid)
    import hierarchy
    hierarchy.track(f"approval:{aid}", agent_id, title, severity, channel_id(channel), mid)
    try:
        hierarchy.sync_item(f"approval:{aid}")
    except (RuntimeError, PermissionError) as error:
        set_approval(aid, status="failed", result=f"Governance blocked change: {error}")
        raise
    return aid


def get_approval(aid):
    with contextlib.closing(db()) as con:
        r = con.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["payload"] = json.loads(d["payload"] or "{}")
    d["command"] = json.loads(d["command"] or "[]")
    return d


def set_approval(aid, **cols):
    keys = ", ".join(f"{k}=?" for k in cols)
    with contextlib.closing(db()) as con, con:
        con.execute(f"UPDATE approvals SET {keys} WHERE id=?", (*cols.values(), aid))
    if cols.get("finished") and cols.get("status") in ("done", "failed", "partial"):
        ap = get_approval(aid) or {}
        annotate(f"Approval #{aid} {cols['status']}: {ap.get('title', '')}", [ap.get("agent") or "roundtable",
                 "approval", cols["status"]], agent=ap.get("agent"))


def annotate(text, tags=(), agent=None):
    """Mark a change on the Grafana graphs (org annotation tagged 'roundtable'), so a dip next to a deploy or
    reboot has its explanation beside it. Best effort: never raises, skips personal agents."""
    try:
        if agent:
            import hierarchy
            if hierarchy.private(agent):
                return
        user, pw = os.environ.get("GRAFANA_USER"), os.environ.get("GRAFANA_PASS")
        if not user:
            return
        auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
        http("POST", os.environ.get("GRAFANA_URL", "http://192.168.40.13:3030").rstrip("/") + "/api/annotations",
             {"Authorization": f"Basic {auth}"}, {"time": int(time.time() * 1000), "text": text[:500],
                                                   "tags": ["roundtable", *[t for t in tags if t]]},
             timeout=5, retries=1)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- misc helpers

def md_table(rows, headers):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |")
    return "\n".join(out)


def money(x):
    return f"${x:,.4f}" if x < 0.1 else f"${x:,.2f}"


def strip_secrets(text):
    text = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key)(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", text or "")
    return re.sub(r"\b(sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_\-]{20,})\b", "[REDACTED]", text)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--agent", required=True)
    r.add_argument("--task", required=True)
    r.add_argument("--status", default="ok")
    r.add_argument("--summary", default="")
    r.add_argument("--trigger", default="schedule")
    r.add_argument("--model")
    r.add_argument("--codex-jsonl", action="append", default=[])
    r.add_argument("--started")
    sub.add_parser("spend")
    sub.add_parser("init")
    a = ap.parse_args()
    if a.cmd == "record":
        usage = None
        for f in a.codex_jsonl:
            u = codex_usage(f)
            usage = u if usage is None else {k: usage[k] + u[k] for k in u}
        run = record_run(a.agent, a.task, a.status, a.summary, a.model, usage, a.trigger, a.started)
        print(f"run {run.id} cost {money(run.cost)}")
    elif a.cmd == "spend":
        print(f"{money(month_spend())} of ${budget_limit():.0f}")
    else:
        db().close()
        for d in (RUNS_DIR, LOG_DIR):
            d.mkdir(parents=True, exist_ok=True)
        print(f"initialised {DB_FILE}")


if __name__ == "__main__":
    main()


def codex_tiered(run, prompt, escalate, cheap_effort="medium", strong_effort="medium", **kw):
    """Try the cheap model first; re-run on the strong model only when escalate(result) is true.
    Returns (result, transcript, model_used). A budget-stopped cheap run is not escalated (it would be as large)."""
    res, tr = codex(run, prompt, model=CHEAP_MODEL, effort=cheap_effort, **kw)
    if run.budget_stopped or not escalate(res):
        return res, tr, CHEAP_MODEL
    log(run.agent, f"escalating {kw.get('tag') or 'task'} from {CHEAP_MODEL} to {STRONG_MODEL}")
    res, tr = codex(run, prompt, model=STRONG_MODEL, effort=strong_effort, **kw)
    return res, tr, STRONG_MODEL


def needs_escalation(text):
    """For free-text answers produced with ESCALATE_RULE appended to the prompt."""
    return not text or text.strip().upper().startswith("ESCALATE")
