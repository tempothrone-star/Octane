"""Exercise proposal acceptance and every rejection without running Docker."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from octane.db import _id, _now
from octane.experiments import propose_patch
from octane.openclaw_bridge import OctaneBridge
from octane.test_protection import baseline_tests
from octane.workspace import WorkspaceManager


class PatchGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "source.py").write_text("VALUE = 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_source.py").write_text("assert VALUE == 2\n")
        self.bridge = OctaneBridge(str(base / "pil.sqlite3"), [str(self.root)], str(base / "workspaces"))
        self.pil = self.bridge.intelligence
        self.project = self.call("project_create", name="gate", root=str(self.root))["id"]
        self.investigation = self.call("investigation_create", projectId=self.project, objective="repair VALUE")["id"]
        self.original = self.call("workspace_create", projectId=self.project, mode="observation")
        self.candidate = self.call("workspace_create", projectId=self.project, mode="experimentation")
        self.manager = WorkspaceManager(self.pil, base / "workspaces")
        self.manager.apply_candidate_patch(self.candidate["id"], "source.py", "VALUE = 2\n")
        self.actor = self.bridge._actor(self.project, "workflow")
        self.intent = self.call("evidence_add", projectId=self.project, evidenceType="documented_behavior",
                                description="VALUE must be two", payload={})["id"]
        self.focus = ["python", "-B", "tests/test_source.py"]
        self.preserve = ["python", "-B", "-c", "assert 1 + 1 == 2"]
        self.baseline = self.record(self.original, self.focus, "failed")
        self.preserved = self.record(self.original, self.preserve, "passed")
        self.fixed = self.record(self.candidate, self.focus, "passed")
        self.regression = self.record(self.candidate, self.preserve, "passed")

    def call(self, operation, **params):
        return self.bridge.dispatch({"operation": operation, "params": params})

    def record(self, workspace, command, status):
        stored = self.manager._get(workspace["id"])
        tests, _ = baseline_tests(Path(stored["baseline_path"]))
        comparison = self.manager.compare_workspace(workspace["id"])
        result = {"stdout": "stable\n", "stderr": "", "returncode": 0 if status == "passed" else 1,
                  "test_integrity": tests}
        evidence = self.pil.submit_evidence(self.project, "runtime_observation", result,
                                            "Fake runner result", self.actor, stored["base_revision"])
        identifier = _id("RUN")
        with self.pil._transaction() as db:
            db.execute("""INSERT INTO execution_runs
                (id,project_id,workspace_id,investigation_id,evidence_id,command_json,image,image_id,
                 before_hash,after_hash,snapshot_hash,status,result_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, self.project, workspace["id"], self.investigation, evidence["id"],
                 json.dumps(command), "image:test", "sha256:" + "a" * 64, comparison["candidate_hash"],
                 comparison["candidate_hash"], stored["snapshot_hash"], status, json.dumps(result), _now()))
        return identifier

    def edit_run(self, identifier, **fields):
        with self.pil._transaction() as db:
            for field, value in fields.items():
                self.assertIn(field, {"status", "before_hash", "after_hash", "image_id", "command_json",
                                      "result_json", "snapshot_hash", "evidence_id"})
                db.execute(f"UPDATE execution_runs SET {field}=? WHERE id=?", (value, identifier))

    def result(self, identifier, **fields):
        with self.pil._session() as db:
            data = json.loads(db.execute("SELECT result_json FROM execution_runs WHERE id=?", (identifier,)).fetchone()[0])
        data.update(fields)
        self.edit_run(identifier, result_json=json.dumps(data))

    def propose(self, **options):
        arguments = dict(manager=self.manager, project_id=self.project, investigation_id=self.investigation,
                         workspace_id=self.candidate["id"], baseline_run_id=self.baseline,
                         candidate_run_id=self.fixed, regression_run_ids=[self.regression],
                         supporting_evidence_ids=[self.intent], rationale="Correct VALUE while preserving the other check",
                         agent_id=self.actor)
        arguments.update(options)
        return propose_patch(**arguments)

    def reject(self, message, **options):
        with self.assertRaisesRegex(ValueError, message):
            self.propose(**options)
        with self.pil._session() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM patch_proposals").fetchone()[0], 0)

    def test_accepts_paired_checks_and_records_review_evidence(self):
        result = self.propose(strict_preservation_output=True)
        self.assertTrue(result["review_required"])
        self.assertEqual(result["preservation_pairs"][0]["baseline_run_id"], self.preserved)
        self.assertFalse(result["test_changes"]["allowed"])

    def test_rejects_invalid_boolean_options(self):
        for options, message in (({"strict_preservation_output": 1}, "strictPreservationOutput"),
                                 ({"allow_test_changes": "yes"}, "allowTestChanges")):
            with self.subTest(options=options):
                self.reject(message, **options)

    def test_requires_a_rationale(self):
        self.reject("intent-preserving rationale", rationale="")

    def test_requires_preservation_checks(self):
        self.reject("separate recorded regression run", regression_run_ids=[])

    def test_requires_intent_evidence(self):
        self.reject("evidence explaining expected behavior", supporting_evidence_ids=[])

    def test_requires_distinct_run_ids(self):
        self.reject("distinct strings", candidate_run_id=self.baseline)
        self.reject("distinct strings", baseline_run_id=None)

    def test_requires_candidate_workspace(self):
        self.reject("experimentation workspace", workspace_id=self.original["id"])

    def test_rejects_changed_original(self):
        (self.root / "source.py").write_text("VALUE = 3\n")
        self.reject("original project changed")

    def test_rejects_empty_binary_and_non_modified_patches(self):
        comparison = self.manager.compare_workspace(self.candidate["id"])
        variants = [
            {**comparison, "patch": ""},
            {**comparison, "differences": [{"path": "source.py", "kind": "added"}]},
            {**comparison, "differences": [{"path": "source.py", "kind": "modified", "binary": True}]},
        ]
        for item in variants:
            with self.subTest(comparison=item):
                with patch.object(self.manager, "compare_workspace", return_value=item):
                    self.reject("text-only patch")

    def test_rejects_oversized_patch(self):
        comparison = self.manager.compare_workspace(self.candidate["id"])
        with patch.object(self.manager, "compare_workspace", return_value={**comparison, "patch": "a" * 1_000_001}):
            self.reject("1 MB proposal limit")

    def test_rejects_credential_like_patch(self):
        comparison = self.manager.compare_workspace(self.candidate["id"])
        with patch.object(self.manager, "compare_workspace", return_value={**comparison, "patch": "password=hunter2"}):
            self.reject("credential-like text")

    def test_requires_current_investigation(self):
        self.reject("current investigation", investigation_id="missing")

    def test_rejects_terminal_investigation(self):
        with self.pil._transaction() as db:
            db.execute("UPDATE investigations SET status='completed' WHERE id=?", (self.investigation,))
        self.reject("terminal investigation")

    def test_requires_recorded_runs(self):
        self.reject("every run must be recorded", candidate_run_id="missing")
        self.edit_run(self.fixed, evidence_id=None)
        self.reject("every run must be recorded")

    def test_requires_original_test_protection(self):
        self.result(self.fixed, test_integrity=None)
        self.reject("original baseline tests protected")

    def test_rejects_source_changes_and_incomplete_runs(self):
        for field in ("timed_out", "cleanup_warning"):
            with self.subTest(field=field):
                self.result(self.fixed, **{field: True})
                self.reject("without changing the tested source")
                self.result(self.fixed, **{field: False})
        self.edit_run(self.fixed, after_hash="changed")
        self.reject("without changing the tested source")

    def test_requires_requested_trace(self):
        self.result(self.fixed, python_trace={"available": False})
        self.reject("runtime trace is unavailable")

    def test_requires_same_snapshot(self):
        self.edit_run(self.fixed, snapshot_hash="different")
        self.reject("same baseline snapshot")

    def test_requires_failing_baseline_and_passing_candidate(self):
        self.edit_run(self.baseline, status="passed")
        self.reject("baseline must fail")
        self.edit_run(self.baseline, status="failed")
        self.edit_run(self.fixed, status="failed")
        self.reject("baseline must fail")

    def test_requires_unchanged_observation_baseline(self):
        self.edit_run(self.baseline, before_hash="changed", after_hash="changed")
        self.reject("baseline must test the unchanged snapshot")

    def test_requires_same_command_image_and_trace(self):
        for field, value in (("image_id", "sha256:other"), ("command_json", '["different"]')):
            with self.subTest(field=field):
                with self.pil._session() as db:
                    original = db.execute(f"SELECT {field} FROM execution_runs WHERE id=?", (self.fixed,)).fetchone()[0]
                self.edit_run(self.fixed, **{field: value})
                self.reject("same command, immutable image, and trace")
                self.edit_run(self.fixed, **{field: original})
        self.result(self.fixed, python_trace={"available": True})
        self.reject("same command, immutable image, and trace")

    def test_requires_current_candidate_bytes_and_passing_regressions(self):
        self.edit_run(self.regression, status="failed")
        self.reject("pass on the current candidate bytes")

    def test_rejects_unavailable_structured_reports(self):
        self.result(self.fixed, report_format="junit", report_error="missing output")
        self.reject("structured report is unavailable")

    def test_requires_original_failed_test_ids_to_pass_in_reports(self):
        self.result(self.baseline, report_format="junit", report={"tests": [{"id": "zero", "status": "failed"}]})
        self.result(self.fixed, report_format="junit", report={"tests": [{"id": "other", "status": "passed"}]})
        self.reject("original failing tests passing")
        self.result(self.fixed, report_format="junit", report={"tests": [{"id": "zero", "status": "passed"}]})
        self.assertTrue(self.propose()["review_required"])

    def test_coverage_report_records_touched_lines(self):
        self.result(self.fixed, report={"coverage": {"source.py": {"covered": [1], "missing": []}}})
        proposal = self.propose()
        self.assertEqual(proposal["changed_paths"][0]["covered_changed_lines"], [1])

    def test_mutation_evidence_is_bound_to_current_candidate_and_original_tests(self):
        evidence = self.call("evidence_add", projectId=self.project, evidenceType="mutation_result",
                             description="wrong candidate", payload={})["id"]
        self.reject("mutation evidence must detect", supporting_evidence_ids=[self.intent, evidence])

    def test_optional_mutation_summary_and_provenance_bind_to_proposal(self):
        from octane.provenance import statement
        comparison = self.manager.compare_workspace(self.candidate["id"])
        tests, _ = baseline_tests(Path(self.manager._get(self.candidate["id"])["baseline_path"]))
        payload = {"candidate_hash": comparison["candidate_hash"], "image_id": "sha256:" + "a" * 64,
                   "test_integrity": tests, "original_bug_detected": True, "score": 1.0, "killed": 1, "survived": 0,
                   "candidate_run_id": self.fixed,
                   "mutants": [{"run_id": self.baseline, "operator": "original-source", "status": "killed"}]}
        evidence = self.call("evidence_add", projectId=self.project, evidenceType="mutation_result",
                             description="bounded mutation fixture", payload=payload)["id"]
        proposal = self.propose(supporting_evidence_ids=[self.intent, evidence])
        self.assertEqual(proposal["mutation_results"][0]["score"], 1.0)
        attestation = statement(self.pil, self.project, proposal["evidence_id"])
        self.assertEqual(attestation["predicate"]["candidate_hash"], comparison["candidate_hash"])
        self.assertEqual(len(attestation["predicate"]["execution_runs"]), 4)
        with self.assertRaisesRegex(ValueError, "patch proposal"):
            statement(self.pil, self.project, self.intent)

    def test_mutation_rejects_unrecorded_checks_and_surviving_reversions(self):
        comparison = self.manager.compare_workspace(self.candidate["id"])
        tests, _ = baseline_tests(Path(self.manager._get(self.candidate["id"])["baseline_path"]))
        payload = {"candidate_hash": comparison["candidate_hash"], "image_id": "sha256:" + "a" * 64,
                   "test_integrity": tests, "original_bug_detected": True, "candidate_run_id": self.fixed}
        def submit(value):
            return self.call("evidence_add", projectId=self.project, evidenceType="mutation_result",
                             description="mutation validation fixture", payload=value)["id"]
        evidence = submit(payload)
        self.reject("individual recorded executions", supporting_evidence_ids=[self.intent, evidence])
        payload["mutants"] = [{"run_id": "missing", "operator": "original-source", "status": "killed"}]
        evidence = submit(payload)
        self.reject("mutation executions must match", supporting_evidence_ids=[self.intent, evidence])
        payload["mutants"] = [{"run_id": self.fixed, "operator": "original-source", "status": "survived"}]
        evidence = submit(payload)
        self.reject("detect the tested original-source", supporting_evidence_ids=[self.intent, evidence])

    def test_trace_modes_and_scope_include_runtime_and_native_paths(self):
        from octane.experiments import _proposal_scope, _trace_settings
        native = {"result_json": json.dumps({"native_trace": {"symbols": ["entry"]}})}
        runtime = {"result_json": json.dumps({"runtime_trace": {"format": "node"}})}
        self.assertEqual(_trace_settings(native)[:2], ("native", ("entry",)))
        self.assertEqual(_trace_settings(runtime)[:2], ("runtime", "node"))
        traces = {"result_json": json.dumps({"python_trace": {"calls": [
            {"caller": {"path": "source.py"}, "callee": {"path": "helper.py"}}]}})}
        scope = _proposal_scope(traces, traces, {"differences": [{"path": "source.py"}]})
        self.assertTrue(scope[0]["seen_in_focused_trace"])

    def test_requires_regression_image(self):
        self.edit_run(self.regression, image_id="sha256:other")
        self.reject("regressions must use the candidate image")

    def test_requires_separate_regression_command(self):
        self.edit_run(self.regression, command_json=json.dumps(self.focus))
        self.reject("regression command must be separate")

    def test_requires_passing_baseline_preservation(self):
        self.edit_run(self.preserved, status="failed")
        self.reject("run each preservation check")

    def test_requires_strict_output_match(self):
        self.result(self.regression, stdout="changed")
        self.reject("preservation output differs", strict_preservation_output=True)

    def test_requires_current_supporting_evidence(self):
        with self.pil._transaction() as db:
            db.execute("UPDATE evidence SET stale=1 WHERE id=?", (self.intent,))
        self.reject("supporting evidence must be current")

    def test_requires_evidence_of_intent(self):
        with self.pil._transaction() as db:
            db.execute("UPDATE evidence SET evidence_type='note' WHERE id=?", (self.intent,))
        self.reject("cite current")

    def test_rejects_test_only_and_source_plus_test_edits(self):
        self.manager.apply_candidate_patch(self.candidate["id"], "tests/test_source.py", "assert True\n")
        self.reject("patch modifies test files")
        self.manager.apply_candidate_patch(self.candidate["id"], "source.py", "VALUE = 1\n")
        self.reject("patch modifies test files")

    def test_test_edits_need_justification_and_protected_reruns(self):
        self.manager.apply_candidate_patch(self.candidate["id"], "tests/test_source.py", "assert True\n")
        self.reject("testChangesRationale", allow_test_changes=True)
        self.fixed = self.record(self.candidate, self.focus, "passed")
        self.regression = self.record(self.candidate, self.preserve, "passed")
        proposal = self.propose(allow_test_changes=True, test_changes_rationale="Update explanatory test text")
        self.assertEqual(proposal["test_changes"]["paths"], ["tests/test_source.py"])
        self.assertEqual(proposal["test_changes"]["justification"], "Update explanatory test text")
        self.assertTrue(proposal["test_changes"]["baseline_tests"]["original_tests"])
