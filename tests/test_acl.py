"""Torrent's Gateway ACL control: the rule set's safety checks, phase planning and the Omada body format."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import aclctl  # noqa: E402  (standard library only)

RS = aclctl.load()
API = aclctl.API_DEFAULT


def rule(name):
    return next(r for r in RS["rules"] if r["name"] == name)


class RuleSetSafety(unittest.TestCase):
    def test_shipped_rule_set_is_clean(self):
        self.assertEqual(aclctl.validate(RS), [])

    def test_every_rule_is_lan_to_lan_so_internet_is_untouched(self):
        nets = set(RS["vlans"])
        for r in RS["rules"]:
            for side in ("src", "dst"):
                self.assertTrue({str(v) for v in r[side].get("nets", [])} <= nets)
        b = aclctl.body(rule("DENY-Prod-Rest"), {v: f"n{v}" for v in (1, 20, 40, 90, 91)}, {}, API)
        self.assertEqual(b["direction"], {"lanToWan": False, "lanToLan": True, "wanInIds": [], "vpnInIds": []})

    def test_vlan10_is_one_way(self):
        bad = aclctl.apply_proposal(RS, {"upsert": [{"order": 24.5, "name": "ALLOW-Prod-to-PC", "action": "permit",
                                                     "src": {"nets": [40]}, "dst": {"nets": [10]}, "proto": "all",
                                                     "log": True, "phase": 2}]})
        self.assertTrue(any("INTO VLAN 10" in e for e in aclctl.validate(bad)))
        ok = copy.deepcopy(bad)
        ok["rules"][-1]["reverse_exception"] = "test"
        self.assertEqual(aclctl.validate(ok), [])

    def test_shadowed_permit_rejected(self):
        bad = aclctl.apply_proposal(RS, {"upsert": [{"order": 39, "name": "ALLOW-Late", "action": "permit",
                                                     "src": {"groups": ["G_HELIOS"]}, "dst": {"nets": [20]},
                                                     "proto": "tcp", "ports": ["22"], "log": True, "phase": 4}]})
        self.assertTrue(any("shadowed by DENY-Prod-Rest" in e for e in aclctl.validate(bad)))

    def test_admin_lockout_guard(self):
        bad = aclctl.apply_proposal(RS, {"remove": ["ALLOW-Admin-Infra"]})
        self.assertTrue(any("ALLOW-Admin-Infra" in e for e in aclctl.validate(bad)))
        late = copy.deepcopy(RS)
        next(r for r in late["rules"] if r["name"] == "ALLOW-Admin-Infra")["phase"] = 4
        self.assertTrue(any("first phase" in e for e in aclctl.validate(late)))

    def test_denies_must_log_and_groups_stay_internal(self):
        bad = copy.deepcopy(RS)
        next(r for r in bad["rules"] if r["name"] == "DENY-Guest-Out")["log"] = False
        bad["groups"]["G_DNS_OUT"] = ["1.1.1.1"]
        errs = aclctl.validate(bad)
        self.assertTrue(any("must log" in e for e in errs))
        self.assertTrue(any("outside the homelab" in e for e in errs))


class GuestIsolation(unittest.TestCase):
    def test_guest_gets_pihole_dns_and_nothing_else(self):
        rules = aclctl.desired(RS, 3)[0]
        order = [r["name"] for r in rules]
        dns = rule("ALLOW-DNS-PiHole")
        self.assertIn(50, dns["src"]["nets"])
        self.assertEqual((dns["dst"], dns["proto"], dns["ports"]), ({"groups": ["G_PIHOLE"]}, "tcpudp", ["53"]))
        self.assertEqual(RS["groups"]["G_PIHOLE"], ["192.168.90.53", "192.168.90.60"])  # Pi-hole + Technitium
        self.assertLess(order.index("ALLOW-DNS-PiHole"), order.index("DENY-Guest-Out"))
        guest_permits = [r["name"] for r in RS["rules"] if r["action"] == "permit" and r.get("enabled", True)
                         and 50 in aclctl.side_vlans(r["src"], RS)]
        self.assertEqual(guest_permits, ["ALLOW-DNS-PiHole"])
        deny = rule("DENY-Guest-Out")
        self.assertEqual(set(deny["dst"]["nets"]), {int(v) for v in RS["vlans"]} - {50})


class Planning(unittest.TestCase):
    def test_phases_are_cumulative_and_skip_undecided_rules(self):
        names = lambda p: [r["name"] for r in aclctl.desired(RS, p)[0]]  # noqa: E731
        self.assertEqual(names(1), [])
        self.assertEqual(names(2), ["ALLOW-Admin-Infra", "ALLOW-Admin-RDP-HyperV", "ALLOW-Internal-Sonos",
                                    "ALLOW-Sonos-AppEvents", "ALLOW-Internal-Services", "DENY-to-Internal"])
        self.assertTrue(set(names(2)) < set(names(3)) < set(names(4)))
        self.assertNotIn("ALLOW-HA-IoT", names(4))
        self.assertNotIn("ALLOW-Runner-Deploy", names(4))

    def test_creation_order_keeps_permits_before_their_denies(self):
        order = [r["name"] for r in aclctl.desired(RS, 4)[0]]
        self.assertLess(order.index("ALLOW-Sonos-AppEvents"), order.index("DENY-to-Internal"))
        self.assertLess(order.index("ALLOW-Backup-All"), order.index("DENY-to-HyperV"))
        self.assertLess(order.index("ALLOW-IoT-Managers"), order.index("DENY-to-IoT"))

    def test_ports_become_ip_port_groups(self):
        rules, groups = aclctl.desired(RS, 2)
        self.assertEqual(groups["TP060"], {"ips": [("192.168.80.0", 24)], "ports": ["3389"]})
        self.assertEqual(groups["TP120"]["ports"], ["3400", "3401", "3500"])
        nets = {v: f"net{v}" for v in (1, 10, 20, 30, 40, 50, 60, 90)}  # 70/80/91 missing on the controller
        ids = {"G_ADMIN_PC": "g-admin", "TP060": "tp060", "TP120": "tp120"}
        rdp = aclctl.body(rule("ALLOW-Admin-RDP-HyperV"), nets, ids, API)
        self.assertEqual((rdp["name"], rdp["sourceType"], rdp["sourceIds"], rdp["destinationType"], rdp["destinationIds"],
                          rdp["protocols"]), ("T060-ALLOW-Admin-RDP-HyperV", 1, ["g-admin"], 2, ["tp060"], [6, 17]))
        deny = aclctl.body(rule("DENY-to-Internal"), nets, ids, API)
        self.assertEqual((deny["policy"], deny["destinationIds"], deny["syslog"]), (0, ["net10"], True))
        self.assertNotIn("net80", deny["sourceIds"])
        self.assertIsNone(aclctl.body(rule("ALLOW-HyperV-to-Prod"), nets, ids, API))

    def test_group_budget_fits_the_controller(self):
        """The ER605 controller holds 16 IP groups and two already exist; Torrent needs IP groups only for sources and
        port-less destinations, and identical destination+port sets share one IP-port group."""
        self.assertEqual(aclctl.ip_groups_needed(aclctl.desired(RS, 2)[0]), {"G_ADMIN_PC"})
        rules4, pgs4 = aclctl.desired(RS, 4)
        self.assertLessEqual(len(aclctl.ip_groups_needed(rules4)), 12)
        for unused in ("G_PIHOLE", "G_NAS", "G_SYSLOG"):
            self.assertNotIn(unused, aclctl.ip_groups_needed(rules4))
        pg = {r["name"]: r.get("_pg") for r in rules4}
        self.assertEqual(pg["ALLOW-Syslog-Devices"], pg["ALLOW-Syslog-Controller"])
        self.assertEqual(pg["ALLOW-Ansible-Omada"], pg["ALLOW-Monitoring-Omada"])
        self.assertEqual(len(pgs4), len(set(pg.values()) - {None}))
        self.assertLessEqual(len(pgs4), 12)

    def test_sync_frees_unused_groups_before_creating(self):
        class Fake:
            def __init__(self):
                self.g = {n: {"groupId": n, "type": 0, "ipList": [{"ip": "192.168.0.9", "mask": 32}]}
                          for n in ("IPGroup_Any", "OPNsense_DNS", "G_PIHOLE", "G_NAS", "G_SRE")}
                self.rules, self.ops = [], []

            def networks(self):
                return {v: f"net{v}" for v in (1, 10, 20, 30, 40, 50, 60, 80, 90)}

            def groups(self):
                return dict(self.g)

            def managed(self):
                return list(self.rules)

            def call(self, method, path, data=None):
                self.ops.append((method, path.split("/")[-1] if method == "DELETE" else (data or {}).get("name")))
                if method == "POST" and path.endswith("/groups"):
                    if sum(1 for x in self.g.values() if x["type"] == data["type"]) >= 16:
                        raise RuntimeError("Omada POST: -33724 The number of IpGroups has reached the limit.")
                    self.g[data["name"]] = {**data, "groupId": data["name"]}
                elif method == "DELETE" and "/groups/" in path:
                    self.g.pop(path.split("/")[-1])
                elif method == "POST":
                    self.rules.append({**data, "id": data["name"]})
        f = Fake()
        logs = []
        self.assertTrue(aclctl.sync(f, RS, 2, API, logs.append))
        self.assertNotIn("G_PIHOLE", f.g)
        self.assertIn("OPNsense_DNS", f.g)
        self.assertEqual(sorted(n for n in f.g if n.startswith(("G_", "TP"))), ["G_ADMIN_PC", "TP060", "TP120"])
        self.assertEqual([r["name"] for r in f.rules][0], "T050-ALLOW-Admin-Infra")
        self.assertLess(f.ops.index(("DELETE", "G_PIHOLE")), f.ops.index(("POST", "G_ADMIN_PC")))
        self.assertFalse(aclctl.sync(f, RS, 2, API, logs.append))
        self.assertEqual(aclctl.drift(f, RS, 2, API), [])
        f.rules[0]["syslog"] = not f.rules[0]["syslog"]  # owner flips logging in the Omada portal
        self.assertEqual(aclctl.drift(f, RS, 2, API), [f"{f.rules[0]['name']}: differs in logging"])

    def test_fingerprint_ignores_id_order(self):
        a = {"name": "x", "policy": 0, "protocols": [17, 6], "sourceType": 0, "sourceIds": ["b", "a"],
             "destinationType": 0, "destinationIds": ["c"], "syslog": True}
        b = dict(a, protocols=[6, 17], sourceIds=["a", "b"], id="live-id", status=True)
        self.assertEqual(aclctl.fingerprint(a), aclctl.fingerprint(b))


class Routing(unittest.TestCase):
    def test_owner_phrasings_reach_the_acl_handler(self):
        import re
        src = (ROOT / "netops.py").read_text(encoding="utf-8")
        acl_re = re.compile(re.search(r'ACL_RE = re\.compile\(r"(.+?)", re\.I\)', src).group(1), re.I)
        for q in ("probe the omada api", "probe the ACL API", "apply phase 2", "acl status", "acl off",
                  "why is the firewall blocking my phone", "inter-vlan issue"):
            self.assertTrue(acl_re.search(q), q)
        self.assertFalse(acl_re.search("why is the internet slow"))


if __name__ == "__main__":
    unittest.main()
