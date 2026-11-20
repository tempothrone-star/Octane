"""Cosign version, exact signer policy, frozen inputs, and trust-root handling."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from octane.sigstore import _require_cosign_version, verify_sigstore


class SigstoreTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name); self.statement=self.base/'statement.json'; self.bundle=self.base/'bundle.json'
        self.value={'_type':'https://in-toto.io/Statement/v1','subject':[]}
        self.statement.write_text(json.dumps(self.value));self.bundle.write_text('{"verificationMaterial":{}}')
        selected=patch('octane.sigstore.shutil.which',return_value='/approved/cosign')
        selected.start();self.addCleanup(selected.stop)

    def version(self,arguments):
        self.assertEqual(arguments[1:],['version','--json'])
        return subprocess.CompletedProcess(arguments,0,b'{"gitVersion":"v3.0.5"}',b'')

    def verify(self,**options):
        return verify_sigstore(self.statement,self.bundle,'https://example.invalid/workflow','https://issuer.invalid',**options)

    def test_exact_identity_policy_and_frozen_inputs(self):
        def backend(arguments,**kwargs):
            if arguments[1]=='version': return self.version(arguments)
            self.assertEqual(Path(arguments[-1]).read_text(),json.dumps(self.value))
            self.assertEqual(arguments[arguments.index('--certificate-identity')+1],'https://example.invalid/workflow')
            self.assertEqual(arguments[arguments.index('--certificate-oidc-issuer')+1],'https://issuer.invalid')
            self.assertNotIn('--insecure-ignore-tlog',arguments)
            self.assertNotIn('--certificate-identity-regexp',arguments)
            self.statement.write_text('{"changedAfterRead":true}')
            return subprocess.CompletedProcess(arguments,0,b'',b'Verified OK')
        with patch('octane.sigstore.run_bounded',side_effect=backend):
            result=self.verify()
        self.assertEqual(result['statement'],self.value)
        self.assertEqual(result['cosign_version'],'v3.0.5')
        self.assertTrue(result['signature_verified'])

    def test_wrong_identity_and_issuer_reject_the_bundle(self):
        # The controlled verifier peer represents a bundle's authenticated signer;
        # this tests transport/policy propagation rather than claiming live OIDC validation.
        for field in ('identity','issuer'):
            self.bundle.write_text(json.dumps({'identity':'https://example.invalid/workflow',
                                               'issuer':'https://issuer.invalid',field:'wrong-signer'}))
            def backend(arguments,**kwargs):
                if arguments[1]=='version': return self.version(arguments)
                bundle=json.loads(Path(arguments[arguments.index('--bundle')+1]).read_text())
                matched=(arguments[arguments.index('--certificate-identity')+1]==bundle['identity'] and
                         arguments[arguments.index('--certificate-oidc-issuer')+1]==bundle['issuer'])
                return subprocess.CompletedProcess(arguments,0 if matched else 1,b'',b'certificate identity or issuer mismatch')
            with self.subTest(field=field), patch('octane.sigstore.run_bounded',side_effect=backend):
                with self.assertRaisesRegex(ValueError,'verification failed'):
                    self.verify()

    def test_old_prerelease_unknown_and_failed_versions_are_rejected_before_verification(self):
        for value,code in [('v2.4.1',0),('v3.0.4',0),('v3.0.5-rc.1',0),('devel',0),('v3.0.5',1)]:
            result=subprocess.CompletedProcess([],code,json.dumps({'gitVersion':value}).encode(),b'')
            with self.subTest(value=value,code=code), patch('octane.sigstore.run_bounded',return_value=result) as run:
                with self.assertRaisesRegex(RuntimeError,'3.0.5'):
                    self.verify()
                self.assertEqual(run.call_count,1)
        for raw in (b'bad json',b'[]'):
            with patch('octane.sigstore.run_bounded',return_value=subprocess.CompletedProcess([],0,raw,b'')):
                with self.assertRaisesRegex(RuntimeError,'valid version'):
                    _require_cosign_version('/approved/cosign')

    def test_supported_newer_versions_are_accepted(self):
        for version in ('v3.0.5','3.1.0','v4.0.0','v3.0.5+build.1'):
            with patch('octane.sigstore.run_bounded',return_value=subprocess.CompletedProcess([],0,json.dumps({'gitVersion':version}).encode(),b'')):
                self.assertEqual(_require_cosign_version('/approved/cosign'),version)

    def test_explicit_trust_root_is_frozen_and_no_insecure_flags_are_used(self):
        trusted=self.base/'trusted.json';trusted.write_text('{"trusted":true}')
        def backend(arguments,**kwargs):
            if arguments[1]=='version': return self.version(arguments)
            self.assertEqual(Path(arguments[arguments.index('--trusted-root')+1]).read_text(),'{"trusted":true}')
            self.assertNotIn('--insecure-ignore-sct',arguments)
            return subprocess.CompletedProcess(arguments,0,b'',b'')
        with patch('octane.sigstore.run_bounded',side_effect=backend):
            self.assertTrue(self.verify(trusted_root=trusted)['signature_verified'])

    def test_invalid_identity_payload_missing_binary_and_oversized_input_are_refused(self):
        with self.assertRaisesRegex(ValueError,'exact certificate'):
            verify_sigstore(self.statement,self.bundle,'','issuer')
        with patch('octane.sigstore.shutil.which',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'Install Cosign'):
                self.verify()
        with patch('octane.sigstore.run_bounded',side_effect=lambda args,**kw:self.version(args)):
            self.statement.write_text('[]')
            with self.assertRaisesRegex(ValueError,'in-toto'):
                self.verify()
            self.statement.write_bytes(b'x'*4_000_001)
            with self.assertRaisesRegex(ValueError,'4 MB'):
                self.verify()
