"""Regression coverage for the unified package and pre-rename ledgers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from octane.analysis import analyzer
from octane.ledger import polygraph, source
from octane.ledger.core import OctaneError, Project, init_project


ROOT = Path(__file__).resolve().parents[1]


class OctaneIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()

    def test_native_and_cli_use_the_same_analyzer_modules(self):
        self.assertEqual(analyzer(), (source, polygraph))

    def test_existing_ledger_preserves_records_hashes_and_identity(self):
        init_project(self.root)
        original = Project(self.root)
        evidence = original.add_evidence("AG-owner", "source_observation", "existing record", {})
        claim = original.add_claim("AG-owner", "hypothesis", "existing explanation",
                                   supporting=[evidence["evidence_id"]])
        integrity = original.ledger.verify_log(original.project_id)
        revision = original.revision()
        original.state.rename(self.root / ".octance")
        legacy = self.root / ".octance"
        (legacy / "ledger.db").rename(legacy / "octance.db")
        (self.root / ".octane").mkdir()
        (self.root / ".octane/pil.sqlite3").touch()

        result = init_project(self.root)
        reopened = Project(self.root)
        self.assertFalse(result["created"])
        self.assertEqual(result["database"], str(legacy / "octance.db"))
        self.assertEqual(reopened.project_id, original.project_id)
        self.assertEqual(reopened.revision(), revision)
        self.assertEqual(reopened.get_claim(claim["claim_id"])["text"], "existing explanation")
        self.assertEqual(reopened.ledger.verify_log(reopened.project_id), integrity)
        self.assertTrue(reopened.ledger.rebuild_check(reopened.project_id)["matches"])
        init_project(self.root, update_gitignore=True)
        self.assertIn(".octance/", (self.root / ".gitignore").read_text())
        self.assertFalse((self.root / ".octane/ledger.db").exists())
        code = subprocess.run([sys.executable, str(ROOT / "scripts/octane.py"), "status"],
                              cwd=self.root, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(code.stdout)["data"]["project_id"], original.project_id)

    def test_two_ledger_locations_report_a_conflict(self):
        init_project(self.root)
        (self.root / ".octance").mkdir()
        for operation in (lambda: init_project(self.root), lambda: Project(self.root)):
            with self.assertRaises(OctaneError) as caught:
                operation()
            self.assertEqual(caught.exception.kind, "project_conflict")

    def test_agent_environment_alias_and_precedence(self):
        init_project(self.root)
        project = Project(self.root)
        other = project.register_agent("reviewer", "explorer", "standard", "AG-owner")["agent_id"]
        with patch.dict(os.environ, {"OCTANCE_AGENT": "reviewer"}):
            os.environ.pop("OCTANE_AGENT", None)
            self.assertEqual(project.agent_id(), other)
            os.environ["OCTANE_AGENT"] = "owner"
            self.assertEqual(project.agent_id(), "AG-owner")
            self.assertEqual(project.agent_id("reviewer"), other)

    def test_copied_skill_and_legacy_script_share_the_new_ledger(self):
        installed = self.base / "installed"
        installed.mkdir()
        for directory in ("scripts", "octane", "references"):
            shutil.copytree(ROOT / directory, installed / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(ROOT / "SKILL.md", installed / "SKILL.md")
        env = {**os.environ, "PYTHONPATH": "", "OCTANE_AGENT": "owner", "PYTHONDONTWRITEBYTECODE": "1"}
        init = subprocess.run([sys.executable, str(installed / "scripts/octane.py"), "init"],
                              cwd=self.root, env=env, capture_output=True, text=True, check=True)
        created = json.loads(init.stdout)["data"]
        alias = subprocess.run([sys.executable, str(installed / "scripts/octance.py"), "status"],
                               cwd=self.root, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(alias.stdout)["data"]["project_id"], created["project_id"])
        self.assertTrue((self.root / ".octane/ledger.db").is_file())
        self.assertFalse((self.root / ".octance").exists())
