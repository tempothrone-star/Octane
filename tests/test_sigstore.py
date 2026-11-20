"""Cosign adapter enforces exact signer policy and refuses failed verification."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from octane.sigstore import verify_sigstore


class SigstoreTests(unittest.TestCase):
    def test_exact_identity_policy_and_frozen_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); statement=base/'statement.json'; bundle=base/'bundle.json'
            value={'_type':'https://in-toto.io/Statement/v1','subject':[]}
            statement.write_text(json.dumps(value)); bundle.write_text('{"verificationMaterial":{}}')
            def backend(arguments,**kwargs):
                self.assertEqual(Path(arguments[-1]).read_text(),json.dumps(value))
                self.assertEqual(arguments[arguments.index('--certificate-identity')+1],'https://example.invalid/workflow')
                self.assertNotIn('--insecure-ignore-tlog',arguments)
                self.assertNotIn('--certificate-identity-regexp',arguments)
                statement.write_text('{"changedAfterRead":true}')
                return subprocess.CompletedProcess(arguments,0,b'',b'Verified OK')
            with patch('octane.sigstore.shutil.which',return_value='/approved/cosign'):
                with patch('octane.sigstore.run_bounded',side_effect=backend):
                    result=verify_sigstore(statement,bundle,'https://example.invalid/workflow','https://issuer.invalid')
                self.assertEqual(result['statement'],value)
                self.assertTrue(result['signature_verified'])
                statement.write_text(json.dumps(value))
                with patch('octane.sigstore.run_bounded',return_value=subprocess.CompletedProcess([],1,b'',b'wrong signer')):
                    with self.assertRaisesRegex(ValueError,'verification failed'):
                        verify_sigstore(statement,bundle,'identity','issuer')
                with self.assertRaisesRegex(ValueError,'exact certificate'):
                    verify_sigstore(statement,bundle,'','issuer')
                statement.write_text('[]')
                with self.assertRaisesRegex(ValueError,'in-toto'):
                    verify_sigstore(statement,bundle,'identity','issuer')
            with patch('octane.sigstore.shutil.which',return_value=None):
                with self.assertRaisesRegex(RuntimeError,'Install Cosign'):
                    verify_sigstore(statement,bundle,'identity','issuer')
