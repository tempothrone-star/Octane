from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "octance" / "scripts"))
from octance.core import Project  # noqa: E402
ENTRY = ROOT / "scripts" / "octance.py"


class OctancePhaseOneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def invoke(self, project: Path, *args: str, env=None):
        result = subprocess.run(
            [sys.executable, str(ENTRY), *args], cwd=str(project), text=True,
            capture_output=True, env={**os.environ, **(env or {})}, timeout=20,
        )
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            self.fail("CLI did not return JSON: stdout=%r stderr=%r" % (result.stdout, result.stderr))
        self.assertTrue({"ok", "data", "warnings", "next"} <= set(payload))
        return result.returncode, payload

    def init(self, name="project", *args):
        root = self.base / name
        root.mkdir()
        code, result = self.invoke(root, "init", "--project-root", str(root), "--format", "compact", *args)
        self.assertEqual(code, 0, result)
        self.assertTrue((root / ".octance" / "octance.db").is_file())
        self.assertTrue((root / ".octance" / "project.json").is_file())
        self.assertTrue((root / ".octance" / "config.json").is_file())
        return root, result["data"]["project_id"]

    def test_init_project_identity_owner_and_opt_in_gitignore_edit(self):
        root, project_id = self.init()
        self.assertEqual(json.loads((root / ".octance/project.json").read_text())["project_id"], project_id)
        self.assertFalse((root / ".gitignore").exists())
        code, result = self.invoke(root, "agent", "whoami")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["agent_id"], "AG-owner")
        second, second_id = self.init("approved", "--update-gitignore")
        self.assertNotEqual(project_id, second_id)
        self.assertIn(".octance/", (second / ".gitignore").read_text())

    def test_project_json_mismatch_and_cross_project_evidence_are_refused(self):
        first, first_id = self.init("first")
        second, second_id = self.init("second")
        code, ev = self.invoke(first, "evidence", "add", "--type", "source_observation", "--description", "found input")
        self.assertEqual(code, 0, ev)
        code, response = self.invoke(second, "claim", "add", "--kind", "hypothesis", "--text", "possible issue", "--evidence", "EV-0001")
        self.assertEqual(code, 2)
        self.assertFalse(response["ok"])
        meta_path = first / ".octance/project.json"
        metadata = json.loads(meta_path.read_text())
        metadata["project_id"] = second_id
        meta_path.write_text(json.dumps(metadata))
        code, response = self.invoke(first, "status")
        self.assertNotEqual(code, 0)
        self.assertEqual(response["error"], "project_mismatch")
        self.assertNotEqual(first_id, second_id)

    def test_hash_chain_rejects_edit_deletion_and_reorder(self):
        for operation in ("edit", "delete", "reorder"):
            with self.subTest(operation=operation):
                root, _ = self.init(operation)
                code, _ = self.invoke(root, "evidence", "add", "--type", "file_hash", "--description", "snapshot")
                self.assertEqual(code, 0)
                db = sqlite3.connect(root / ".octance/octance.db")
                db.execute("DROP TRIGGER event_log_no_update")
                db.execute("DROP TRIGGER event_log_no_delete")
                if operation == "edit":
                    db.execute("UPDATE event_log SET payload_json='{}' WHERE seq=3")
                elif operation == "delete":
                    db.execute("DELETE FROM event_log WHERE seq=3")
                else:
                    db.execute("UPDATE event_log SET seq=99 WHERE seq=2")
                db.commit()
                db.close()
                code, result = self.invoke(root, "verify-log")
                self.assertNotEqual(code, 0)
                self.assertEqual(result["error"], "integrity_error")

    def test_idempotency_immutability_and_rebuild_match(self):
        root, _ = self.init()
        args = ("evidence", "add", "--type", "source_observation", "--description", "caller found", "--payload", '{"line":4}')
        code, first = self.invoke(root, *args)
        self.assertEqual(code, 0, first)
        code, retry = self.invoke(root, *args)
        self.assertEqual(code, 0, retry)
        self.assertEqual(first["data"]["evidence_id"], retry["data"]["evidence_id"])
        self.assertEqual(first["data"]["created_at"], retry["data"]["created_at"])
        code, claim = self.invoke(root, "claim", "add", "--kind", "hypothesis", "--text", "caller may normalize", "--evidence", "EV-0001")
        self.assertEqual(code, 0, claim)
        code, result = self.invoke(root, "rebuild", "--check")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["matches"], result)
        db = sqlite3.connect(root / ".octance/octance.db")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE evidence SET description='rewritten' WHERE evidence_id='EV-0001'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM evidence WHERE evidence_id='EV-0001'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE claims SET status='retracted' WHERE claim_id='CL-0001'")
        db.rollback()
        db.close()

    def test_indexed_file_symbol_search_and_multiword_fallback(self):
        root, _ = self.init()
        code, ev = self.invoke(root, "evidence", "add", "--type", "source_observation", "--description", "unrelated note",
                               "--file", "src/parser.py", "--symbol", "parse_widget")
        self.assertEqual(code, 0, ev)
        code, result = self.invoke(root, "evidence", "search", "--query", "parser")
        self.assertEqual(code, 0, result)
        self.assertEqual([row["evidence_id"] for row in result["data"]], [ev["data"]["evidence_id"]])
        code, result = self.invoke(root, "evidence", "search", "--query", "parse widget")
        self.assertEqual(code, 0, result)
        self.assertEqual(len(result["data"]), 1)
        project = Project(root)
        project.ledger.fts5 = False
        self.assertEqual(len(project.search_evidence("parser widget")), 1)
        self.assertEqual(project.search_evidence("missing widget"), [])

    def test_secret_patterns_and_line_range_validation(self):
        root, _ = self.init()
        code, result = self.invoke(root, "evidence", "add", "--type", "source_observation", "--description",
                                   "Authorization: Bearer supersecret123 and https://alice:password@example.test/a access_token=access123 client_secret=secret123 db_password=dbpass123",
                                   "--payload", '{"my_secret_value":"hidden","note":"token=abc123"}')
        self.assertEqual(code, 0, result)
        database = (root / ".octance/octance.db").read_bytes()
        for secret in (b"supersecret123", b"alice:password", b"access123", b"secret123", b"dbpass123", b"hidden", b"token=abc123"):
            self.assertNotIn(secret, database)
        bad, response = self.invoke(root, "evidence", "add", "--type", "source_observation", "--description", "range",
                                    "--line-range", "token=exposed")
        self.assertEqual(bad, 2, response)
        self.assertEqual(response["error"], "invalid_input")

    def test_doctor_runs_before_initialization_and_claim_links_transitions_are_replayable(self):
        root = self.base / "doctor-root"
        root.mkdir()
        code, result = self.invoke(root, "doctor")
        self.assertEqual(code, 0, result)
        self.assertIsNone(result["data"]["schema_version"])
        self.assertFalse((root / ".octance" / "octance.db").exists())
        root, _ = self.init("claim-root")
        code, self_evidence = self.invoke(root, "evidence", "add", "--type", "source_observation", "--description", "self link", "--related", "EV-0001")
        self.assertEqual(code, 2, self_evidence)
        _, evidence = self.invoke(root, "evidence", "add", "--type", "source_observation", "--description", "supports parser", "--file", "src/parser.py", "--symbol", "parse_widget")
        _, first = self.invoke(root, "claim", "add", "--kind", "hypothesis", "--text", "parser hypothesis", "--evidence", evidence["data"]["evidence_id"])
        _, second = self.invoke(root, "claim", "add", "--kind", "decision", "--text", "separate conclusion")
        code, link = self.invoke(root, "claim", "link", first["data"]["claim_id"], second["data"]["claim_id"], "--relation", "supports")
        self.assertEqual(code, 0, link)
        code, self_link = self.invoke(root, "claim", "link", first["data"]["claim_id"], first["data"]["claim_id"], "--relation", "supports")
        self.assertEqual(code, 2, self_link)
        code, updated = self.invoke(root, "claim", "status", first["data"]["claim_id"], "--status", "corroborated")
        self.assertEqual(code, 0, updated)
        code, found = self.invoke(root, "claim", "get", first["data"]["claim_id"])
        self.assertEqual(code, 0, found)
        self.assertEqual(found["data"]["status"], "corroborated")
        self.assertEqual(found["data"]["links"][0]["direction"], "outgoing")
        code, incoming = self.invoke(root, "claim", "get", second["data"]["claim_id"])
        self.assertEqual(code, 0, incoming)
        self.assertEqual(incoming["data"]["links"][0]["direction"], "incoming")
        code, brief = self.invoke(root, "brief", "--task", "nothing matches", "--files", "src/parser.py", "--budget-tokens", "1000")
        self.assertEqual(code, 0, brief)
        self.assertIn(first["data"]["claim_id"], [item["claim_id"] for item in brief["data"]["claims"]])
        code, result = self.invoke(root, "rebuild", "--check")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["matches"], result)
        code, result = self.invoke(root, "claim", "search", "--status", "asserted")
        self.assertEqual(code, 0, result)
        self.assertNotIn(first["data"]["claim_id"], [item["claim_id"] for item in result["data"]])

    def test_brief_uses_whole_terms_not_substrings(self):
        root, _ = self.init()
        _, claim = self.invoke(root, "claim", "add", "--kind", "hypothesis", "--text", "hypothesis without matching term")
        _, brief = self.invoke(root, "brief", "--task", "the")
        self.assertNotIn(claim["data"]["claim_id"], [item["claim_id"] for item in brief["data"]["claims"]])
        project = Project(root)
        project.ledger.fts5 = False
        self.assertEqual(project.search_claims("the"), [])

    def test_agents_permissions_low_trust_quarantine_and_verified_forbidden(self):
        root, _ = self.init()
        code, agent = self.invoke(root, "agent", "register", "--name", "quiet-explorer", "--role", "explorer", "--trust-tier", "low")
        self.assertEqual(code, 0, agent)
        code, result = self.invoke(root, "claim", "add", "--agent", "quiet-explorer", "--kind", "hypothesis", "--text", "possible bug")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["status"], "quarantined")
        code, brief = self.invoke(root, "brief", "--task", "possible bug")
        self.assertEqual(code, 0, brief)
        self.assertEqual(brief["data"]["claims"], [])
        code, denied = self.invoke(root, "agent", "register", "--agent", "quiet-explorer", "--name", "other", "--role", "investigator")
        self.assertEqual(code, 3)
        self.assertEqual(denied["error"], "permission_denied")
        code, unsupported = self.invoke(root, "claim", "update", "CL-0001", "--status", "verified")
        self.assertEqual(code, 2)
        self.assertFalse(unsupported["ok"])

    def test_secret_redaction_is_recorded_and_secrets_do_not_enter_database(self):
        root, _ = self.init()
        payload = json.dumps({"api_token": "sensitive-value", "notes": "password=hunter2", "fact": "ordinary"})
        code, result = self.invoke(root, "evidence", "add", "--type", "config_observation", "--description", "config checked", "--payload", payload)
        self.assertEqual(code, 0, result)
        self.assertGreaterEqual(result["data"]["redactions"], 2)
        db_bytes = (root / ".octance/octance.db").read_bytes()
        self.assertNotIn(b"sensitive-value", db_bytes)
        self.assertNotIn(b"hunter2", db_bytes)
        code, claim = self.invoke(root, "claim", "add", "--kind", "intent", "--text", "password=do-not-store")
        self.assertEqual(code, 0, claim)
        self.assertIn("[REDACTED]", claim["data"]["text"])

    def test_fts5_and_like_fallback_search_agree(self):
        root, _ = self.init()
        code, evidence = self.invoke(root, "evidence", "add", "--type", "source_observation", "--description", "caller parses token input")
        self.assertEqual(code, 0, evidence)
        code, claim = self.invoke(root, "claim", "add", "--kind", "observation", "--text", "caller parser handles token input", "--evidence", evidence["data"]["evidence_id"])
        self.assertEqual(code, 0, claim)
        project = Project(root)
        fts_ids = [item["evidence_id"] for item in project.search_evidence("caller token")]
        fts_claims = [item["claim_id"] for item in project.search_claims("caller token")]
        project.ledger.fts5 = False
        like_ids = [item["evidence_id"] for item in project.search_evidence("caller")]
        like_claims = [item["claim_id"] for item in project.search_claims("caller")]
        self.assertEqual(fts_ids, ["EV-0001"])
        self.assertEqual(fts_claims, ["CL-0001"])
        self.assertEqual(like_ids, ["EV-0001"])
        self.assertEqual(like_claims, ["CL-0001"])

    def test_migration_from_empty_and_newer_schema_refusal(self):
        root, _ = self.init()
        db = sqlite3.connect(root / ".octance/octance.db")
        self.assertEqual(db.execute("SELECT version FROM schema_version").fetchone()[0], 1)
        db.execute("UPDATE schema_version SET version=99")
        db.commit()
        db.close()
        code, result = self.invoke(root, "status")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "schema_too_new")
        code, doctor = self.invoke(root, "doctor")
        self.assertEqual(code, 0)
        self.assertEqual(doctor["data"]["schema_version"], 99)
        self.assertTrue(any("newer" in warning for warning in doctor["warnings"]))

    def test_doctor_reports_malformed_config_without_masking_it(self):
        root, _ = self.init()
        (root / ".octance/config.json").write_text("{broken", encoding="utf-8")
        code, doctor = self.invoke(root, "doctor")
        self.assertEqual(code, 0)
        self.assertFalse(doctor["data"]["config_valid"])
        self.assertTrue(any("config" in warning for warning in doctor["warnings"]))

    def test_concurrent_process_writers_and_failed_write_rollback(self):
        root, _ = self.init()
        processes = []
        for index in range(8):
            processes.append(subprocess.Popen(
                [sys.executable, str(ENTRY), "evidence", "add", "--type", "source_observation",
                 "--description", "concurrent observation %d" % index, "--format", "compact"],
                cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        outputs = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _stdout, _stderr, code in outputs), outputs)
        self.assertEqual(len({json.loads(stdout)["data"]["evidence_id"] for stdout, _stderr, _code in outputs}), 8)
        code, result = self.invoke(root, "evidence", "add", "--type", "source_observation", "--description", "bad link", "--related", "EV-9999")
        self.assertEqual(code, 2)
        connection = sqlite3.connect(root / ".octance/octance.db")
        self.assertEqual(connection.execute("SELECT count(*) FROM evidence").fetchone()[0], 8)
        self.assertEqual(connection.execute("SELECT count(*) FROM event_log").fetchone()[0], 10)
        connection.close()
        code, result = self.invoke(root, "rebuild", "--check")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["matches"])

    def test_brief_is_bounded_json_and_status_reports_integrity(self):
        root, _ = self.init()
        for n in range(3):
            code, evidence = self.invoke(root, "evidence", "add", "--type", "source_observation", "--description", "entry caller finding %s" % n)
            self.assertEqual(code, 0, evidence)
            code, claim = self.invoke(root, "claim", "add", "--kind", "observation", "--text", "entry caller established fact %s" % n, "--evidence", evidence["data"]["evidence_id"])
            self.assertEqual(code, 0, claim)
        code, brief = self.invoke(root, "brief", "--task", "entry caller", "--budget-tokens", "80", "--format", "compact")
        self.assertEqual(code, 0, brief)
        self.assertLessEqual(brief["data"]["estimated_tokens"], 80)
        self.assertEqual(brief["data"]["token_estimate"], "characters divided by four")
        code, status = self.invoke(root, "status")
        self.assertEqual(code, 0, status)
        self.assertTrue(status["data"]["log_integrity"]["valid"])
        code, doctor = self.invoke(root, "doctor")
        self.assertEqual(code, 0, doctor)
        self.assertIn("sqlite", doctor["data"])


if __name__ == "__main__":
    unittest.main()
