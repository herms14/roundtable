#!/usr/bin/env python3
"""Varys - keeper of the homelab's secrets (OpenBao on openbao-lxc, CT213). Knows where every secret is kept and who
may read it; never reads a value and has no authority to use one.

  keeper.py plan                                     # which entry script needs which scope (code analysis, no secrets)
  keeper.py status                                   # store, scopes, tokens, broker use (metadata only)
  keeper.py audit                                    # daily: post problems; Mondays the full weekly report
  keeper.py request --script netops --scope dns --reason "..."   # ask the owner (Discord ✅/❌)
  keeper.py grant --script netops --scope dns        # run by Gideon only after that ✅ (checks the approval)
  keeper.py revoke --script netops [--scope dns]     # drop a scope, or (no --scope) revoke the script's token
  keeper.py rotated OPENAI_API_KEY                   # owner rotated a key (in OpenBao): restart its reminder

What Varys's OpenBao token (policies agent-keeper + keeper) may do: list/read KV metadata (versions, times; never
data), read and write agent-* policies except its own, look up and revoke tokens by accessor. Its process loads only
.discord_env (to post), never the other env files. The audit numbers come from a summary CT213 builds from its audit
log (forced-command SSH user bao-report, key ~/.ssh/keeper_ed25519). Values in the audit log are HMACed by OpenBao.
"""
import argparse
import ast
import contextlib
import datetime as dt
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import olympus_core as o

AGENT = "keeper"
CHANNEL = "security-alerts"
BAO = "https://192.168.40.39:8200"
BAO_DIR = o.HOME / ".olympus" / "bao"
PLAN_FILE = o.CODE_DIR / "secret_scopes.json"
GRANTS_FILE = BAO_DIR / "grants.json"
REPORT_SSH = ["ssh", "-i", str(o.HOME / ".ssh" / "keeper_ed25519"), "-o", "BatchMode=yes",
              "-o", "IdentitiesOnly=yes", "bao-report@192.168.40.39"]
FIXED = {"keeper": ["discord", "gitlab"]}   # Varys: post and track approvals, nothing it manages
TOKEN_WARN_DAYS = 21


# --------------------------------------------------------------------------- plan (code only)

def scope_plan():
    return json.loads(PLAN_FILE.read_text(encoding="utf-8"))


def plan():
    """{entry_script: [scopes]} from each script's import closure, plus keys the plan does not cover."""
    sp = scope_plan()["scopes"]
    key_scope = {k: s for s, v in sp.items() for k in v["keys"]}
    mods = {p.stem: p for p in o.CODE_DIR.glob("*.py")}
    text, top, lazy = {}, {}, {}
    for m, p in mods.items():
        text[m] = p.read_text(encoding="utf-8", errors="replace")
        top[m], lazy[m] = set(), set()
        with contextlib.suppress(SyntaxError):
            tree = ast.parse(text[m])
            body = {id(n) for n in tree.body}
            for n in ast.walk(tree):
                names = ({a.name.split(".")[0] for a in n.names} if isinstance(n, ast.Import) else
                         {n.module.split(".")[0]} if isinstance(n, ast.ImportFrom) and n.module and not n.level
                         else set())
                (top if id(n) in body else lazy)[m].update(names & set(mods))
    uses = {m: {k for k in key_scope if re.search(rf"\b{k}\b", t)} for m, t in text.items()}

    def closure(start):   # module-level imports, transitively
        seen, todo = set(), list(start)
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo += top[x]
        return seen

    out = {}
    for m, t in text.items():
        if not re.search(r"__name__\s*==\s*['\"]__main__['\"]", t) or m in ("bao_broker",):
            continue
        seen = closure([m])
        # A function-level import (olympus_core -> access for ACCESS_GAP lines, -> hierarchy for approvals) adds
        # that module's own keys, not what it imports in turn; deeper paths use the env-file fallback or an
        # owner-approved grant (keeper.py request).
        seen |= {x for y in seen for x in lazy[y]}
        out[m] = sorted({key_scope[k] for x in seen for k in uses[x]})
    out.update(FIXED)
    for script, scopes in grants().items():
        out[script] = sorted(set(out.get(script, [])) | set(scopes))
    env_names = set()
    for m, t in text.items():
        env_names |= set(re.findall(r"os\.environ(?:\.get)?\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]", t))
        env_names |= set(re.findall(r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]+)['\"]", t))
    unused = sorted(k for k in key_scope if not any(k in uses[m] for m in uses))
    return out, unused, sorted(env_names)


def policy_text(scopes):
    lines = [f"# Roundtable broker policy (Varys). Read-only KV scopes. Generated {o.iso()}"]
    for s in sorted(set(scopes)):
        lines.append(f'path "{scope_plan()["mount"]}/data/{s}" {{\n  capabilities = ["read"]\n}}')
    return "\n".join(lines) + "\n"


def grants():
    with contextlib.suppress(OSError, ValueError):
        return json.loads(GRANTS_FILE.read_text())
    return {}


# --------------------------------------------------------------------------- OpenBao (metadata only)

def bao(method, path, data=None, token=None):
    tok = token if token is not None else (BAO_DIR / "tokens" / "keeper.token").read_text().strip()
    req = urllib.request.Request(f"{BAO}/v1/{path}", method=method, headers={"X-Vault-Token": tok},
                                 data=json.dumps(data).encode() if data is not None else None)
    ctx = ssl.create_default_context(cafile=str(BAO_DIR / "ca.crt"))
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        with contextlib.suppress(ValueError):
            return e.code, json.loads(e.read() or b"{}")
        return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"errors": [type(e).__name__]}


def health():
    s, d = bao("GET", "sys/health?standbyok=true&sealedcode=200&uninitcode=200", token="")
    return {"reachable": s == 200, "sealed": d.get("sealed"), "initialized": d.get("initialized"),
            "version": d.get("version")}


def scopes_meta():
    mount = scope_plan()["mount"]
    out = {}
    s, d = bao("LIST", f"{mount}/metadata")
    for name in (d.get("data") or {}).get("keys", []) if s == 200 else []:
        s2, m = bao("GET", f"{mount}/metadata/{name}")
        if s2 == 200:
            m = m["data"]
            out[name.rstrip("/")] = {"created": m["created_time"][:10], "updated": m["updated_time"][:10],
                                     "version": m["current_version"], "meta": m.get("custom_metadata") or {}}
    return out, s


def tokens():
    out = {}
    with contextlib.suppress(OSError, ValueError):
        acc = json.loads((BAO_DIR / "accessors.json").read_text())
        for script, a in sorted(acc.items()):
            s, d = bao("POST", "auth/token/lookup-accessor", {"accessor": a})
            if s == 200:
                ttl = d["data"].get("ttl") or 0
                out[script] = {"ok": True, "days": round(ttl / 86400, 1), "policies": d["data"].get("policies", [])}
            else:
                out[script] = {"ok": False, "days": 0, "error": f"HTTP {s}"}
    return out


def broker_status():
    out = {}
    for p in sorted((BAO_DIR / "status").glob("*.json")):
        with contextlib.suppress(OSError, ValueError):
            out[p.stem] = json.loads(p.read_text())
    return out


def audit_summary():
    try:
        r = subprocess.run(REPORT_SSH, capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout) if r.returncode == 0 else {"error": (r.stderr or "ssh failed")[:200]}
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return {"error": type(e).__name__}


def rotation(meta):
    """[(key, scope, age_days, rotate_days, since)] for keys past their rotation interval."""
    sp, due = scope_plan()["scopes"], []
    rotated = o.kv_get("keeper:rotated", {}) or {}
    today = o.local().date()
    for scope, info in sp.items():
        every = int(info.get("rotate_days") or 0)
        if not every or scope not in meta:
            continue
        for k in info["keys"]:
            since = rotated.get(k) or meta[scope]["meta"].get("imported") or meta[scope]["created"]
            age = (today - dt.date.fromisoformat(since[:10])).days
            if age >= every:
                due.append((k, scope, age, every, since))
    return due


# --------------------------------------------------------------------------- reports

def collect():
    h = health()
    meta, ms = scopes_meta() if h["reachable"] and not h["sealed"] else ({}, 0)
    return {"health": h, "meta": meta, "meta_status": ms, "tokens": tokens() if meta else {},
            "broker": broker_status(), "audit": audit_summary()}


def problems(c):
    p = []
    h = c["health"]
    if not h["reachable"]:
        p.append("🔴 OpenBao is unreachable (agents are on the env-file fallback)")
    elif h["sealed"]:
        p.append("🔴 OpenBao is sealed (auto-unseal failed; agents are on the env-file fallback)")
    elif c["meta_status"] != 200:
        p.append(f"🟠 Varys cannot list the secret metadata (HTTP {c['meta_status']})")
    for s, t in c["tokens"].items():
        if not t["ok"]:
            p.append(f"🟠 token for `{s}` is gone ({t.get('error')})")
        elif t["days"] < TOKEN_WARN_DAYS:
            p.append(f"🟡 token for `{s}` expires in {t['days']} days (it renews when `{s}` runs)")
    for s, b in c["broker"].items():
        if b.get("drift"):
            p.append(f"🟠 `{s}`: env file differs from OpenBao for {', '.join(b['drift'])} (OpenBao wins)")
        if b.get("ok") is False and time.time() - b.get("time", 0) < 2 * 86400:
            p.append(f"🟠 `{s}` could not read OpenBao: {b.get('error') or 'error'}")
    a = c["audit"]
    if a.get("error"):
        p.append(f"🟡 audit summary unavailable: {a['error']}")
    else:
        seen = o.kv_get("keeper:audit_seen", "") or ""
        denied = [e for e in a.get("denied_events", []) if e["time"] > seen]
        root = [e for e in a.get("root_events", []) if e["time"] > seen]
        if denied:
            who = sorted({f"{e['name']} → {e['path']}" for e in denied})
            p.append(f"🟠 {len(denied)} denied request(s) since the last check: " + "; ".join(who[:6]))
        if root:
            p.append(f"🟡 the root token was used {len(root)} time(s) since the last check "
                     "(expected only for owner/admin setup)")
    return p


def report_fields(c):
    h, meta, tok, br, a = c["health"], c["meta"], c["tokens"], c["broker"], c["audit"]
    sp = scope_plan()["scopes"]
    store = (f"OpenBao {h.get('version') or '?'} · {'sealed' if h['sealed'] else 'unsealed'}" if h["reachable"]
             else "unreachable")
    scopes = "\n".join(f"`{s}` v{m['version']} · {len(sp.get(s, {}).get('keys', []))} keys · updated {m['updated']}"
                       for s, m in sorted(meta.items())) or "none visible"
    due = rotation(meta) if meta else []
    rot = "\n".join(f"`{k}` ({s}) {age} d (every {every} d)" for k, s, age, every, _ in due[:15]) or "nothing due"
    exp = sorted(tok.items(), key=lambda kv: kv[1]["days"])[:5]
    toks = (f"{sum(t['ok'] for t in tok.values())}/{len(tok)} valid · shortest: " +
            ", ".join(f"`{s}` {t['days']} d" for s, t in exp)) if tok else "none"
    used = [s for s, b in br.items() if b.get("ok") and b.get("keys")]
    fb = [s for s, b in br.items() if not b.get("ok")]
    brk = f"{len(used)} scripts read OpenBao" + (f" · fallback/error: {', '.join(fb)}" if fb else "")
    if a.get("error"):
        aud = a["error"]
    else:
        top = ", ".join(f"{n} {c_}" for n, c_ in sorted(a.get("by_name", {}).items(), key=lambda kv: -kv[1])[:6])
        aud = (f"{a.get('requests', 0)} requests in {a.get('days', 7)} d · denied {a.get('denied', 0)} · "
               f"root used {a.get('root', 0)} · clients {', '.join(a.get('remotes', [])) or '-'}\n{top}")
    return [("Store", store, True), ("Tokens", toks, False), ("Scopes", scopes, False),
            ("Rotation due", rot, False), ("Broker", brk, False), ("Audit (7 days)", aud, False)]


def status():
    c = collect()
    for n, v, _ in report_fields(c):
        print(f"== {n}\n{v}")
    for p in problems(c):
        print("!", p)


def audit():
    c = collect()
    probs = problems(c)
    weekly = o.local().weekday() == 0
    if probs or weekly:
        title = "Weekly secrets report" if weekly else "Secrets need attention"
        desc = ("\n".join(probs) if probs else "✅ Nothing needs attention.") + \
               "\n\nI keep the map of who may read what; I never read the values."
        o.post(CHANNEL, embeds=[o.embed(AGENT, title, desc[:3500], report_fields(c),
                                        color=o.SEV_COLORS["high" if any(x[0] == "🔴" for x in probs) else
                                                           "medium" if probs else "ok"])])
    a = c["audit"]
    if not a.get("error"):
        o.kv_set("keeper:audit_seen", a.get("generated", ""))
    summary = f"{len(probs)} problem(s)" + (" · weekly report posted" if weekly else "")
    o.record_run(AGENT, "secrets-audit", status="error" if any(p[0] == "🔴" for p in probs) else "ok", summary=summary)
    print(summary)
    for p in probs:
        print(" ", p)


# --------------------------------------------------------------------------- access requests

def _check(script, scope):
    if not re.fullmatch(r"[a-z0-9_]+", script or "") or script == AGENT:
        raise SystemExit(f"bad script name {script!r} (Varys cannot grant itself)")
    if scope and scope not in scope_plan()["scopes"]:
        raise SystemExit(f"unknown scope {scope!r}; known: {', '.join(scope_plan()['scopes'])}")


def request(script, scope, reason):
    _check(script, scope)
    if scope in plan()[0].get(script, []):
        print(f"{script} already reads {scope}")
        return
    info = scope_plan()["scopes"][scope]
    cmd = ["python3", "-u", str(o.CODE_DIR / "keeper.py"), "grant", "--script", script, "--scope", scope]
    aid = o.request_approval(
        AGENT, f"Secret access: {script} → {scope}",
        f"`{script}.py` asks to read the OpenBao scope **{scope}** ({info.get('about', '')}).\n"
        f"Keys: {', '.join(f'`{k}`' for k in info['keys'])}\nReason: {reason}\n\n"
        "On ✅ Varys adds read access to that one scope to the script's policy. No value is shown to anyone.",
        cmd, CHANNEL, severity="medium",
        fields=[("Script", script, True), ("Scope", scope, True)])
    print(f"approval #{aid} posted")


def _approved(cmd):
    with contextlib.closing(o.db()) as con:
        rows = con.execute("SELECT id, command FROM approvals WHERE status='running' AND agent=?", (AGENT,)).fetchall()
    return next((r["id"] for r in rows if json.loads(r["command"] or "[]")[3:] == cmd[3:]), None)


def _write_policy(script, scopes):
    s, d = bao("PUT", f"sys/policies/acl/agent-{script}", {"policy": policy_text(scopes)})
    if s not in (200, 204):
        raise SystemExit(f"policy write failed: HTTP {s} {d.get('errors')}")
    sf = BAO_DIR / "scopes.json"
    cur = json.loads(sf.read_text())
    cur[script] = sorted(scopes)
    tmp = sf.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1))
    tmp.replace(sf)


def grant(script, scope):
    _check(script, scope)
    cmd = ["python3", "-u", str(o.CODE_DIR / "keeper.py"), "grant", "--script", script, "--scope", scope]
    aid = _approved(cmd)
    if not aid:
        raise SystemExit("no running owner-approved request for this grant; use `keeper.py request` first")
    g = grants()
    g[script] = sorted(set(g.get(script, [])) | {scope})
    GRANTS_FILE.write_text(json.dumps(g, indent=1))
    _write_policy(script, plan()[0].get(script, []))
    o.record_run(AGENT, "secret-grant", summary=f"approval #{aid}: {script} may read {scope}", trigger="approval")
    print(f"granted: {script} reads {scope} (approval #{aid})")


def revoke(script, scope=None):
    _check(script, scope)
    if scope:
        g = grants()
        g[script] = sorted(set(g.get(script, [])) - {scope})
        GRANTS_FILE.write_text(json.dumps(g, indent=1))
        keep = [s for s in json.loads((BAO_DIR / "scopes.json").read_text()).get(script, []) if s != scope]
        _write_policy(script, keep)
        msg = f"{script} no longer reads {scope}"
    else:
        acc = json.loads((BAO_DIR / "accessors.json").read_text()).get(script)
        s, d = bao("POST", "auth/token/revoke-accessor", {"accessor": acc}) if acc else (404, {})
        if s not in (200, 204):
            raise SystemExit(f"revoke failed: HTTP {s}")
        msg = f"token for {script} revoked (it falls back to the env files until bao_setup.py issues a new one)"
    o.post(CHANNEL, embeds=[o.embed(AGENT, "Secret access removed", msg)])
    o.record_run(AGENT, "secret-revoke", summary=msg, trigger="manual")
    print(msg)


def rotated(key):
    if key not in {k for v in scope_plan()["scopes"].values() for k in v["keys"]}:
        raise SystemExit(f"unknown key {key}")
    r = o.kv_get("keeper:rotated", {}) or {}
    r[key] = o.local().date().isoformat()
    o.kv_set("keeper:rotated", r)
    print(f"{key}: rotation recorded {r[key]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    sub.add_parser("status")
    sub.add_parser("audit")
    r = sub.add_parser("request")
    r.add_argument("--script", required=True)
    r.add_argument("--scope", required=True)
    r.add_argument("--reason", required=True)
    for n in ("grant", "revoke"):
        g = sub.add_parser(n)
        g.add_argument("--script", required=True)
        g.add_argument("--scope", required=(n == "grant"))
    k = sub.add_parser("rotated")
    k.add_argument("key")
    a = ap.parse_args()
    if a.cmd == "plan":
        out, unused, env_names = plan()
        for s, sc in sorted(out.items()):
            print(f"{s:22} {', '.join(sc) or '-'}")
        print("keys no script references:", ", ".join(unused) or "-")
    elif a.cmd == "request":
        request(a.script, a.scope, a.reason)
    elif a.cmd == "grant":
        grant(a.script, a.scope)
    elif a.cmd == "revoke":
        revoke(a.script, a.scope)
    elif a.cmd == "rotated":
        rotated(a.key)
    else:
        {"status": status, "audit": audit}[a.cmd]()


if __name__ == "__main__":
    main()
