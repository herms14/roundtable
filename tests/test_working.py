"""Gideon's "agent is working" indicator: typing while the agent runs, ⏳ on the owner's message, then ✅ or ⚠️."""
import ast
import subprocess
import sys
import threading
import time
import types
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(fake_o):
    tree = ast.parse((ROOT / "boss.py").read_text(encoding="utf-8"))
    names = {"WORKING", "DONE", "FAILED", "TYPING_EVERY", "WORKING_MAX", "show_working", "AGENT"}
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in names) or
            (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in names or
                                               any(getattr(e, "id", "") in names for e in getattr(t, "elts", []))
                                               for t in n.targets))]
    ns = {"o": fake_o, "threading": threading, "time": time, "urllib": urllib}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "boss.py", "exec"), ns)
    ns["TYPING_EVERY"] = 1
    return ns


def fake():
    calls = []
    o = types.SimpleNamespace(calls=calls)
    o.channel_id = lambda name: "123"
    o.react = lambda cid, mid, emoji: calls.append(("react", cid, mid, emoji))
    o.dc = lambda method, path, *a, **k: calls.append((method, path))
    o.log = lambda *a: calls.append(("log",) + a)
    return o


class WorkingIndicator(unittest.TestCase):
    def run_with(self, code, mid="m1"):
        o = fake()
        ns = load(o)
        p = subprocess.Popen([sys.executable, "-c", f"import time, sys; time.sleep(1.5); sys.exit({code})"])
        ns["show_working"](p, "999", mid)
        p.wait()
        time.sleep(1.5)
        return o.calls

    def test_success_types_then_marks_done(self):
        calls = self.run_with(0)
        self.assertEqual(calls[0], ("react", "999", "m1", "⏳"))
        self.assertIn(("POST", "/channels/999/typing"), calls)
        self.assertIn(("DELETE", "/channels/999/messages/m1/reactions/" + urllib.parse.quote("⏳") + "/@me"), calls)
        self.assertEqual(calls[-1], ("react", "999", "m1", "✅"))

    def test_failure_marks_warning(self):
        self.assertEqual(self.run_with(3)[-1], ("react", "999", "m1", "⚠️"))

    def test_no_message_only_types(self):
        calls = self.run_with(0, mid=None)
        self.assertTrue(calls and all(c == ("POST", "/channels/999/typing") for c in calls))


if __name__ == "__main__":
    unittest.main()
