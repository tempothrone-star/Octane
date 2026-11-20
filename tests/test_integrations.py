"""Integration boundaries and real offline provenance signatures."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from octane.bisect import _first_bad
from octane.db import ProjectIntelligence
from octane.github import GitHubIntegration
from octane.process import run_bounded
from octane.provenance import sign_statement, verify_envelope
from octane.property_checks import property_command
from octane.source_archive import export_revision, resolve_revision


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.pil = ProjectIntelligence(self.base / "pil.sqlite3")
        self.project = self.pil.create_project("integrations", self.root)["id"]
        self.actor = self.pil.register_agent(self.project, "integrator", "observer", [], ["write_evidence"])["id"]
        self.github = GitHubIntegration(self.pil, self.project, self.actor)

    def test_bisect_completion_supports_old_and_new_git_output(self):
        for sha in ("a" * 40, "b" * 64):
            for term in ("bad", "'bad'"):
                output = "Bisecting: 0 revisions left\n" + sha + " is the first " + term + " commit\ncommit details\n"
                self.assertEqual(_first_bad(output), sha)
        self.assertIsNone(_first_bad("subject: the branch is the first bad commit"))
        self.assertIsNone(_first_bad("abc is the first 'bad' commit\n"))

    def test_github_issue_and_workflow_ingest_and_publication_approval(self):
        issue = {"number": 3, "title": "Zero fails", "body": "Expected zero", "html_url": "https://github.com/o/r/issues/3"}
        with patch.object(self.github, "_run", return_value=json.dumps(issue).encode()):
            evidence = self.github.issue("owner/repo", 3)
        recorded = self.pil.retrieve_evidence(evidence["id"])
        self.assertEqual(recorded["payload"]["issue"]["number"], 3)
        with patch.object(self.github, "_run", side_effect=[b'{"head_sha":"abc","conclusion":"failure"}', b"AssertionError"]):
            run = self.github.workflow("owner/repo", 123)
        self.assertEqual(self.pil.retrieve_evidence(run["id"])["payload"]["log"], "AssertionError")
        with self.assertRaisesRegex(PermissionError, "explicit --publish"):
            self.github.publish("owner/repo", [evidence["id"]], pull_request=4)
        seen = []

        def publish(arguments):
            seen.append(arguments)
            if "--body-file" in arguments:
                body = Path(arguments[-1]).read_text()
                self.assertIn(evidence["id"], body)
            return b"https://github.com/owner/repo/pull/4#comment"

        with patch.object(self.github, "_run", side_effect=publish):
            self.assertTrue(self.github.publish("owner/repo", [evidence["id"]], pull_request=4, approved=True)["published"])
            self.assertTrue(self.github.publish("owner/repo", [evidence["id"]], head_sha="a" * 40, approved=True)["published"])
        self.assertIn("--body-file", seen[0])
        self.assertIn("--input", seen[1])
        with self.assertRaisesRegex(ValueError, "owner/name"):
            self.github.issue("../outside", 1)

    def test_host_executable_output_and_timeout_are_bounded(self):
        result = run_bounded([sys.executable, "-c", "print('hello')"])
        self.assertEqual(result.stdout, ("hello" + os.linesep).encode())
        with self.assertRaisesRegex(ValueError, "output budget"):
            run_bounded([sys.executable, "-c", "print('x'*100)"], limit=10)
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, "-c", "import time; time.sleep(3)"], timeout=0.01)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_finished_host_parent_cannot_leave_a_pipe_holding_descendant(self):
        started = time.monotonic()
        command = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('done')"
        result = run_bounded([sys.executable, "-c", command], timeout=2)
        self.assertEqual(result.stdout, b"done\n")
        self.assertLess(time.monotonic() - started, 3)

    def test_git_archive_exports_a_revision_and_filters_credentials(self):
        def git(*args):
            return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                                            cwd=self.root)
        git("init", "-q")
        (self.root / "app.py").write_text("VALUE=1\n")
        (self.root / "private.PEM").write_text("secret fixture")
        git("add", ".")
        git("commit", "-qm", "fixture")
        sha = resolve_revision(self.root, "HEAD")
        exported = export_revision(self.root, sha, self.base / "export")
        self.assertTrue((exported / "app.py").is_file())
        self.assertFalse((exported / "private.PEM").exists())
        self.assertFalse((exported / ".git").exists())

    def test_property_commands_are_explicit_and_confined_to_project_functions(self):
        (self.root / "app.py").write_text("def normalize(value): return value\n")
        command = property_command(self.root, {"module": "app", "function": "normalize", "rule": "idempotent"})
        self.assertEqual(command[:3], ["python", "-B", "-c"])
        self.assertIn("derandomize=True", command[3])
        (self.root / "app.js").write_text("exports.normalize = x => x;\n")
        node = property_command(self.root, {"runtime": "node", "file": "app.js", "function": "normalize",
                                             "rule": "differential", "reference": "normalize"})
        self.assertEqual(node[:2], ["node", "-e"])
        for spec in ({"module": "os", "function": "system", "rule": "idempotent"},
                     {"module": "app", "function": "normalize", "rule": "roundtrip"},
                     {"module": "app", "function": "normalize", "rule": "idempotent", "examples": 0}):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    property_command(self.root, spec)

    def test_real_dsse_signature_verification_detects_tampering_and_wrong_keys(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("install the provenance extra for cryptographic signing")
        private = Ed25519PrivateKey.generate()
        secret = self.base / "test-key.pem"
        secret.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()))
        public = self.base / "public.pem"
        public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        digest = hashlib.sha256(b"patch").hexdigest()
        value = {"_type": "https://in-toto.io/Statement/v1", "subject": [{"name": "patch.diff", "digest": {"sha256": digest}}],
                 "predicateType": "https://example.invalid/octane/v1", "predicate": {"candidate": "sha"}}
        envelope = sign_statement(value, secret)
        self.assertTrue(verify_envelope(envelope, public, digest)["signature_verified"])
        with self.assertRaisesRegex(ValueError, "expected patch"):
            verify_envelope(envelope, public, "b" * 64)
        damaged = {**envelope, "payload": base64.b64encode(b'{"changed":true}').decode()}
        with self.assertRaisesRegex(ValueError, "no valid signature"):
            verify_envelope(damaged, public)
        other = Ed25519PrivateKey.generate()
        public.write_bytes(other.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        with self.assertRaisesRegex(ValueError, "no valid signature"):
            verify_envelope(envelope, public)
