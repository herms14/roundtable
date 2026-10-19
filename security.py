#!/usr/bin/env python3
"""Loretta - security CTO of the Roundtable (sees every layer; reports to Gideon).

  security.py scan [--dry]       # posture scan, every 6 h (security-scan.timer)
  security.py watch              # attack signals, every 15 min (security-watch.timer): SSH brute force, 401/403
                                 # bursts at Traefik, unknown devices joining trusted VLANs
  security.py brief              # 07:45 daily security brief (security-brief.timer)
  security.py fix <finding-id>   # run by Gideon after the owner's ✅: apply the approved fix, re-check, report
  security.py ask "<question>" [--reply-to MID] [--channel CID]
  security.py list

Posture checks are deterministic (no LLM): application (TLS expiry, Traefik routers without SSO, risky container
settings, databases listening on the LAN), compute (SSH hardening, pending security updates, Proxmox 2FA, privileged
LXCs, Proxmox firewall), network (Omada port forwards, UPnP, inter-VLAN ACLs, firmware), storage (backup freshness of
every live guest). Only NEW findings are explained by gpt-6.1-sol, as a security CTO would: what it is, how an
attacker would use it, impact, severity and an exact fix plan. Fixes run only after the owner's ✅ in Discord.
"""
import argparse
import concurrent.futures as cf
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import olympus_core as o

AGENT = "security"
CHANNEL = "security-alerts"
TOPIC = ("🏹 Loretta, security CTO of the Roundtable: posture findings, attack signals and the 07:45 security brief. "
         "Fixes need Master Hermes's ✅.")
HOME = Path.home()
LOCK = HOME / ".olympus" / "security.lock"
DOC_AGENT = HOME / "doc_agent.py"
SRE_HISTORY = HOME / ".sre-agent" / "history.jsonl"
TRAEFIK_HOST, TRAEFIK_IP = "traefik-lxc", "192.168.40.20"
EXPLAIN_MODEL, FIX_MODEL, ASK_MODEL = "gpt-6.1-sol", "gpt-6.1-sol", "gpt-6.1-sol"
MAX_EXPLAIN = 8                       # new findings explained per scan (rest wait for the next scan)
MUTE_DAYS = 30                        # rejected fix = accepted risk for 30 days
SIGNAL_COOLDOWN_H = 2
SSH_FAIL_THRESHOLD = 20               # failed SSH logins per host per 15 min
AUTH_FAIL_THRESHOLD = 150             # 401/403 per Traefik service per 15 min
BACKUP_MAX_AGE_H = 48
TRUSTED_VLANS = {20: "Homelab", 40: "Production", 90: "Management", 91: "Firewall"}
SEV = ["critical", "high", "medium", "low", "info"]
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}
SEV_WEIGHT = {"critical": 25, "high": 10, "medium": 4, "low": 1, "info": 0}
RISKY_PORTS = {2375: ("Docker API without TLS (root on the host)", "critical"),
               6379: ("Redis", "medium"), 5432: ("PostgreSQL", "medium"), 3306: ("MySQL/MariaDB", "medium"),
               27017: ("MongoDB", "medium"), 9200: ("Elasticsearch", "medium"), 11211: ("memcached", "medium"),
               5984: ("CouchDB", "medium"), 8086: ("InfluxDB", "medium")}
PRIV_OK = ("cadvisor", "node-exporter", "node_exporter", "netdata")
SOCK_OK = ("traefik", "portainer", "watchtower", "dockge", "homepage", "glance", "cadvisor", "dozzle", "diun",
           "promtail", "alloy", "komodo", "socket-proxy", "dockerproxy", "docker-proxy", "autoheal")

HOST_SCRIPT = r"""
S=""; [ "$(id -u)" = 0 ] || { sudo -n true 2>/dev/null && S="sudo -n"; }
$S sshd -T 2>/dev/null | grep -E '^(permitrootlogin|passwordauthentication|permitemptypasswords) ' | sed 's/^/SSHD|/'
command -v apt-get >/dev/null && echo "SECUPD|$(apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep -c '^Inst .*[Ss]ecurity')"
[ -f /var/run/reboot-required ] && echo "REBOOT|1"
$S ss -Htlnp 2>/dev/null | awk '{print "L|"$4"|"$6}'
D=""
if command -v docker >/dev/null 2>&1; then
  if docker ps -q >/dev/null 2>&1; then D="docker"; elif sudo -n docker ps -q >/dev/null 2>&1; then D="sudo -n docker"; fi
fi
if [ -n "$D" ]; then
  ids=$($D ps -q); [ -n "$ids" ] && $D inspect $ids --format 'C|{{.Name}}|{{.HostConfig.Privileged}}|{{.Config.Image}}|{{range .Mounts}}{{.Source}},{{end}}'
fi
echo "END|$(hostname)"
"""


# --------------------------------------------------------------------------- state

@contextlib.contextmanager
def locked(wait=900):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "w")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                f.close()
                raise RuntimeError("security state lock busy")
            time.sleep(2)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def load():
    return o.kv_get("security", {"findings": {}, "next_id": 1, "baseline": False})


def save(st):
    o.kv_set("security", st)


def load_watch():
    return o.kv_get("security:watch", {"cooldown": {}, "log": [], "known_clients": None})


def find(st, fid):
    for k, f in st["findings"].items():
        if f["id"] == int(fid):
            return k, f
    return None, None


def channel():
    return o.channel_id(CHANNEL, create_topic=TOPIC)


def finding(check, layer, severity, title, targets, evidence, key=None):
    return {"key": key or check, "check": check, "layer": layer, "severity": severity, "title": title,
            "targets": sorted(set(targets)), "evidence": evidence[:40]}


# --------------------------------------------------------------------------- checks: compute + application (hosts)

def _host_scan():
    hosts = list(o.load_hosts().values())

    def one(h):
        rc, out, err = o.ssh(h["name"], HOST_SCRIPT, timeout=60)
        return h, out if "END|" in out else None, (err or str(rc))[:120]

    with cf.ThreadPoolExecutor(10) as ex:
        return list(ex.map(one, hosts))


def check_hosts(out):
    ssh_root_pw, ssh_pw, ssh_empty, upd, reboot, priv, sock, unreach = [], [], [], [], [], [], [], []
    for h, txt, err in _host_scan():
        name = h["name"]
        if txt is None:
            unreach.append(f"{name}: {err}")
            continue
        sshd, ports = {}, []
        for line in txt.splitlines():
            p = line.split("|")
            if p[0] == "SSHD" and len(p) == 2 and " " in p[1]:
                k, v = p[1].split(" ", 1)
                sshd[k] = v.strip()
            elif p[0] == "SECUPD" and len(p) == 2 and p[1].strip().isdigit() and int(p[1]) > 0:
                upd.append(f"{name}: {int(p[1])} security update(s)")
            elif p[0] == "REBOOT":
                reboot.append(name)
            elif p[0] == "L" and len(p) >= 2:
                addr = p[1]
                port = addr.rsplit(":", 1)[-1]
                bind = addr.rsplit(":", 1)[0]
                if port.isdigit() and bind in ("0.0.0.0", "*", "[::]") and int(port) in RISKY_PORTS:
                    ports.append(int(port))
            elif p[0] == "C" and len(p) >= 5:
                cname = p[1].lstrip("/")
                if p[2] == "true" and not any(x in cname.lower() for x in PRIV_OK):
                    priv.append(f"{name}/{cname} ({p[3]})")
                if "docker.sock" in p[4] and not any(x in cname.lower() for x in SOCK_OK):
                    sock.append(f"{name}/{cname}")
        if sshd.get("permitemptypasswords") == "yes":
            ssh_empty.append(name)
        if sshd.get("passwordauthentication") == "yes":
            (ssh_root_pw if sshd.get("permitrootlogin") == "yes" else ssh_pw).append(name)
        for port in sorted(set(ports)):
            what, sev = RISKY_PORTS[port]
            out.append(finding("exposed-port", "application", sev, f"{what} listening on all interfaces of {name}",
                               [name], [f"{name} listens on 0.0.0.0/[::]:{port} ({what}); reachable from every VLAN "
                                        "that can route to it, not just localhost or the Docker network"],
                               key=f"exposed-port:{name}:{port}"))
    if ssh_empty:
        out.append(finding("ssh-empty-passwords", "compute", "critical", "SSH accepts EMPTY passwords", ssh_empty,
                           [f"{h}: PermitEmptyPasswords yes" for h in ssh_empty]))
    if ssh_root_pw:
        out.append(finding("ssh-root-password", "compute", "high",
                           "Root can log in over SSH with a password", ssh_root_pw,
                           [f"{h}: PermitRootLogin yes + PasswordAuthentication yes" for h in ssh_root_pw]))
    if ssh_pw:
        out.append(finding("ssh-password-auth", "compute", "medium", "SSH password authentication is enabled", ssh_pw,
                           [f"{h}: PasswordAuthentication yes (root: key only)" for h in ssh_pw]))
    if upd:
        out.append(finding("security-updates", "compute", "low", "Pending OS security updates",
                           [u.split(":")[0] for u in upd], upd))
    if reboot:
        out.append(finding("reboot-required", "compute", "low", "Kernel/library updates waiting for a reboot", reboot,
                           [f"{h}: /var/run/reboot-required present" for h in reboot]))
    if priv:
        out.append(finding("privileged-containers", "application", "medium",
                           "Privileged Docker containers (full root on the host if compromised)",
                           [p.split("/")[0] for p in priv], priv))
    if sock:
        out.append(finding("docker-socket", "application", "low",
                           "Containers with the Docker socket mounted (root-equivalent)",
                           [s.split("/")[0] for s in sock], sock))
    return unreach


# --------------------------------------------------------------------------- checks: compute (Proxmox)

def check_proxmox(out):
    script = r"""
echo "TFA|$(pvesh get /access/tfa --output-format json 2>/dev/null)"
for f in /etc/pve/nodes/*/lxc/*.conf; do
  grep -q '^unprivileged: 1' "$f" || echo "PRIV|$(basename "$f" .conf)|$(awk -F': ' '/^hostname:/{print $2; exit}' "$f")|$(echo "$f" | cut -d/ -f5)"
done
echo "FW|$(pvesh get /cluster/firewall/options --output-format json 2>/dev/null)"
echo END
"""
    for node in ("node01", "node02", "node03"):
        rc, txt, err = o.ssh(node, script, timeout=60)
        if "END" in txt:
            break
    else:
        return f"Proxmox unreachable: {err[:120]}"
    priv = []
    for line in txt.splitlines():
        p = line.split("|", 1)
        if p[0] == "TFA":
            try:
                tfa = json.loads(p[1] or "[]")
            except json.JSONDecodeError:
                tfa = None
            if tfa is not None and not any((t.get("userid") == "root@pam" and t.get("entries")) for t in tfa):
                out.append(finding("pve-no-2fa", "compute", "high", "Proxmox root@pam has no two-factor authentication",
                                   ["proxmox"], ["pvesh /access/tfa: no TOTP/WebAuthn/recovery entries for root@pam",
                                                 "root@pam controls every VM, LXC, backup and node in MorpheusCluster",
                                                 "The web UI (8006) is reachable from the LAN and via Traefik"]))
        elif p[0] == "PRIV":
            vmid, host, node = (p[1].split("|") + ["", ""])[:3]
            priv.append(f"CT{vmid} {host or '?'} @{node}")
        elif p[0] == "FW":
            try:
                fw = json.loads(p[1] or "{}")
            except json.JSONDecodeError:
                fw = {}
            if not fw.get("enable"):
                out.append(finding("pve-firewall-off", "network", "low", "Proxmox cluster firewall is disabled",
                                   ["proxmox"], ["pvesh /cluster/firewall/options: enable is not set",
                                                 "Guests rely only on the Omada gateway; no host/guest-level filtering"]))
    if priv:
        out.append(finding("privileged-lxc", "compute", "medium",
                           "Privileged LXC containers (container root = host root)", priv, priv))
    return None


# --------------------------------------------------------------------------- checks: network (Omada)

def check_network(out):
    import netops
    try:
        om = netops.Omada()
    except Exception as e:  # noqa: BLE001
        return f"Omada unavailable: {e}"
    try:
        pf = om.get("/setting/transmission/portForwardings?currentPage=1&currentPageSize=100").get("data", [])
        for r in pf:
            if r.get("status") is False:
                continue
            name = r.get("name") or f"rule {r.get('id', '?')}"
            out.append(finding("port-forward", "network", "high", f"Internet port forward: {name}",
                               [name], [f"WAN {r.get('externalPort')} -> {r.get('forwardIp')}:{r.get('forwardPort')} "
                                        f"({r.get('protocol')})", "Exposes an internal service directly to the internet"],
                               key=f"port-forward:{name}"))
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"port forwards: {e}")
    try:
        if (om.get("/setting/upnp") or {}).get("enable"):
            out.append(finding("upnp", "network", "medium", "UPnP is enabled on the gateway", ["ER605"],
                               ["Any LAN device (incl. IoT) can open inbound ports on the WAN without approval"]))
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"upnp: {e}")
    try:
        acl = om.get("/setting/firewall/acls?type=0&currentPage=1&currentPageSize=100") or {}
        rules = [r for r in acl.get("data", []) if r.get("status", True)]
        if not rules:
            out.append(finding("no-vlan-isolation", "network", "medium",
                               "No inter-VLAN firewall rules: IoT and Guest can reach Management and Homelab",
                               ["ER605"], ["Omada gateway ACL list is empty (aclDisable: "
                                           f"{acl.get('aclDisable')})",
                                           "VLANs 30 IoT / 50 Guest are routed to 20 Homelab, 40 Production "
                                           "and 90 Management without restriction"]))
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"acls: {e}")
    try:
        devs = om.get("/devices") or []
        old = [f"{d['name']} ({d.get('model')}) fw {d.get('version')}" for d in devs if d.get("needUpgrade")]
        if old:
            out.append(finding("network-firmware", "network", "low", "Omada devices have firmware updates available",
                               [x.split(" (")[0] for x in old], old))
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"firmware: {e}")
    return None


# --------------------------------------------------------------------------- checks: application (Traefik / TLS)

def parse_routers(txt):
    routers, cur, in_r, r_ind, mw_mode = [], None, False, None, False
    for raw in txt.splitlines():
        if raw.startswith("@@FILE"):
            in_r, cur = False, None
            continue
        s = raw.rstrip()
        if not s.strip() or s.strip().startswith("#"):
            continue
        ind, t = len(s) - len(s.lstrip()), s.strip()
        if t == "routers:":
            in_r, r_ind, cur = True, None, None
            continue
        if not in_r:
            continue
        if r_ind is None:
            r_ind = ind
        if ind < r_ind:
            in_r, cur = False, None
            continue
        if ind == r_ind and t.endswith(":"):
            cur = {"name": t[:-1].strip("\"'"), "rule": "", "mw": []}
            routers.append(cur)
            mw_mode = False
            continue
        if cur is None:
            continue
        if t.startswith("rule:"):
            cur["rule"], mw_mode = t[5:].strip().strip("\"'"), False
        elif t.startswith("middlewares:"):
            rest = t[12:].strip()
            if rest.startswith("["):
                cur["mw"] += [x.strip(" \"'") for x in rest.strip("[]").split(",") if x.strip()]
            else:
                mw_mode = True
        elif mw_mode and t.startswith("- "):
            cur["mw"].append(t[2:].strip(" \"'"))
        elif not t.startswith("- "):
            mw_mode = False
    return routers


def cert_days(host):
    ctx = ssl.create_default_context()
    with socket.create_connection((TRAEFIK_IP, 443), timeout=6) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
        na = ss.getpeercert()["notAfter"]
    return (ssl.cert_time_to_seconds(na) - time.time()) / 86400


def check_traefik(out):
    rc, txt, err = o.ssh(TRAEFIK_HOST, 'for f in /opt/traefik/config/dynamic/*.yml; do echo "@@FILE $f"; cat "$f"; done',
                         timeout=40)
    if not txt:
        return f"Traefik config unreadable: {err[:120]}"
    routers = parse_routers(txt)
    open_r, hosts = [], set()
    for r in routers:
        hs = re.findall(r"Host\(`([^`]+)`", r["rule"])
        hosts.update(h for h in hs if h.endswith("hrmsmrflrii.xyz"))
        if hs and not any(re.search(r"authentik|sso|forward-?auth", m, re.I) for m in r["mw"]):
            open_r.append(f"{r['name']}: {', '.join(hs)}")
    if open_r:
        out.append(finding("no-sso-routers", "application", "low",
                           f"{len(open_r)} Traefik routes are not behind Authentik SSO",
                           [x.split(":")[0] for x in open_r],
                           open_r + ["(services with strong built-in login are acceptable; the rest should get the "
                                     "authentik middleware)"]))
    soon, bad = [], []

    def one(h):
        try:
            return h, cert_days(h), None
        except Exception as e:  # noqa: BLE001
            return h, None, str(e)[:120]

    with cf.ThreadPoolExecutor(12) as ex:
        for h, days, e in ex.map(one, sorted(hosts)):
            if days is None:
                if "CERTIFICATE_VERIFY_FAILED" in (e or "") or "certificate" in (e or "").lower():
                    bad.append(f"{h}: {e}")
            elif days < 20:
                soon.append((h, days))
    if soon:
        sev = "high" if min(d for _, d in soon) < 7 else "medium"
        out.append(finding("tls-expiring", "application", sev, "TLS certificates expiring soon (renewal not happening)",
                           [h for h, _ in soon], [f"{h}: expires in {d:.0f} days" for h, d in soon]
                           + ["Traefik normally renews Let's Encrypt certs 30 days before expiry"]))
    if bad:
        out.append(finding("tls-invalid", "application", "medium", "Invalid / untrusted TLS certificates",
                           [b.split(":")[0] for b in bad], bad))
    return None


# --------------------------------------------------------------------------- checks: storage (backups)

def check_backups(out):
    import inventory
    import watcher
    try:
        comp = inventory.get("compute")
        # PBS itself is not backed up to PBS (its datastore is the backup target).
        guests = [g for g in comp.get("guests", []) if not g["template"] and g["status"] == "running"
                  and "pbs" not in g["name"].lower()]
        res = watcher.prom("max by (vm_id) (pbs_snapshot_vm_last_timestamp)")
    except Exception as e:  # noqa: BLE001
        return f"backup check unavailable: {e}"
    if not guests or not res:
        return "backup check: no data"
    last = {r["metric"].get("vm_id"): float(r["value"][1]) for r in res}
    now = time.time()
    stale = []
    for g in guests:
        ts = last.get(str(g["id"]))
        if ts is None:
            stale.append(f"{g['id']} {g['name']}: never backed up to PBS")
        elif (now - ts) / 3600 > BACKUP_MAX_AGE_H:
            stale.append(f"{g['id']} {g['name']}: last backup {(now - ts) / 3600:.0f} h ago")
    if stale:
        out.append(finding("stale-backups", "storage", "medium",
                           "Running guests without a recent backup (ransomware / disaster exposure)",
                           [s.split(":")[0] for s in stale], stale))
    return None


CHECKS = [("hosts", check_hosts), ("proxmox", check_proxmox), ("network", check_network),
          ("traefik", check_traefik), ("backups", check_backups)]


def collect():
    found, notes = [], []
    for name, fn in CHECKS:
        try:
            n = fn(found)
            if isinstance(n, list):
                notes += [f"unreachable {x}" for x in n]
                import access
                for x in n:
                    host = str(x).split(":")[0].strip()
                    access.from_error(AGENT, f"SSH to {host}", x, needed=f"hermes-admin SSH key login on {host}",
                                      why="security posture scan (sshd, updates, listening ports, docker)",
                                      source="security scan")
            elif n:
                notes.append(n)
        except Exception as e:  # noqa: BLE001
            notes.append(f"{name} check failed: {e!r}"[:200])
            o.log(AGENT, f"check {name} failed: {e!r}")
    return {f["key"]: f for f in found}, notes


def score(findings):
    return max(0, 100 - sum(SEV_WEIGHT.get(f["severity"], 0) for f in findings))


# --------------------------------------------------------------------------- explanation (LLM, new findings only)

EXPL_SCHEMA = o.S(items=o.A(o.S(
    key=o.STR, title=o.STR, severity=o.E("critical", "high", "medium", "low", "info"), what=o.STR,
    attack_scenario=o.STR, impact=o.STR, fix_plan=o.A(o.STR), commands=o.A(o.STR), risk=o.STR, rollback=o.STR,
    verification=o.A(o.STR), auto_fixable=o.BOOL, manual_reason=o.STR)))

CONTEXT = """Homelab facts: Proxmox VE 9 cluster MorpheusCluster (node01-03, 192.168.20.20-22, VLAN 20) whose nodes talk
to each other as root over SSH keys (corosync/pve-cluster need that - never break root KEY login on nodes). Services run
in Docker on VMs/LXCs on VLAN 40, published through Traefik (192.168.40.20, *.hrmsmrflrii.xyz, Let's Encrypt) with
Authentik SSO (192.168.40.21) as forward-auth middleware. Network: TP-Link Omada (ER605 gateway, controller
192.168.0.101) with VLANs 1 Default, 10 Internal, 20 Homelab, 30 IoT, 40 Production, 50 Guest,
80 HyperV, 90 Management. DNS Technitium 192.168.90.60 (primary) + Pi-hole 192.168.90.53 (secondary). Backups: Proxmox Backup Server (pbs-lxc100). No
public DNS for the domain; the lab is LAN/VPN only. Management access is from codex-agent-lxc (192.168.40.16) and
the owner's PC with SSH keys. Automated changes run from codex-agent-lxc via SSH (sudo -n), so a fix must never lock
that host out: verify key auth works before disabling passwords, validate sshd with `sshd -t` before reload, never
restart networking remotely without a fallback, never change Omada/Proxmox settings that need the web UI unless the
step is marked manual."""


def explain(run, items):
    prompt = f"""You are Loretta, the Enterprise Security Architect and Security Lead of Master Hermes's homelab
(CISSP/CCSP-informed expertise, on-premises/cloud security and MITRE ATT&CK; not actual software certifications).
You set strategy, Millicent remediates owner-approved plans and Rogier handles IAM. Assume breach, least privilege, defence
in depth; pragmatic for a home lab - do not cry wolf, but never downplay a real attack path).
{CONTEXT}

Deterministic checks found these NEW security findings (JSON):
{json.dumps(items, indent=1)[:24000]}

For EACH finding return an item with the same `key`:
- title: short, specific. severity: re-assess honestly for a LAN-only home lab (keep or adjust the check's level).
- what: what is wrong, plainly, with the evidence. attack_scenario: how a real attacker (or a compromised IoT device /
  malicious container) would use it, step by step. impact: what they would get (blast radius).
- fix_plan: exact ordered steps; commands: the exact shell commands (with ssh <host> prefixes). risk: what could break.
  rollback: how to undo. verification: checks proving it is fixed.
- auto_fixable: true ONLY if it can be done safely over SSH by an agent with no UI clicks and no risk of locking out
  management access (e.g. sshd hardening with key check first, closing a port binding, updating packages). Omada
  controller changes, Proxmox 2FA enrolment, firmware upgrades and anything needing a decision are false; then explain
  the manual steps in manual_reason (Omada/Proxmox click-path)."""
    return o.llm(run, prompt, model=EXPLAIN_MODEL, effort="medium", schema=EXPL_SCHEMA,
                 instructions="You are Loretta, Security Lead and Enterprise Security Architect. Precise, calm, never alarmist, "
                              "never invent evidence.", max_output_tokens=16000)["items"]


def post_finding(f, cid, run):
    x = f["expl"]
    sev = x["severity"]
    body = (f"**What**\n{x['what']}\n\n**How an attacker would use it**\n{x['attack_scenario']}\n\n"
            f"**Impact**\n{x['impact']}")
    fields = [("Severity", f"{SEV_ICON[sev]} **{sev.upper()}**", True), ("Layer", f["layer"], True),
              ("Finding", f"S-{f['id']}" + (f" · GitLab #{f['gitlab_iid']}" if f.get("gitlab_iid") else ""), True),
              ("🎯 Affected", ", ".join(f["targets"])[:1000] or "-", False),
              ("🔎 Evidence", "\n".join(f"• {e}" for e in f["evidence"][:8])[:1000], False),
              ("🛠️ Fix plan", "\n".join(f"{i + 1}. {s}" for i, s in enumerate(x["fix_plan"][:10]))[:1000], False),
              ("⚠️ Risk", x["risk"][:1000], True), ("↩️ Rollback", x["rollback"][:1000], True)]
    full = (f"# Security finding S-{f['id']} - {x['title']}\n\nSeverity: {sev} · Layer: {f['layer']} · "
            f"Check: {f['check']}\n\n## What\n{x['what']}\n\n## Attack scenario\n{x['attack_scenario']}\n\n"
            f"## Impact\n{x['impact']}\n\n## Affected\n" + "\n".join(f"- {t}" for t in f["targets"])
            + "\n\n## Evidence\n" + "\n".join(f"- {e}" for e in f["evidence"]) + "\n\n## Fix plan\n"
            + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(x["fix_plan"])) + "\n\n## Commands\n```bash\n"
            + "\n".join(x["commands"]) + f"\n```\n\n## Risk\n{x['risk']}\n\n## Rollback\n{x['rollback']}\n\n"
            "## Verification\n" + "\n".join(f"- {v}" for v in x["verification"])
            + (f"\n\n## Manual steps\n{x['manual_reason']}\n" if not x["auto_fixable"] else "\n"))
    e = o.embed(AGENT, f"{SEV_ICON[sev]} S-{f['id']} · {x['title']}", body, fields, color=o.SEV_COLORS.get(sev, 0x95A5A6),
                footer=f"Roundtable run #{run.id} · {EXPLAIN_MODEL} · Loretta reports to Gideon")
    mid = o.post(cid, content=f"<@{o.OWNER_ID}>" if sev in ("critical", "high") else None,
                 mention_owner=sev in ("critical", "high"), embeds=[e],
                 files=[(f"security-S{f['id']}.md", full)])
    f["msg"] = mid
    if x["auto_fixable"]:
        nb = None if sev in ("critical", "high") else o.next_window()
        f["approval"] = o.request_approval(
            "security_engineer", f"Security fix S-{f['id']}: {x['title']}",
            f"Millicent will apply Loretta's {len(x['fix_plan'])}-step fix plan above on {', '.join(f['targets'])[:300]}, "
            "re-run the security check, report back here and have Miriel update the vault.",
            [sys.executable, str(Path(__file__).resolve()), "fix", str(f["id"])], cid, severity=sev if sev != "info"
            else "low", not_before=nb, reply_to=mid,
            payload={"finding_id": str(f["id"]), "reviewed_plan": x, "targets": f["targets"]},
            fields=[("When", "Immediately after ✅" if not nb else
                     f"Next maintenance window ({o.local(nb).strftime('%a %d %b %H:%M')}) - {sev} severity", True)])
        f["state"] = "awaiting"
    else:
        o.post(cid, f"🏹 S-{f['id']} needs your hands, Master Hermes - not safe to automate:\n{x['manual_reason'][:1700]}",
               reply_to=mid)
        f["state"] = "manual"
    return full


# --------------------------------------------------------------------------- scan

def gl_issue(f, full):
    import medic
    if SEV.index(f["expl"]["severity"]) > SEV.index("medium"):
        return None
    return medic.gl_issue_create(f"Security S-{f['id']}: {f['expl']['title']}", full,
                                 ["security", f"severity::{f['expl']['severity']}"])


def scan(dry=False):
    found, notes = collect()
    if dry:
        for k, f in sorted(found.items(), key=lambda kv: SEV.index(kv[1]["severity"])):
            print(f"{SEV_ICON[f['severity']]} {f['severity']:<8} {k:<40} {f['title']}  [{', '.join(f['targets'])[:80]}]")
        print("score", score(found.values()), "notes", notes)
        return
    import medic
    cid = channel()
    now = o.iso()
    with locked():
        st = load()
        st["last_scan"], st["notes"] = now, notes
        for k, f in st["findings"].items():
            if f["state"] in ("resolved", "fixed"):
                continue
            if f["state"] == "muted" and o.parse_iso(f.get("muted_until", now)) > o.utcnow():
                if k in found:
                    f.update(evidence=found[k]["evidence"], targets=found[k]["targets"], last=now)
                continue
            if k in found:
                f.update(evidence=found[k]["evidence"], targets=found[k]["targets"], last=now)
                if f.get("approval") and f["state"] == "awaiting":
                    ap = o.get_approval(f["approval"])
                    if ap and ap["status"] == "rejected":
                        f["state"], f["muted_until"] = "muted", o.iso(o.utcnow() + dt.timedelta(days=MUTE_DAYS))
                        o.post(cid, f"🏹 S-{f['id']} marked as **accepted risk** for {MUTE_DAYS} days (fix rejected).")
                    elif ap and ap["status"] == "scheduled":
                        f["state"] = "scheduled"
                continue
            if f["state"] == "fixing":
                continue
            f.update(state="resolved", resolved=now)
            if f.get("approval"):
                ap = o.get_approval(f["approval"])
                if ap and ap["status"] in ("pending", "scheduled"):
                    o.set_approval(f["approval"], status="expired", finished=now, result="finding cleared")
            if f.get("expl"):
                o.post(cid, embeds=[o.embed(AGENT, f"✅ S-{f['id']} resolved · {f['expl']['title']}",
                                            "No longer detected by the posture scan.", color=o.SEV_COLORS["ok"])])
            medic.gl_note(f.get("gitlab_iid"), "✅ No longer detected by Loretta's posture scan.")
            medic.gl_close(f.get("gitlab_iid"), ["resolved"])
        new = []
        for k, f in found.items():
            cur = st["findings"].get(k)
            if cur and cur["state"] not in ("resolved", "fixed"):
                if cur["state"] == "new":
                    new.append(k)
                continue
            f.update(id=st["next_id"], state="new", first=now, last=now)
            st["next_id"] += 1
            st["findings"][k] = f
            new.append(k)
        cutoff = o.utcnow() - dt.timedelta(days=30)
        for k in [k for k, f in st["findings"].items() if f["state"] in ("resolved", "fixed")
                  and o.parse_iso(f.get("resolved") or f.get("fixed_at") or f["last"]) < cutoff]:
            del st["findings"][k]
        first = not st.get("baseline")
        st["baseline"] = True
        save(st)
    opn = [f for f in st["findings"].values() if f["state"] not in ("resolved", "fixed")]
    if first:
        lines = [f"{SEV_ICON[f['severity']]} **{f['title']}** - {', '.join(f['targets'])[:120]}"
                 for f in sorted(opn, key=lambda f: SEV.index(f["severity"]))]
        o.post(cid, embeds=[o.embed(AGENT, f"🏹 Loretta stands guard · posture score {score(opn)}/100",
                                    "First full security posture scan across application, compute, network and "
                                    f"storage. {len(opn)} finding(s):\n\n" + "\n".join(lines)[:3500],
                                    footer=f"Each finding is explained in detail below (max {MAX_EXPLAIN} per scan). "
                                           "Fixes always need your ✅.")])
    todo = sorted(new, key=lambda k: SEV.index(st["findings"][k]["severity"]))[:MAX_EXPLAIN]
    if todo:
        try:
            with o.Run(AGENT, f"explain {len(todo)} new security finding(s)", "timer") as run:
                items = [{k2: st["findings"][k][k2] for k2 in ("key", "check", "layer", "severity", "title", "targets",
                                                                "evidence")} for k in todo]
                expl = {x["key"]: x for x in explain(run, items)}
                with locked():
                    st = load()
                    for k in todo:
                        f = st["findings"].get(k)
                        if not f or k not in expl:
                            continue
                        f["expl"], f["severity"] = expl[k], expl[k]["severity"]
                        full = post_finding(f, cid, run)
                        f["gitlab_iid"] = gl_issue(f, full)
                        save(st)
                run.summary = f"explained {len(expl)} finding(s); score {score(opn)}"
        except o.BudgetExceeded:
            pass
    o.heartbeat(AGENT, f"posture score {score(opn)}/100 · {len(opn)} open finding(s)")


# --------------------------------------------------------------------------- watch (attack signals)

def watch():
    import watcher
    w = load_watch()
    signals = []
    try:
        for r in watcher.loki('sum by (host) (count_over_time({unit=~"ssh.service|sshd.service"} '
                              '|~ "Failed password|Invalid user|authentication failure" [15m]))'):
            n = float(r["value"][1])
            if n >= SSH_FAIL_THRESHOLD:
                h = r["metric"].get("host", "?")
                signals.append(("high", f"ssh-bruteforce:{h}", f"SSH brute force on {h}",
                                f"{n:.0f} failed SSH logins in the last 15 minutes on **{h}**.",
                                "Someone is guessing passwords. Password authentication is still enabled on most "
                                "hosts, so this is a live risk. Check the source IP (`journalctl -u ssh`), block it "
                                "at the Omada gateway and approve Loretta's SSH-hardening fix."))
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"watch loki: {e}")
    try:
        for r in watcher.prom('sum by (service) (increase(traefik_service_requests_total{code=~"401|403"}[15m]))'):
            n = float(r["value"][1])
            if n >= AUTH_FAIL_THRESHOLD:
                s = r["metric"].get("service", "?")
                signals.append(("medium", f"auth-burst:{s}", f"Authentication failures burst on {s}",
                                f"{n:.0f} HTTP 401/403 responses in 15 minutes from Traefik service **{s}**.",
                                "Could be credential stuffing against the app, or a misconfigured client hammering "
                                "it. Check Traefik access logs for the client IPs."))
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"watch prom: {e}")
    try:
        import netops
        om = netops.Omada()
        cl = om.get("/clients?currentPage=1&currentPageSize=1000&filters.active=true").get("data", [])
        seen = {c.get("mac"): c for c in cl if c.get("mac")}
        known = w.get("known_clients")
        if known is None:
            w["known_clients"] = sorted(seen)
        else:
            for mac, c in seen.items():
                if mac in known:
                    continue
                known.append(mac)
                vid = c.get("vid")
                if vid in TRUSTED_VLANS:
                    nm = c.get("name") or c.get("hostName") or mac
                    signals.append(("medium", f"new-device:{mac}", f"New device on trusted VLAN {vid}",
                                    f"**{nm}** (`{mac}`, {c.get('ip')}) joined VLAN {vid} {TRUSTED_VLANS[vid]} for the "
                                    "first time.",
                                    "If you did not add this device, treat it as a rogue: block it in Omada "
                                    "(Clients → Block) and check which switch port / SSID it came from."))
            w["known_clients"] = known[-3000:]
    except Exception as e:  # noqa: BLE001
        o.log(AGENT, f"watch omada: {e}")
    now = o.utcnow()
    cid = None
    for sev, key, title, what, todo in signals:
        last = w["cooldown"].get(key)
        if last and o.parse_iso(last) > now - dt.timedelta(hours=SIGNAL_COOLDOWN_H):
            continue
        w["cooldown"][key] = o.iso()
        w["log"] = (w.get("log") or [])[-49:] + [{"t": o.iso(), "sev": sev, "title": title, "what": what}]
        cid = cid or channel()
        o.post(cid, content=f"<@{o.OWNER_ID}>" if sev in ("critical", "high") else None,
               mention_owner=sev in ("critical", "high"),
               embeds=[o.embed(AGENT, f"{SEV_ICON[sev]} Attack signal · {title}", what,
                               [("What it means / what to do", todo, False)], color=o.SEV_COLORS.get(sev))])
    w["cooldown"] = {k: v for k, v in w["cooldown"].items() if o.parse_iso(v) > now - dt.timedelta(days=2)}
    o.kv_set("security:watch", w)
    o.heartbeat(AGENT, f"watch: {len(signals)} signal(s)")


# --------------------------------------------------------------------------- brief

def brief():
    st, w = load(), load_watch()
    opn = sorted([f for f in st["findings"].values() if f["state"] not in ("resolved", "fixed")],
                 key=lambda f: SEV.index(f["severity"]))
    day = o.utcnow() - dt.timedelta(days=1)
    sig = [s for s in w.get("log", []) if o.parse_iso(s["t"]) > day]
    fixed = [f for f in st["findings"].values() if f["state"] in ("resolved", "fixed")
             and o.parse_iso(f.get("fixed_at") or f.get("resolved") or f["last"]) > day]
    counts = " · ".join(f"{SEV_ICON[s]} {sum(f['severity'] == s for f in opn)} {s}" for s in SEV[:4])
    body = (f"**Posture score {score(opn)}/100** · {len(opn)} open · {counts}\n\n"
            + ("**Top risks**\n" + "\n".join(
                f"{SEV_ICON[f['severity']]} S-{f.get('id')} {(f.get('expl') or {}).get('title') or f['title']} "
                f"[{f['state']}]" for f in opn[:6]) if opn else "No open findings. 🏹")
            + (f"\n\n**Attack signals (24 h)**\n" + "\n".join(f"{SEV_ICON[s['sev']]} {s['title']}" for s in sig[-6:])
               if sig else "\n\n**Attack signals (24 h):** none")
            + (f"\n\n**Closed (24 h):** " + ", ".join(f"S-{f['id']}" for f in fixed) if fixed else ""))
    o.post(channel(), embeds=[o.embed(AGENT, f"🏹 Security brief · {o.local().strftime('%a %d %b')}", body,
                                      footer=f"Last posture scan {o.local(o.parse_iso(st['last_scan'])).strftime('%H:%M') if st.get('last_scan') else 'never'}"
                                             " · Loretta reports to Gideon")])
    o.heartbeat(AGENT, "brief posted")


# --------------------------------------------------------------------------- fix

FIX_SCHEMA = o.S(status=o.E("fixed", "partial", "failed"), summary=o.STR, actions=o.A(o.STR),
                 changes=o.A(o.S(target=o.STR, change=o.STR)), backups=o.A(o.STR), verification=o.A(o.STR),
                 remaining=o.STR, docs_notes=o.STR)


def recheck(f):
    found, _ = collect()
    cur = found.get(f["key"])
    if not cur:
        return True, "finding no longer detected"
    left = set(cur["targets"])
    return False, "still detected on: " + ", ".join(sorted(left))[:400]


def fix(fid):
    import medic
    import hierarchy
    approval = hierarchy.require_approval("security_engineer")
    with locked():
        st = load()
        key, f = find(st, fid)
        if not f or not f.get("expl"):
            print("unknown finding")
            sys.exit(1)
        if (approval["payload"].get("finding_id") != str(f["id"]) or
                approval["payload"].get("reviewed_plan") != f["expl"] or
                approval["payload"].get("targets") != f["targets"] or f.get("approval") != approval["id"]):
            raise PermissionError("Security finding differs from the exact owner-reviewed plan; re-propose it")
        f["state"] = "fixing"
        save(st)
    x = f["expl"]
    ch = o.get_approval(f["approval"])["channel_id"] if f.get("approval") else channel()
    with medic.sre_lock(wait=3600) as got:
        if not got:
            o.post(ch, f"⚠️ S-{f['id']}: patch executor busy for an hour - fix not started. Re-approve later.",
                   reply_to=f.get("msg"))
            with locked():
                st = load()
                st["findings"][key]["state"] = "fix-failed"
                save(st)
            sys.exit(1)
        with o.Run("security_engineer", f"fix security finding S-{f['id']}: {x['title']}", "approval") as run:
            prompt = f"""You are Millicent, the Security Engineer reporting to Loretta. The owner APPROVED Loretta's exact security fix.
SSH aliases work for every host (ssh <name>; sudo -n available). Proxmox: ssh node01.
{CONTEXT}

FINDING S-{f['id']}: {x['title']} (severity {x['severity']}, layer {f['layer']})
Affected: {', '.join(f['targets'])}
Evidence: {'; '.join(f['evidence'])}

APPROVED FIX PLAN (execute exactly this, in order):
{chr(10).join(f'{i + 1}. {s}' for i, s in enumerate(x['fix_plan']))}
Reference commands:
{chr(10).join(x['commands'])}
Rollback if it goes wrong: {x['rollback']}

RULES:
- Stay within the approved plan and the affected targets. Anything materially different -> stop, status=partial.
- Before editing any file: cp it to <file>.bak-loretta-{o.local().strftime('%Y%m%d')} and list it in backups.
- SSH changes: first prove key login works (ssh -o BatchMode=yes <host> true), run `sshd -t` before
  `systemctl reload ssh`, keep root KEY login on Proxmox nodes, and re-test login after the reload; if the re-test
  fails, restore the backup immediately and report failed.
- Never delete data, never reboot a Proxmox node, never restart networking, never touch Omada.
- Then run every verification check: {'; '.join(x['verification'])}
Final JSON: status, summary (<=120 words), actions, changes (target + change), backups, verification (each check
with its result), remaining ('' if none), docs_notes (what the homelab docs should now say, '' if nothing)."""
            res, transcript = o.codex(run, prompt, model=FIX_MODEL, effort="medium", schema=FIX_SCHEMA,
                                      sandbox="danger", timeout=2400, tag=f"secfix{f['id']}", critical=True, token_budget=0)
            res = res or {"status": "failed", "summary": f"Codex returned no result; transcript {transcript}",
                          "actions": [], "changes": [], "backups": [], "verification": [], "remaining": "check manually",
                          "docs_notes": ""}
            ok, why = recheck(f) if res["status"] != "failed" else (False, "fix failed")
            final = "fixed" if res["status"] == "fixed" and ok else ("failed" if res["status"] == "failed" else "partial")
            run.summary = f"{final}: {res['summary'][:300]}"
            color = {"fixed": o.SEV_COLORS["ok"], "partial": o.SEV_COLORS["medium"], "failed": o.SEV_COLORS["critical"]}[final]
            fields = [("Result", {"fixed": "✅ Fixed", "partial": "🟡 Partially fixed", "failed": "❌ Fix failed"}[final], True),
                      ("Independent re-check", ("✅ " if ok else "❌ ") + why, True), ("Cost", o.money(run.cost), True),
                      ("🛠️ Actions", "\n".join(f"• {a}" for a in res["actions"][:12])[:1000] or "none", False),
                      ("📝 Changes", "\n".join(f"• `{c['target']}` - {c['change']}" for c in res["changes"][:8])[:1000]
                       or "none", False),
                      ("💾 Backups", "\n".join(f"• `{b}`" for b in res["backups"][:6])[:1000] or "none", False),
                      ("✅ Verification", "\n".join(f"• {v}" for v in res["verification"][:8])[:1000] or "n/a", False)]
            if res["remaining"]:
                fields.append(("👤 Left for you", res["remaining"][:1000], False))
            fields.append(("📚 Documentation", "Miriel is updating the vault changelog and service docs now."
                           if f.get("gitlab_iid") else "Low severity (no GitLab issue) - not auto-documented.", False))
            report = (f"# Security finding S-{f['id']} - fix report\n\nResult: {final}\nRe-check: {why}\n\n"
                      f"## Summary\n{res['summary']}\n\n## Actions\n" + "\n".join(f"- {a}" for a in res["actions"])
                      + "\n\n## Changes\n" + "\n".join(f"- {c['target']}: {c['change']}" for c in res["changes"])
                      + "\n\n## Backups\n" + "\n".join(f"- {b}" for b in res["backups"]) + "\n\n## Verification\n"
                      + "\n".join(f"- {v}" for v in res["verification"]) + f"\n\n## Remaining\n{res['remaining'] or 'none'}\n")
            o.post(ch, content=f"<@{o.OWNER_ID}>", mention_owner=True,
                   embeds=[o.embed(AGENT, f"S-{f['id']} · security fix report", res["summary"], fields, color=color,
                                   footer=f"Roundtable run #{run.id} · {FIX_MODEL}")],
                   files=[(f"security-S{f['id']}-fix-report.md", report)], reply_to=f.get("msg"))
            medic.gl_note(f.get("gitlab_iid"), f"⚔️ **Millicent report to Loretta** - {final}\n\n" + report)
            if final == "fixed":
                medic.gl_close(f.get("gitlab_iid"), ["fixed"])
            else:
                run.finish("failed" if final == "failed" else "partial", run.summary)
    with locked():
        st = load()
        st["findings"][key].update(state="fixed" if final == "fixed" else "fix-failed", fix=res, fixed_at=o.iso())
        save(st)
    if f.get("gitlab_iid"):
        entry = {"ts": o.local().isoformat(timespec="seconds"), "iid": int(f["gitlab_iid"]), "host": ", ".join(f["targets"]),
                 "kind": "incident", "result": f"incident-{final}", "incident": f"S-{f['id']}",
                 "title": f"Security: {x['title']}", "severity": x["severity"], "symptom": x["what"],
                 "root_cause": x["attack_scenario"], "fix_summary": res.get("summary"), "actions": res.get("actions"),
                 "changes": res.get("changes"), "prevention": "Loretta posture scan every 6 h",
                 "verification_result": res.get("verification"), "docs_notes": res.get("docs_notes")}
        SRE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with SRE_HISTORY.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        if DOC_AGENT.exists():
            subprocess.Popen([sys.executable, str(DOC_AGENT), "run", "--hours", "6", "--trigger", "incident"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    sys.exit(0 if final == "fixed" else 1)


# --------------------------------------------------------------------------- ask / voice / list

def findings_brief(flt=None):
    st, w = load(), load_watch()
    q = (flt or "").lower().strip().lstrip("s-")
    rows = []
    for f in sorted(st["findings"].values(), key=lambda f: (f["state"] in ("resolved", "fixed"), SEV.index(f["severity"]))):
        x = f.get("expl") or {}
        blob = json.dumps(f).lower()
        if q and q not in blob and str(f.get("id")) != q:
            continue
        rows.append({"id": f"S-{f.get('id')}", "state": f["state"], "severity": f["severity"], "layer": f["layer"],
                     "title": x.get("title") or f["title"], "affected": f["targets"][:12],
                     "what": (x.get("what") or "; ".join(f["evidence"][:4]))[:600],
                     "attack_scenario": (x.get("attack_scenario") or "")[:600], "impact": (x.get("impact") or "")[:400],
                     "fix_plan": x.get("fix_plan", [])[:8], "auto_fixable": x.get("auto_fixable"),
                     "manual_steps": (x.get("manual_reason") or "")[:500]})
    opn = [f for f in st["findings"].values() if f["state"] not in ("resolved", "fixed")]
    return {"posture_score": score(opn), "open": len(opn), "last_scan": st.get("last_scan"),
            "findings": rows[:12], "recent_attack_signals": (w.get("log") or [])[-8:],
            "how_to_fix": "Fixes are proposed in Discord #security-alerts; Master Hermes approves with ✅."}


def ask(question, reply_to=None, chan=None):
    import inventory
    chan = chan or "roundtable-hold"
    o.transcript(AGENT, "Request", question, "discord", chan, reply_to or "")
    with o.Run(AGENT, f"ask: {question[:200]}", "chat") as run:
        try:
            inv = inventory.text("all", limit=6000)[:14000]
        except Exception as e:  # noqa: BLE001
            inv = f"(inventory unavailable: {e})"
        prompt = f"""You are Loretta, the Enterprise Security Architect and Security Lead. Your CISSP/CCSP-informed
expertise covers on-premises/cloud strategy and MITRE ATT&CK (not real certifications held by software).
You guide both personal and enterprise digital security, separating work secrets from this personal lab.
Millicent handles approved remediation, Rogier IAM; consolidate their results for Gideon.
You are an expert in threat modelling,
hardening (CIS), incident response, network exposure and backup/ransomware resilience. You see ALL layers
(application, compute, network, storage) and report to Gideon. {CONTEXT}

Current security findings and attack signals (JSON):
{json.dumps(findings_brief(), indent=1)[:12000]}

Live inventory:
{inv}

QUESTION from Master Hermes: {question}

Investigate READ-ONLY if needed (ssh <host> for configs/logs, journalctl, ss, docker inspect; Omada read-only:
python3 {Path(__file__).with_name('netops.py')} omada <api-path>; Prometheus http://192.168.40.13:9090; Loki
http://192.168.40.13:3100; Grafana/Glance as text: python3 {Path(__file__).with_name('observe.py')} grafana|glance
[name] --agent security). Do NOT change anything - fixes go through Discord approvals.
Answer in Markdown like a security CTO briefing the owner: **What happened / what I see** (with evidence),
**Attack path & impact**, **Severity**, **How to fix** (exact steps/commands), **Prevention**. Be concise."""
        text, transcript, used = o.codex_tiered(run, prompt + o.ESCALATE_RULE, o.needs_escalation, sandbox="danger",
                                                timeout=1500, tag="ask")
        text = None if o.needs_escalation(text) else text
        run.summary = (text or "no answer")[:300]
        o.respond(AGENT, chan, "🏹 Loretta · security briefing", text or (o.BUDGET_STOP_TEXT if run.budget_stopped else f"No answer (transcript `{transcript}`)"),
                  reply_to=reply_to, run=run)


def list_findings():
    st = load()
    for k, f in sorted(st["findings"].items(), key=lambda kv: kv[1]["id"]):
        print(f"S-{f['id']:<4} {f['state']:<10} {f['severity']:<8} {k:<40} {((f.get('expl') or {}).get('title') or f['title'])[:70]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "watch", "brief", "fix", "ask", "list"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "scan":
        scan(a.dry)
    elif a.cmd == "watch":
        watch()
    elif a.cmd == "brief":
        brief()
    elif a.cmd == "fix":
        fix(a.arg)
    elif a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    else:
        list_findings()


if __name__ == "__main__":
    main()
