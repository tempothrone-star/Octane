"""Executable seeded acceptance benchmark, run in the existing Docker CI job."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from octane.db import ProjectIntelligence
from octane.holdout import validate_holdout
from octane.localization import select_tests, spectrum_localize, trace_diff
from octane.workspace import WorkspaceManager
from tests.accuracy_cases import CASES, DRIVER

IMAGE = os.environ.get('OCTANE_TEST_DOCKER_IMAGE')


@unittest.skipUnless(IMAGE, 'Docker benchmark needs OCTANE_TEST_DOCKER_IMAGE')
class AccuracyBenchmarkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        self.pil = ProjectIntelligence(self.base / 'pil.db')
        self.project = self.pil.create_project('accuracy', self.root)['id']
        self.manager = WorkspaceManager(self.pil, self.base / 'workspaces')
        self.workspaces = []
        self.addCleanup(self.cleanup_workspaces)

    def cleanup_workspaces(self):
        for workspace in self.workspaces:
            self.manager.destroy_workspace(workspace['id'])

    def workspace(self):
        workspace = self.manager.create_workspace(self.project, 'experimentation')
        self.workspaces.append(workspace)
        return workspace

    def test_seeded_line_localization_and_clean_controls(self):
        for case in CASES:
            (self.root / (case['id'] + '.py')).write_text(case['source'])
        (self.root / 'cases.json').write_text(json.dumps(CASES))
        (self.root / 'driver.py').write_text(DRIVER)
        baseline = self.workspace()
        run = self.manager.run_experiment(baseline['id'], ['python', '-B', 'driver.py'], IMAGE, report_format='spectrum')
        self.assertEqual(run['returncode'], 1, run)
        self.assertIsNone(run['report_error'])
        outcomes = []
        for case in CASES:
            rows = [r for r in run['report']['test_spectra'] if r['id'].startswith(case['id'] + ':')]
            started = time.perf_counter()
            ranked = spectrum_localize(rows)['suspects']
            elapsed = time.perf_counter() - started
            rank = next((n for n, row in enumerate(ranked, 1) if row['path'] == case['id'] + '.py' and row['line'] == 3), None)
            outcomes.append({'case': case['id'], 'rank': rank, 'analysis_seconds': elapsed})
            self.assertIsNotNone(rank)
            self.assertLessEqual(rank, 5)
            fixed = case['source'].splitlines()
            fixed[2] = '        ' + case['fixed']
            self.manager.apply_candidate_patch(baseline['id'], case['id'] + '.py', '\n'.join(fixed) + '\n')
        clean = self.manager.run_experiment(baseline['id'], ['python', '-B', 'driver.py'], run['image_id'], report_format='spectrum')
        self.assertEqual(clean['returncode'], 0, clean)
        negatives = 0
        for case in CASES:
            rows = [r for r in clean['report']['test_spectra'] if r['id'].startswith(case['id'] + ':')]
            negatives += bool(spectrum_localize(rows)['suspects'])
        metrics = {'benchmark': 'eight isolated seeded Python bugs and eight repaired controls',
                   'top1': sum(r['rank'] == 1 for r in outcomes) / len(outcomes),
                   'top5': sum(r['rank'] <= 5 for r in outcomes) / len(outcomes),
                   'negative_control_ranking_rate': negatives / len(CASES), 'outcomes': outcomes,
                   'limits': 'Not an external dataset or a general false-positive/fix-accuracy estimate'}
        self.assertEqual(negatives, 0)
        self.assertGreaterEqual(sum(row['rank'] == 1 for row in outcomes), 6)
        print('OCTANE_ACCURACY ' + json.dumps(metrics, sort_keys=True))

    def test_visible_test_passes_but_holdout_rejects_overfitting_then_accepts_real_fix(self):
        (self.root / 'calc.py').write_text('def magnitude(n):\n    if n < 0:\n        return -n + 1\n    return n\n')
        (self.root / 'visible.py').write_text('from calc import magnitude\nactual = magnitude(-1)\nassert actual == 1\n')
        original = self.workspace()
        failing = self.manager.run_experiment(original['id'], ['python', '-B', 'visible.py'], IMAGE, python_trace=True)
        self.assertEqual(failing['returncode'], 1)
        candidate = self.workspace()
        self.manager.apply_candidate_patch(candidate['id'], 'calc.py', 'def magnitude(n):\n    if n < 0:\n        return 1\n    return n\n')
        visible = self.manager.run_experiment(candidate['id'], ['python', '-B', 'visible.py'], failing['image_id'], python_trace=True)
        self.assertEqual(visible['returncode'], 0)
        difference = trace_diff(visible['python_trace'], failing['python_trace'])
        first = difference['first_divergence']
        self.assertEqual(first['passing']['function'], 'magnitude')
        self.assertEqual(first['passing']['values']['<return>'], 1)
        self.assertEqual(first['failing']['values']['<return>'], 2)
        suite = self.base / 'oracle'
        suite.mkdir()
        (suite / 'check.py').write_text('import sys\nsys.path.insert(0, "/workspace")\nfrom calc import magnitude\nprint("ORACLE-PRIVATE-8491")\nassert magnitude(-2) == 2\n')
        policy = self.base / 'policy.json'
        policy.write_text(json.dumps({'projectRoot': str(self.root), 'suiteRoot': str(suite),
                                     'command': ['python', '-B', '/octane-holdout/check.py', 'PRIVATE-INPUT-ARG-523']}))
        with self.assertRaisesRegex(ValueError, 'holdout rejected'):
            validate_holdout(self.manager, candidate['id'], visible['run_id'], policy, [self.root, self.manager.storage_root])
        self.manager.apply_candidate_patch(candidate['id'], 'calc.py', 'def magnitude(n):\n    if n < 0:\n        return -n\n    return n\n')
        fixed = self.manager.run_experiment(candidate['id'], ['python', '-B', 'visible.py'], failing['image_id'], python_trace=True)
        self.assertTrue(validate_holdout(self.manager, candidate['id'], fixed['run_id'], policy,
                                        [self.root, self.manager.storage_root])['passed'])
        with self.pil._session() as db:
            stored = '\n'.join(row[0] for row in db.execute('SELECT payload_json FROM evidence'))
            stored += '\n'.join(row[0] for row in db.execute('SELECT result_json FROM execution_runs'))
        self.assertNotIn('ORACLE-PRIVATE-8491', stored)
        self.assertNotIn('assert magnitude(-2)', stored)
        self.assertNotIn('PRIVATE-INPUT-ARG-523', stored)
        print('OCTANE_HOLDOUT ' + json.dumps({'visible_passing_candidates': 2, 'holdout_accepted': 1, 'overfit_rejected': 1}))

    def test_test_ordering_reduces_measured_time_to_failure(self):
        rows = [{'id': f'pass-{n:02d}', 'status': 'passed', 'coverage': {'work.py': [4]}} for n in range(60)]
        rows += [{'id': 'fail', 'status': 'failed', 'coverage': {'work.py': [3]}}]
        chosen = select_tests(rows, {'work.py': [3]})
        self.assertEqual(len(chosen['first']) + len(chosen['remainder']), 61)
        (self.root / 'order.json').write_text(json.dumps({'full': [r['id'] for r in rows], 'selected': chosen['first'] + chosen['remainder']}))
        (self.root / 'latency.py').write_text('''import json, time
from pathlib import Path
orders = json.loads(Path("order.json").read_text())
results = {}
for name, order in orders.items():
    start = time.perf_counter()
    for test in order:
        time.sleep(.003)
        if test == "fail":
            break
    results[name] = time.perf_counter() - start
print(json.dumps(results))
assert results["selected"] < results["full"] / 2
''')
        run = self.manager.run_experiment(self.workspace()['id'], ['python', '-B', 'latency.py'], IMAGE)
        self.assertEqual(run['returncode'], 0, run)
        print('OCTANE_FAILURE_LATENCY ' + run['stdout'].strip())

    def test_recorded_optimization_gate_reports_container_variance(self):
        from octane.performance import compare_recorded
        (self.root / 'calc.py').write_text('def total(n):\n    value = 0\n    for i in range(n):\n        value += i\n    return value\n')
        (self.root / 'timing.py').write_text('from calc import total\nassert total(15000000) == 112499992500000\nprint("checked")\n')
        (self.root / 'preserve.py').write_text('from calc import total\nassert [total(n) for n in (-1, 0, 1, 10)] == [0, 0, 0, 45]\nprint("preserved")\n')
        original, candidate = self.workspace(), self.workspace()
        self.manager.apply_candidate_patch(candidate['id'], 'calc.py', 'def total(n):\n    return n * (n - 1) // 2 if n > 0 else 0\n')
        before_check = self.manager.run_experiment(original['id'], ['python', '-B', 'preserve.py'], IMAGE)
        image = before_check['image_id']
        after_check = self.manager.run_experiment(candidate['id'], ['python', '-B', 'preserve.py'], image)
        before, after = [], []
        for index in range(8):
            # Alternate order to reduce systematic warmup/load bias; no reuse of a run ID.
            order = [(original, before), (candidate, after)]
            if index % 2:
                order.reverse()
            for workspace, target in order:
                run = self.manager.run_experiment(workspace['id'], ['python', '-B', 'timing.py'], image)
                self.assertEqual(run['returncode'], 0, run)
                target.append(run['run_id'])
        actor = self.pil.get_or_register_agent(self.project, 'benchmark', 'observer', [], ['write_evidence'])['id']
        result = compare_recorded(self.manager, self.project, before, after,
                                  [[before_check['run_id'], after_check['run_id']]], actor)
        self.assertTrue(result['preservation_passed'])
        self.assertEqual(result['sample_count'], 8)
        # Busy shared hosts may legitimately make the statistical gate decline.
        print('OCTANE_PERFORMANCE ' + json.dumps({key: result[key] for key in
              ('accepted', 'median_reduction', 'median_reduction_interval', 'before_mean', 'after_mean',
               'before_stdev', 'after_stdev', 'sample_count')}, sort_keys=True))
