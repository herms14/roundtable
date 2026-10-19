"""Ranni's RFC process: which changes need an RFC, the note/register format and automatic closure (Linux host)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rfc  # noqa: E402

PY, D = "/usr/bin/python3", "/home/hermes-admin/olympus/"
SECTIONS = {"what": "Sync ACLs to phase 2", "change": "6 rules", "recovery": "@torrent acl off", "risk": "lockout"}


class RfcRules(unittest.TestCase):
    def test_normal_changes_need_an_rfc_standard_changes_do_not(self):
        self.assertTrue(rfc.required("netops", "high", [PY, D + "netops.py", "acl", "apply", "2"]))
        self.assertFalse(rfc.required("netops", "medium", [PY, D + "netops.py", "acl", "probe"]))
        self.assertTrue(rfc.required("devops", "medium", [PY, D + "builder.py", "forge", "x"]))
        self.assertTrue(rfc.required("medic", "high", [PY, D + "medic.py", "fix", "21"]))
        self.assertFalse(rfc.required("medic", "medium", [PY, D + "medic.py", "fix", "21"]))
        self.assertFalse(rfc.required("media", "high", [PY, D + "media.py", "restart", "sonarr"]))
        self.assertFalse(rfc.required("backup", "high", [PY, D + "backup.py", "backup-now", "101"]))
        self.assertTrue(rfc.required("media", "low", [PY, D + "media.py", "x"], rfc=SECTIONS))

    def test_note_register_and_closure(self):
        info = {"agent": "netops", "title": "Gateway ACLs → phase 2", "status": "In review",
                "created": "2026-10-15T14:00:00Z", "sections": SECTIONS, "approval": 32,
                "rel": "07 HomeLab Things/Claude Managed Homelab/RFCs/RFC-2026-001 - Gateway ACLs phase 2.md"}
        note = rfc.note_text("RFC-2026-001", info)
        for part in ("# RFC-2026-001 · Gateway ACLs → phase 2", "## What will be done", "## What will change",
                     "## Recovery plan", "Torrent", "approval #32", 'rfc-status: "In review"', "type: project",
                     "[[07 HomeLab Things/Claude Managed Homelab/RFCs/RFC Register|RFC Register]]"):
            self.assertIn(part, note)
        line = rfc._register_line("RFC-2026-001", info)
        self.assertTrue(line.startswith("| [[07 HomeLab Things/Claude Managed Homelab/RFCs/RFC-2026-001 - Gateway ACLs phase 2|RFC-2026-001]] |"))
        self.assertEqual(rfc.status_for_result("done", ""), "Implemented")
        self.assertEqual(rfc.status_for_result("failed", "acl: rolling back: Not confirmed"), "Rolled back")
        self.assertEqual(rfc.status_for_result("failed", "Traceback"), "Failed")

    def test_embed_fits_discord(self):
        big = {k: "x" * 5000 for k in ("what", "change", "recovery", "risk")}
        fields = rfc.embed_fields("RFC-2026-002", {"agent": "netops", "sections": big})
        self.assertLessEqual(sum(len(n) + len(v) for n, v, _ in fields), 2400)


if __name__ == "__main__":
    unittest.main()
