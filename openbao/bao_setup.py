#!/usr/bin/env python3
"""Converge OpenBao (openbao-lxc, CT213) with the Roundtable's secrets and Varys's scope plan. Run on CT211:

  ssh openbao-lxc 'sudo /usr/local/sbin/openbao-admin-token' | python3 ~/bao_setup.py [--dry-run]

The admin token (1 h, from the root token on CT213) arrives on stdin and is never printed or stored. Idempotent:
  1. homelab/<scope> (KV v2) = the scope's keys from ~/.discord_env, .codex_env, .olympus_env, .gitlab_env
     (written only when different; custom metadata: key names, about, rotate_days, imported date)
  2. token role roundtable-agent (orphan, periodic 90 d), policies agent-<script> from keeper.py plan + grants, keeper
  3. one token per entry script -> ~/.olympus/bao/tokens/<script>.token (600), accessors.json, scopes.json
Prints only names and counts.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home() / "olympus"))
import keeper  # noqa: E402  (scope plan + policy text; imports olympus_core)

BAO = keeper.BAO
D = keeper.BAO_DIR
ENV_FILES = (".discord_env", ".codex_env", ".olympus_env", ".gitlab_env")
DRY = "--dry-run" in sys.argv
TOKEN = sys.stdin.readline().strip()
if not TOKEN:
    sys.exit("no admin token on stdin")
CTX = ssl.create_default_context(cafile=str(D / "ca.crt"))

KEEPER_POLICY = """# Varys (keeper.py): metadata, agent policies and token accessors only. Never secret data.
path "homelab/metadata" {
  capabilities = ["list"]
}
path "homelab/metadata/*" {
  capabilities = ["read", "list"]
}
path "sys/policies/acl" {
  capabilities = ["list"]
}
path "sys/policies/acl/agent-*" {
  capabilities = ["read", "create", "update"]
}
path "sys/policies/acl/agent-keeper" {
  capabilities = ["deny"]
}
path "sys/policies/acl/keeper" {
  capabilities = ["deny"]
}
path "auth/token/lookup-accessor" {
  capabilities = ["update"]
}
path "auth/token/revoke-accessor" {
  capabilities = ["update"]
}
"""


def call(method, path, data=None, token=TOKEN):
    req = urllib.request.Request(f"{BAO}/v1/{path}", method=method, headers={"X-Vault-Token": token},
                                 data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def must(method, path, data=None):
    s, d = call(method, path, data)
    if s not in (200, 204):
        sys.exit(f"{method} {path}: HTTP {s} {d.get('errors')}")
    return d


def env_values():
    vals, src = {}, {}
    for n in ENV_FILES:
        p = Path.home() / n
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
            vals.setdefault(k.strip(), v)
            src.setdefault(k.strip(), n)
    return vals, src


def secrets():
    sp = keeper.scope_plan()
    vals, src = env_values()
    covered = set()
    for scope, info in sp["scopes"].items():
        data = {k: vals[k] for k in info["keys"] if k in vals}
        covered |= set(info["keys"])
        missing = [k for k in info["keys"] if k not in vals]
        s, cur = call("GET", f"{sp['mount']}/data/{scope}")
        same = s == 200 and cur["data"]["data"] == data
        print(f"scope {scope:14} {len(data)} keys" + (f" (missing: {', '.join(missing)})" if missing else "") +
              (" unchanged" if same else " -> write"))
        if DRY or not data:
            continue
        if not same:
            must("POST", f"{sp['mount']}/data/{scope}", {"data": data})
        s, m = call("GET", f"{sp['mount']}/metadata/{scope}")
        old = (m.get("data") or {}).get("custom_metadata") or {} if s == 200 else {}
        meta = {"keys": ",".join(sorted(data)), "about": info.get("about", "")[:500],
                "rotate_days": str(info.get("rotate_days", 0)), "source": ",".join(sorted({src[k] for k in data})),
                "imported": old.get("imported") or keeper.o.local().date().isoformat()}
        must("POST", f"{sp['mount']}/metadata/{scope}", {"custom_metadata": meta, "max_versions": 10})
    extra = sorted(set(vals) - covered)
    print("env keys not in any scope (left in the files only):", ", ".join(extra) or "-")


def policies_and_tokens():
    plan, unused, _ = keeper.plan()
    print("keys no script references:", ", ".join(unused) or "-")
    if DRY:
        for s, sc in sorted(plan.items()):
            print(f"  {s:22} {', '.join(sc) or '-'}")
        return
    must("POST", "auth/token/roles/roundtable-agent",
         {"allowed_policies": ["keeper"], "allowed_policies_glob": ["agent-*"], "orphan": True, "renewable": True,
          "token_period": "2160h", "token_type": "service", "token_no_default_policy": False})
    must("PUT", "sys/policies/acl/keeper", {"policy": KEEPER_POLICY})
    for d in ("tokens", "status"):
        (D / d).mkdir(parents=True, exist_ok=True, mode=0o700)
    acc_file = D / "accessors.json"
    acc = json.loads(acc_file.read_text()) if acc_file.exists() else {}
    made = 0
    for script, scopes in sorted(plan.items()):
        must("PUT", f"sys/policies/acl/agent-{script}", {"policy": keeper.policy_text(scopes)})
        tf = D / "tokens" / f"{script}.token"
        if tf.exists():
            s, _ = call("GET", "auth/token/lookup-self", token=tf.read_text().strip())
            if s == 200:
                continue
        pols = [f"agent-{script}"] + (["keeper"] if script == "keeper" else [])
        d = must("POST", "auth/token/create/roundtable-agent",
                 {"policies": pols, "display_name": f"agent-{script}", "meta": {"script": script}})
        fd = os.open(tf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(d["auth"]["client_token"])
        acc[script] = d["auth"]["accessor"]
        made += 1
    acc_file.write_text(json.dumps(acc, indent=1))
    out = dict(plan)
    out["_files"] = {"keeper": [".discord_env"]}
    (D / "scopes.json").write_text(json.dumps(out, indent=1))
    for p in (acc_file, D / "scopes.json"):
        p.chmod(0o600)
    print(f"policies {len(plan)} agent-* + keeper · tokens created {made} · existing kept {len(plan) - made}")


if __name__ == "__main__":
    D.mkdir(parents=True, exist_ok=True, mode=0o700)
    D.chmod(0o700)
    secrets()
    policies_and_tokens()
