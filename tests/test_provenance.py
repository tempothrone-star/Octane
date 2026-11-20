"""Signatures and ledger rebuild verification detect independent record tampering."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from octane.provenance import _differences, sign_statement, statement, verify_envelope
from tests import test_patch_gate as gate_fixture


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest('install provenance extra')
        self.gate = gate_fixture.PatchGateTests('test_accepts_paired_checks_and_records_review_evidence')
        self.gate.setUp()
        self.addCleanup(self.gate.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        key = Ed25519PrivateKey.generate()
        self.private = self.base/'private.pem'
        self.public = self.base/'public.pem'
        self.private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.BestAvailableEncryption(b'fixture-password')))
        self.public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        proposal = self.gate.propose()
        self.value = statement(self.gate.pil, self.gate.project, proposal['evidence_id'])
        self.envelope = sign_statement(self.value, self.private, password=b'fixture-password')

    def verify(self):
        return verify_envelope(self.envelope, self.public, intelligence=self.gate.pil, rebuild=True)

    def test_signature_and_rebuilt_ledger_agree(self):
        result = self.verify()
        self.assertTrue(result['signature_verified'])
        self.assertTrue(result['ledger_verified'])
        self.assertIsNone(verify_envelope(self.envelope, self.public)['ledger_verified'])
        self.assertEqual(self.value['subject'][0]['digest']['sha256'],
                         hashlib.sha256(self.gate.propose()['patch'].encode()).hexdigest())
        with self.assertRaisesRegex(ValueError, 'existing PIL'):
            verify_envelope(self.envelope, self.public, rebuild=True)

    def test_mutated_execution_record_fails_rebuild_despite_valid_signature(self):
        self.gate.edit_run(self.gate.fixed, status='failed')
        self.assertTrue(verify_envelope(self.envelope, self.public)['signature_verified'])
        with self.assertRaisesRegex(ValueError, 'differs from the ledger.*execution_runs'):
            self.verify()

    def test_mutated_execution_result_and_missing_evidence_are_detected(self):
        self.gate.result(self.gate.fixed, stdout='tampered output')
        with self.assertRaisesRegex(ValueError, 'differs from the ledger'):
            self.verify()
        with self.gate.pil._transaction() as db:
            db.execute('UPDATE execution_runs SET evidence_id=NULL WHERE id=?', (self.gate.fixed,))
        with self.assertRaisesRegex(ValueError, 'evidence is missing'):
            self.verify()

    def test_modified_runtime_observation_payload_fails_ledger_rebuild(self):
        with self.gate.pil._transaction() as db:
            identifier = db.execute("SELECT evidence_id FROM execution_runs WHERE id=?", (self.gate.fixed,)).fetchone()[0]
            db.execute("UPDATE evidence SET payload_json=? WHERE id=?", ('{"modified":true}',identifier))
        with self.assertRaisesRegex(ValueError,"observation_sha256"):
            self.verify()

    def test_wrong_payload_type_signature_subject_and_key_types_fail(self):
        for changed, message in (({**self.envelope, 'payloadType':'wrong'}, 'payload type'),
                                  ({**self.envelope, 'signatures':[]}, 'no valid signature')):
            with self.assertRaisesRegex(ValueError, message):
                verify_envelope(changed, self.public)
        with self.assertRaisesRegex(ValueError, 'expected patch'):
            verify_envelope(self.envelope, self.public, '0'*64)
        malformed = sign_statement({'_type':'unexpected'}, self.private, password=b'fixture-password')
        with self.assertRaisesRegex(ValueError, 'not an in-toto'):
            verify_envelope(malformed, self.public)
        with self.assertRaises(ValueError):
            sign_statement(self.value, self.private, password=b'wrong')
        payload = json.loads(base64.b64decode(self.envelope['payload']))
        self.assertEqual(payload, self.value)
        self.assertEqual(_differences({'a':[1]}, {'a':[2]}), ['/a/0'])
        self.assertEqual(_differences({'a':[]}, {'a':[1]}), ['/a'])
        self.assertEqual(_differences(1, '1'), ['/'])

    def test_legacy_v1_rebuild_and_wrong_signing_key_types(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        predicate=self.value['predicate']
        legacy=statement(self.gate.pil,self.gate.project,predicate['proposal_evidence_id'],format_version=1)
        envelope=sign_statement(legacy,self.private,password=b'fixture-password')
        self.assertTrue(verify_envelope(envelope,self.public,intelligence=self.gate.pil,rebuild=True)['ledger_verified'])
        self.assertNotIn('result_sha256',legacy['predicate']['execution_runs'][0])
        with self.assertRaisesRegex(ValueError,'unsupported Octane'):
            statement(self.gate.pil,self.gate.project,predicate['proposal_evidence_id'],format_version=3)
        wrong=ec.generate_private_key(ec.SECP256R1())
        self.private.write_bytes(wrong.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        with self.assertRaisesRegex(ValueError,'Ed25519 key'):
            sign_statement(self.value,self.private)
        self.public.write_bytes(wrong.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
        with self.assertRaisesRegex(ValueError,'Ed25519 public'):
            verify_envelope(envelope,self.public)
