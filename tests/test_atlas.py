"""atlas.py: layout + live facts overlay, change detection and rendering (no network, no Proxmox/Docker/Omada)."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("fcntl", types.SimpleNamespace(flock=lambda *a: None, LOCK_EX=2, LOCK_NB=4))
core = MagicMock(CODE_DIR=ROOT, DATA_DIR=Path(tempfile.mkdtemp()))
sys.modules["olympus_core"] = core
import atlas  # noqa: E402


def guest(name, node="node01", kind="LXC", status="running", size=(2, 2.0, 20.0), ip="192.168.40.50", template=False):
    return {"name": name, "node": node, "kind": kind, "status": status, "template": template, "ip": ip, "size": list(size)}


def layout():
    return {
        "container_hosts": ["dock"], "ignore_containers": r"^alloy$", "hidden_containers": [],
        "warnings": [{"tab": "svc", "node": "app", "t": "App warning", "d": ""}],
        "atlas": [
            {"id": "svc", "groups": [{"id": "g1", "label": "Apps"}], "layout": [["g1"]],
             "nodes": [{"id": "app", "g": "g1", "t": "container", "n": "app", "f": {"port": "80"},
                        "live": {"containers": ["app"], "host": "dock", "base": {"state": "running"}}},
                       {"id": "web", "g": "g1", "t": "lxc", "n": "web-lxc", "f": {"host": "CT201 · node01"},
                        "live": {"guest": 201, "base": {"name": "web-lxc", "node": "node01", "ip": "192.168.40.50",
                                                        "status": "running"}}}],
             "edges": [["web", "app", "", "dep"], ["web", "g1", "", "dep"]]},
            {"id": "hw", "groups": [{"id": "h1", "label": "node01"}, {"id": "h2", "label": "node02"}],
             "layout": [["h1", "h2"]],
             "nodes": [{"id": "g201", "g": "h1", "t": "lxc", "n": "CT201 web-lxc", "f": {"host": "node01", "size": "2 vCPU · 2 GiB · 20 GB"},
                        "live": {"guest": 201, "spec": True, "base": {"name": "web-lxc", "node": "node01",
                                                                      "ip": "192.168.40.50", "status": "running",
                                                                      "size": [2, 2.0, 20.0]}}}],
             "edges": []},
            {"id": "agents", "groups": [{"id": "gov", "label": "Gov"}], "layout": [["gov"]],
             "nodes": [{"id": "scribe", "g": "gov", "t": "agent", "n": "Miriel", "f": {"model": "m1", "channel": "#docs"},
                        "live": {"agent": "scribe", "base": {"name": "Miriel", "model": "m1", "channel": "docs",
                                                             "reports_to": "governance"}}}],
             "edges": []}]}


def facts():
    return {"guests": {"201": guest("web-lxc")}, "nodes": {"node01": {"status": "online", "ram_max_gb": 60}},
            "containers": {"dock": {"app": "running", "alloy": "running"}}, "devices": {},
            "agents": {"scribe": {"name": "Miriel", "model": "m1", "channel": "docs", "reports_to": "governance",
                                  "group": "", "role": "Scribe"}},
            "units": {}}


def nodes(atlas_out, did):
    return {n["id"]: n for n in next(d for d in atlas_out if d["id"] == did)["nodes"]}


class OverlayTest(unittest.TestCase):
    def test_unchanged_lab_reproduces_the_layout(self):
        lay = layout()
        out, warnings, gone = atlas.overlay(lay, facts(), {}, "2026-10-18")
        for d0, d1 in zip(lay["atlas"], out):
            want = [{k: v for k, v in n.items() if k != "live"} for n in d0["nodes"]]
            self.assertEqual(want, d1["nodes"])
            self.assertEqual(d0["edges"], d1["edges"])
        self.assertEqual(lay["warnings"], warnings)
        self.assertEqual([], gone)

    def test_removed_items_drop_with_their_edges_and_warnings(self):
        f = facts()
        f["containers"]["dock"].pop("app")
        out, warnings, gone = atlas.overlay(layout(), f, {}, "2026-10-18")
        self.assertNotIn("app", nodes(out, "svc"))
        self.assertEqual([["web", "g1", "", "dep"]], out[0]["edges"])
        self.assertEqual([], warnings)
        self.assertEqual(["app"], gone)

    def test_unreachable_host_keeps_its_containers(self):
        f = facts()
        del f["containers"]["dock"]
        out, _, gone = atlas.overlay(layout(), f, {}, "2026-10-18")
        self.assertIn("app", nodes(out, "svc"))
        self.assertEqual([], gone)

    def test_rename_move_resize_and_status(self):
        f = facts()
        f["guests"]["201"] = guest("web2-lxc", node="node02", status="stopped", size=(4, 2.0, 20.0))
        f["containers"]["dock"]["app"] = "exited"
        out, _, _ = atlas.overlay(layout(), f, {}, "2026-10-18")
        hw, svc = nodes(out, "hw")["g201"], nodes(out, "svc")
        self.assertEqual("CT201 web2-lxc", hw["n"])
        self.assertEqual("h2", hw["g"])
        self.assertEqual("4 vCPU · 2 GiB · 20 GB", hw["f"]["size"])
        self.assertEqual("CT201 · node02", svc["web"]["f"]["host"])
        self.assertEqual(("down", "Stopped in Proxmox"), (svc["web"]["s"], svc["web"]["note"]))
        self.assertEqual(("down", "app exited"), (svc["app"]["s"], svc["app"]["note"]))

    def test_new_guest_container_and_agents_are_placed(self):
        f = facts()
        f["guests"]["213"] = guest("uptime-lxc", node="node02")
        f["containers"]["dock"]["it-tools"] = "running"
        f["containers"]["dock"]["vibrant_wilson"] = "created"   # leftover random-named container: ignored
        f["agents"]["archivist"] = dict(f["agents"]["scribe"], name="Thops")
        f["agents"]["greeter"] = dict(f["agents"]["scribe"], name="Kenneth", reports_to="boss")
        seen = {}
        out, warnings, _ = atlas.overlay(layout(), f, seen, "2026-10-18")
        self.assertEqual("h2", nodes(out, "hw")["g213"]["g"])
        self.assertEqual("new", nodes(out, "svc")["new-dock-it-tools"]["g"])
        self.assertNotIn("new-dock-vibrant_wilson", nodes(out, "svc"))
        self.assertNotIn("new-dock-alloy", nodes(out, "svc"))
        self.assertEqual("gov", nodes(out, "agents")["archivist"]["g"])
        self.assertEqual("new", nodes(out, "agents")["greeter"]["g"])
        self.assertIn(["new"], next(d for d in out if d["id"] == "agents")["layout"])
        self.assertIn("New guest CT213 uptime-lxc", [w["t"] for w in warnings])
        self.assertEqual("2026-10-18", seen["guest:213"])


class DiffTest(unittest.TestCase):
    def test_first_run_reports_nothing(self):
        self.assertEqual(([], []), atlas.diff(None, facts()))

    def test_structural_changes_versus_status_flips(self):
        a, b = facts(), facts()
        b["guests"]["213"] = guest("uptime-lxc")
        b["guests"]["201"] = dict(b["guests"]["201"], status="stopped")
        b["containers"]["dock"].pop("app")
        b["containers"]["dock"]["zen_einstein"] = "exited"
        b["agents"]["scribe"] = dict(b["agents"]["scribe"], model="m2")
        big, small = atlas.diff(a, b)
        self.assertEqual(["Added LXC 213 uptime-lxc on node01 (2 vCPU · 2 GiB · 20 GB)",
                          "Removed container app from dock", "Agent Miriel: model m1 → m2"], big)
        self.assertEqual(["LXC 201 web-lxc: running → stopped"], small)


class RenderTest(unittest.TestCase):
    def test_render_fills_the_single_data_slot(self):
        html = atlas.render([{"id": "x", "nodes": [{"n": "</script>"}]}], [], "2026-10-18 · live")
        self.assertNotIn("/*__DATA__*/", html)
        self.assertIn('const SNAPSHOT="2026-10-18 · live";', html)
        self.assertNotIn('"</script>"', html)

    def test_shipped_layout_links_carry_a_base(self):
        lay = json.loads((ROOT / "atlas_layout.json").read_text(encoding="utf-8"))
        live = [n["live"] for d in lay["atlas"] for n in d["nodes"] if "live" in n]
        self.assertGreater(len(live), 100)
        for lv in live:
            if any(k in lv for k in ("guest", "containers", "agent", "device")):
                self.assertIn("base", lv)


if __name__ == "__main__":
    unittest.main()
