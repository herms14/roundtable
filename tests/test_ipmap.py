"""ipmap.py: Traefik and vault parsing, the per-IP merge and status rules, and agent records (no network)."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

ROOT = Path(__file__).resolve().parents[1]
TRAEFIK = """# comment
http:
  routers:
    study:
      rule: Host(`study.hrmsmrflrii.xyz`)
      service: study
      middlewares:
        - authentik-auth@file
    roundtable-avatars:
      rule: Host(`roundtable.hrmsmrflrii.xyz`) && PathPrefix(`/avatars/`)
      service: roundtable@file
    dash:
      rule: Host(`traefik.hrmsmrflrii.xyz`)
      service: api@internal
  middlewares:
    authentik-auth:
      forwardAuth:
        address: http://192.168.40.21:9000/outpost.goauthentik.io/auth/traefik
  services:
    study:
      loadBalancer:
        servers:
          - url: http://192.168.40.38:8090
    roundtable:
      loadBalancer:
        servers:
        - url: "http://192.168.40.16:8088"
"""
VAULT = """# IP map
### Core Application Services

| IP | Hostname | Purpose |
|----|----------|---------|
| 192.168.40.20 | traefik-lxc (LXC 203) | Reverse Proxy |
| 192.168.40.10 | ~~docker-vm-core-utilities01~~ | **DECOMMISSIONED** - Replaced |
| 192.168.40.15, 17-19 | Additional Docker hosts |
| 192.168.40.99 | old-box | Gone |

## Roundtable IP registry (maintained by Diallos)

| IP | Service | Purpose | URL | Status |
|----|---------|---------|-----|--------|
| 192.168.40.77 | ghost | should be ignored | | ok |
"""


class IpMap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["OLYMPUS_HOME"] = self.tmp.name
        sys.path.insert(0, str(ROOT))
        for m in ("olympus_core", "ipam", "inventory", "ipmap"):
            sys.modules.pop(m, None)
        self.o = importlib.import_module("olympus_core")
        self.o.DATA_DIR = Path(self.tmp.name) / ".olympus"
        self.o.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.o.DB_FILE = self.o.DATA_DIR / "olympus.db"
        self.kv = {}
        self.o.kv_get = Mock(side_effect=lambda k, d=None: self.kv.get(k, d))
        self.o.kv_set = Mock(side_effect=lambda k, v: self.kv.__setitem__(k, v))
        self.m = importlib.import_module("ipmap")

    def tearDown(self):
        self.tmp.cleanup()

    def test_traefik_routes_map_backend_ips_to_public_hosts(self):
        t = self.m.parse_traefik(TRAEFIK)
        self.assertEqual(t["192.168.40.38"], [{"host": "study.hrmsmrflrii.xyz", "port": "8090", "router": "study"}])
        self.assertEqual(t["192.168.40.16"][0]["host"], "roundtable.hrmsmrflrii.xyz", "@file suffix and quotes")
        self.assertNotIn("192.168.40.21", t, "middleware addresses are not backends")

    def test_vault_tables_parse_and_the_auto_section_is_ignored(self):
        v = self.m.parse_vault(VAULT)
        self.assertEqual(v["192.168.40.20"]["purpose"], "Reverse Proxy")
        self.assertTrue(v["192.168.40.10"]["retired"])
        self.assertNotIn("192.168.40.77", v, "Diallos must not read back his own section")
        self.assertNotIn("192.168.40.15", v)

    def test_vlan_usage_counts_live_addresses_and_pool_leases(self):
        net = {"vlans": [{"vlan": 40, "name": "Production", "subnet": "192.168.40.1/24", "dhcp": True,
                          "dhcp_range": {"start": "192.168.40.50", "end": "192.168.40.254"}}],
               "active_clients": [{"ip": "192.168.40.120", "vlan": 40}, {"ip": "192.168.40.120", "vlan": 40},
                                  {"ip": "192.168.40.30", "vlan": 40}]}
        row = lambda ip, status, *src: {"ip": ip, "status": status, "sources": list(src)}
        rows = [row("192.168.40.1", "ok", "omada-vlan"), row("192.168.40.20", "ok", "proxmox", "traefik"),
                row("192.168.40.30", "undocumented", "network-client"),     # connected now: used
                row("192.168.40.31", "undocumented", "network-client"),     # only remembered by the controller
                row("192.168.40.32", "not-seen", "vault"), row("192.168.40.33", "retired", "vault"),
                row("192.168.40.40", "reserved", "ipam"), row("192.168.10.5", "ok", "proxmox")]
        strong = {"proxmox", "omada-device", "omada-vlan", "dhcp-reservation", "roundtable-host", "traefik", "ipam"}
        (u,) = self.m.usage(net, rows, strong)
        self.assertEqual((u["size"], u["used"], u["free"]), (254, 5, 249))
        self.assertEqual(u["static"], {"size": 49, "used": 4, "free": 45})
        self.assertEqual((u["pool"]["size"], u["pool"]["used"], u["pool"]["free"]), (205, 1, 204))
        self.assertEqual(self.m.usage({}, rows, strong), [], "no network inventory: no usage rows, no crash")

    def test_merge_statuses_purposes_and_agent_records(self):
        inv = MagicMock()
        inv.get = Mock(side_effect=lambda kind, fresh=False: {
            "compute": {"guests": [{"id": 212, "kind": "LXC", "node": "node02", "status": "running", "name": "study-lxc",
                                    "ip": "192.168.40.38/24"},
                                   {"id": 203, "kind": "LXC", "node": "node01", "status": "running", "name": "traefik-lxc",
                                    "ip": "192.168.40.20"},
                                   {"id": 300, "kind": "VM", "node": "node03", "status": "running", "name": "mystery",
                                    "ip": "192.168.40.41"}]},
            "network": {"vlans": [{"vlan": 40, "name": "Production", "subnet": "192.168.40.1/24", "dhcp_range": {"start": "192.168.40.50", "end": "192.168.40.254"}}],
                        "devices": [], "dhcp_reservations": [],
                        "clients": [{"ip": "192.168.40.120", "vlan": 40, "name": "phone"},
                                    {"ip": "192.168.40.10", "vlan": 40, "name": "BC-24-11-00-00-01"}]}}[kind])
        sys.modules["inventory"] = inv
        self.o.load_hosts = Mock(return_value={})
        self.m.traefik = Mock(return_value=self.m.parse_traefik(TRAEFIK))
        self.m.vault_map = Mock(return_value=self.m.parse_vault(VAULT))
        ipam = importlib.import_module("ipam")
        with ipam._db() as con:
            con.execute("INSERT INTO ip_reservations VALUES('192.168.40.39','next-app',40,'pending','x','2999-01-01T00:00:00Z')")
        self.m.record("192.168.40.38", "study-lxc", "Study notebook", "https://study.hrmsmrflrii.xyz", by="builder")
        self.assertFalse(self.m.record("192.168.40.38", "guess", "AI guess", by="diallos-ai", suggested=True),
                         "a suggestion never overwrites an agent's record")
        rows = {r["ip"]: r for r in self.m.collect()["rows"]}
        self.assertEqual(rows["192.168.40.38"]["status"], "ok")
        self.assertEqual(rows["192.168.40.38"]["purpose"], "Study notebook")
        self.assertEqual(rows["192.168.40.38"]["purpose_by"], "builder")
        self.assertIn("https://study.hrmsmrflrii.xyz", rows["192.168.40.38"]["urls"])
        self.assertEqual(rows["192.168.40.20"]["purpose"], "Reverse Proxy")
        self.assertEqual(rows["192.168.40.41"]["status"], "undocumented")
        self.assertEqual(rows["192.168.40.39"]["status"], "reserved")
        self.assertEqual(rows["192.168.40.99"]["status"], "not-seen")
        self.assertEqual(rows["192.168.40.10"]["status"], "retired", "remembered Omada clients alone are not proof of life")
        self.assertEqual((rows["192.168.40.1"]["status"], rows["192.168.40.1"]["service"]), ("undocumented", "Gateway (Production)"))
        self.assertNotIn("192.168.40.120", rows, "DHCP-pool leases are not assignments")
        self.assertNotIn("BC-24-11-00-00-01", rows["192.168.40.10"]["names"], "bare MACs are not names")
        self.m.record("192.168.40.41", "mystery-vm", "Probably a lab VM", by="diallos-ai", suggested=True)
        r = {r["ip"]: r for r in self.m.collect()["rows"]}["192.168.40.41"]
        self.assertEqual((r["status"], r["purpose_by"]), ("undocumented", "AI suggestion"))
        self.assertEqual(r["service"], "mystery", "a real name beats the AI's guess")
        self.assertIn("192.168.40.41 | mystery | Probably a lab VM *(AI suggestion)*",
                      self.m.vault_section(self.m.collect()))
        with self.assertRaises(ValueError):
            self.m.record("not-an-ip", "x")

    def test_vault_section_has_ip_and_name_order(self):
        row = lambda ip, name: {"ip": ip, "service": name, "purpose": "", "purpose_by": "", "urls": [],
                                "status": "ok"}
        rows = [row("192.168.40.9", "node10"), row("192.168.40.10", ""), row("192.168.40.39", "Node2"),
                row("192.168.40.100", "alpha")]
        self.assertEqual([r["ip"] for r in self.m.by_name(rows)],
                         ["192.168.40.100", "192.168.40.39", "192.168.40.9", "192.168.40.10"],
                         "A-Z, natural numbers, case-insensitive, unnamed last")
        text = self.m.vault_section({"rows": rows})
        by_ip, by_name = text.split("> [!list]- Sorted by name")
        self.assertIn("> [!list]+ Sorted by IP address", by_ip)
        self.assertLess(by_ip.index("192.168.40.9 |"), by_ip.index("192.168.40.100 |"))
        self.assertLess(by_name.index("192.168.40.100 |"), by_name.index("192.168.40.9 |"))
        self.kv["ipmap:published"] = self.m._fingerprint({"rows": rows})
        self.assertIsNone(self.m.publish({"rows": rows}), "an unchanged map is not rewritten")


if __name__ == "__main__":
    unittest.main()
