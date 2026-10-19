"""Varys's broker: the running script's scoped secrets from OpenBao (openbao-lxc, CT213), read-only.

olympus_core.load_env() calls load() first; the ~/.olympus_env-style files then fill any gap (fallback until the
owner retires them). Each entry script (sys.argv[0], e.g. netops.py) has its own token
~/.olympus/bao/tokens/<script>.token whose policy agent-<script> can read only the scopes listed for it in
~/.olympus/bao/scopes.json (written by agents/openbao/bao_setup.py and keeper.py grant). Values are never
logged or written anywhere; the status file keeps only counts, scope names and the names of keys that differ
from the files (drift), for Varys's audit. Python stdlib only; never imports olympus_core.
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BAO_DIR = Path.home() / ".olympus" / "bao"
ADDR = os.environ.get("BAO_ADDR", "https://192.168.40.39:8200")
MOUNT = "homelab"
TIMEOUT = 3
RENEW_EVERY = 86400   # renew-self at most once a day (periodic token)


def script():
    return Path(sys.argv[0]).stem if sys.argv and sys.argv[0] not in ("", "-c", "-m") else ""


def _ctx():
    return ssl.create_default_context(cafile=str(BAO_DIR / "ca.crt"))


def _call(method, path, token, ctx):
    req = urllib.request.Request(f"{ADDR}/v1/{path}", method=method, headers={"X-Vault-Token": token})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return json.load(r)


def _status_file(name):
    return BAO_DIR / "status" / f"{name}.json"


def _write_status(name, **info):
    p = _status_file(name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        old = json.loads(p.read_text()) if p.exists() else {}
        old.update(info, time=int(time.time()))
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(old))
        tmp.replace(p)
    except OSError:
        pass


def load(name=None):
    """Set this script's secrets into os.environ (setdefault). Returns {key: value} as read, or {} on any problem."""
    name = name or script()
    tok = BAO_DIR / "tokens" / f"{name}.token"
    if not name or not tok.exists():
        return {}
    try:
        scopes = json.loads((BAO_DIR / "scopes.json").read_text()).get(name, [])
    except (OSError, ValueError):
        scopes = []
    got, failed = {}, []
    try:
        token, ctx = tok.read_text().strip(), _ctx()
        for scope in scopes:
            try:
                got.update(_call("GET", f"{MOUNT}/data/{scope}", token, ctx)["data"]["data"])
            except urllib.error.HTTPError as e:  # a missing/denied scope must not stop the others
                failed.append(f"{scope}: HTTP {e.code}")
        try:
            last = json.loads(_status_file(name).read_text()).get("renewed", 0)
        except (OSError, ValueError):
            last = 0
        renewed = last
        if time.time() - last > RENEW_EVERY:
            _call("POST", "auth/token/renew-self", token, ctx)
            renewed = int(time.time())
    except Exception as e:  # noqa: BLE001 - OpenBao down or token bad: the files are the fallback
        _write_status(name, ok=False, error=type(e).__name__, scopes=scopes, keys=0)
        return {}
    for k, v in got.items():
        os.environ.setdefault(k, str(v))
    _write_status(name, ok=not failed, error="; ".join(failed), scopes=scopes, keys=len(got), renewed=renewed)
    return got


def env_files(default, name=None):
    """Env files this script may still read. scopes.json "_files" narrows them (Varys: only .discord_env, so the
    keeper never holds the values it manages); every other script keeps all the files as the fallback."""
    try:
        files = json.loads((BAO_DIR / "scopes.json").read_text()).get("_files", {}).get(name or script())
    except (OSError, ValueError):
        files = None
    return tuple(files) if files is not None else default


def drift(name, keys):
    """Record the NAMES of keys whose file value differs from OpenBao (never the values)."""
    _write_status(name or script(), drift=sorted(keys))
