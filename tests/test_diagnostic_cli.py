"""Native CLI diagnosis and operator-policy boundaries."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from octane.diagnostic_cli import add_commands, run_command
from tests import test_accuracy_validation


class DiagnosticCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_accuracy_validation.ValidationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.gate = self.fixture.gate
        self.parser = argparse.ArgumentParser()
        add_commands(self.parser.add_subparsers(dest='command'))

    def args(self, command, *extra):
        common = [command, self.gate.project, '--database', str(self.gate.pil.database)]
        if command != 'diagnose':
            common += ['--workspace-root', str(self.gate.manager.storage_root)]
        return self.parser.parse_args([*common, *extra])

    def test_native_cli_imports_a_spectrum_and_records_diagnosis(self):
        report = {'test_spectra': [{'id': 'negative', 'status': 'failed', 'coverage': {'source.py': [2]}},
                                   {'id': 'positive', 'status': 'passed', 'coverage': {'source.py': [3]}}]}
        (self.gate.root / 'spectra.json').write_text(json.dumps(report))
        prefix = [sys.executable, '-m', 'octane', 'native']
        common = ['--database', str(self.gate.pil.database)]
        imported = subprocess.run([*prefix, 'import-report', self.gate.project, 'spectra.json', '--format', 'spectrum', *common],
                                   capture_output=True, text=True, check=True)
        identifier = json.loads(imported.stdout)['id']
        result = subprocess.run([*prefix, 'diagnose', self.gate.project, '--kind', 'localize', '--evidence', identifier, *common],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['result']['suspects'][0]['line'], 2)
        args = self.args('diagnose', '--kind', 'fuse')
        self.assertEqual(run_command(self.gate.pil, args)['result']['findings'], [])
        args.project_id = 'missing'
        with self.assertRaises(KeyError):
            run_command(self.gate.pil, args)

    def test_cli_holdout_keeps_policy_operator_selected_and_checks_project(self):
        args = self.args('holdout', self.gate.candidate['id'], self.gate.fixed, '--policy', str(self.fixture.policy))
        with patch('octane.holdout.validate_holdout', return_value={'passed': True}) as validator:
            self.assertTrue(run_command(self.gate.pil, args)['passed'])
            self.assertEqual(validator.call_args.args[3], self.fixture.policy)
        other = self.gate.pil.create_project('other', self.gate.root)
        args.project_id = other['id']
        with self.assertRaisesRegex(ValueError, 'selected project'):
            run_command(self.gate.pil, args)

    def test_cli_benchmark_rejects_non_recorded_timings(self):
        args = self.args('benchmark', '--before-runs', ','.join('a' + str(n) for n in range(8)),
                         '--after-runs', ','.join('b' + str(n) for n in range(8)), '--preservation-pairs-json', '[["x","y"]]')
        with self.assertRaisesRegex(ValueError, 'engine-recorded'):
            run_command(self.gate.pil, args)
