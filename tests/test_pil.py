from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
import sqlite3

from octane import ExplorationProfile, ExplorationScheduler, ProjectIntelligence, PythonAdapter, Tentacle, WorkspaceManager
from octane.db import _SCHEMA
from octane.cli import run_demo


class PILTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.database = ProjectIntelligence(self.base / "pil.sqlite3")
        self.project = self.database.create_project("fixture", self.root)

    def test_project_metadata_and_secret_config_rejection(self):
        project = self.database.create_project("metadata", self.root, languages=["Python"],
                                              repository_url="https://user:pass@example.com/org/repo?token=hidden",
                                              configuration={"index_tests": True})
        self.assertEqual(project["languages"], ["Python"])
        self.assertEqual(project["repository_url"], "https://example.com/org/repo")
        self.assertEqual(project["configuration"], {"index_tests": True})
        updated = self.database.update_project(project["id"], name="metadata-renamed", languages=["Python", "Rust"])
        self.assertEqual(updated["name"], "metadata-renamed")
        self.assertEqual(updated["languages"], ["Python", "Rust"])
        with self.assertRaises(ValueError):
            self.database.update_project(project["id"], configuration={"services": {"api_token": "do-not-store"}})

    def test_v1_database_migrates_transactionally_to_current_schema(self):
        path = self.base / "v1.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(_SCHEMA)
            connection.execute("INSERT INTO schema_migrations VALUES(1,'2025-01-01T00:00:00+00:00')")
        upgraded = ProjectIntelligence(path)
        with upgraded._session() as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")], [1, 2, 3, 4])
            columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
            self.assertIn("last_indexed_at", columns)
            investigation_columns = {row[1] for row in connection.execute("PRAGMA table_info(investigations)")}
            self.assertIn("assigned_agent_id", investigation_columns)
        ProjectIntelligence(path)
        concurrent_path = self.base / "concurrent-v1.sqlite3"
        with sqlite3.connect(concurrent_path) as connection:
            connection.executescript(_SCHEMA)
            connection.execute("INSERT INTO schema_migrations VALUES(1,'2025-01-01T00:00:00+00:00')")
        errors = []
        def migrate():
            try:
                ProjectIntelligence(concurrent_path)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=migrate) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        with ProjectIntelligence(concurrent_path)._session() as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 4)

    def test_investigation_lifecycle_and_audit_terminal_state(self):
        agent = self.database.register_agent(self.project["id"], "owner", "investigator",
                                             permissions=["investigate", "checkpoint_investigation"])
        task = self.database.create_investigation(self.project["id"], "Inspect the project", agent["id"])
        self.database.pause_investigation(task["id"], agent["id"], {"completed": ["scan"], "next_action": "verify"})
        self.database.update_investigation(task["id"], context={"next_action": "uncheckpointed change"}, agent_id=agent["id"])
        resumed = self.database.resume_investigation(task["id"], agent["id"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["context"]["next_action"], "verify")
        completed = self.database.complete_investigation(task["id"], agent["id"], {"completed": ["verified"]})
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(self.database.get_agent(agent["id"])["current_investigation_id"])
        with self.assertRaises(ValueError):
            self.database.resume_investigation(task["id"], agent["id"])
        self.assertGreaterEqual(len(self.database.search_audit_logs(self.project["id"], agent["id"])), 4)

    def test_project_isolation_evidence_provenance_and_persistence(self):
        second = self.database.create_project("second", self.root)
        agent = self.database.register_agent(self.project["id"], "explorer", "explorer", permissions=["write_evidence"])
        self.assertTrue(self.database.check_permission(agent["id"], "write_evidence"))
        self.assertFalse(self.database.check_permission(agent["id"], "delete_evidence"))
        item = self.database.submit_evidence(self.project["id"], "observation", {"value": 4}, "observed value", agent["id"], verification_status="observed")
        self.assertEqual(self.database.retrieve_evidence(item["id"])["payload"], {"value": 4})
        self.assertEqual(len(self.database.search_evidence(self.project["id"])), 1)
        self.assertEqual(self.database.search_evidence(second["id"]), [])
        reopened = ProjectIntelligence(self.base / "pil.sqlite3")
        self.assertEqual(reopened.retrieve_evidence(item["id"])["agent_id"], agent["id"])
        self.assertEqual(json.loads(item["payload_json"]), {"value": 4})

    def test_transactions_roll_back_and_concurrent_writes(self):
        with self.assertRaises(RuntimeError):
            with self.database._transaction() as db:
                db.execute("INSERT INTO audit_logs VALUES(?,?,?,?,?,?,?,?)", ("rollback", self.project["id"], None, "x", "x", None, "{}", "now"))
                raise RuntimeError("rollback")
        with self.database._session() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_logs WHERE id='rollback'").fetchone()[0], 0)
        errors = []
        def add(index):
            try:
                self.database.submit_evidence(self.project["id"], "concurrent", {"i": index}, str(index))
            except Exception as exc:  # surfaced below if a thread fails
                errors.append(exc)
        threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.database.search_evidence(self.project["id"], evidence_type="concurrent")), 8)

    def test_verification_requires_separate_observed_evidence(self):
        reviewer = self.database.register_agent(self.project["id"], "verifier", "tester", permissions=["verify_evidence"])
        finding = self.database.submit_evidence(self.project["id"], "static_finding", {"issue": "candidate"}, "candidate finding")
        with self.assertRaises(ValueError):
            self.database.submit_evidence(self.project["id"], "static_finding", {}, "not pre-verified", verification_status="verified")
        run = self.database.submit_evidence(self.project["id"], "test_result", {"passed": True}, "reproducing test passed")
        self.database.mark_evidence_verified(finding["id"], run["id"], reviewer["id"])
        self.assertEqual(self.database.retrieve_evidence(finding["id"])["verification_status"], "verified")

    def test_agent_permissions_and_cross_project_relationships_are_enforced(self):
        no_rights = self.database.register_agent(self.project["id"], "read-only", "reviewer")
        with self.assertRaises(PermissionError):
            self.database.submit_evidence(self.project["id"], "observation", {}, "not allowed", no_rights["id"])
        with self.assertRaises(PermissionError):
            self.database.search_evidence(self.project["id"], requesting_agent_id=no_rights["id"])
        other_project = self.database.create_project("other", self.root)
        other_evidence = self.database.submit_evidence(other_project["id"], "observation", {}, "other project")
        with self.assertRaises(ValueError):
            self.database.add_memory(self.project["id"], "architecture", "cross-project", supporting_evidence=[other_evidence["id"]])

    def test_investigation_handover_and_hypothesis_evidence(self):
        agent_a = self.database.register_agent(self.project["id"], "A", "investigator", permissions=["investigate", "write_evidence", "checkpoint_investigation"])
        agent_b = self.database.register_agent(self.project["id"], "B", "tester", permissions=["checkpoint_investigation"])
        investigation = self.database.create_investigation(self.project["id"], "Find the regression", agent_a["id"])
        evidence = self.database.submit_evidence(self.project["id"], "test", {"passed": False}, "failing test", agent_a["id"])
        hypothesis = self.database.create_hypothesis(self.project["id"], "An input is normalized incorrectly", investigation["id"], agent_a["id"], confidence=.7)
        self.database.attach_evidence(hypothesis["id"], evidence["id"], "supports", agent_a["id"])
        self.assertEqual(self.database.retrieve_hypotheses(self.project["id"], investigation["id"])[0]["evidence"][0]["relation"], "supports")
        updated_hypothesis = self.database.update_hypothesis(hypothesis["id"], status="under_investigation", agent_id=agent_a["id"])
        with self.assertRaises(RuntimeError):
            self.database.update_hypothesis(hypothesis["id"], status="supported", agent_id=agent_a["id"], expected_updated_at=hypothesis["updated_at"])
        self.assertEqual(updated_hypothesis["status"], "under_investigation")
        self.database.checkpoint_investigation(investigation["id"], "paused", {"evidence_ids": [evidence["id"]], "next_action": "reproduce"}, agent_a["id"])
        resumed = self.database.retrieve_investigation(investigation["id"])
        self.assertTrue(resumed["revision_compatible"])
        self.assertEqual(resumed["checkpoints"][-1]["payload"]["evidence_ids"], [evidence["id"]])
        self.assertEqual(agent_b["role"], "tester")
        handover = self.database.resume_investigation(investigation["id"], agent_b["id"])
        self.assertTrue(handover["resumed"])
        self.assertEqual(handover["context"]["next_action"], "reproduce")
        self.assertEqual(self.database.retrieve_investigation(investigation["id"])["assigned_agent_id"], agent_b["id"])
        self.database.set_revision(self.project["id"], "next-revision")
        self.assertFalse(self.database.retrieve_investigation(investigation["id"])["revision_compatible"])
        blocked_resume = self.database.resume_investigation(investigation["id"], agent_b["id"])
        self.assertFalse(blocked_resume["resumed"])
        self.assertIn("revalidate", blocked_resume["reason"])

    def test_incremental_python_scan_graph_and_stale_knowledge(self):
        source = self.root / "app.py"
        source.write_text("def leaf(value):\n    return value + 1\n\ndef main(value):\n    return leaf(value)\n", encoding="utf-8")
        scanner = PythonAdapter()
        first = scanner.scan(self.database, self.project["id"], self.root)
        self.assertEqual(first["symbols_discovered"], 2)
        repeat = scanner.scan(self.database, self.project["id"], self.root)
        self.assertEqual(repeat["changed_files"], [])
        scan_evidence = self.database.search_evidence(self.project["id"], evidence_type="file_scan")
        self.assertTrue(all(item["agent_id"] for item in scan_evidence))
        graph = self.database.query_graph(self.project["id"])
        self.assertTrue(any(edge["kind"] == "calls" for edge in graph["edges"]))
        evidence = self.database.submit_evidence(self.project["id"], "review", {"ok": True}, "app inspected", source_path="app.py")
        memory = self.database.add_memory(self.project["id"], "intent", "The increment behavior is intentional", supporting_evidence=[evidence["id"]])
        self.assertEqual(self.database.retrieve_memory(memory["id"])["supporting_evidence_ids"], [evidence["id"]])
        source.write_text("def leaf(value):\n    return value + 2\n", encoding="utf-8")
        second = scanner.scan(self.database, self.project["id"], self.root)
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(second["changed_files"], ["app.py"])
        current_project = self.database.get_project(self.project["id"])
        self.assertIn("python", current_project["languages"])
        self.assertIsNotNone(current_project["last_indexed_at"])
        changes = self.database.get_project_changes(self.project["id"], since_revision=first["revision"])
        self.assertEqual(changes["changes"][-1]["changes"]["changed"], ["app.py"])
        self.assertEqual(self.database.retrieve_evidence(evidence["id"])["stale"], 1)
        self.assertEqual(self.database.search_memories(self.project["id"]), [])
        with self.database._session() as db:
            self.assertEqual(db.execute("SELECT status FROM memories WHERE id=?", (memory["id"],)).fetchone()["status"], "stale")
        old_graph = self.database.query_graph(self.project["id"], include_stale=True)
        self.assertTrue(any(edge["kind"] == "calls" and edge["stale"] for edge in old_graph["edges"]))
        self.assertTrue(any(node["name"] == "main" and node["stale"] for node in old_graph["nodes"]))

    def test_atomic_graph_update_records_confidence_and_provenance(self):
        agent = self.database.register_agent(self.project["id"], "graph-writer", "explorer", permissions=["update_graph"])
        result = self.database.update_graph(self.project["id"],
            nodes=[{"node_key": "file:a.py", "kind": "file", "name": "a.py"},
                    {"node_key": "symbol:a.py:run", "kind": "function", "name": "run", "path": "a.py"}],
            relationships=[{"source": "symbol:a.py:run", "target": "file:a.py", "kind": "depends_on", "confidence": .75}],
            agent_id=agent["id"])
        graph = self.database.query_graph(self.project["id"])
        self.assertEqual(len(result["relationships"]), 1)
        edge = graph["edges"][0]
        self.assertEqual(edge["confidence"], .75)
        self.assertEqual(edge["provenance_agent_id"], agent["id"])
        self.database.update_graph(self.project["id"], relationships=[{"source": "symbol:a.py:run", "target": "file:a.py",
            "kind": "depends_on", "confidence": .6, "revision": "revision-2"}], agent_id=agent["id"])
        history = self.database.get_relationship_history(self.project["id"], "symbol:a.py:run")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["confidence"], .75)
        before = len(graph["nodes"])
        with self.assertRaises(ValueError):
            self.database.update_graph(self.project["id"], nodes=[{"node_key": "rolled-back", "kind": "file", "name": "bad"}],
                                       relationships=[{"source": "rolled-back", "target": "missing", "kind": "calls"}], agent_id=agent["id"])
        self.assertEqual(len(self.database.query_graph(self.project["id"])["nodes"]), before)

    def test_tentacle_traversal_records_evidence_with_node_budget(self):
        (self.root / "app.py").write_text("def leaf(value):\n    return value + 1\n\ndef main(value):\n    return leaf(value)\n", encoding="utf-8")
        agent = self.database.register_agent(self.project["id"], "explorer", "explorer", permissions=["write_evidence", "read_evidence", "read_graph"])
        PythonAdapter().scan(self.database, self.project["id"], self.root)
        profile = ExplorationProfile("Trace main callers", ["symbol:app.py:main"], ["calls"], depth_limit=2, node_budget=1)
        first = Tentacle().explore(self.database, self.project["id"], profile, agent["id"])
        self.assertEqual(first["nodes_examined"], 1)
        self.assertEqual(first["stop_reason"], "node_budget")
        records = self.database.search_evidence(self.project["id"], evidence_type="tentacle_exploration")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["payload"]["relationships"][0]["target"], "symbol:app.py:leaf")
        second = Tentacle().explore(self.database, self.project["id"], profile, agent["id"])
        self.assertEqual(second["nodes_examined"], 0)

    def test_scheduler_limits_and_avoids_repeat_work(self):
        scheduler = ExplorationScheduler({"relevance": 2.0})
        items = [
            {"node_key": "low", "relevance": 1, "uncertainty": 0, "estimated_seconds": 1},
            {"node_key": "high", "relevance": 3, "uncertainty": 1, "estimated_seconds": 2},
            {"node_key": "done", "relevance": 10, "estimated_seconds": 1},
        ]
        result = scheduler.prioritize(items, explored={"done"}, node_budget=1, time_budget_seconds=3)
        self.assertEqual([item["node_key"] for item in result], ["high"])
        self.assertEqual(scheduler.prioritize(items, node_budget=0), [])

    def test_workspace_snapshot_patch_and_diff_do_not_change_original(self):
        file = self.root / "sample.py"
        file.write_text("value = 1\n", encoding="utf-8")
        external = self.base / "outside.txt"
        external.write_text("must not be copied", encoding="utf-8")
        (self.root / "outside-link.txt").symlink_to(external)
        workspace_manager = WorkspaceManager(self.database, self.base / "snapshots")
        observation = workspace_manager.create_workspace(self.project["id"], "observation")
        self.assertFalse((Path(observation["path"]) / "outside-link.txt").exists())
        with self.assertRaises(PermissionError):
            workspace_manager.apply_candidate_patch(observation["id"], "sample.py", "value = 2\n")
        experimental = workspace_manager.create_workspace(self.project["id"], "experimentation")
        workspace_manager.apply_candidate_patch(experimental["id"], "sample.py", "value = 2\n")
        comparison = workspace_manager.compare_workspace(experimental["id"])
        self.assertEqual(comparison["differences"][0]["path"], "sample.py")
        self.assertIn("value = 2", comparison["patch"])
        self.assertEqual(file.read_text(encoding="utf-8"), "value = 1\n")
        with self.assertRaises(ValueError):
            workspace_manager.apply_candidate_patch(experimental["id"], "../escape", "bad")

    def test_three_agent_demo_persists_handover_and_revision_staleness(self):
        report = run_demo(ProjectIntelligence(self.base / "demo.sqlite3"))
        self.assertEqual(len(report["agents"]), 3)
        self.assertEqual(report["hypothesis"]["status"], "supported")
        self.assertEqual(report["investigation_status"], "blocked")
        self.assertTrue(report["resume_revision_compatible"])
        self.assertEqual(report["revision_change_demo"]["changed_files"], ["pipeline.py"])
        self.assertTrue(report["revision_change_demo"]["historical_evidence_preserved"])
        self.assertEqual(report["revision_change_demo"]["supporting_memory_status"], "stale")


if __name__ == "__main__":
    unittest.main()
