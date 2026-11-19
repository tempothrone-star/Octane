"""Regression coverage for ledger retries, malformed state, and source triage."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from octane.ledger.core import Ledger, OctaneError, Project, init_project
from octane.ledger.source import LANGUAGES, LANGUAGE_IMPORT_SYNTAX, recent_project_changes, register_language, source_inventory


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        init_project(self.root)
        self.project = Project(self.root)

    def invoke(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/octane.py"), *args],
                                cwd=self.root, capture_output=True, text=True, timeout=20,
                                env={**os.environ, "OCTANE_AGENT": "owner"})
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_payload_only_search_matches_with_and_without_fts(self):
        evidence = self.project.add_evidence("AG-owner", "source_observation", "ordinary description",
                                             {"nested": {"detail": "rareword endpoint"}})
        for use_fts in (self.project.ledger.fts5, False):
            self.project.ledger.fts5 = use_fts
            with self.subTest(fts=use_fts):
                self.assertEqual([row["evidence_id"] for row in self.project.search_evidence("rareword endpoint")],
                                 [evidence["evidence_id"]])
                self.assertEqual(self.project.search_evidence("rare"), [])

    def test_explicit_status_retry_returns_original_without_reapplying(self):
        project = self.project
        claim = project.add_claim("AG-owner", "hypothesis", "retry fixture")["claim_id"]
        first = project.transition_claim("AG-owner", claim, "stale", "first-transition")
        project.transition_claim("AG-owner", claim, "needs_review", "second-transition")
        self.assertEqual(project.transition_claim("AG-owner", claim, "stale", "first-transition"), first)
        self.assertEqual(project.get_claim(claim)["status"], "needs_review")
        with self.assertRaises(OctaneError) as caught:
            project.transition_claim("AG-owner", claim, "needs_review", "first-transition")
        self.assertEqual(caught.exception.kind, "idempotency_conflict")
        self.assertTrue(project.ledger.rebuild_check(project.project_id)["matches"])

    def test_explicit_idempotency_key_cannot_be_reused_by_another_agent(self):
        project = self.project
        other = project.register_agent("other", "explorer", "standard", "AG-owner")["agent_id"]
        project.add_evidence("AG-owner", "source_observation", "same content", {}, explicit_key="shared")
        with self.assertRaises(OctaneError) as caught:
            project.add_evidence(other, "source_observation", "same content", {}, explicit_key="shared")
        self.assertEqual(caught.exception.kind, "idempotency_conflict")

    def test_non_object_metadata_and_anchors_return_structured_errors(self):
        for filename, malformed in (("project.json", []), ("config.json", []),
                                    ("config.json", {"schema": 1, "default_trust_tier": [], "execution_allowlist": []})):
            with self.subTest(filename=filename, malformed=malformed):
                path = self.root / ".octane" / filename
                original = path.read_text()
                try:
                    path.write_text(json.dumps(malformed))
                    code, response = self.invoke("status")
                    self.assertNotEqual(code, 0, response)
                    self.assertFalse(response["ok"])
                    code, doctor = self.invoke("doctor")
                    self.assertEqual(code, 0, doctor)
                    self.assertTrue(doctor["warnings"])
                finally:
                    path.write_text(original)
        path = self.root / ".octane/project.json"
        metadata = json.loads(path.read_text())
        metadata["log_anchor"] = []
        path.write_text(json.dumps(metadata))
        code, response = self.invoke("verify-log")
        self.assertEqual(code, 1, response)
        self.assertEqual(response["error"], "integrity_error")

    def test_compound_extensions_use_longest_registered_suffix(self):
        with patch.dict(LANGUAGES), patch.dict(LANGUAGE_IMPORT_SYNTAX):
            register_language("custom_template", [".view.html"])
            for name in ("page.view.html", "upper.VIEW.HTML", "plain.html"):
                (self.root / name).write_text("fixture")
            files = {item["path"]: item["language"] for item in source_inventory(self.root)["files"]}
            self.assertEqual(files, {"page.view.html": "custom_template", "upper.VIEW.HTML": "custom_template",
                                     "plain.html": "html"})

    def test_recent_changes_preserve_unusual_git_filenames(self):
        def git(*args):
            return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                                  cwd=self.root, capture_output=True, check=True)
        git("init", "-q")
        names = ["café.py", "line\nbreak.py", "quote\"name.py", " leading.py"]
        for name in names:
            (self.root / name).write_text("value = 1\n")
        git("add", "--", *names)
        git("commit", "-qm", "fixture")
        changed, warning = recent_project_changes(self.root)
        self.assertIsNone(warning)
        self.assertEqual(set(changed) & set(names), set(names))
        for name in names:
            (self.root / name).write_text("value = 2\n")
        changed, warning = recent_project_changes(self.root, limit=0)
        self.assertIsNone(warning)
        self.assertEqual(set(changed) & set(names), set(names))

    def test_log_verification_accepts_a_writer_advancing_the_anchor(self):
        read_text = Path.read_text
        appended = False

        def read_after_write(path, *args, **kwargs):
            nonlocal appended
            if path == self.root / ".octane/project.json" and not appended:
                appended = True
                self.project.add_evidence("AG-owner", "source_observation", "concurrent write", {})
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_after_write):
            result = self.project.ledger.verify_log(self.project.project_id)
        self.assertTrue(appended)
        self.assertTrue(result["valid"], result)

    def test_rebuild_uses_one_snapshot_while_another_writer_commits(self):
        materialized = Ledger.materialized

        def read_after_write(conn):
            self.project.add_evidence("AG-owner", "source_observation", "concurrent write", {})
            return materialized(conn)

        with patch.object(Ledger, "materialized", side_effect=read_after_write):
            result = self.project.ledger.rebuild_check(self.project.project_id)
        self.assertTrue(result["matches"], result)
        self.assertEqual(len(self.project.search_evidence()), 1)


if __name__ == "__main__":
    unittest.main()
