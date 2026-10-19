"""Torrent's Omada Gateway ACL control (LAN -> LAN only). Used by netops.py `acl ...`.

Source of truth: acl_ruleset.json. Every write needs an owner-approved Roundtable approval (hierarchy.require_approval),
uses Torrent's Omada account (read/write; ~/.omada_admin_env overrides it), and is followed by health checks plus an owner confirmation
window: no ✅ in time, a ❌, or a failed check rebuilds the previously applied state. `acl off` disables every
Torrent-managed rule immediately (loosening only, to recover from a lockout).

Managed objects are recognised by name: rules `T<order*10:03d>-<NAME>`, auto IP-port groups `TP<order*10:03d>`,
and the named IP groups from the rule set. Rules on the controller that Torrent did not create are never touched.
"""
import copy
import hashlib
import ipaddress
import json
import os
import re
import time
from pathlib import Path

RULESET = Path(__file__).with_name("acl_ruleset.json")
ADMIN_ENV = Path.home() / ".omada_admin_env"
PROTO = {"all": [256], "tcp": [6], "udp": [17], "tcpudp": [6, 17], "icmp": [1]}
RULE_NAME = re.compile(r"^T(\d{3})-")
PORT = re.compile(r"^\d{1,5}(-\d{1,5})?$")
CONFIRM_MIN = 15
KV_APPLIED, KV_API, KV_PROPOSAL = "netops:acl:applied", "netops:acl:api", "netops:acl:proposal:"
# Omada v2 body field values; `acl probe` confirms them against the live controller and stores the working profile.
API_DEFAULT = {"network": 0, "ip_group": 1, "ip_port_group": 2, "port_group_type": 1, "state_mode": 0}


# --------------------------------------------------------------------------- rule set: load, validate, plan

def load(path=RULESET):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(ruleset):
    return hashlib.sha256(json.dumps(ruleset, sort_keys=True).encode()).hexdigest()[:16]


def code(rule):
    return f"{int(round(rule['order'] * 10)):03d}"


def rule_name(rule):
    return f"T{code(rule)}-{rule['name']}"


def _vlan_of(ip, vlans):
    a = ipaddress.ip_address(ip)
    return next((int(v) for v, net in vlans.items() if a in ipaddress.ip_network(net)), None)


def side_vlans(side, ruleset):
    if "nets" in side:
        return set(side["nets"])
    return {_vlan_of(ip, ruleset["vlans"]) for g in side["groups"] for ip in ruleset["groups"][g]} - {None}


def validate(ruleset):
    """Return a list of problems; empty means safe to apply. Also run on every proposal Torrent makes."""
    errs, names, orders = [], set(), set()
    vlans = {int(v) for v in ruleset["vlans"]}
    for g, ips in ruleset["groups"].items():
        if not re.fullmatch(r"G_[A-Z0-9_]{1,24}", g):
            errs.append(f"group {g}: name must be G_UPPERCASE")
        for ip in ips:
            try:
                if _vlan_of(ip, ruleset["vlans"]) is None:
                    errs.append(f"group {g}: {ip} is outside the homelab VLANs (internet/WAN is never managed)")
            except ValueError:
                errs.append(f"group {g}: bad IP {ip}")
    rules = sorted(ruleset["rules"], key=lambda r: r["order"])
    for r in rules:
        n = r.get("name", "?")
        if r["order"] in orders or n in names:
            errs.append(f"{n}: duplicate order or name")
        orders.add(r["order"]); names.add(n)
        if not re.fullmatch(r"[A-Za-z0-9-]{3,40}", n):
            errs.append(f"{n}: name must be 3-40 letters, digits or dashes")
        if r.get("action") not in ("permit", "deny") or r.get("proto") not in PROTO:
            errs.append(f"{n}: action must be permit/deny and proto one of {sorted(PROTO)}")
        if not (0 < r["order"] < 100) or round(r["order"] * 10) != r["order"] * 10:
            errs.append(f"{n}: order must be between 0 and 100 with at most one decimal")
        for key in ("src", "dst"):
            side = r.get(key) or {}
            if len(side) != 1 or not (side.get("nets") or side.get("groups")):
                errs.append(f"{n}: {key} needs exactly one of nets or groups")
                continue
            if "nets" in side and not set(side["nets"]) <= vlans:
                errs.append(f"{n}: {key} has unknown VLANs {sorted(set(side['nets']) - vlans)}")
            if "groups" in side and not set(side["groups"]) <= set(ruleset["groups"]):
                errs.append(f"{n}: {key} has unknown groups")
        if r.get("ports"):
            if r["proto"] not in ("tcp", "udp", "tcpudp") or not all(PORT.match(p) for p in r["ports"]):
                errs.append(f"{n}: ports need proto tcp/udp/tcpudp and N or N-M values")
        if r["action"] == "deny" and not r.get("log"):
            errs.append(f"{n}: deny rules must log (VLAN Flow relies on it)")
    if errs:
        return errs
    # One-way VLAN 10: nothing may start connections into VLAN 10 without a documented reason.
    for r in rules:
        if r["action"] == "permit" and 10 in side_vlans(r["dst"], ruleset) and 10 not in side_vlans(r["src"], ruleset) \
                and not r.get("reverse_exception"):
            errs.append(f"{r['name']}: permits a connection INTO VLAN 10; add reverse_exception with the reason")
    # Lockout guard: the admin PC keeps its management access ahead of every deny.
    admin = [r for r in rules if r["name"] == "ALLOW-Admin-Infra" and r.get("enabled", True)]
    if not admin:
        errs.append("ALLOW-Admin-Infra (admin PC -> management VLANs) must exist and stay enabled")
    else:
        first_deny = min((r["order"] for r in rules if r["action"] == "deny" and r.get("enabled", True)), default=999)
        if admin[0]["order"] > first_deny:
            errs.append("ALLOW-Admin-Infra must come before every deny rule")
        if admin[0].get("phase", 1) > min((r.get("phase", 1) for r in rules if r["action"] == "deny"), default=9):
            errs.append("ALLOW-Admin-Infra must be in the first phase that contains a deny")
    # Shadowing: a permit placed after a deny that covers the same VLAN pair never matches.
    for d in (r for r in rules if r["action"] == "deny" and r.get("enabled", True)):
        for a in (r for r in rules if r["action"] == "permit" and r.get("enabled", True) and r["order"] > d["order"]):
            if side_vlans(a["src"], ruleset) <= side_vlans(d["src"], ruleset) and \
                    side_vlans(a["dst"], ruleset) <= side_vlans(d["dst"], ruleset) and "nets" in d["src"] and \
                    "nets" in d["dst"]:
                errs.append(f"{a['name']} (order {a['order']}) is shadowed by {d['name']} (order {d['order']})")
    return errs


def desired(ruleset, phase):
    """Rules to exist at `phase`, in creation order (copies carrying `_pg`, their IP-port group), and the IP-port
    groups they need. Identical destination+port sets share one group (the controller caps group counts)."""
    rules = [dict(r) for r in sorted(ruleset["rules"], key=lambda r: r["order"])
             if r.get("enabled", True) and r.get("phase", 1) <= phase]
    port_groups, by_key = {}, {}
    for r in rules:
        if r.get("ports"):
            ips = ([ipaddress.ip_network(ruleset["vlans"][str(v)]) for v in r["dst"]["nets"]] if "nets" in r["dst"]
                   else [ipaddress.ip_network(ip) for g in r["dst"]["groups"] for ip in ruleset["groups"][g]])
            spec = {"ips": sorted({(str(n.network_address), n.prefixlen) for n in ips}), "ports": list(r["ports"])}
            key = json.dumps(spec, sort_keys=True)
            if key not in by_key:
                by_key[key] = f"TP{code(r)}"
                port_groups[by_key[key]] = spec
            r["_pg"] = by_key[key]
    return rules, port_groups


def ip_groups_needed(rules):
    """Named IP groups the rules reference directly; a group used only as a ported destination lives inside an
    IP-port group instead and needs no IP group of its own."""
    need = set()
    for r in rules:
        need.update(r["src"].get("groups", []))
        if not r.get("ports"):
            need.update(r["dst"].get("groups", []))
    return need


def body(rule, net_ids, group_ids, api):
    """Omada Gateway ACL body. Missing VLAN networks are dropped; returns None when a side ends up empty."""
    def side(s, dst):
        if dst and rule.get("ports"):
            return api["ip_port_group"], [group_ids[rule.get("_pg") or f"TP{code(rule)}"]]
        if "nets" in s:
            return api["network"], [net_ids[v] for v in s["nets"] if v in net_ids]
        return api["ip_group"], [group_ids[g] for g in s["groups"]]
    st, sids = side(rule["src"], False)
    dt_, dids = side(rule["dst"], True)
    if not sids or not dids:
        return None
    return {"name": rule_name(rule), "status": True, "policy": 1 if rule["action"] == "permit" else 0,
            "protocols": PROTO[rule["proto"]], "sourceType": st, "sourceIds": sids,
            "destinationType": dt_, "destinationIds": dids, "type": 0,
            "direction": {"lanToWan": False, "lanToLan": True, "wanInIds": [], "vpnInIds": []},
            "stateMode": api["state_mode"], "syslog": bool(rule.get("log"))}


def fingerprint(b):
    return (b["name"], b["policy"], tuple(sorted(b["protocols"])), b["sourceType"], tuple(sorted(b["sourceIds"])),
            b["destinationType"], tuple(sorted(b["destinationIds"])), bool(b.get("syslog")), bool(b.get("status", True)))


def apply_proposal(ruleset, proposal):
    """proposal = {"summary", "upsert": [rule...], "remove": [name...], "groups": {G_X: [ips]}} -> new rule set."""
    new = copy.deepcopy(ruleset)
    for g, ips in (proposal.get("groups") or {}).items():
        new["groups"][g] = ips
    drop = set(proposal.get("remove") or [])
    up = {r["name"]: r for r in proposal.get("upsert") or []}
    new["rules"] = [up.pop(r["name"], r) for r in new["rules"] if r["name"] not in drop] + list(up.values())
    return new


# --------------------------------------------------------------------------- Omada write client

def admin_credentials():
    """Write login: the optional ~/.omada_admin_env, else Torrent's normal account (OMADA_USER, owner made it
    read/write on 2026-10-15). The controller itself rejects writes if the role is still read-only."""
    if not ADMIN_ENV.exists():
        return None, None
    env = dict(line.strip().split("=", 1) for line in ADMIN_ENV.read_text().splitlines() if "=" in line)
    return env["OMADA_ADMIN_USER"].strip().strip("'\""), env["OMADA_ADMIN_PASS"].strip().strip("'\"")


class Controller:
    """Thin wrapper over netops.Omada for site-level reads/writes with errorCode checking."""

    def __init__(self, om):
        self.om = om

    def call(self, method, path, data=None):
        r = self.om._req(method, f"/{self.om.cid}/api/v2/sites/{self.om.site}{path}", data, h=self.om.h)
        if r.get("errorCode") != 0:
            raise RuntimeError(f"Omada {method} {path}: {r.get('errorCode')} {r.get('msg')}")
        return r.get("result")

    def networks(self):
        data = self.call("GET", "/setting/lan/networks?currentPage=1&currentPageSize=100")["data"]
        return {int(n["vlan"]): n["id"] for n in data if n.get("vlan") is not None}

    def groups(self):
        return {g["name"]: g for g in self.call("GET", "/setting/profiles/groups")["data"]}

    def acls(self):
        res = self.call("GET", "/setting/firewall/acls?type=0&currentPage=1&currentPageSize=500")
        return res.get("data") or [], res

    def managed(self):
        rules, _ = self.acls()
        return [r for r in rules if RULE_NAME.match(r.get("name", ""))]


def wanted_groups(ruleset, rules, port_groups, api):
    w = {g: {"name": g, "type": 0, "ipList": [{"ip": ip, "mask": 32} for ip in ruleset["groups"][g]]}
         for g in sorted(ip_groups_needed(rules))}
    w.update({g: {"name": g, "type": api["port_group_type"], "ipList": [{"ip": ip, "mask": m} for ip, m in spec["ips"]],
                  "portList": spec["ports"], "portType": 0} for g, spec in port_groups.items()})
    return w


def _same(cur, w):
    return sorted((i["ip"], i["mask"]) for i in cur.get("ipList", [])) == sorted((i["ip"], i["mask"]) for i in w["ipList"]) \
        and sorted(cur.get("portList") or []) == sorted(w.get("portList") or [])


def is_managed_group(name, ruleset):
    return name in ruleset["groups"] or bool(re.fullmatch(r"TP\d{3}", name))


def ensure_groups(ctl, wanted, log):
    have = ctl.groups()
    for g, w in wanted.items():
        cur = have.get(g)
        try:
            if cur is None:
                ctl.call("POST", "/setting/profiles/groups", w)
                log(f"created group {g}")
            elif not _same(cur, w):
                ctl.call("PATCH", f"/setting/profiles/groups/{cur['type']}/{cur['groupId']}", {**w, "groupId": cur["groupId"]})
                log(f"updated group {g}")
        except RuntimeError as e:
            if "-33724" in str(e) or "limit" in str(e).lower():
                other = sorted(n for n, x in have.items() if x.get("type") == w["type"] and not x.get("buildIn"))
                raise RuntimeError(f"{e}. The controller's group limit is full; groups of this type now: {other}. "
                                   "Remove an unused one (owner decision) or shrink the rule set.") from e
            raise
    have = ctl.groups()
    return {g: have[g]["groupId"] for g in wanted}


def drop_unused_groups(ctl, ruleset, keep, log):
    for name, grp in ctl.groups().items():
        if is_managed_group(name, ruleset) and name not in keep:
            try:
                ctl.call("DELETE", f"/setting/profiles/groups/{grp['type']}/{grp['groupId']}")
                log(f"removed unused group {name}")
            except RuntimeError as e:
                log(f"left group {name}: {e}")


def sync(ctl, ruleset, phase, api, log):
    """Make the managed rules equal to `desired(ruleset, phase)`. Rebuild (delete + recreate in order) on any drift,
    because the controller appends new rules and ordering is what makes the policy correct. Unused Torrent groups
    are removed before new ones are created, so the controller's group limit is not wasted."""
    rules, port_groups = desired(ruleset, phase)
    wanted = wanted_groups(ruleset, rules, port_groups, api)
    net_ids = ctl.networks()
    have = ctl.groups()
    live = ctl.managed()
    if all(g in have and _same(have[g], w) for g, w in wanted.items()):
        ids = {g: have[g]["groupId"] for g in wanted}
        bodies = [b for b in (body(r, net_ids, ids, api) for r in rules) if b]
        if [fingerprint(b) for b in bodies] == [fingerprint({**x, "status": x.get("status", True)}) for x in live]:
            drop_unused_groups(ctl, ruleset, set(wanted), log)
            log("no change: controller already matches")
            return False
    for x in live:
        ctl.call("DELETE", f"/setting/firewall/acls/{x['id']}")
    log(f"removed {len(live)} managed rules")
    drop_unused_groups(ctl, ruleset, set(wanted), log)
    ids = ensure_groups(ctl, wanted, log)
    created = 0
    for r in rules:
        b = body(r, net_ids, ids, api)
        if b is None:
            log(f"skipped {r['name']}: its VLAN networks do not exist on the controller")
            continue
        ctl.call("POST", "/setting/firewall/acls", b)
        created += 1
    log(f"created {created} rules in order")
    return True


FIELDS = ("name", "action", "protocols", "source type", "sources", "destination type", "destinations", "logging",
          "enabled")


def drift(ctl, ruleset, phase, api):
    """Differences between the rule set at `phase` and the controller's Torrent-managed rules, including setting
    changes made by hand in the Omada portal (e.g. logging switched on). Empty list = in sync."""
    rules, port_groups = desired(ruleset, phase)
    wanted = wanted_groups(ruleset, rules, port_groups, api)
    have, net_ids = ctl.groups(), ctl.networks()
    out = [f"group {g}: missing on controller" for g in wanted if g not in have]
    out += [f"group {g}: members differ" for g, w in wanted.items() if g in have and not _same(have[g], w)]
    ids = {g: have[g]["groupId"] for g in wanted if g in have}
    want = []
    for r in rules:
        try:
            b = body(r, net_ids, ids, api)
        except KeyError:
            out.append(f"{rule_name(r)}: cannot be built (group missing)")
            continue
        if b:
            want.append(b)
    live = ctl.managed()
    live_by = {x["name"]: x for x in live}
    for b in want:
        x = live_by.get(b["name"])
        if not x:
            out.append(f"{b['name']}: missing on controller")
            continue
        fa, fb = fingerprint(b), fingerprint({**x, "status": x.get("status", True)})
        if fa != fb:
            out.append(f"{b['name']}: differs in " + ", ".join(FIELDS[i] for i in range(len(fa)) if fa[i] != fb[i]))
    names = {b["name"] for b in want}
    out += [f"{x['name']}: on controller but not in the rule set" for x in live if x["name"] not in names]
    if not out and [x["name"] for x in live] != [b["name"] for b in want]:
        out.append("rule order differs from the rule set")
    return out


def disable_all(ctl, log):
    n = 0
    for x in ctl.managed():
        if x.get("status", True):
            ctl.call("PATCH", f"/setting/firewall/acls/{x['id']}", {**x, "status": False})
            n += 1
    log(f"disabled {n} Torrent-managed rules")
    return n


def probe(ctl, api_candidates, log):
    """Create disabled throwaway objects to learn which body format this controller accepts, then delete them."""
    net_ids = ctl.networks()
    a, b = (net_ids.get(50), net_ids.get(80)) if 50 in net_ids and 80 in net_ids else list(net_ids.values())[:2]
    for cand in api_candidates:
        made = []
        try:
            ctl.call("POST", "/setting/profiles/groups", {"name": "TP999", "type": cand["port_group_type"],
                                                          "ipList": [{"ip": "192.0.2.0", "mask": 24}],
                                                          "portList": ["1400"], "portType": 0})
            gid = ctl.groups()["TP999"]["groupId"]
            made.append(("group", cand["port_group_type"], gid))
            for name, dst_type, dst in (("T999-probe-net", cand["network"], [b]), ("T998-probe-port", cand["ip_port_group"], [gid])):
                bd = {"name": name, "status": False, "policy": 1, "protocols": [6], "sourceType": cand["network"],
                      "sourceIds": [a], "destinationType": dst_type, "destinationIds": dst, "type": 0,
                      "direction": {"lanToWan": False, "lanToLan": True, "wanInIds": [], "vpnInIds": []},
                      "stateMode": cand["state_mode"], "syslog": True}
                ctl.call("POST", "/setting/firewall/acls", bd)
                got = next(x for x in ctl.acls()[0] if x.get("name") == name)
                made.append(("acl", None, got["id"]))
                if (got.get("destinationType"), sorted(got.get("destinationIds") or [])) != (dst_type, sorted(dst)):
                    raise RuntimeError(f"controller stored {name} differently: {json.dumps(got)[:400]}")
            log(f"probe OK with {cand}")
            return cand, ctl.acls()[1]
        except (RuntimeError, StopIteration, KeyError) as e:
            log(f"probe variant {cand} failed: {e}")
        finally:
            for kind, typ, oid in reversed(made):
                try:
                    ctl.call("DELETE", f"/setting/firewall/acls/{oid}" if kind == "acl"
                             else f"/setting/profiles/groups/{typ}/{oid}")
                except RuntimeError as e:
                    log(f"cleanup of probe {kind} {oid} failed: {e}")
    return None, None


def api_candidates():
    base = dict(API_DEFAULT)
    out = [base]
    for k, v in (("ip_port_group", 1), ("ip_port_group", 3), ("port_group_type", 2)):
        c = dict(base); c[k] = v
        out.append(c)
    return out


# --------------------------------------------------------------------------- reporting

def describe(rule, ruleset):
    def side(s):
        return ", ".join(f"VLAN {v}" for v in s["nets"]) if "nets" in s else ", ".join(s["groups"])
    ports = f" {rule['proto'].upper()} {','.join(rule['ports'])}" if rule.get("ports") else f" {rule['proto'].upper()}"
    return f"`{rule_name(rule)}` {'✅ permit' if rule['action'] == 'permit' else '⛔ deny'} {side(rule['src'])} → {side(rule['dst'])}{ports}"


def phase_text(ruleset, phase, prev_phase=0):
    rules, groups = desired(ruleset, phase)
    new = [r for r in rules if r.get("phase", 1) > prev_phase]
    lines = [describe(r, ruleset) for r in new]
    return (f"{len(rules)} rules at phase {phase} ({len(new)} new) + {len(ip_groups_needed(rules))} IP groups, "
            f"{len(groups)} IP-port groups.\n" + "\n".join(lines))
