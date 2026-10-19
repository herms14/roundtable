"""Roundtable Office (/office): mood rules, the page script and Gideon's board-meeting hook."""
import ast
import re
import subprocess
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def office_mood():
    """Load office_mood/OFFICE_MOODS from portal.py without importing the portal (needs the ledger and fcntl)."""
    tree = ast.parse((ROOT / "portal.py").read_text(encoding="utf-8"))
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "office_mood") or
            (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "OFFICE_MOODS" for t in n.targets))]
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "portal.py", "exec"), ns)
    return ns["office_mood"]


class OfficeTests(unittest.TestCase):
    def test_moods_follow_the_task(self):
        m = office_mood()
        self.assertEqual(m(True, "Incident triage: disk alert")[0], "stressed")
        self.assertEqual(m(True, "Security review of approval #12")[0], "serious")
        self.assertEqual(m(True, "Patch delivery: build nginx-lxc")[0], "determined")
        self.assertEqual(m(True, "Monthly API cost report")[0], "worried")
        self.assertEqual(m(True, "Weekly homelab ideas research")[0], "curious")
        self.assertEqual(m(True, "Docs sync to vault")[0], "content")
        self.assertEqual(m(True, "something else")[0], "focused")
        self.assertEqual(m(True, "Docs sync", minutes=50)[0], "tired")

    def test_idle_moods(self):
        m = office_mood()
        self.assertEqual(m(False)[0], "relaxed")
        self.assertEqual(m(False, error=True, waiting=True)[0], "sad")
        self.assertEqual(m(False, waiting=True)[0], "anxious")
        self.assertEqual(m(False, finished_recently=True)[0], "happy")

    def test_routes_and_meeting_endpoint_exist(self):
        src = (ROOT / "portal.py").read_text(encoding="utf-8")
        self.assertIn('u.path == "/office"', src)
        self.assertIn('u.path == "/api/office"', src)
        self.assertIn('u.path == "/api/office/meeting"', src)
        # the meeting switch is a POST, so it sits behind the existing authed() check in do_POST
        post = src[src.index("def do_POST"):]
        self.assertLess(post.index("if not self.authed()"), post.index('"/api/office/meeting"'))

    def test_gideon_can_call_and_adjourn_a_board_meeting(self):
        voice = (ROOT / "voice.py").read_text(encoding="utf-8")
        self.assertIn('"meeting", "adjourn"', voice)
        self.assertIn("args.action=='meeting'", (ROOT / "portal.py").read_text(encoding="utf-8"))

    def test_every_homelab_agent_has_a_persona(self):
        import json
        page = (ROOT / "office.html").read_text(encoding="utf-8")
        personas = set(re.findall(r"^ (\w+):\{name:", page, re.M))
        agents = json.loads((ROOT / "agents.json").read_text(encoding="utf-8"))["agents"]
        office = {a["id"] for a in agents}
        self.assertEqual(office - personas, set())

    def test_page_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        page = (ROOT / "office.html").read_text(encoding="utf-8")
        script = re.search(r'<script type="module">(.*)</script>', page, re.S).group(1)
        r = subprocess.run([node, "--input-type=module", "--check"], input=script, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_three_is_self_hosted_and_pinned(self):
        page = (ROOT / "office.html").read_text(encoding="utf-8")
        self.assertIn('"three":"/vendor/three/three.module.min.js"', page)
        self.assertNotRegex(page, r"https?://(cdn|unpkg)")
        lib = (ROOT / "vendor" / "three" / "three.module.min.js").read_text(encoding="utf-8")
        self.assertIn('const t="160"', lib[:400])
        self.assertTrue((ROOT / "vendor" / "three" / "OrbitControls.js").is_file())
        src = (ROOT / "portal.py").read_text(encoding="utf-8")
        self.assertIn('self.vendor(u.path[14:], "three")', src)


if __name__ == "__main__":
    unittest.main()
