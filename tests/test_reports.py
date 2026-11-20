"""Bounded report ingestion with source paths and privacy-preserving telemetry."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from octane.reports import parse_report, read_report
from octane.openclaw_bridge import OctaneBridge


class ReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def parse(self, format_name, data):
        raw = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        return parse_report(raw, format_name, self.root)

    def test_go_coverage_aggregate_expansion_is_bounded(self):
        report = b"mode: set\n" + b"app.go:1.1,100000.1 1 1\n" * 3
        with self.assertRaisesRegex(ValueError, "aggregate line budget"):
            parse_report(report, "go-coverage", self.root)

    def test_test_runner_formats_capture_failures_and_source(self):
        junit = self.parse("junit", '<testsuite><testcase name="zero" file="/workspace/test_app.py"><failure message="expected zero"/></testcase><testcase name="positive"/></testsuite>')
        self.assertEqual([item["status"] for item in junit["tests"]], ["failed", "passed"])
        self.assertEqual(junit["tests"][0]["path"], "test_app.py")
        pytest = self.parse("pytest", {"tests": [{"nodeid": "tests/test_app.py::test_zero", "outcome": "failed",
                                                "call": {"longrepr": "expected zero"}}]})
        self.assertEqual(pytest["tests"][0]["message"], "expected zero")
        go = self.parse("go-test", '{"Action":"fail","Package":"example/cache","Test":"TestZero"}\n')
        self.assertEqual(go["tests"][0]["status"], "failed")
        jest = self.parse("jest", {"testResults": [{"name": "test.js", "assertionResults": [
            {"fullName": "zero", "status": "failed", "failureMessages": ["expected zero"]}]}]})
        self.assertEqual(jest["tests"][0]["path"], "test.js")

    def test_coverage_formats_record_lines_within_the_project(self):
        report = self.parse("coverage", {"files": {"src/app.py": {"executed_lines": [1, 2], "missing_lines": [3]},
                                                  "/outside/app.py": {"executed_lines": [7]}}})
        self.assertEqual(report["coverage"], {"src/app.py": {"covered": [1, 2], "missing": [3]}})
        go = self.parse("go-coverage", "mode: set\napp.go:1.1,3.2 1 1\napp.go:4.1,5.2 1 0\n")
        self.assertEqual(go["coverage"]["app.go"], {"covered": [1, 2, 3], "missing": [4, 5]})
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.parse("coverage", {"files": {"app.py": {"executed_lines": [-1]}}})

    def test_sarif_finding_includes_rule_and_location(self):
        data = {"runs": [{"results": [{"ruleId": "F401", "message": {"text": "unused import"},
                                       "locations": [{"physicalLocation": {"artifactLocation": {"uri": "app.py"},
                                                                          "region": {"startLine": 4}}}]}]}]}
        finding = self.parse("sarif", data)["findings"][0]
        self.assertEqual((finding["rule"], finding["path"], finding["line"]), ("F401", "app.py", 4))

    def test_telemetry_imports_omit_requests_users_and_attribute_values(self):
        sentry = self.parse("sentry", {"event_id": "event", "user": {"email": "private@example.invalid"},
            "request": {"headers": {"Authorization": "private"}},
            "exception": {"values": [{"type": "ValueError", "stacktrace": {"frames": [
                {"filename": "app.py", "lineno": 7, "function": "parse"}]}}]}})
        self.assertEqual(sentry["telemetry"][0]["frames"][0]["path"], "app.py")
        self.assertNotIn("private", json.dumps(sentry))
        otel = self.parse("otel", {"resourceSpans": [{"scopeSpans": [{"spans": [
            {"traceId": "trace", "spanId": "span", "name": "parse", "attributes": [
                {"key": "code.filepath", "value": {"stringValue": "app.py"}},
                {"key": "authorization", "value": {"stringValue": "private"}}]}]}]}]})
        self.assertEqual(otel["telemetry"][0]["path"], "app.py")
        self.assertNotIn("private", json.dumps(otel))

    def test_mutation_report_formats_have_explicit_scores(self):
        reports = [
            self.parse("mutmut", {"mutants": [{"status": "killed"}, {"status": "survived"}]}),
            self.parse("stryker", {"files": {"app.js": {"mutants": [{"status": "Killed"}, {"status": "Survived"}]}}}),
            self.parse("pit", '<mutations><mutation status="KILLED"/><mutation status="SURVIVED"/></mutations>'),
        ]
        self.assertTrue(all(report["mutation"]["score"] == 0.5 for report in reports))

    def test_uncovered_and_timed_out_imported_mutants_cannot_inflate_a_score(self):
        values = {"files": {"app.js": {"mutants": [{"status":"Killed"}, {"status":"NoCoverage"}]}}}
        result = self.parse("stryker", values)["mutation"]
        self.assertEqual(result["score"], 0.5)
        self.assertEqual(result["survived"], 1)
        values["files"]["app.js"]["mutants"].append({"status":"Timeout"})
        result = self.parse("stryker", values)["mutation"]
        self.assertIsNone(result["score"])
        self.assertEqual(result["status"], "inconclusive")

    def test_entity_expansion_large_reports_and_outside_paths_are_rejected(self):
        for format_name, raw in (("junit", b'<!DOCTYPE x [<!ENTITY e "x">]><x/>'), ("junit", b"a" * 4_000_001)):
            with self.subTest(format=format_name):
                with self.assertRaises(ValueError):
                    parse_report(raw, format_name, self.root)
        with self.assertRaisesRegex(ValueError, "within the project"):
            read_report(self.root, "../outside.json", "coverage")
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.parse("jest", {"testResults": [None]})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.parse("unknown", {})

    def test_bridge_records_report_as_unverified_evidence(self):
        project = self.root / "project"
        project.mkdir()
        (project / "report.json").write_text('{"files":{"app.py":{"executed_lines":[1]}}}')
        bridge = OctaneBridge(str(self.root / "pil.sqlite3"), [str(project)], str(self.root / "workspaces"))
        registered = bridge.dispatch({"operation": "project_create", "params": {"name": "import", "root": str(project)}})
        result = bridge.dispatch({"operation": "evidence_import", "params": {
            "projectId": registered["id"], "reportPath": "report.json", "reportFormat": "coverage"}})
        self.assertEqual(result["verification_status"], "unverified")
        recorded = bridge.intelligence.retrieve_evidence(result["id"])
        self.assertEqual(recorded["payload"]["coverage"]["app.py"]["covered"], [1])
