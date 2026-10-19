"""Isolated roster, governance, privacy, approval and IPAM regressions; no credentials or network."""
import ast
import concurrent.futures
import contextlib
import copy
import datetime as dt
import importlib.util
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]

class FakeCodex:
    """Stands in for the codex process: writes a transcript; runaway=True keeps streaming large command outputs."""
    last = None

    def __init__(self, cmd, stdout=None, runaway=False, **kw):
        self.cmd, self.out, self.runaway, self.terminated, self.n = cmd, stdout, runaway, False, 0
        FakeCodex.last = self

    def poll(self):
        if not self.runaway or self.terminated or self.n >= 60:
            return 0
        self.n += 1
        self.out.write(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                                                                      "aggregated_output": "x" * 40000}}) + "\n")
        self.out.flush()
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

class HierarchyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"HOME": self.temp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.modules = patch.dict(sys.modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.path = patch.object(sys, "path", [str(ROOT), *sys.path])
        self.path.start()
        self.addCleanup(self.path.stop)
        core = types.ModuleType("olympus_core")
        core.__file__ = str(ROOT / "olympus_core.py")
        tree = ast.parse(Path(core.__file__).read_text())
        tree.body = [n for n in tree.body if not (
            isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and
            isinstance(n.value.func, ast.Name) and n.value.func.id == "load_env")]
        exec(compile(tree, core.__file__, "exec"), core.__dict__)
        sys.modules["olympus_core"] = self.o = core
        self.real_private = core.private_channel
        for name in ("http", "ssh", "dc"):
            setattr(core, name, Mock(side_effect=AssertionError("Unexpected network/process call")))
        core.post = Mock(return_value="111")
        core.react = Mock()
        core.private_channel = Mock(return_value="222")
        core.log = Mock()
        self.h = self.load("hierarchy")
        self.ip = self.load("ipam")
        self.c = self.load("council")
        self.issues = {}
        self.h._gl = Mock(side_effect=self.gl)

    def load(self, name):
        spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def gl(self, method, path, data=None):
        if method == "GET" and path.startswith("/issues?"):
            return list(self.issues.values())
        if method == "POST" and path == "/issues":
            iid = len(self.issues) + 1
            self.issues[iid] = {"iid": iid, "title": data["title"], "web_url": f"https://test/issues/{iid}", "state": "opened"}
            return self.issues[iid]
        iid = int(path.split("/")[2])
        if method == "GET":
            return dict(self.issues[iid])
        if method == "PUT":
            self.issues[iid]["state"] = "closed" if data["state_event"] == "close" else "opened"
        return {"id": 1}

    def row(self, key):
        with contextlib.closing(self.h._db()) as con:
            return dict(con.execute("SELECT * FROM work_items WHERE key=?", (key,)).fetchone())

    def approval(self, agent="devops", payload=None, status="running"):
        with contextlib.closing(self.o.db()) as con, con:
            return con.execute(
                "INSERT INTO approvals(agent,title,severity,payload,command,channel_id,status,created,decided,message_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (agent, "Reviewed change", "medium", json.dumps(payload or {}), '["python","executor"]',
                 "333", status, self.o.iso(), self.o.iso(), "444")).lastrowid

    def test_portal_approval_decisions_are_validated_queued_and_applied_once_by_gideon(self):
        portal = self.load("portal")
        self.assertEqual(portal.approvers(), {"akadmin"})
        with patch.dict(os.environ, {"PORTAL_APPROVERS": "akadmin, hermes"}):
            self.assertEqual(portal.approvers(), {"akadmin", "hermes"})
        pending, scheduled, done = (self.approval(status=s) for s in ("pending", "scheduled", "done"))
        self.assertEqual(portal.queue_decision("1", "approve", "akadmin")[0], 400)
        self.assertEqual(portal.queue_decision(pending, "delete", "akadmin")[0], 400)
        self.assertEqual(portal.queue_decision(99999, "approve", "akadmin")[0], 404)
        self.assertEqual(portal.queue_decision(done, "approve", "akadmin")[0], 409)
        self.assertEqual(portal.queue_decision(pending, "run_now", "akadmin")[0], 409, "run now is only for scheduled")
        self.assertEqual(portal.queue_decision(pending, "approve", "akadmin")[0], 202)
        self.assertEqual(portal.queue_decision(scheduled, "run_now", "akadmin")[0], 202)
        rows = {r["id"]: r for r in portal.approvals_payload()}
        self.assertTrue(rows[pending]["queued"] and rows[scheduled]["queued"] and not rows[done]["queued"])
        boss = self.load("boss")
        boss.decide, boss.execute, self.o.post = Mock(), Mock(), Mock(return_value="m")
        boss.portal_decisions()
        boss.decide.assert_called_once()
        self.assertEqual((boss.decide.call_args[0][0]["id"], boss.decide.call_args[0][1]), (pending, True))
        boss.execute.assert_called_once()
        self.assertEqual(self.o.get_approval(scheduled)["status"], "approved")
        notes = [c.args[1] for c in self.o.post.call_args_list]
        self.assertTrue(any(f"#{pending} approved on the Roundtable portal by akadmin" in n for n in notes), notes)
        self.assertTrue(all(c.kwargs.get("reply_to") == "444" for c in self.o.post.call_args_list), "audit note not in the thread")
        boss.portal_decisions()
        self.assertEqual(boss.decide.call_count, 1, "a portal decision must be applied only once")
        self.o.set_approval(pending, status="rejected")   # decided in Discord before Gideon picked it up
        self.o.kv_set(f"portal_decision:{pending}", {"decision": "approve", "by": "akadmin"})
        boss.portal_decisions()
        self.assertEqual(boss.decide.call_count, 1, "a stale portal decision must not override the current state")
        src = (ROOT / "portal.py").read_text(encoding="utf-8")
        route = src[src.index('if u.path == "/api/approvals/decide"'):][:900]
        self.assertIn("TRUSTED_PROXIES", route)
        self.assertIn("approvers()", route)
        self.assertNotIn("Companion", route, "the desktop device token must not decide approvals")

    def test_roster_has_exactly_four_leads_under_gideon(self):
        agents = {a["id"]: a for a in self.o.registry()["agents"]}
        self.assertEqual(len(agents), 22)
        self.assertEqual({a["id"] for a in agents.values() if a.get("is_lead")}, {"boss", "security", "governance", "medic"})
        self.assertEqual(agents["boss"]["reports_to"], "hermes")
        for aid in agents:
            if aid != "boss":
                self.assertIn(agents[aid]["reports_to"], {"boss", "security", "governance", "medic"})
                self.assertEqual(self.h.chain(aid)[-1], "boss")
        self.assertEqual(self.h.chain("iam"), ["security", "boss"])
        self.assertEqual(self.h.chain("builder"), ["medic", "boss"])
        self.assertEqual(self.h.chain("project_manager"), ["governance", "boss"])
        self.assertEqual(agents["boss"]["role"], "COO & Enterprise Architect")
        self.assertIn("sre-agent.service", agents["builder"]["units"])
        self.assertNotIn("sre-agent.service", agents["medic"]["units"])

    def test_bad_reporting_chain_is_rejected(self):
        reg = self.o.registry()
        reg["agents"][0]["reports_to"] = "missing"
        with patch.object(self.o, "registry", return_value=reg), self.assertRaises(ValueError):
            self.h.chain(reg["agents"][0]["id"])


    def test_portal_hierarchy_snapshot_is_pending_and_metadata_only(self):
        portal = self.load("portal")
        reg = self.o.registry()
        snapshot = Path(self.temp.name) / "hierarchy.json"
        with patch.object(self.o, "CODE_DIR", Path(self.temp.name)):
            configured = portal.hierarchy_payload()
            self.assertEqual(configured["status"], "configured")
            self.assertNotIn("character", json.dumps(configured))
            self.assertNotIn("does", configured["agents"][0])
            self.assertEqual(len(configured["agents"]), 22)
            self.assertTrue({"scout", "writer"}.issubset(a["id"] for a in configured["agents"]))
            self.assertTrue({"scout", "writer"}.issubset(configured["active_ids"]))
            snapshot.write_text(json.dumps({"status": "pending", "agents": reg["agents"]}))
            active = {"agents": [a for a in reg["agents"] if a["id"] != "keeper"]}
            with patch.object(self.o, "registry", return_value=active):
                pending = portal.hierarchy_payload()
            self.assertEqual(pending["status"], "pending")
            self.assertIn("keeper", {a["id"] for a in pending["agents"]})
            self.assertNotIn("keeper", pending["active_ids"])
            snapshot.write_text(json.dumps({"status": "configured", "agents": reg["agents"]}))
            with self.assertRaises(ValueError):
                portal.hierarchy_payload()
            snapshot.write_text(json.dumps({"status": "pending", "agents": []}))
            with self.assertRaises(ValueError):
                portal.hierarchy_payload()

    def test_cycle_is_rejected(self):
        reg = self.o.registry()
        boss = next(a for a in reg["agents"] if a["id"] == "boss")
        boss["reports_to"] = "iam"  # iam -> security -> boss -> iam
        with patch.object(self.o, "registry", return_value=reg), self.assertRaises(ValueError):
            self.h.chain("iam")

    def test_operational_runs_are_tracked_and_failed_runs_stay_open(self):
        r = self.o.Run("netops", "Investigate DNS", "chat")
        r.finish("failed", "DNS remains unavailable")
        row = self.row(f"run:{r.id}")
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["acknowledged"])
        self.h.sync_item(row["key"])
        self.assertEqual(self.issues[row["gitlab_iid"] or 1]["state"], "opened")

    def test_audit_resolution_is_evidenced_and_idempotent(self):
        self.h.track("test:1", "netops", "DNS investigation")
        self.h.sync_item("test:1")
        self.h.resolve("test:1", "resolved", "Verified DNS answers")
        self.h.sync_item("test:1")
        self.h.sync_item("test:1")
        self.assertEqual(len(self.issues), 1)
        self.assertEqual(self.issues[1]["state"], "closed")
        self.assertIsNotNone(self.row("test:1")["resolution_discord_id"])
        self.assertEqual(self.o.post.call_count, 2)

    def test_resolution_delivery_failure_is_retried_after_gitlab_closed(self):
        self.h.track("test:2", "netops", "DNS")
        self.h.sync_item("test:2")
        self.h.resolve("test:2", "done", "Verified")
        self.o.post.return_value = None
        with self.assertRaises(RuntimeError):
            self.h.sync_item("test:2")
        self.assertEqual(self.issues[1]["state"], "closed")
        self.assertIn("Discord", self.row("test:2")["sync_error"])
        self.o.post.return_value = "555"
        self.h.sync_item("test:2")
        self.assertEqual(self.row("test:2")["resolution_discord_id"], "555")
        self.assertIsNone(self.row("test:2")["sync_error"])

    def test_unsupported_closure_is_reopened(self):
        self.h.track("test:3", "netops", "DNS")
        self.h.sync_item("test:3")
        self.issues[1]["state"] = "closed"
        with self.assertRaises(RuntimeError):
            self.h.sync_item("test:3")
        self.assertEqual(self.issues[1]["state"], "opened")

    def test_shared_work_item_is_not_closed_while_another_owner_is_open(self):
        self.h.track("shared:1", "medic", "Older incident")
        self.h.sync_item("shared:1")
        self.h.track("shared:2", "medic", "New incident")
        with contextlib.closing(self.h._db()) as con, con:
            con.execute("UPDATE work_items SET gitlab_iid=1 WHERE key='shared:2'")
        self.h.resolve("shared:1", "resolved", "Earlier episode verified")
        self.h.sync_item("shared:1")
        self.assertEqual(self.issues[1]["state"], "opened")
        self.h.resolve("shared:2", "fixed", "New episode verified")
        self.h.sync_item("shared:2")
        self.assertEqual(self.issues[1]["state"], "closed")

    def test_legacy_resolved_patch_incident_uses_later_matching_verified_evidence(self):
        first = self.o.iso(self.o.utcnow() - dt.timedelta(days=2))
        later = self.o.iso(self.o.utcnow() - dt.timedelta(days=1))
        legacy = {"id": 2, "state": "resolved", "first": first, "gitlab_iid": 1,
                  "finding": {"type": "patch-failed", "host": "gitlab-vm01"}}
        verified = {"id": 9, "state": "fixed", "first": later, "gitlab_iid": 1,
                    "finding": {"type": "patch-failed", "host": "gitlab-vm01"},
                    "fix": {"status": "fixed", "summary": "Reconciled durable completion",
                            "verification": ["Host healthy and completion verified"]}}
        self.issues[1] = {"iid": 1, "title": "Patch drift", "state": "closed", "web_url": "https://test/1"}
        self.o.kv_set("medic", {"incidents": {"inc:2": legacy, "inc:9": verified}})
        self.h.sync()
        key = self.h.source_key("medic", "inc:2", legacy)
        self.assertEqual(self.row(key)["status"], "resolved")
        self.assertIn("later verified incident #9", self.row(key)["evidence"])
        self.h.sync()
        self.assertEqual(self.issues[1]["state"], "closed")
        for candidate in (
                {**verified, "first": self.o.iso(self.o.utcnow() - dt.timedelta(days=3))},
                {**verified, "gitlab_iid": 2},
                {**verified, "finding": {"host": "other-host"}},
                {**verified, "fix": {"status": "partial", "verification": ["Incomplete"]}}):
            self.assertIsNone(self.h._later_incident_evidence(legacy, {"other": candidate}))
        self.assertIsNone(self.h._later_incident_evidence({**legacy, "state": "open"}, {"verified": verified}))

    def test_sync_rotates_old_unresolved_rows(self):
        for n in range(105):
            self.h.track(f"task:{n}", "netops", "DNS")
        self.h.sync()
        self.h.sync()
        self.assertEqual(len(self.issues), 105)

    def test_sla_top_five_and_finops_are_deterministic(self):
        past = self.o.iso(self.o.utcnow() - dt.timedelta(days=10))
        for n in range(7):
            self.h.track(f"late:{n}", "netops", "DNS", "critical", created=past)
        report = self.h.compliance()
        self.assertEqual(len(report["top5"]), 5)
        self.assertIn("acknowledgement objective missed", report["top5"][0]["problems"])
        self.assertIn("resolution objective overdue", report["top5"][0]["problems"])
        r = self.o.Run("netops", "test")
        r.usage("gpt-6-luna", 1000, 100, 200)
        r.finish("ok", "verified")
        cost = self.h.finops()
        self.assertEqual(cost["models"][0]["model"], "gpt-6-luna")
        self.assertGreater(cost["spend"], 0)
        self.assertIn("unrecorded", cost["scope"])

    def test_approval_gate_requires_running_role_and_complete_audit(self):
        aid = self.approval()
        self.h.reconcile()
        with patch.dict(os.environ, {"OLYMPUS_APPROVAL_ID": str(aid)}):
            self.assertEqual(self.h.require_approval("devops")["id"], aid)
            with self.assertRaises(PermissionError):
                self.h.require_approval("iam")
            self.o.set_approval(aid, status="pending")
            with self.assertRaises(PermissionError):
                self.h.require_approval("devops")
        with self.assertRaises(PermissionError):
            self.h.require_approval()

    def test_missing_discord_approval_never_returns_success(self):
        self.o.post.return_value = None
        with self.assertRaises(RuntimeError):
            self.o.request_approval("devops", "Change", "Plan", ["python", "execute"], "333")
        self.assertEqual(self.o.get_approval(1)["status"], "failed")

    def picture(self):
        return {"vlans": [{"vlan": 40, "subnet": "192.168.40.1/24", "dhcp": True,
                           "dhcp_range": {"start": "192.168.40.50", "end": "192.168.40.254"}}],
                "used": ["192.168.40.36"], "reservations": []}

    def test_ipam_excludes_dhcp_known_addresses_and_unknown_data(self):
        data = self.picture()
        options = self.ip.available(40, data)
        self.assertEqual(options, [f"192.168.40.{i}" for i in range(37, 50)])
        data["vlans"][0]["dhcp_range"]["start"] = "192.168.40.40"
        self.assertEqual(self.ip.available(40, data), [f"192.168.40.{i}" for i in range(37, 40)])
        data["vlans"][0]["dhcp_range"] = {}
        with self.assertRaises(RuntimeError):
            self.ip.available(40, data)
        with self.assertRaises(ValueError):
            self.ip.available(60, data)

    def test_ipam_atomic_reservations_do_not_collide(self):
        with patch.object(self.ip, "picture", side_effect=self.picture), patch.object(self.ip, "_responds", return_value=False):
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                rows = list(pool.map(lambda i: self.ip.reserve(f"svc-{i}", 40), range(4)))
        self.assertEqual(len({r["ip"] for r in rows}), 4)
        with patch.object(self.ip, "picture", side_effect=self.picture), patch.object(self.ip, "_responds", return_value=False):
            self.ip.verify(rows[0]["service"], rows[0]["ip"], 40)
        self.ip.activate(rows[0]["service"])
        with self.assertRaises(PermissionError):
            self.ip.activate("not-reserved")

    def test_ipam_keeps_active_holds_and_rejects_old_dhcp_overlap(self):
        data = self.picture()
        data["reservations"] = [
            {"ip": "192.168.40.37", "service": "active", "state": "active", "expires": "2000-01-01"},
            {"ip": "192.168.40.38", "service": "expired", "state": "pending", "expires": "2000-01-01"},
            {"ip": "192.168.40.39", "service": "pending", "state": "pending", "expires": "2999-01-01"}]
        options = self.ip.available(40, data)
        self.assertNotIn("192.168.40.37", options)
        self.assertIn("192.168.40.38", options)
        self.assertNotIn("192.168.40.39", options)
        with patch.object(self.ip, "picture", return_value=data), self.assertRaises(ValueError):
            self.ip.reserve("new", 40, "192.168.40.150")
        with patch.object(self.ip, "picture", return_value=data), patch.object(self.ip, "_responds", return_value=True):
            with self.assertRaises(RuntimeError):
                self.ip.reserve("new", 40, "192.168.40.38")

    def test_ipam_preserves_vlan_and_global_evidence_blocks(self):
        data = self.picture()
        data["allocation_blockers"] = {"90": ["offline management client"]}
        self.assertTrue(self.ip.available(40, data))
        data["allocation_blockers"]["40"] = ["offline production client"]
        with self.assertRaisesRegex(RuntimeError, "offline production client"):
            self.ip.available(40, data)
        data["allocation_blockers"] = {"*": ["untagged client"]}
        with self.assertRaisesRegex(RuntimeError, "untagged client"):
            self.ip.available(40, data)

    def test_generic_changes_cannot_bypass_ipam_or_shell_restrictions(self):
        for command in ("bash script.sh", "terraform apply", "qm create 900", "eval bad", "true", "git -C /tmp/repo push origin main"):
            with self.subTest(command=command), self.assertRaises((ValueError, PermissionError)):
                self.c._safe_command("devops", "node01", command)
        self.assertEqual(self.c._safe_command("netops", "pihole", "sudo -n systemctl restart unbound"),
                         ["sudo", "-n", "systemctl", "restart", "unbound"])
        with self.assertRaises(PermissionError):
            self.c._safe_command("netops", "node01", "sudo -n systemctl restart networking")

    def test_private_channel_refuses_shared_fallback(self):
        self.o.dc.side_effect = [(200, {"id": "bot"}), (200, [{"id": "channel", "name": "private-assistant",
                                                             "type": 0, "permission_overwrites": []}])]
        with patch.dict(os.environ, {"DISCORD_GUILD_ID": "guild"}), self.assertRaises(PermissionError):
            self.real_private("private-assistant")
        self.o.post.assert_not_called()


    def test_morgott_is_read_only_pmo_with_separate_deadlines(self):
        voice = self.load("voice")
        boss = self.load("boss")
        self.h.track("delivery:1", "netops", "Recorded network work")
        result = self.c.context("project_manager")
        self.assertEqual(result["open_count"], 1)
        self.assertIn("not project delivery commitments", result["scope"])
        self.assertEqual(result["patch_delivery"]["delivery_owner"], "builder")
        self.assertNotIn("project_manager", self.c.CHANGE_AGENTS)
        self.assertEqual(boss.ASK["project_manager"][2:5], ["ask", "--agent", "project_manager"])
        config = voice.session_config("project_manager")
        self.assertEqual({t["name"] for t in config["tools"]},
                         {"lead_brief", "consult_role", "hand_over", "end_call", "look_at_camera", "post_plan"})
        self.assertIn("post_plan", config["instructions"])

    def test_plan_drafts_never_become_blueprints_or_approvals(self):
        builder = self.load("builder")
        voice = self.load("voice")
        task = voice.PLAN_TASK.format(topic="a strategy to monitor API usage")
        self.assertTrue(task.startswith(self.c.PLAN_DRAFT))
        self.assertEqual(builder.PLAN_DRAFT, self.c.PLAN_DRAFT)

    def test_patch_delivery_reads_existing_backlog_without_executor(self):
        builder = self.load("builder")
        self.h._gl = Mock(return_value=[{"iid": 7, "title": "Patch host", "labels": ["patch-management"]}])
        result = builder.voice_tool("patch_status", {})
        self.assertEqual(result["delivery_owner"], "Hewg")
        self.assertEqual(result["accountable_lead"], "Corhyn")
        self.assertEqual(result["executor"], "sre-agent.service")
        self.assertEqual(len(result["backlog"]), 1)
        self.o.ssh.assert_not_called()

    def network_data(self):
        def page(rows):
            return {"totalRows": len(rows), "currentPage": 1, "currentSize": 1000, "data": rows}
        return {"/dashboard/overviewDiagram": {}, "/devices": [],
                "/setting/lan/networks?currentPage=1&currentPageSize=1000": page([
                    {"id": "production", "name": "Production", "vlan": 40, "gatewaySubnet": "192.168.40.1/24",
                     "dhcpSettings": {"enable": True, "ipaddrStart": "192.168.40.50",
                                      "ipaddrEnd": "192.168.40.254"}}]),
                "/clients?currentPage=1&currentPageSize=1000&filters.active=true": page([]),
                "/insight/clients?currentPage=1&currentPageSize=1000": page([]),
                "/setting/service/dhcp?currentPage=1&currentPageSize=1000": page([]),
                "/setting/firewall/imbs?currentPage=1&currentPageSize=1000": page([])}

    def network(self, data):
        inventory = self.load("inventory")
        netops = self.load("netops")
        with patch.object(netops, "Omada") as api:
            api.return_value.get.side_effect = lambda path: data[path]
            return inventory._network()

    def test_omada_real_dhcp_fields_and_missing_historical_ips_fail_closed(self):
        data = self.network_data()
        data["/insight/clients?currentPage=1&currentPageSize=1000"].update(
            totalRows=1, data=[{"mac": "AA-BB-CC-DD-EE-FF", "name": "known", "vid": 40, "lastSeen": 10}])
        data["/clientHistory/AA-BB-CC-DD-EE-FF?currentPage=1&currentPageSize=1000"] = {
            "totalRows": 0, "currentPage": 1, "data": []}
        network = self.network(data)
        self.assertIsNone(network["ipam_error"])
        self.assertEqual(len(network["unresolved_clients"]), 1)
        self.assertEqual(network["vlans"][0]["dhcp_range"],
                         {"start": "192.168.40.50", "end": "192.168.40.254"})
        self.o.load_hosts = Mock(return_value={})
        inventory = sys.modules["inventory"]
        with patch.object(inventory, "get", side_effect=lambda s, fresh: network if s == "network" else {"guests": []}):
            picture = self.ip.picture()
        with self.assertRaisesRegex(RuntimeError, "known"):
            self.ip.available(40, picture)

    def test_omada_current_clients_resolve_history_and_reservations_stay_excluded(self):
        data = self.network_data()
        mac = "AA-BB-CC-DD-EE-FF"
        data["/clients?currentPage=1&currentPageSize=1000&filters.active=true"].update(
            totalRows=1, data=[{"mac": mac, "ip": "192.168.40.36", "vid": 40}])
        data["/insight/clients?currentPage=1&currentPageSize=1000"].update(
            totalRows=1, data=[{"mac": mac, "vid": 40}])
        data["/setting/service/dhcp?currentPage=1&currentPageSize=1000"].update(
            totalRows=2, data=[{"id": "a", "ip": "192.168.40.37", "status": True},
                               {"id": "b", "ip": "192.168.40.38", "status": False}])
        data["/setting/firewall/imbs?currentPage=1&currentPageSize=1000"].update(
            totalRows=1, data=[{"id": "binding", "ip": "192.168.40.39", "enable": False}])
        network = self.network(data)
        self.assertIsNone(network["ipam_error"])
        self.assertEqual(network["unresolved_clients"], [])
        self.o.load_hosts = Mock(return_value={})
        with patch.object(sys.modules["inventory"], "get",
                          side_effect=lambda s, fresh: network if s == "network" else {"guests": []}):
            picture = self.ip.picture()
        self.assertEqual(self.ip.available(40, picture), [f"192.168.40.{i}" for i in range(40, 50)])

    def test_omada_offline_history_requires_the_latest_assignment(self):
        data = self.network_data()
        mac = "AA-BB-CC-DD-EE-FF"
        data["/insight/clients?currentPage=1&currentPageSize=1000"].update(
            totalRows=1, data=[{"mac": mac, "vid": 40, "lastSeen": 20}])
        history = {"totalRows": 2, "currentPage": 1, "data": [
            {"id": "older", "ip": "192.168.40.36", "vid": 40, "lastSeen": 10},
            {"id": "latest", "ip": "192.168.40.37", "vid": 40, "lastSeen": 20}]}
        data[f"/clientHistory/{mac}?currentPage=1&currentPageSize=1000"] = history
        network = self.network(data)
        self.assertEqual(network["unresolved_clients"], [])
        self.assertEqual({c["ip"] for c in network["clients"]}, {"192.168.40.36", "192.168.40.37"})
        history["data"][1].pop("ip")
        network = self.network(data)
        self.assertEqual(len(network["unresolved_clients"]), 1)
        history["data"][1].update(ip="192.168.40.37", lastSeen=15)
        self.assertEqual(len(self.network(data)["unresolved_clients"]), 1)

    def test_omada_pagination_requires_complete_stable_unique_rows(self):
        inventory = self.load("inventory")
        api = Mock()
        api.get.side_effect = [
            {"totalRows": 2, "currentPage": 1, "data": [{"id": "a"}]},
            {"totalRows": 2, "currentPage": 2, "data": [{"id": "b"}]}]
        self.assertEqual(inventory._omada_rows(api, "/test"), [{"id": "a"}, {"id": "b"}])
        for second in (
                {"totalRows": 2, "currentPage": 2, "data": []},
                {"totalRows": 3, "currentPage": 2, "data": [{"id": "b"}]},
                {"totalRows": 2, "currentPage": 1, "data": [{"id": "b"}]},
                {"totalRows": 2, "currentPage": 2, "data": [{"id": "a"}]}):
            api.get.side_effect = [{"totalRows": 2, "currentPage": 1, "data": [{"id": "a"}]}, second]
            with self.subTest(response=second), self.assertRaises(RuntimeError):
                inventory._omada_rows(api, "/test")

    def test_omada_missing_reservations_and_multiple_pools_block_allocation(self):
        data = self.network_data()
        data.pop("/setting/service/dhcp?currentPage=1&currentPageSize=1000")
        self.assertIn("Known client inventory unavailable", self.network(data)["ipam_error"])
        data = self.network_data()
        data["/setting/lan/networks?currentPage=1&currentPageSize=1000"]["data"][0]["dhcpSettings"]["ipRangePool"] = [
            {"ipaddrStart": "192.168.40.50", "ipaddrEnd": "192.168.40.99"},
            {"ipaddrStart": "192.168.40.150", "ipaddrEnd": "192.168.40.254"}]
        network = self.network(data)
        self.assertIn("Multiple DHCP pools", network["ipam_error"])
        with patch.object(sys.modules["inventory"], "get",
                          side_effect=lambda s, fresh: network if s == "network" else {"guests": []}):
            with self.assertRaises(RuntimeError):
                self.ip.picture()

    def test_compute_interface_addresses_match_the_correct_mac(self):
        inventory = self.load("inventory")
        resources = [{"type": "qemu", "vmid": 101, "name": "multi-nic", "status": "stopped"}]
        self.o.load_hosts = Mock(return_value={})
        self.o.ssh.side_effect = None
        self.o.ssh.return_value = (0, json.dumps(resources) + "\n@@@\n" +
            "101|net0: virtio=AA:BB:CC:DD:EE:00,tag=40\n" +
            "101|net1: virtio=AA:BB:CC:DD:EE:01,tag=90\n" +
            "101|ipconfig1: ip=192.168.90.38/24\n" +
            "101|ipconfig0: ip=192.168.40.37/24\n", "")
        compute = inventory._compute()
        self.assertEqual(compute["guests"][0]["interfaces"], [
            {"mac": "AA-BB-CC-DD-EE-00", "vlan": 40, "ip": "192.168.40.37"},
            {"mac": "AA-BB-CC-DD-EE-01", "vlan": 90, "ip": "192.168.90.38"}])
        network = {"vlans": self.picture()["vlans"], "unresolved_clients": [
            {"mac": "AA-BB-CC-DD-EE-00", "vlan": 40, "name": "offline", "reason": "no IP"}]}
        with patch.object(inventory, "get", side_effect=lambda s, fresh: network if s == "network" else compute):
            picture = self.ip.picture()
        self.assertEqual(picture["allocation_blockers"], {})
        self.assertNotIn("192.168.40.37", self.ip.available(40, picture))
        network["unresolved_clients"][0]["vlan"] = 90
        with patch.object(inventory, "get", side_effect=lambda s, fresh: network if s == "network" else compute):
            self.assertIn("90", self.ip.picture()["allocation_blockers"])

    def test_unaddressed_guest_and_untagged_client_remain_blocked(self):
        inventory = self.load("inventory")
        self.o.load_hosts = Mock(return_value={})
        compute = {"guests": [{"id": 101, "ip": "dhcp", "vlan": "40", "template": False}]}
        network = {"vlans": self.picture()["vlans"], "unresolved_clients": [
            {"mac": "AA-BB-CC-DD-EE-FF", "vlan": 0, "name": "untagged", "reason": "no IP"}]}
        with patch.object(inventory, "get", side_effect=lambda s, fresh: network if s == "network" else compute):
            picture = self.ip.picture()
        self.assertIn("40", picture["allocation_blockers"])
        self.assertIn("*", picture["allocation_blockers"])
        with self.assertRaises(RuntimeError):
            self.ip.available(40, picture)

    def test_destroyed_proxmox_guest_client_does_not_block_its_vlan(self):
        inventory = self.load("inventory")
        self.o.load_hosts = Mock(return_value={})
        compute = {"guests": [], "macs": ["BC-24-11-00-00-01"]}
        network = {"vlans": self.picture()["vlans"], "unresolved_clients": [
            {"mac": "BC:24:11:BC:BB:24", "vlan": 40, "name": "deleted-lxc", "reason": "no IP"}]}
        with patch.object(inventory, "get", side_effect=lambda s, fresh: network if s == "network" else compute):
            picture = self.ip.picture()
        self.assertEqual(picture["allocation_blockers"], {})
        self.assertEqual(picture["retired_guest_clients"][0]["mac"], "BC-24-11-BC-BB-24")
        self.assertTrue(self.ip.available(40, picture))
        # Still blocks when the MAC belongs to an existing guest, isn't a Proxmox MAC, or the MAC list is missing.
        for macs, mac in ((["BC-24-11-BC-BB-24"], "BC:24:11:BC:BB:24"), ([], "AA:BB:CC:DD:EE:FF"), (None, "BC:24:11:BC:BB:24")):
            network["unresolved_clients"][0]["mac"] = mac
            data = {"guests": []} if macs is None else {"guests": [], "macs": macs}
            with patch.object(inventory, "get", side_effect=lambda s, fresh: network if s == "network" else data):
                self.assertIn("40", self.ip.picture()["allocation_blockers"])

    def test_unaddressed_second_interface_and_duplicate_macs_are_not_proof(self):
        inventory = self.load("inventory")
        self.o.load_hosts = Mock(return_value={})
        mac = "AA-BB-CC-DD-EE-FF"
        compute = {"guests": [{"id": 101, "ip": "192.168.40.37", "vlan": "40, 90",
                              "interfaces": [{"mac": mac, "ip": "192.168.40.37", "vlan": 40},
                                             {"mac": "00-11-22-33-44-55", "vlan": 90}]}]}
        network = {"vlans": self.picture()["vlans"], "unresolved_clients": [
            {"mac": mac, "vlan": 40, "name": "offline", "reason": "no IP"}]}
        with patch.object(inventory, "get", side_effect=lambda s, fresh: network if s == "network" else compute):
            self.assertIn("90", self.ip.picture()["allocation_blockers"])
        compute["guests"].append({"id": 102, "ip": "192.168.40.38", "vlan": "40",
                                 "interfaces": [{"mac": mac, "ip": "192.168.40.38", "vlan": 40}]})
        with patch.object(inventory, "get", side_effect=lambda s, fresh: network if s == "network" else compute):
            self.assertIn("40", self.ip.picture()["allocation_blockers"])

    def mesh_fixture(self):
        parent, peer, station = "00-11-22-33-44-55", "AA-BB-CC-DD-EE-00", "02-99-88-77-66-55"
        client = {"mac": station, "wireless": True, "vid": 0, "lastSeen": 20}
        history = [{"id": "h", "mac": station, "vid": 0, "ssid": "mesh_001122334455",
                    "deviceName": "Root AP", "lastSeen": 20}]
        devices = [{"mac": parent, "name": "Root AP", "type": "ap", "statusCategory": 1,
                    "ip": "192.168.90.52", "model": "EAP", "wirelessLinked": False},
                   {"mac": peer, "name": "Mesh AP", "type": "ap", "statusCategory": 1,
                    "ip": "192.168.90.55", "model": "EAP", "wirelessLinked": True, "uplink": parent}]
        responses = {"/wlanSsid": [{"ssids": [{"name": "ordinary-wifi"}]}],
                     "/eaps/" + parent: {"mac": parent, "isMeshStatus": True, "statusCategory": 1},
                     "/eaps/" + parent + "/mesh/stastic": {
                         "statusCategory": 1, "wirelessLinked": False, "childAps": [
                             {"mac": peer, "type": "ap", "ip": "192.168.90.55", "statusCategory": 1}]}}
        return client, history, devices, responses

    def test_mesh_transport_requires_controller_history_and_reciprocal_topology(self):
        inventory = self.load("inventory")
        client, history, devices, responses = self.mesh_fixture()
        api = Mock()
        api.get.side_effect = lambda path: responses[path]
        transport = inventory._mesh_transport(api, client, history, devices)
        self.assertEqual(transport["kind"], "omada_mesh_backhaul")
        self.assertEqual(transport["peer_ip"], "192.168.90.55")
        self.assertEqual(transport["mac"], client["mac"])
        for change in ("wired", "tagged", "old", "addressed", "mixed", "wrong-parent", "user-ssid",
                       "mesh-off", "offline", "nonreciprocal", "ambiguous", "wrong-peer-ip"):
            c, h, d, r = copy.deepcopy((client, history, devices, responses))
            if change == "wired": c["wireless"] = False
            elif change == "tagged": c["vid"] = 40
            elif change == "old": c["lastSeen"] = 30
            elif change == "addressed": h[0]["ip"] = "192.168.40.38"
            elif change == "mixed": h.append({**h[0], "id": "old", "ssid": "ordinary-wifi"})
            elif change == "wrong-parent": h[0]["deviceName"] = "Other AP"
            elif change == "user-ssid": r["/wlanSsid"][0]["ssids"][0]["name"] = h[0]["ssid"]
            elif change == "mesh-off": r["/eaps/" + d[0]["mac"]]["isMeshStatus"] = False
            elif change == "offline": d[1]["statusCategory"] = 0
            elif change == "nonreciprocal": d[1]["uplink"] = "wrong"
            elif change == "ambiguous":
                r["/eaps/" + d[0]["mac"] + "/mesh/stastic"]["childAps"].append(dict(d[1]))
            elif change == "wrong-peer-ip": d[1]["ip"] = "192.168.90.56"
            api.get.side_effect = lambda path: r[path]
            with self.subTest(case=change):
                self.assertIsNone(inventory._mesh_transport(api, c, h, d))

    def test_confirmed_mesh_backhaul_unblocks_pools_without_removing_ip_exclusions(self):
        client, history, devices, responses = self.mesh_fixture()
        data = self.network_data()
        data.update(responses)
        data["/devices"] = devices
        data["/insight/clients?currentPage=1&currentPageSize=1000"].update(totalRows=1, data=[client])
        data["/clientHistory/" + client["mac"] + "?currentPage=1&currentPageSize=1000"] = {
            "currentPage": 1, "totalRows": 1, "data": history}
        network = self.network(data)
        self.assertIsNone(network["ipam_error"])
        self.assertEqual(network["unresolved_clients"], [])
        self.assertEqual(len(network["layer2_transports"]), 1)
        self.o.load_hosts = Mock(return_value={})
        with patch.object(sys.modules["inventory"], "get",
                          side_effect=lambda s, fresh: network if s == "network" else {"guests": []}):
            picture = self.ip.picture()
        self.assertEqual(picture["allocation_blockers"], {})
        self.assertTrue(self.ip.available(40, picture))
        self.assertTrue({"192.168.90.52", "192.168.90.55"}.issubset(picture["used"]))
        self.assertEqual(picture["layer2_transports"], network["layer2_transports"])
        data.pop("/eaps/" + devices[0]["mac"] + "/mesh/stastic")
        failed = self.network(data)
        self.assertIn("Known client inventory unavailable", failed["ipam_error"])

    def test_retirement_removes_active_timers_and_preserves_helper_only(self):
        text = (ROOT / "install.sh").read_text()
        definitions = text.split("TIMERS=$(cat <<'EOF'", 1)[1].split("EOF", 1)[0]
        self.assertIn("vault-refresh", definitions)
        boss = self.load("boss")
        self.assertLessEqual(set(boss.ASK), {a["id"] for a in self.o.registry()["agents"]})

    def plan(self, command, host="pihole"):
        return {"steps": [{"host": host, "command": command}],
                "verification": [{"host": host, "command": "systemctl is-active unbound"}],
                "rollback": "Owner restores known DNS configuration", "manual_steps": [], "access_request": "",
                "new_service": "", "vlan": 0, "ip": "", "title": "Restart DNS", "severity": "medium"}

    def test_cli_supports_the_registered_argument_order(self):
        self.c.ask = Mock()
        with patch.object(sys, "argv", ["council.py", "ask", "--agent", "boss", "hello", "--channel", "222"]):
            self.c.main()
        self.c.ask.assert_called_once_with("hello", None, "222", "boss")

    def test_iam_grants_are_request_bound_expiring_and_verified(self):
        self.o.load_hosts = Mock(return_value={})
        access = types.ModuleType("access")
        access.MAX_HOURS = 24
        req = {"id": "AG-1", "resource": "GitLab project", "needed": "read only", "level": "read", "hours": 4}
        access.brief = Mock(return_value=[req])
        sys.modules["access"] = access
        plan = self.plan("gitlab grant 6 42 20 4", "gitlab-vm01")
        plan.update(access_request="AG-1", verification=plan["steps"])
        self.c.validate_plan("iam", plan)
        for bad in ("gitlab grant 6 42 50 4", "gitlab grant 6 42 20 25", "gitlab grant 6 42 20 0",
                    f"gitlab grant 6 42 20 {self.o.local().date() + dt.timedelta(days=30)}"):
            with self.assertRaises((PermissionError, ValueError), msg=bad):
                self.c._safe_command("iam", "gitlab-vm01", bad)
        plan["steps"][0]["command"] = "gitlab grant 6 42 30 4"
        with self.assertRaises(PermissionError):
            self.c.validate_plan("iam", plan)
        plan["steps"][0]["command"] = "gitlab grant 6 42 20 8"
        with self.assertRaisesRegex(PermissionError, "longer than the 4 h"):
            self.c.validate_plan("iam", plan)
        plan["steps"][0]["command"] = "gitlab grant 6 42 20 4"
        for change, err in (({"level": None}, ValueError), ({"hours": None}, ValueError), ({"level": "admin"}, PermissionError)):
            access.brief.return_value = [{**req, **change}]
            with self.assertRaises(err, msg=change):
                self.c.validate_plan("iam", plan)
        member = {"access_level": 20, "expires_at": (self.o.utcnow() + dt.timedelta(hours=28)).date().isoformat()}
        self.o.http.side_effect = [
            (200, {"visibility": "private"}), (404, {}),
            (404, {}), (201, member), (200, member)]
        with patch.dict(os.environ, {"GITLAB_TOKEN": "synthetic-test-value"}):
            self.assertIn("for 4 h", self.c._gitlab(["gitlab", "grant", "6", "42", "20", "4"]))
        self.o.http.side_effect = [(200, {"visibility": "private"}), (200, member), (204, None), (404, {})]
        with patch.dict(os.environ, {"GITLAB_TOKEN": "synthetic-test-value"}):
            self.assertIn("removed", self.c._gitlab(["gitlab", "revoke", "6", "42"]))
        self.o.http.side_effect = [(200, {"visibility": "private"}), (200, member), (204, None), (200, member)]
        with patch.dict(os.environ, {"GITLAB_TOKEN": "synthetic-test-value"}), self.assertRaises(RuntimeError):
            self.c._gitlab(["gitlab", "revoke", "6", "42"])

    def test_iam_authentik_grants_only_grantable_groups_for_new_members_and_revoke(self):
        self.o.load_hosts = Mock(return_value={})
        access = types.ModuleType("access")
        access.MAX_HOURS, access.AUTHENTIK_GRANTABLE = 24, ("authentik Agent-Users", "media-users")
        req = {"id": "AG-3", "resource": "Jellyfin via Authentik SSO", "needed": "media-users membership", "level": "read", "hours": 4}
        access.brief = Mock(return_value=[req])
        sys.modules["access"] = access
        plan = self.plan("authentik grant media-users 12 4", "authentik-lxc")
        plan.update(access_request="AG-3", verification=plan["steps"])
        self.c.validate_plan("iam", plan)
        for bad in ('authentik grant "authentik Admins" 12 4', "authentik grant media-admins 12 4",
                    "authentik grant media-users 12 25", "authentik grant media-users abc 4", "authentik revoke media-users 12"):
            with self.assertRaises((PermissionError, ValueError), msg=bad):
                self.c._safe_command("iam", "authentik-lxc", bad)
        with self.assertRaises(PermissionError):
            self.c._safe_command("devops", "authentik-lxc", "authentik grant media-users 12 4")
        with self.assertRaises(PermissionError):
            self.c._safe_command("iam", "gitlab-vm01", "authentik grant media-users 12 4")
        plan["steps"][0]["command"] = plan["verification"][0]["command"] = "authentik grant media-users 12 8"
        with self.assertRaisesRegex(PermissionError, "longer than the 4 h"):
            self.c.validate_plan("iam", plan)
        plan["steps"][0]["command"] = plan["verification"][0]["command"] = "authentik grant media-users 12 4"
        access.brief.return_value = [{**req, "resource": "Omada controller", "needed": "viewer"}]
        with self.assertRaises(PermissionError):
            self.c.validate_plan("iam", plan)
        access.brief.return_value = [{**req, "level": "admin"}]
        with self.assertRaises(PermissionError):
            self.c.validate_plan("iam", plan)
        groups = (200, {"results": [{"pk": "g1", "name": "media-users", "is_superuser": False}]})
        env = {"AUTHENTIK_IAM_TOKEN": "synthetic-test-value", "AUTHENTIK_URL": "http://ak"}
        self.o.http = Mock(side_effect=[groups, (200, {"groups": []}), (204, None), (200, {"groups": ["g1"]})])
        with patch.dict(os.environ, env):
            self.assertIn("for 4 h", self.c._authentik(["authentik", "grant", "media-users", "12", "4"]))
        self.assertEqual(self.o.http.call_args_list[2].args[:2], ("POST", "http://ak/api/v3/core/groups/g1/add_user/"))
        self.o.http = Mock(side_effect=[groups, (200, {"groups": ["g1"]})])
        with patch.dict(os.environ, env), self.assertRaisesRegex(PermissionError, "already a member"):
            self.c._authentik(["authentik", "grant", "media-users", "12", "4"])
        self.o.http = Mock(side_effect=[groups, (200, {"groups": ["g1"]}), (204, None), (200, {"groups": []})])
        with patch.dict(os.environ, env):
            self.assertIn("removed", self.c._authentik(["authentik", "revoke", "media-users", "12"]))
        self.o.http = Mock(side_effect=[groups, (200, {"groups": ["g1"]}), (204, None), (200, {"groups": ["g1"]})])
        with patch.dict(os.environ, env), self.assertRaises(RuntimeError):
            self.c._authentik(["authentik", "revoke", "media-users", "12"])
        self.o.http = Mock(side_effect=[(200, {"results": [{"pk": "g0", "name": "media-users", "is_superuser": True}]})])
        with patch.dict(os.environ, env), self.assertRaises(PermissionError):
            self.c._authentik(["authentik", "grant", "media-users", "12", "4"])

    def test_expired_grants_are_revoked_logged_and_announced(self):
        spec = importlib.util.spec_from_file_location("access", ROOT / "access.py")
        a = importlib.util.module_from_spec(spec)
        sys.modules["access"] = a
        spec.loader.exec_module(a)
        a._gl_open = lambda g: g.update(issue=7, url="https://gitlab/7")
        a._gl, a._sync_later, a.sync_obsidian = Mock(return_value=(201, {})), Mock(), Mock(return_value=True)
        self.o.channel_id = Mock(return_value="333")
        self.o.post = Mock(return_value="m1")
        g, new = a.report("devops", "GitLab project 6", "reporter on project 6", "read CI logs", level="READ", hours=99)
        self.assertTrue(new)
        self.assertEqual((g["level"], g["hours"]), ("read", 24), "level must normalise and hours cap at 24")
        g2, _ = a.report("netops", "Omada controller", "read-only viewer", source="auto")
        self.assertIn("not stated", a._span(g2))
        _, gaps = a.scan_text("medic", "x\nACCESS_GAP: Proxmox node01 | VM.Audit on VM 120 | read guest status | read | 2\n")
        self.assertEqual((gaps[0]["level"], gaps[0]["hours"]), ("read", 2))
        with self.assertRaises(PermissionError):
            a.record_grant(g["id"], "omada", {}, 2, "no automatic revoker")
        x = a.record_grant(g["id"], "gitlab", {"project": "6", "user": "42"}, 2, "GitLab project 6 user 42 level 20")
        self.assertEqual(self.o.parse_iso(x["expires"]) - self.o.parse_iso(x["granted"]), dt.timedelta(hours=2))
        revoke = Mock(side_effect=[RuntimeError("HTTP 502")] + [RuntimeError("HTTP 502")] * 5 + ["Verified removed"])
        a.REVOKERS["gitlab"] = revoke
        self.assertEqual(a.expire(), ([], []), "nothing may be revoked before it expires")
        later = self.o.utcnow() + dt.timedelta(hours=2, minutes=1)
        self.assertEqual(a.expire(later), ([], [x["id"]]))
        self.assertEqual(a.grants(True)[0]["state"], "revoke_failed")
        page = self.o.post.call_args
        self.assertIn("<@", page.kwargs.get("content", ""), "a failed revocation must page the owner")
        pages = self.o.post.call_count
        for _ in range(4):
            a.expire(later)
        self.assertEqual(self.o.post.call_count, pages, "the owner is not re-paged on every retry")
        a.expire(later)
        self.assertEqual(self.o.post.call_count, pages + 1, "the owner is re-paged every 6th attempt")
        self.assertEqual(a.expire(later), ([x["id"]], []))
        done = a.grants()[0]
        self.assertEqual(done["state"], "revoked")
        self.assertFalse(a.grants(True))
        embed = self.o.post.call_args.kwargs["embeds"][0]
        self.assertIn("revoked", str(embed).lower())
        self.assertTrue(any("revoked on expiry by Rogier" in str(c) for c in a._gl.call_args_list), "no GitLab audit note")
        a.sync_obsidian.assert_called()
        self.assertIn("Revoked on expiry", a._note_body({**a._state(), "grants": a.grants()}))

    def test_iam_does_not_accept_missing_or_wrong_verification(self):
        self.o.load_hosts = Mock(return_value={"gitlab-vm01": {}})
        plan = self.plan("gitlab grant 6 42 20 4", "gitlab-vm01")
        with self.assertRaises((ValueError, PermissionError)):
            self.c.validate_plan("iam", plan)

    def test_network_executor_only_completes_after_verification_and_docs(self):
        self.o.load_hosts = Mock(return_value={"pihole": {}})
        plan = self.plan("sudo -n systemctl restart unbound")
        aid = self.approval("netops", {"council_plan": plan})
        self.h.reconcile()
        self.o.ssh.side_effect = [(0, "", ""), (0, "active", "")]
        scribe = types.ModuleType("scribe")
        scribe.log_change = Mock(return_value=True)
        sys.modules["scribe"] = scribe
        with patch.dict(os.environ, {"OLYMPUS_APPROVAL_ID": str(aid)}):
            self.c.apply()
        self.assertEqual(self.o.get_approval(aid)["status"], "done")
        self.assertEqual(self.o.ssh.call_count, 2)
        scribe.log_change.assert_called_once()

    def test_failed_documentation_does_not_report_success(self):
        self.o.load_hosts = Mock(return_value={"pihole": {}})
        plan = self.plan("sudo -n systemctl restart unbound")
        aid = self.approval("netops", {"council_plan": plan})
        self.h.reconcile()
        self.o.ssh.return_value = (0, "active", "")
        self.o.ssh.side_effect = None
        scribe = types.ModuleType("scribe")
        scribe.log_change = Mock(return_value=False)
        sys.modules["scribe"] = scribe
        with patch.dict(os.environ, {"OLYMPUS_APPROVAL_ID": str(aid)}), self.assertRaises(RuntimeError):
            self.c.apply()
        self.assertEqual(self.o.get_approval(aid)["status"], "failed")
        self.assertEqual(self.row(f"approval:{aid}")["status"], "failed")

    def test_portal_graph_has_all_active_agents_and_real_report_edges(self):
        portal = self.load("portal")
        portal.agents_payload = Mock(return_value=[])
        graph = portal.graph_payload()
        ids = {n["id"] for n in graph["nodes"] if n["type"] == "agent"}
        self.assertEqual(ids, {a["id"] for a in self.o.registry()["agents"]})
        edges = {(e["from"], e["to"]) for e in graph["edges"] if e["kind"] == "report"}
        self.assertFalse(any(e[0] == "boss" for e in edges), "Gideon reports to Hermes, not to an agent")
        self.assertIn(("iam", "security"), edges)
        self.assertIn(("scribe", "governance"), edges)
        self.assertNotIn(("builder", "boss"), edges)
        self.assertIn(("project_manager", "governance"), edges)

    def test_all_profiles_have_unique_stable_fictional_traits_and_complete_original_art(self):
        from xml.etree import ElementTree as ET
        characters = self.load("character_art")
        registry = self.o.registry()["agents"]
        self.assertEqual(set(characters.PROFILES), {a["id"] for a in registry})
        self.assertEqual(len({a["personality"]["quirks"] for a in registry}), len(registry))
        for a in registry:
            p = a["personality"]
            self.assertTrue(p["fictional"])
            self.assertTrue(all(p.get(k) for k in ("temperament", "quirks", "delivery")))
            for key in ("avatar", "avatar_full"):
                if not a.get(key):
                    continue
                path = ROOT / a[key].lstrip("/")
                self.assertTrue(path.is_file())
                if path.suffix == ".svg":
                    tree = ET.fromstring(path.read_text())
                    self.assertEqual(tree.tag, "{http://www.w3.org/2000/svg}svg")
                    self.assertFalse(any(n.tag.rsplit("}", 1)[-1] in ("script", "foreignObject", "image") for n in tree.iter()))
                else:
                    self.assertTrue(path.read_bytes().startswith(b"\xff\xd8\xff" if path.suffix == ".jpg" else b"\x89PNG\r\n\x1a\n"))
                if path.suffix == ".svg" and "-persona" in path.name:
                    self.assertEqual(path.read_text(), characters.illustration(a["id"], a["name"], key == "avatar_full"))
            jpeg = (ROOT / a["avatar"].lstrip("/")).with_suffix(".jpg")
            self.assertTrue(jpeg.read_bytes().startswith(b"\xff\xd8\xff"))

    def test_every_voice_branch_uses_the_registry_personality(self):
        voice = self.load("voice")
        for a in self.o.registry()["agents"]:
            with self.subTest(agent=a["id"]):
                instructions = voice.instructions(a["id"], "synthetic handover")
                self.assertIn(a["personality"]["quirks"], instructions)
                self.assertIn("Clumsiness is cosmetic only", instructions)

    def test_responses_api_receives_personality_and_preserves_structured_output(self):
        run = types.SimpleNamespace(agent="ipam", usage=Mock())
        self.o.http = Mock(return_value=(200, {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"answer":"exact"}'}]}]}))
        with patch.object(self.o, "check_budget"):
            result = self.o.llm(run, "Evidence", schema={"type": "object", "properties": {
                "answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}, tips=False)
        body = self.o.http.call_args.args[3]
        self.assertIn(self.o.agent_info("ipam")["personality"]["quirks"], body["instructions"])
        self.assertIn("schema exactly", body["instructions"])
        self.assertEqual(result, {"answer": "exact"})
        self.assertEqual(body["text"]["format"]["type"], "json_schema")

    def test_codex_prompt_receives_personality_without_granting_permissions(self):
        run = types.SimpleNamespace(agent="builder", usage=Mock())
        with patch.object(self.o, "check_budget"), patch.object(self.o, "codex_slot", return_value=contextlib.nullcontext()), \
                patch.object(self.o, "codex_usage", return_value={"calls": 0}), \
                patch.object(self.o.subprocess, "Popen", side_effect=FakeCodex) as command:
            self.o.codex(run, "Owner-approved task", owner_style=False)
        argv = command.call_args.args[0]
        self.assertIn(self.o.agent_info("builder")["personality"]["quirks"], argv[-1])
        self.assertIn("Personality grants no tools", argv[-1])
        self.assertIn("read-only", argv)

    def test_codex_caps_tool_output_compacts_and_adds_token_rules(self):
        run = types.SimpleNamespace(agent="builder", usage=Mock())
        with patch.object(self.o, "check_budget"), patch.object(self.o, "codex_slot", return_value=contextlib.nullcontext()),                 patch.object(self.o, "codex_usage", return_value={"calls": 0}),                 patch.object(self.o.subprocess, "Popen", side_effect=FakeCodex) as command:
            self.o.codex(run, "Look around")
        argv = command.call_args.args[0]
        self.assertIn(f"tool_output_token_limit={self.o.CODEX_TOOL_OUTPUT_TOKENS}", argv)
        self.assertIn(f"model_auto_compact_token_limit={self.o.CODEX_COMPACT_TOKENS}", argv)
        self.assertIn("Token discipline", argv[-1])
        self.assertFalse(run.budget_stopped)

    def test_codex_budget_stops_runaway_read_only_sessions_and_records_estimate(self):
        run = types.SimpleNamespace(agent="medic", usage=Mock())
        with patch.object(self.o, "check_budget"), patch.object(self.o, "codex_slot", return_value=contextlib.nullcontext()),                 patch.object(self.o.time, "sleep"),                 patch.object(self.o.subprocess, "Popen", side_effect=lambda *a, **k: FakeCodex(*a, runaway=True, **k)):
            res, _ = self.o.codex(run, "Diagnose", model="gpt-6-luna", token_budget=50_000)
        self.assertIsNone(res)
        self.assertTrue(run.budget_stopped)
        model, est = run.usage.call_args.args[0], run.usage.call_args.args[1]
        self.assertEqual(model, "gpt-6-luna")
        self.assertGreater(est, 50_000)

    def test_codex_never_budget_stops_sessions_that_change_systems(self):
        run = types.SimpleNamespace(agent="medic", usage=Mock())
        with patch.object(self.o, "check_budget"), patch.object(self.o, "codex_slot", return_value=contextlib.nullcontext()),                 patch.object(self.o, "codex_usage", return_value={"calls": 0}), patch.object(self.o.time, "sleep"),                 patch.object(self.o.subprocess, "Popen", side_effect=lambda *a, **k: FakeCodex(*a, runaway=True, **k)) as p:
            self.o.codex(run, "Apply the approved fix", model="gpt-6.1-sol", token_budget=0)
        self.assertFalse(run.budget_stopped)
        self.assertFalse(p.call_args and FakeCodex.last.terminated)

    def test_codex_tiered_uses_luna_first_and_escalates_only_when_needed(self):
        run = types.SimpleNamespace(agent="netops", budget_stopped=False)
        calls = []
        def fake(r, prompt, model=None, **kw):
            calls.append(model)
            return ("ESCALATE" if model == self.o.CHEAP_MODEL else "real answer"), "t"
        with patch.object(self.o, "codex", side_effect=fake):
            text, _, used = self.o.codex_tiered(run, "q", self.o.needs_escalation)
        self.assertEqual((calls, text, used), ([self.o.CHEAP_MODEL, self.o.STRONG_MODEL], "real answer", self.o.STRONG_MODEL))
        calls.clear()
        with patch.object(self.o, "codex", side_effect=lambda r, p, model=None, **kw: (calls.append(model) or "fine", "t")):
            text, _, used = self.o.codex_tiered(run, "q", self.o.needs_escalation)
        self.assertEqual((calls, text, used), ([self.o.CHEAP_MODEL], "fine", self.o.CHEAP_MODEL))

    def test_voice_defaults_to_the_mini_realtime_model(self):
        voice = self.load("voice")
        if "ROUNDTABLE_VOICE_MODEL" not in os.environ:
            self.assertEqual(voice.MODEL, "gpt-realtime-2.1-mini")


    def test_painted_generator_covers_only_the_missing_seven_with_the_existing_style(self):
        art = self.load("avatars")
        new = {"project_manager", "security_engineer", "iam", "compliance", "cost", "ipam", "devops"}
        for aid in new:
            self.assertIn(aid, art.PROMPTS)
            self.assertIn("original", art.PROMPTS[aid].lower())
            self.assertNotIn(aid, art.STYLE_OVERRIDE)
        self.assertIn("Dark-fantasy oil painting", art.STYLE)
        self.assertIn("muted gold, ash and deep-blue", art.STYLE)

    def test_applying_personality_profiles_cannot_replace_the_active_avatar_selection(self):
        art = self.load("character_art")
        before = self.o.registry()
        base = Path(self.temp.name) / "art"
        base.mkdir()
        registry = base / "agents.json"
        registry.write_text(json.dumps(before))
        with patch.object(art, "ROOT", base), patch.object(sys, "argv", ["character_art.py", "--apply-profiles"]):
            art.main()
        after = json.loads(registry.read_text())
        for old, updated in zip(before["agents"], after["agents"]):
            self.assertEqual(old.get("avatar"), updated.get("avatar"))
            self.assertEqual(old.get("avatar_full"), updated.get("avatar_full"))
            self.assertEqual(old["personality"], updated["personality"])

    def test_private_notes_and_credentials_are_excluded_before_reading(self):
        vault = self.load("vault")
        for rel in ("AI/Personal/notes.md", "AI/Transcripts/private.md", "AI/sessions/history.md", "11 - Credentials.md", "ordinary.md"):
            path = vault.MIRROR / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic note")
        self.assertEqual([p.name for p in vault._notes()], ["ordinary.md"])

    def test_source_recurrence_has_a_new_audit_episode(self):
        first = {"first": self.o.iso(self.o.utcnow() - dt.timedelta(hours=1)), "title": "DNS", "sev": "high"}
        second = {**first, "first": self.o.iso()}
        a = self.h.track_source("watcher_state", "dns", first, "watcher", "333")
        self.h.resolve(a, "resolved", "Consecutive checks clear")
        b = self.h.track_source("watcher_state", "dns", second, "watcher", "333")
        self.assertNotEqual(a, b)
        self.assertEqual(self.row(a)["status"], "resolved")
        self.assertEqual(self.row(b)["status"], "open")

    def test_verified_security_clearance_closes_adopted_issue(self):
        self.issues[1] = {"iid": 1, "title": "Existing security finding", "state": "closed", "web_url": "https://test/1"}
        item = {"first": self.o.iso(), "state": "resolved", "resolved": self.o.iso(), "title": "Exposure", "gitlab_iid": 1}
        self.o.kv_set("security", {"findings": {"exposure": item}})
        self.h.sync()
        key = self.h.source_key("security", "exposure", item)
        self.assertEqual(self.row(key)["status"], "resolved")
        self.assertEqual(self.issues[1]["state"], "closed")
        self.assertEqual(len(self.issues), 1)

    def test_scribe_updates_both_changelogs_without_overwriting_concurrent_edits(self):
        scribe = self.load("scribe")
        remote = {scribe.CHANGELOG_REL: "---\nupdated: 2026-10-10\n---\n# Homelab\n",
                  scribe.VAULT_CHANGELOG_REL: "---\nupdated: 2026-10-10\n---\n# Vault\n"}
        def copy(*args):
            _, source, target = args
            if source.startswith(scribe.REMOTE):
                Path(target).write_text(remote[source[len(scribe.REMOTE) + 1:]])
            else:
                remote[target[len(scribe.REMOTE) + 1:]] = Path(source).read_text()
            return types.SimpleNamespace(returncode=0, stderr="")
        scribe._rclone = Mock(side_effect=copy)
        self.assertTrue(scribe.log_change("DNS verified", "Both changelogs", agent="netops"))
        for value in remote.values():
            self.assertIn("DNS verified", value)
        before = dict(remote)
        self.assertTrue(scribe.log_change("DNS verified", "Both changelogs", agent="netops"))
        self.assertEqual(before, remote)
        def conflict(*args):
            if "current-" in args[-1]:
                rel = args[1][len(scribe.REMOTE) + 1:]
                remote[rel] += "\nOwner's concurrent edit\n"
            return copy(*args)
        scribe._rclone.side_effect = conflict
        with self.assertRaises(RuntimeError):
            scribe.log_change("Do not overwrite", "Conflict", agent="netops")
        self.assertIn("Owner's concurrent edit", remote[scribe.CHANGELOG_REL])
        self.assertNotIn("Do not overwrite", remote[scribe.CHANGELOG_REL])

    def test_builder_rejects_unapproved_or_changed_plans(self):
        builder = self.load("builder")
        with self.assertRaises(PermissionError):
            builder.build("svc")
        spec = {"name": "svc", "kind": "lxc"}
        payload = {"name": "svc", "spec_sha": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
                   "playbook_sha": hashlib.sha256(builder.PLAYBOOK.encode()).hexdigest(),
                   "fingerprints": {"main.tf": "a" * 64, "tfplan": "b" * 64}}
        builder.builds = Mock(return_value={"svc": {"spec": spec, "status": "planned"}})
        builder.ctl = Mock(return_value=(0, "changed main.tf\nchanged tfplan", ""))
        self.h.require_approval = Mock(return_value={"id": 1, "agent": "devops", "payload": payload})
        builder.tf = Mock(side_effect=AssertionError("Unreviewed Terraform must not execute"))
        with self.assertRaises(PermissionError):
            builder.build("svc")
        builder.tf.assert_not_called()

    def test_builder_does_not_retry_a_failed_saved_plan_with_a_fresh_apply(self):
        builder = self.load("builder")
        spec = {"name": "svc", "kind": "lxc", "vmid": 900, "ip": "192.168.40.151", "vlan": 40, "node": "node01"}
        payload = {"name": "svc", "spec_sha": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
                   "playbook_sha": hashlib.sha256(builder.PLAYBOOK.encode()).hexdigest(),
                   "fingerprints": {"main.tf": "a" * 64, "tfplan": "b" * 64}}
        self.h.require_approval = Mock(return_value={"id": 1, "agent": "devops", "payload": payload})
        builder.builds = Mock(return_value={"svc": {"spec": spec, "status": "planned", "channel": "333"}})
        builder.ctl = Mock(return_value=(0, "a" * 64 + " main.tf\n" + "b" * 64 + " tfplan", ""))
        builder.save_build = Mock()
        builder.used_ips = Mock(return_value=set())
        builder.ping = Mock(return_value=False)
        builder.tf = Mock(return_value=(1, "Synthetic plan failure", ""))
        inventory = types.ModuleType("inventory")
        inventory.get = Mock(return_value={"guests": []})
        sys.modules["inventory"] = inventory
        self.ip.verify = Mock()
        self.ip.activate = Mock()
        self.assertFalse(builder.build("svc"))
        builder.tf.assert_called_once_with("svc", "apply -auto-approve tfplan", timeout=1800)


if __name__ == "__main__":
    unittest.main()
