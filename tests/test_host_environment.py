"""Credential-free host process environments and bounded diagnostics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from octane.host_environment import git_environment, language_server_environment
from octane.language_server import LanguageServer, semantic_calls
from octane.source_archive import git


class HostEnvironmentTests(unittest.TestCase):
    def test_language_server_receives_only_allowed_variables_and_bounded_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            script = "import os,sys; sys.stderr.write('x'*20000+'\\n'+__import__('json').dumps(dict(os.environ))); sys.stderr.flush()"
            with patch.dict(os.environ, {"SECRET_TOKEN": "planted-secret", "OCTANE_LS_CACHE": "cache"}):
                server = LanguageServer([sys.executable, '-c', script], Path(directory), environment_keys=['OCTANE_LS_CACHE'])
                server.process.wait(timeout=5)
                server.close()
            self.assertLessEqual(len(server.stderr_tail), 8192)
            environment = json.loads(server.diagnostics.rsplit('\n', 1)[-1])
            self.assertNotIn('SECRET_TOKEN', environment)
            self.assertEqual(environment['OCTANE_LS_CACHE'], 'cache')
            self.assertNotIn('planted-secret', server.diagnostics)

    def test_invalid_environment_allowlist_is_rejected(self):
        for keys in ('SECRET_TOKEN', ['bad=name'], ['A'] * 33, [1]):
            with self.subTest(keys=keys):
                with self.assertRaises(ValueError):
                    language_server_environment(keys)

    def test_provider_failure_includes_scrubbed_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = [sys.executable, '-c', "import sys; print('API_KEY=synthetic-secret',file=sys.stderr)"]
            _, _, status = semantic_calls(root, [{'path': 'app.py', 'language': 'python'}],
                                         {'app.py': b'def entry(): pass'}, 'r', [], {'python': {'command':command,'trustedProject':True}})
            self.assertIn('diagnostics', status['python'])
            self.assertNotIn('synthetic-secret', status['python']['diagnostics'])

    def test_git_transport_uses_minimal_environment_and_config_overrides(self):
        result = subprocess.CompletedProcess([], 0, b'ok', b'')
        with patch.dict(os.environ, {'SECRET_TOKEN': 'never-forward', 'GIT_CONFIG_COUNT': '1', 'GIT_SSH_COMMAND': 'unsafe'}):
            with patch('octane.source_archive._require_git_version'), patch('octane.source_archive.run_bounded', return_value=result) as runner:
                self.assertEqual(git(Path('/project'), 'rev-parse', 'HEAD'), b'ok')
            environment = runner.call_args.kwargs['env']
            for key in ('SECRET_TOKEN', 'GIT_CONFIG_COUNT', 'GIT_SSH_COMMAND', 'HOME'):
                self.assertNotIn(key, environment)
            self.assertEqual(environment['GIT_CONFIG_GLOBAL'], os.devnull)
            self.assertEqual(environment['GIT_CONFIG_NOSYSTEM'], '1')
            self.assertEqual(environment['GIT_TERMINAL_PROMPT'], '0')
            self.assertIn('core.fsmonitor=false', runner.call_args.args[0])
            self.assertIn('core.hooksPath=' + os.devnull, runner.call_args.args[0])
        self.assertEqual(git_environment()['LC_ALL'], 'C')

    def test_native_git_refuses_older_versions_that_ignore_config_isolation(self):
        from octane.source_archive import _require_git_version
        _require_git_version.cache_clear()
        self.addCleanup(_require_git_version.cache_clear)
        with patch('octane.source_archive.run_bounded', return_value=subprocess.CompletedProcess([],0,b'git version 2.31.1',b'')):
            with self.assertRaisesRegex(RuntimeError, 'Git 2.32'):
                _require_git_version()
