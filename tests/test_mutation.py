"""Runner-independent mutation outcomes and supported JS/TS syntax operators."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from octane.db_support import _id
from octane.mutation import _python_mutants, run_mutations
from octane.mutation_support import javascript_mutants, mutation_support
from tests import test_patch_gate as gate_fixture


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.gate = gate_fixture.PatchGateTests('test_accepts_paired_checks_and_records_review_evidence')
        self.gate.setUp()
        self.addCleanup(self.gate.doCleanups)

    def run_with(self, code, timeout=False, limit=1):
        gate = self.gate
        def execute(*args, **kwargs):
            payload = {'returncode': code, 'timed_out': timeout, 'cleanup_warning': None}
            evidence = gate.pil.submit_evidence(gate.project, 'runtime_observation', payload,
                                               'Canned mutant execution', gate.actor, gate.original['base_revision'])
            return {**payload, 'run_id': _id('RUN'), 'evidence_id': evidence['id']}
        with patch.object(gate.manager, 'run_experiment', side_effect=execute):
            return run_mutations(gate.manager, gate.candidate['id'], gate.fixed, gate.actor, {'image:test'}, limit)

    def test_killed_survived_and_timed_out_are_distinct(self):
        for code, timeout, expected, score, status in ((1, False, 'killed', 1.0, 'completed'),
                                                      (0, False, 'survived', 0.0, 'completed'),
                                                      (None, True, 'inconclusive', None, 'inconclusive'),
                                                      (127, False, 'inconclusive', None, 'inconclusive')):
            with self.subTest(code=code, timeout=timeout):
                result = self.run_with(code, timeout)
                self.assertEqual(result['mutants'][0]['status'], expected)
                self.assertEqual(result['score'], score)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['original_bug_detected'], expected == 'killed')

    def test_unsupported_go_never_runs_or_produces_a_passing_score(self):
        gate = self.gate
        gate.fixture_sources = {'app.go': 'package main\nfunc value() int { return 1 }\n'}
        gate.setUp()
        gate.manager.apply_candidate_patch(gate.candidate['id'], 'source.py', 'VALUE = 1\n')
        gate.manager.apply_candidate_patch(gate.candidate['id'], 'app.go', 'package main\nfunc value() int { return 2 }\n')
        gate.fixed = gate.record(gate.candidate, gate.focus, 'passed')
        gate.regression = gate.record(gate.candidate, gate.preserve, 'passed')
        with patch.object(gate.manager, 'run_experiment') as runner:
            result = run_mutations(gate.manager, gate.candidate['id'], gate.fixed, gate.actor, {'image:test'})
        runner.assert_not_called()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['score'])
        self.assertFalse(result['original_bug_detected'])
        self.assertEqual(result['mutants'], [])
        proposal = gate.propose(supporting_evidence_ids=[gate.intent, result['evidence_id']])
        self.assertEqual(proposal['mutation_results'][0]['status'], 'unavailable')
        self.assertIsNone(proposal['mutation_results'][0]['score'])

    def test_invalid_candidate_limit_image_original_tests_and_revision_are_rejected(self):
        gate = self.gate
        for limit in (0, 21, True):
            with self.assertRaisesRegex(ValueError, 'mutationLimit'):
                self.run_with(1, limit=limit)
        with self.assertRaisesRegex(ValueError, 'passing run'):
            run_mutations(gate.manager, gate.candidate['id'], 'missing', gate.actor, {'image:test'})
        with self.assertRaisesRegex(PermissionError, 'allowedDockerImages'):
            run_mutations(gate.manager, gate.candidate['id'], gate.fixed, gate.actor, set())
        gate.result(gate.fixed, test_integrity={})
        with self.assertRaisesRegex(ValueError, 'test protection'):
            self.run_with(1)

    def test_python_and_js_ts_operators_target_touched_lines(self):
        self.assertEqual(list(_python_mutants('not valid python {', {1})), [])
        mutants = list(_python_mutants('def f(x):\n    return x == 0\n', {2}))
        self.assertTrue(mutants)
        self.assertEqual(list(_python_mutants('VALUE = 1\n', {5})), [])
        from octane.ledger.ast_backend import supported
        for name, text in (('app.js','function f(x) { return x === 0 ? 1 : 2; }\n'),
                           ('app.ts','function f(x: number): number { return x === 0 ? 1 : 2; }\n')):
            language = 'typescript' if name.endswith('.ts') else 'javascript'
            if not supported(language, name):
                continue
            generated = list(javascript_mutants(text, name, {1}))
            self.assertTrue(any('!==' in source for _,source in generated))
            self.assertTrue(all(source != text for _,source in generated))
            self.assertEqual(list(javascript_mutants(text, name, {5})), [])
        self.assertEqual(mutation_support([{'path':'a.go'}])['status'], 'unavailable')
        self.assertEqual(mutation_support([{'path':'a.py'}, {'path':'a.go'}])['status'], 'partial')
