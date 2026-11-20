"""Portable credential and pathological-input regressions for both Octane stores."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

from octane.db_support import _scrub_sensitive_text
from octane.ledger.core import _redact
from octane.ledger.source import parse_stack_frames


class RedactionRegressionTests(unittest.TestCase):
    def test_both_stores_remove_json_quoted_values_and_common_tokens(self):
        secrets = ['quoted value with spaces', 'double escaped " quote', 'plain-password',
                   'aGVsbG86d29ybGQ=', 'xoxb-' + '123456789012-' + 'abcdefghijklmnop', 'AIza' + 'a' * 35]
        value = '\n'.join([json.dumps({'password': secrets[0], 'api_key': secrets[1]}),
                          "TOKEN='quoted value with spaces'", 'password=plain-password',
                          'Authorization: Basic ' + secrets[3], secrets[4], secrets[5]])
        for scrub in (_scrub_sensitive_text, lambda value: _redact(value, [0])):
            result = scrub(value)
            self.assertIn('REDACTED', result)
            for secret in secrets:
                self.assertNotIn(secret, result)
            self.assertNotIn('value with spaces', result)
            self.assertNotIn('escaped', result)
        self.assertEqual(_scrub_sensitive_text('regular_output=42 count: 7'), 'regular_output=42 count: 7')
        self.assertNotIn('space tail', _scrub_sensitive_text('token="space tail'))

    def test_megabyte_blob_cannot_hang_either_scrubber(self):
        # An external timeout ensures a regression fails instead of hanging CI.
        code = '''from octane.db_support import _scrub_sensitive_text
from octane.ledger.core import _redact
for text in ('a'*1000000, 'a'*1000000+'=', 'x'*1000000+': 1'):
    assert _scrub_sensitive_text(text) == text
    assert _redact(text,[0]) == text
'''
        started = time.monotonic()
        subprocess.run([sys.executable, '-c', code], timeout=8, check=True)
        self.assertLess(time.monotonic() - started, 8)

    def test_stack_parser_rejects_pathological_lines_and_preserves_js_symbols(self):
        for text in ('at ' * 20000, 'a' * 4096, ('a' * 1000 + '\n') * 100):
            started = time.monotonic()
            self.assertEqual(parse_stack_frames(text, Path.cwd()), [])
            self.assertLess(time.monotonic() - started, 2)
        rows = parse_stack_frames('at handle (src/app.js:12:4)\n    at src/app.js:13:5', Path.cwd())
        self.assertEqual(rows[0]['symbol'], 'handle')
        self.assertEqual(rows[0]['format'], 'javascript')
        self.assertEqual(rows[1]['format'], 'javascript')
        self.assertIsNone(rows[1]['symbol'])
