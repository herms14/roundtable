"""#roundtable-hold direct @name routing: the owner's exact words go to the named agents, bypassing Gideon's router."""
import ast
import json
import re
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = json.loads((ROOT / "agents.json").read_text(encoding="utf-8"))["agents"]


def load():
    """Load mention_targets without importing boss.py (it needs the ledger, Discord and the bot token)."""
    tree = ast.parse((ROOT / "boss.py").read_text(encoding="utf-8"))
    names = {"MENTION", "NAME_PREFIXES", "_fold", "mention_targets"}
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in names) or
            (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in names for t in n.targets))]
    ns = {"re": re, "unicodedata": unicodedata}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "boss.py", "exec"), ns)
    return ns["mention_targets"]


class DirectMentionTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_single_agent_gets_exact_words(self):
        self.assertEqual(self.m("@iji back up 205 now", AGENTS), (["backup"], "back up 205 now"))
        self.assertEqual(self.m("@Torrent, why can't VLAN 30 reach pihole?", AGENTS),
                         (["netops"], "why can't VLAN 30 reach pihole?"))

    def test_mid_sentence_multiple_and_duplicates(self):
        self.assertEqual(self.m("hey @loretta and @torrent check VLAN 30 @Loretta", AGENTS),
                         (["security", "netops"], "hey and check VLAN 30"))

    def test_names_aliases_accents_and_prefixes(self):
        for text, aid in (("@nepheli recommend a movie", "media"), ("@kale scout apps", "scout"),
                          ("@kalé scout apps", "scout"), ("@netops status", "netops")):
            self.assertEqual(self.m(text, AGENTS)[0], [aid], text)

    def test_no_mention_or_unknown_leaves_gideon_routing(self):
        self.assertEqual(self.m("why is jellyfin slow?", AGENTS), ([], "why is jellyfin slow?"))
        self.assertEqual(self.m("email me at hermes@example.com", AGENTS)[0], [])
        self.assertEqual(self.m("@everyone status", AGENTS)[0], [])
        self.assertEqual(self.m("<@123456789012345678> hi", AGENTS)[0], [])

    def test_only_a_name_gives_empty_task(self):
        self.assertEqual(self.m("@iji", AGENTS), (["backup"], ""))


if __name__ == "__main__":
    unittest.main()
