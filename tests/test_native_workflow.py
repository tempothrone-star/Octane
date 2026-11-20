from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.version_info < (3, 11):
    raise unittest.SkipTest("native plugin requires Python 3.11+")

from octane.openclaw_bridge import OctaneBridge


BUGGY = "def normalize(value):\n    return value or None\n\ndef calculate(value):\n    normalized = normalize(value)\n    if normalized is None:\n        return -1  # Public sentinel for missing input.\n    return normalized * 2\n"
FIXED = BUGGY.replace("return value or None", "return value")
CHECKS = '''import unittest
from pipeline import calculate

class Focused(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(calculate(0), 0)

class Regression(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(calculate(3), 6)
    def test_negative(self):
        self.assertEqual(calculate(-4), -8)
    def test_missing_input_keeps_public_sentinel(self):
        self.assertEqual(calculate(None), -1)
'''


class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "pipeline.py").write_text(BUGGY)
        (self.root / "test_pipeline.py").write_text(CHECKS)
        (self.root / "main.ts").write_text('import { value } from "./helper.js";\n')
        (self.root / "helper.ts").write_text('export const value = 1;\n')
        self.bridge = self.reopen()
        self.project_id = self.call("project_create", root=str(self.root), name="workflow")["id"]
        self.call("scan", projectId=self.project_id)

    def reopen(self):
        return OctaneBridge(str(self.base / "pil.sqlite3"), [str(self.root)], str(self.base / "workspaces"),
                            [os.environ.get("OCTANE_TEST_DOCKER_IMAGE", "python:3.12-slim")])

    def call(self, operation, actor="explorer", **params):
        return self.bridge.dispatch({"operation": operation, "params": params, "context": {"agentId": actor}})

    def investigation(self):
        return self.call("investigation_create", actor="investigator", projectId=self.project_id,
                         objective="Trace calculate and normalize", context={"unresolved": ["Explain zero input"]})["id"]

    def test_multilingual_index_revision_and_atomic_rollback(self):
        graph = self.call("graph_query", projectId=self.project_id)
        nodes = {row["id"]: row["node_key"] for row in graph["nodes"]}
        self.assertIn(("file:main.ts", "file:helper.ts", "imports"),
                      {(nodes[e["source_id"]], nodes[e["target_id"]], e["kind"]) for e in graph["edges"]})
        before = self.call("project_show", projectId=self.project_id)["revision"]
        count = len(self.call("evidence_search", projectId=self.project_id))
        with self.bridge.intelligence._session() as db:
            history_count = db.execute("SELECT count(*) FROM graph_edge_history").fetchone()[0]
        repeated = self.call("scan", projectId=self.project_id)
        self.assertEqual(repeated["changed_files"], [])
        self.assertEqual(len(self.call("evidence_search", projectId=self.project_id)), count)
        with self.bridge.intelligence._session() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM graph_edge_history").fetchone()[0], history_count)
        (self.root / "helper.ts").write_text("export const value = 2;\n")
        with patch.object(self.bridge.intelligence, "update_graph", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.call("scan", projectId=self.project_id)
        self.assertEqual(self.call("project_show", projectId=self.project_id)["revision"], before)
        self.assertEqual(len(self.call("evidence_search", projectId=self.project_id)), count)
        scan = self.call("scan", projectId=self.project_id)
        self.assertNotEqual(scan["revision"], before)
        self.assertEqual(scan["changed_files"], ["helper.ts"])
        historical = self.call("evidence_search", projectId=self.project_id, sourcePath="helper.ts", includeStale=True)
        self.assertEqual(len(historical), 2)
        self.assertEqual(sum(row["stale"] for row in historical), 1)

    def test_scan_limits_leave_index_unchanged(self):
        before = self.call("project_show", projectId=self.project_id)["revision"]
        with self.assertRaises(ValueError):
            self.call("scan", projectId=self.project_id, maxFiles=1)
        self.assertEqual(self.call("project_show", projectId=self.project_id)["revision"], before)

    def test_scan_counts_match_persisted_symbols_and_edges(self):
        (self.root / "repeated.py").write_text(
            "def helper(): ...\ndef helper(): return 1\ndef caller():\n    helper()\n    return helper()\n")
        scan = self.call("scan", projectId=self.project_id)
        graph = self.call("graph_query", projectId=self.project_id)
        self.assertEqual(scan["symbols_discovered"], sum(node["kind"] != "file" for node in graph["nodes"]))
        self.assertEqual(scan["relationships_discovered"], len(graph["edges"]))
        nodes = {node["id"]: node["node_key"] for node in graph["nodes"]}
        edge = next(edge for edge in graph["edges"] if edge["kind"] == "calls"
                    and nodes[edge["source_id"]] == "symbol:repeated.py:caller")
        self.assertEqual(json.loads(edge["metadata_json"])["lines"], [4, 5])

    def test_connections_resolve_relative_imports_and_respect_shadowing(self):
        package = self.root / "pkg"
        package.mkdir()
        (package / "helpers.py").write_text("def convert(v): return v\n")
        (package / "service.py").write_text(
            "from .helpers import convert as adapt\nfrom . import helpers as tools\n"
            "def load(v): return adapt(v)\ndef other(v): return tools.convert(v)\n"
            "def shadow(adapt): return adapt(1)\n"
            "def outer():\n    def local(): return 1\n    return local()\n"
            "class Pipeline:\n    def run(self): return self.convert(1)\n    def convert(self, v): return v\n")
        self.call("scan", projectId=self.project_id)
        callers = self.call("graph_connections", projectId=self.project_id, nodeKey="symbol:pkg/helpers.py:convert", direction="incoming")
        self.assertEqual({row["name"] for row in callers["connections"]}, {"load", "other"})
        self.assertTrue(all(row["evidence_id"] for row in callers["connections"]))
        for caller, target in (("outer", "outer.local"), ("Pipeline.run", "Pipeline.convert")):
            graph = self.call("graph_connections", projectId=self.project_id, nodeKey="symbol:pkg/service.py:" + caller)
            self.assertEqual([(row["direction"], row["name"]) for row in graph["connections"]], [("outgoing", target)])

    def test_persistent_exploration_handoff_and_revision_revalidation(self):
        investigation = self.investigation()
        first = self.call("investigation_explore", projectId=self.project_id, investigationId=investigation,
                          startingNodes=["symbol:pipeline.py:calculate"], relationships=["calls"], nodeBudget=1)
        self.assertEqual(first["stop_reason"], "node_budget")
        self.assertEqual(first["remaining_queue"], 1)
        self.bridge = self.reopen()
        resumed = self.call("investigation_explore", actor="tester", projectId=self.project_id,
                            investigationId=investigation, explorationId=first["exploration_id"], nodeBudget=1)
        self.assertEqual(resumed["stop_reason"], "completed")
        self.assertEqual(resumed["explored"][0]["node_key"], "symbol:pipeline.py:normalize")
        report = self.call("investigation_report", actor="tester", projectId=self.project_id, investigationId=investigation)
        ids = {item["agent_id"] for item in report["evidence"]}
        self.assertEqual(len(ids), 2)
        self.assertGreaterEqual(len(report["investigation"]["checkpoints"]), 3)
        (self.root / "pipeline.py").write_text(FIXED)
        # Resume detects edits without requiring a separate manual scan first.
        refused = self.call("investigation_resume", actor="tester", projectId=self.project_id, investigationId=investigation)
        self.assertFalse(refused["resumed"])
        with self.assertRaises(ValueError):
            self.call("investigation_revalidate", projectId=self.project_id, investigationId=investigation,
                      evidenceIds=[first["explored"][0]["evidence_id"]], rationale="old evidence")
        current = self.call("evidence_search", projectId=self.project_id, sourcePath="pipeline.py", evidenceType="file_scan")
        self.call("investigation_revalidate", projectId=self.project_id, investigationId=investigation,
                  evidenceIds=[current[-1]["id"]], rationale="Re-read the changed function; old traversal needs repeating")
        self.assertTrue(self.call("investigation_resume", actor="tester", projectId=self.project_id, investigationId=investigation)["resumed"])
        with self.assertRaises(ValueError):
            self.call("investigation_explore", projectId=self.project_id, investigationId=investigation,
                      explorationId=first["exploration_id"])

    def test_runtime_identity_is_separate_from_model_arguments(self):
        params = dict(projectId=self.project_id, evidenceType="note", description="observation", payload={})
        first = self.call("evidence_add", actor="agent-a", **params)
        second = self.call("evidence_add", actor="agent-b", runtimeAgentId="agent-a", **params)
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        agent = self.bridge.intelligence.get_agent(second["agent_id"])
        self.assertIn("runtime-agent:agent-b", agent["capabilities"])

    def test_self_reported_test_results_do_not_verify_findings(self):
        finding = self.call("evidence_add", projectId=self.project_id, evidenceType="note", description="claim", payload={})
        alleged_run = self.call("evidence_add", projectId=self.project_id, evidenceType="test_result",
                                verificationStatus="observed", description="self-reported pass", payload={})
        with self.assertRaisesRegex(ValueError, "recorded passing run"):
            self.call("evidence_verify", projectId=self.project_id, evidenceId=finding["id"], verificationEvidenceId=alleged_run["id"])
        with self.assertRaisesRegex(ValueError, "verify action"):
            self.call("evidence_link", projectId=self.project_id, evidenceId=alleged_run["id"],
                      relatedEvidenceId=finding["id"], relationship="verifies")

    def test_workspace_baseline_is_immutable_and_common_credentials_are_excluded(self):
        (self.root / ".env").write_text("PRIVATE_TOKEN=do-not-copy")
        (self.root / "api.env").write_text("NVIDIA_API_KEY=fixture-key")
        workspace = self.call("workspace_create", projectId=self.project_id, mode="experimentation")
        self.assertFalse((Path(workspace["path"]) / ".env").exists())
        self.assertFalse((Path(workspace["path"]) / "api.env").exists())
        self.call("workspace_patch", workspaceId=workspace["id"], relativePath="pipeline.py", content=FIXED)
        (self.root / "pipeline.py").write_text("# unrelated later edit\n")
        comparison = self.call("workspace_compare", workspaceId=workspace["id"])
        self.assertTrue(comparison["original_changed"])
        self.assertIn("-    return value or None", comparison["patch"])
        self.assertNotIn("unrelated later edit", comparison["patch"])

    @unittest.skipUnless(os.environ.get("OCTANE_TEST_DOCKER_IMAGE"), "set OCTANE_TEST_DOCKER_IMAGE to a pre-pulled approved image")
    def test_real_docker_test_edits_cannot_weaken_the_baseline_check(self):
        investigation = self.investigation()
        original = self.call("workspace_create", projectId=self.project_id, mode="observation")
        candidate = self.call("workspace_create", projectId=self.project_id, mode="experimentation")
        self.addCleanup(self.call, "workspace_destroy", workspaceId=original["id"])
        self.addCleanup(self.call, "workspace_destroy", workspaceId=candidate["id"])
        focused = ["python", "-B", "-m", "unittest", "test_pipeline.Focused"]

        def run(workspace, command):
            return self.call("workspace_run", workspaceId=workspace["id"], mode=workspace["mode"],
                             investigationId=investigation, command=command,
                             image=os.environ["OCTANE_TEST_DOCKER_IMAGE"])

        baseline = run(original, focused)
        self.call("workspace_patch", workspaceId=candidate["id"], relativePath="test_pipeline.py",
                  content=CHECKS.replace("self.assertEqual(calculate(0), 0)", "self.assertTrue(True)"))
        weakened = run(candidate, focused)
        self.assertNotEqual(baseline["returncode"], 0)
        self.assertNotEqual(weakened["returncode"], 0, weakened)
        self.assertIn("AssertionError", weakened["stderr"])
        self.assertEqual(weakened["test_integrity"], baseline["test_integrity"])
        self.call("workspace_patch", workspaceId=candidate["id"], relativePath="pipeline.py", content=FIXED)
        self.assertEqual(run(candidate, focused)["returncode"], 0)

    @unittest.skipUnless(os.environ.get("OCTANE_TEST_DOCKER_IMAGE"), "set OCTANE_TEST_DOCKER_IMAGE to a pre-pulled approved image")
    def test_real_docker_baseline_candidate_regression_and_proposal(self):
        investigation = self.investigation()
        exploration = self.call("investigation_explore", actor="explorer", projectId=self.project_id,
                                investigationId=investigation, startingNodes=["symbol:pipeline.py:calculate"],
                                relationships=["calls"], nodeBudget=1)
        finding_id = exploration["explored"][0]["evidence_id"]
        hypothesis = self.call("hypothesis_create", actor="investigator", projectId=self.project_id,
                               investigationId=investigation, statement="Normalization converts numeric zero to None")
        self.call("hypothesis_attach", actor="investigator", projectId=self.project_id, hypothesisId=hypothesis["id"],
                  evidenceId=finding_id, relation="context")
        self.bridge = self.reopen()
        handoff = self.call("investigation_resume", actor="tester", projectId=self.project_id, investigationId=investigation)
        self.assertEqual(handoff["context"]["exploration_id"], exploration["exploration_id"])
        self.assertIn(finding_id, handoff["context"]["evidence_ids"])
        requirement = self.call("evidence_add", actor="investigator", projectId=self.project_id,
                                evidenceType="user_requirement", description="Zero is a valid number and must produce zero",
                                payload={"expected": 0, "input": 0}, sourcePath="pipeline.py")
        intent = self.call("evidence_add", actor="investigator", projectId=self.project_id,
                           evidenceType="documented_behavior", description="Missing input returns the public -1 sentinel",
                           payload={"expected": -1, "documented_at": "pipeline.py:7"}, sourcePath="pipeline.py")
        baseline_ws = self.call("workspace_create", projectId=self.project_id, mode="observation")
        candidate_ws = self.call("workspace_create", projectId=self.project_id, mode="experimentation")
        focused = ["python", "-B", "-m", "unittest", "test_pipeline.Focused"]
        def run(ws, command, trace=False):
            return self.call("workspace_run", actor="tester", workspaceId=ws["id"], mode=ws["mode"],
                             investigationId=investigation, command=command,
                             image=os.environ["OCTANE_TEST_DOCKER_IMAGE"], timeoutSeconds=30, pythonTrace=trace)
        try:
            baseline = run(baseline_ws, focused, trace=True)
            self.assertNotEqual(baseline["returncode"], 0)
            self.assertIn("AssertionError", baseline["stderr"])
            trace = baseline["python_trace"]
            self.assertTrue(trace["available"], trace)
            self.assertIn(("calculate", "normalize", 5), {(e["caller"]["function"], e["callee"]["function"], e["call_line"]) for e in trace["calls"]})
            self.assertTrue(any(e["exception_type"] == "AssertionError" for e in trace["exceptions"]))
            self.call("hypothesis_attach", actor="tester", projectId=self.project_id, hypothesisId=hypothesis["id"],
                      evidenceId=baseline["evidence_id"], relation="supports")
            regression_command = ["python", "-B", "-m", "unittest", "test_pipeline.Regression"]
            overbroad = FIXED.replace("return -1", "return 0")
            self.call("workspace_patch", workspaceId=candidate_ws["id"], relativePath="pipeline.py", content=overbroad)
            overbroad_focus = run(candidate_ws, focused, trace=True)
            overbroad_regression = run(candidate_ws, regression_command)
            self.assertEqual(overbroad_focus["returncode"], 0)
            self.assertNotEqual(overbroad_regression["returncode"], 0)
            with self.assertRaisesRegex(ValueError, "regression runs must pass"):
                self.call("workspace_propose", workspaceId=candidate_ws["id"], investigationId=investigation,
                          baselineRunId=baseline["run_id"], candidateRunId=overbroad_focus["run_id"],
                          regressionRunIds=[overbroad_regression["run_id"]], evidenceIds=[intent["id"]],
                          rationale="A broad fix changes the missing-input sentinel")
            self.call("workspace_patch", workspaceId=candidate_ws["id"], relativePath="pipeline.py", content=FIXED)
            candidate = run(candidate_ws, focused, trace=True)
            regression = run(candidate_ws, regression_command)
            self.assertEqual(candidate["returncode"], 0, candidate)
            self.assertEqual(regression["returncode"], 0, regression)
            proposal_args = dict(workspaceId=candidate_ws["id"], investigationId=investigation,
                                 baselineRunId=baseline["run_id"], candidateRunId=candidate["run_id"],
                                 regressionRunIds=[regression["run_id"]], evidenceIds=[requirement["id"], intent["id"]],
                                 rationale="Preserve the missing-input sentinel and pass numeric zero through")
            with self.assertRaisesRegex(ValueError, "unchanged observation workspace"):
                self.call("workspace_propose", **proposal_args)
            check = run(baseline_ws, regression_command)
            with self.assertRaisesRegex(ValueError, "cite current"):
                self.call("workspace_propose", **dict(proposal_args, evidenceIds=[finding_id]))
            observable = ["python", "-B", "-c", "from pipeline import calculate; print(calculate(0))"]
            old_output = run(baseline_ws, observable)
            new_output = run(candidate_ws, observable)
            self.assertEqual([old_output["returncode"], new_output["returncode"]], [0, 0])
            self.assertNotEqual(old_output["stdout"], new_output["stdout"])
            with self.assertRaisesRegex(ValueError, "preservation output differs"):
                self.call("workspace_propose", **dict(proposal_args, regressionRunIds=[new_output["run_id"]],
                                                       strictPreservationOutput=True))
            proposal = self.call("workspace_propose", actor="tester", **proposal_args)
            self.assertTrue(proposal["review_required"])
            self.assertIn("+    return value\n", proposal["patch"])
            self.assertEqual(proposal["preservation_pairs"][0]["baseline_run_id"], check["run_id"])
            self.assertEqual(proposal["changed_paths"], [{"path": "pipeline.py", "seen_in_focused_trace": True}])
            self.assertEqual((self.root / "pipeline.py").read_text(), BUGGY)
            subprocess.run(["git", "apply", "--check", "-"], cwd=self.root, input=proposal["patch"], text=True, check=True)
            report = self.call("investigation_report", projectId=self.project_id, investigationId=investigation)
            self.assertEqual(len(report["executions"]), 8)
            self.assertEqual(len(report["patch_proposals"]), 1)
            finding = self.call("evidence_add", projectId=self.project_id, evidenceType="note",
                                description="Nonzero inputs work on the original snapshot", payload={})
            self.assertTrue(self.call("evidence_verify", projectId=self.project_id, evidenceId=finding["id"],
                                      verificationEvidenceId=check["evidence_id"])["verified"])
            with self.assertRaisesRegex(ValueError, "unchanged snapshot"):
                self.call("evidence_verify", projectId=self.project_id, evidenceId=finding["id"],
                          verificationEvidenceId=regression["evidence_id"])
            # A later candidate edit cannot reuse old passing executions.
            self.call("workspace_patch", workspaceId=candidate_ws["id"], relativePath="pipeline.py", content=FIXED + "\n# later edit\n")
            with self.assertRaises(ValueError):
                self.call("workspace_propose", workspaceId=candidate_ws["id"], investigationId=investigation,
                          baselineRunId=baseline["run_id"], candidateRunId=candidate["run_id"], regressionRunIds=[regression["run_id"]],
                          evidenceIds=[requirement["id"]], rationale="must fail")
        finally:
            for ws in (baseline_ws, candidate_ws):
                self.call("workspace_destroy", workspaceId=ws["id"])


if __name__ == "__main__":
    unittest.main()
