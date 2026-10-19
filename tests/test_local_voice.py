"""Local-speech voice backend: Responses API turn loop, client actions, accounting; no credentials or network."""
import ast
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class LocalVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"HOME": self.temp.name, "OPENAI_API_KEY": "test-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        mods = patch.dict(sys.modules)
        mods.start()
        self.addCleanup(mods.stop)
        path = patch.object(sys, "path", [str(ROOT), *sys.path])
        path.start()
        self.addCleanup(path.stop)
        core = types.ModuleType("olympus_core")
        core.__file__ = str(ROOT / "olympus_core.py")
        tree = ast.parse(Path(core.__file__).read_text())
        tree.body = [n for n in tree.body if not (
            isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and
            isinstance(n.value.func, ast.Name) and n.value.func.id == "load_env")]
        exec(compile(tree, core.__file__, "exec"), core.__dict__)
        sys.modules["olympus_core"] = self.o = core
        for name in ("http", "ssh", "dc"):
            setattr(core, name, Mock(side_effect=AssertionError("Unexpected network/process call")))
        core.post = Mock(return_value="111")
        core.react = Mock()
        core.private_channel = Mock(return_value="222")
        core.log = Mock()
        self.load("hierarchy")
        self.v = self.load("voice")

    def load(self, name):
        spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def reply(self, text=None, calls=(), usage=None):
        out = [{"type": "function_call", "name": n, "call_id": f"c{i}", "arguments": a} for i, (n, a) in enumerate(calls)]
        if text is not None:
            out.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
        return 200, {"output": out, "usage": usage or {"input_tokens": 1000, "output_tokens": 100}}

    def session(self):
        return self.v.mint_local("devops", "", "owner")

    def test_mint_local_has_no_openai_secret_and_no_camera_tool(self):
        s = self.session()
        self.assertEqual(s["model"], self.v.LOCAL_MODEL)
        self.assertNotIn("value", s)
        names = [t["name"] for t in self.v._LOCAL[s["run_id"]]["tools"]]
        self.assertNotIn("look_at_camera", names)
        self.assertIn("hand_over", names)
        self.assertTrue(all(t["strict"] is False for t in self.v._LOCAL[s["run_id"]]["tools"]))

    def test_plain_turn_costs_and_cleans_text(self):
        s = self.session()
        self.o.http.side_effect = None
        self.o.http.return_value = self.reply("**Hello** Hermes 😀 see https://x.test/a")
        r = self.v.local_turn(s["run_id"], "devops", [{"role": "user", "content": "hi"}], who="owner")
        self.assertEqual(r["text"], "Hello Hermes see link")
        self.assertGreater(r["cost"], 0)
        body = self.o.http.call_args[0][3]
        self.assertIs(body["store"], False)
        self.assertEqual(body["model"], self.v.LOCAL_MODEL)
        with self.o.db() as con:
            row = con.execute("SELECT calls, cost_usd FROM runs WHERE id=?", (s["run_id"],)).fetchone()
        self.assertEqual(row["calls"], 1)
        self.assertAlmostEqual(row["cost_usd"], r["cost"])

    def test_client_tools_become_actions_and_server_tools_run(self):
        s = self.session()
        self.v.tool = Mock(return_value={"ok": True})
        self.o.http.side_effect = [
            self.reply(calls=[("hand_over", '{"agent":"netops","reason":"x"}'), ("some_tool", "{}")]),
            self.reply("Passing you over."),
        ]
        r = self.v.local_turn(s["run_id"], "devops", [{"role": "user", "content": "get netops"}], who="owner")
        self.assertEqual(r["actions"], [{"name": "hand_over", "args": {"agent": "netops", "reason": "x"}}])
        self.v.tool.assert_called_once()
        self.assertEqual(self.v.tool.call_args[0][2], "some_tool")
        self.assertEqual(r["text"], "Passing you over.")

    def test_summon_team_is_gideon_only_and_a_client_action(self):
        names = lambda aid: [t["name"] for t in self.v.session_config(aid)["tools"]]
        self.assertIn("summon_team", names("boss"))
        for other in ("devops", "governance", "security"):
            self.assertNotIn("summon_team", names(other))
        self.assertIn("summon_team", self.v.CLIENT_TOOLS)
        self.assertIn("summon_team", self.v.instructions("boss"))
        self.assertNotIn("summon_team", self.v.instructions("devops"))

    def test_tool_loop_is_bounded(self):
        s = self.session()
        self.v.tool = Mock(return_value={"ok": True})
        self.o.http.side_effect = None
        self.o.http.return_value = self.reply(calls=[("some_tool", "{}")])
        r = self.v.local_turn(s["run_id"], "devops", [{"role": "user", "content": "loop"}], who="owner")
        self.assertEqual(self.o.http.call_count, self.v.MAX_TOOL_ROUNDS)
        self.assertIn("tangled", r["text"])

    def test_inactive_or_mismatched_run_is_refused(self):
        s = self.session()
        with self.assertRaises(ValueError):
            self.v.local_turn(s["run_id"], "netops", [], who="owner")
        with self.assertRaises(ValueError):
            self.v.local_turn(99999, "devops", [], who="owner")
        self.o.http.assert_not_called()

    def test_openai_error_raises(self):
        s = self.session()
        self.o.http.side_effect = None
        self.o.http.return_value = (500, {})
        with self.assertRaises(ValueError):
            self.v.local_turn(s["run_id"], "devops", [{"role": "user", "content": "hi"}], who="owner")

    def test_greet_adds_prompt_and_local_end_clears_cache(self):
        s = self.session()
        self.o.http.side_effect = None
        self.o.http.return_value = self.reply("Hi.")
        self.v.local_turn(s["run_id"], "devops", [], who="owner", greet=True)
        self.assertIn("just connected", self.o.http.call_args[0][3]["input"][-1]["content"])
        self.v.local_end(s["run_id"])
        self.assertNotIn(s["run_id"], self.v._LOCAL)

    def test_local_config_reads_env(self):
        with patch.dict(os.environ, {"LOCAL_VOICE_TOKEN": "t", "LOCAL_VOICE_URL": "wss://v.test"}):
            c = self.v.local_config()
        self.assertEqual((c["url"], c["token"], c["enabled"]), ("wss://v.test", "t", True))
        self.assertFalse(self.v.local_config()["enabled"])


if __name__ == "__main__":
    unittest.main()
