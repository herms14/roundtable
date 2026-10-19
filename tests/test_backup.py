"""backup.py low-space cleanup: what Iji may delete, in which order, and when he stops (no network, no deletes)."""
import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
GB = 1024 ** 3


class FreeSpace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["OLYMPUS_HOME"] = self.tmp.name
        sys.path.insert(0, str(ROOT))
        for m in ("olympus_core", "backup"):
            sys.modules.pop(m, None)
        self.o = importlib.import_module("olympus_core")
        self.o.ssh = Mock(return_value=(0, "", ""))
        self.o.post = Mock()
        self.m = importlib.import_module("backup")
        now = time.time()
        snap = lambda st, vmid, days, size=10 * GB, **kw: {"volid": f"{st}:backup/ct/{vmid}/{days}", "vmid": vmid,  # noqa: E731
                                                            "ctime": now - days * DAY, "size": size, **kw}
        self.content = {
            "pbs-daily": [snap("pbs-daily", 200, d) for d in (1, 2, 40, 60, 90)]
                         + [snap("pbs-daily", 205, 45), snap("pbs-daily", 205, 70, protected=1)]
                         + [snap("pbs-daily", 999, 120)],               # removed guest: its only backup stays
            "ProxmoxData": [snap("ProxmoxData", 200, d, 60 * GB) for d in (3, 35, 50, 80)],
        }

    def tearDown(self):
        sys.path.remove(str(ROOT))
        self.tmp.cleanup()

    def state(self, daily_pct, nas_pct, gc_running=False):
        st = lambda name, pct: {"name": name, "label": name, "size": 1000 * GB, "used": pct * 10 * GB,  # noqa: E731
                                "free": (100 - pct) * 10 * GB, "pct": pct, "backup": True}
        tasks = [{"worker_type": "garbage_collection", "worker_id": "daily"}] if gc_running else []
        return {"storage": [st("pbs-daily", daily_pct), st("ProxmoxData", nas_pct)], "content": self.content,
                "guests": [{"id": 200, "name": "glance"}, {"id": 205, "name": "media"}], "pve_node": "node02",
                "pbs": {"tasks": tasks}}

    def test_candidates_are_old_oldest_first_and_spare_newest_and_protected(self):
        c = self.m.prune_candidates(self.state(85, 85), "pbs-daily")
        self.assertEqual([x["volid"] for x in c], ["pbs-daily:backup/ct/200/90", "pbs-daily:backup/ct/200/60",
                                                   "pbs-daily:backup/ct/200/40"])

    def test_nothing_happens_under_the_threshold(self):
        plans = self.m.free_space(self.state(79, 50))
        self.assertTrue(all(not p["pick"] for p in plans))
        self.o.ssh.assert_not_called()
        self.o.post.assert_not_called()

    def test_nas_deletes_only_until_under_target(self):
        p = {x["store"]: x for x in self.m.prune_plan(self.state(50, 85))}["ProxmoxData"]
        self.assertEqual([x["volid"] for x in p["pick"]], ["ProxmoxData:backup/ct/200/80", "ProxmoxData:backup/ct/200/50"])  # 150 GB free + 2 x 60 GB >= 250 GB (75%)
        self.assertEqual(p["eligible"], 3)

    def test_pbs_batch_deletes_then_starts_gc_and_waits_for_it(self):
        plans = self.m.free_space(self.state(85, 50))
        daily = {p["store"]: p for p in plans}["pbs-daily"]
        self.assertEqual(len(daily["deleted"]), 3)
        cmds = [c.args[1] for c in self.o.ssh.call_args_list]
        self.assertTrue(all("pvesm free" in x for x in cmds[:3]))
        self.assertIn("garbage-collection start daily", cmds[3])
        self.o.post.assert_called_once()
        self.o.ssh.reset_mock()
        again = {p["store"]: p for p in self.m.prune_plan(self.state(85, 50, gc_running=True))}["pbs-daily"]
        self.assertFalse(again["pick"])
        self.assertIn("garbage collection", again["skipped"])

    def test_dry_run_deletes_nothing(self):
        plans = self.m.free_space(self.state(85, 85), dry=True)
        self.assertTrue(any(p["pick"] for p in plans))
        self.o.ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
