"""Final-validation isolation, gate integration, candidate ranking and timing provenance."""
from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_patch_gate
from octane.holdout import _policy, _suite_hash, validate_holdout
from octane.performance import compare_recorded
from octane.diagnostics import diagnose
from octane.patch_ranking import masking_notes
from octane.workspace import _withhold_private_output, _private_mount


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.gate = test_patch_gate.PatchGateTests()
        self.gate.setUp()
        self.addCleanup(self.gate.doCleanups)
        self.base = self.gate.root.parent
        self.suite = self.base / 'oracle'
        self.suite.mkdir()
        (self.suite / 'check.py').write_text('assert True\n')
        self.policy = self.base / 'policy.json'
        self.policy.write_text(json.dumps({'projectRoot': str(self.gate.root), 'suiteRoot': str(self.suite),
                                           'command': ['python', '-B', '/octane-holdout/check.py']}))
        self.forbidden = [self.gate.root, self.gate.manager.storage_root]

    def output(self, **fields):
        hashed = self.gate.manager.compare_workspace(self.gate.candidate['id'])['candidate_hash']
        return {'returncode': 0, 'timed_out': False, 'cleanup_warning': None, 'run_id': 'hidden-run',
                'before_hash': hashed, 'after_hash': hashed, 'evidence_id': self.gate.intent, **fields}

    def test_oracle_digest_covers_hidden_data_and_rejects_unsupported_entries(self):
        data = self.suite / '.oracle-data'
        data.write_text('first')
        before = _suite_hash(self.suite)
        data.write_text('second')
        self.assertNotEqual(_suite_hash(self.suite), before)
        link = self.suite / 'link'
        link.symlink_to(data)
        with self.assertRaisesRegex(ValueError, 'regular files'):
            _suite_hash(self.suite)
        link.unlink()
        data.write_bytes(b'x' * 2000001)
        with self.assertRaisesRegex(ValueError, '2 MB'):
            _suite_hash(self.suite)
        with patch.object(Path, 'rglob', return_value=iter([data] * 1001)):
            with self.assertRaisesRegex(ValueError, '1000'):
                _suite_hash(self.suite)
            # New iterator is needed because the first hash deliberately consumes it.
        with patch.object(Path, 'rglob', return_value=iter([data] * 1001)):
            data.write_text('small')
            with self.assertRaisesRegex(ValueError, '1000'):
                _policy(self.policy, self.gate.root, self.forbidden)

    def test_private_output_is_removed_before_recording(self):
        output = {'stdout': 'ORACLE-SECRET', 'stderr': 'sensitive failing input', 'command': ['python', '-c', 'PRIVATE INLINE ORACLE']}
        _withhold_private_output(output)
        self.assertNotIn('ORACLE-SECRET', json.dumps(output))
        self.assertNotIn('sensitive', json.dumps(output))
        self.assertNotIn('PRIVATE INLINE ORACLE', json.dumps(output))
        self.assertTrue(output['private_validation'])
        self.assertEqual(len(output['output_sha256']), 64)
        self.assertEqual(_private_mount(None), [])
        self.assertIn('readonly', _private_mount(self.suite)[1])
        output = {'stdout': 'visible', 'stderr': ''}
        _withhold_private_output(output, False)
        self.assertEqual(output['stdout'], 'visible')

    def test_holdout_binds_candidate_image_policy_and_suppresses_oracle(self):
        gate = self.gate
        with patch.object(gate.manager, '_docker_run', return_value=self.output()) as run:
            result = validate_holdout(gate.manager, gate.candidate['id'], gate.fixed, self.policy, self.forbidden)
        self.assertTrue(result['passed'])
        self.assertEqual(run.call_args.args[2], 'sha256:' + 'a' * 64)
        self.assertFalse(run.call_args.kwargs['writable'])
        self.assertIn('private_suite', run.call_args.kwargs)
        for changes in ({'returncode': 1}, {'timed_out': True}, {'cleanup_warning': 'failed'}, {'after_hash': 'changed'}):
            with patch.object(gate.manager, '_docker_run', return_value=self.output(**changes)):
                with self.assertRaisesRegex(ValueError, 'holdout rejected'):
                    validate_holdout(gate.manager, gate.candidate['id'], gate.fixed, self.policy, self.forbidden)
        (Path(gate.candidate['path']) / 'source.py').write_text('VALUE = 3\n')
        with self.assertRaisesRegex(ValueError, 'unchanged current candidate'):
            validate_holdout(gate.manager, gate.candidate['id'], gate.fixed, self.policy, self.forbidden)

    def test_operator_policy_cannot_be_in_the_agent_checkout_or_overlap(self):
        for path, forbidden in ((self.policy, [self.base]), (self.policy, [self.suite]), (self.policy, [self.base / 'oracle/nested'])):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                _policy(path, self.gate.root, forbidden)
        data = json.loads(self.policy.read_text())
        for edit in ({'projectRoot': str(self.base)}, {'command': []}, {'command': ['']}, {'suiteRoot': str(self.base / 'absent')}):
            self.policy.write_text(json.dumps({**data, **edit}))
            with self.assertRaises((ValueError, FileNotFoundError)):
                _policy(self.policy, self.gate.root, self.forbidden)
        self.policy.write_text(json.dumps(data))
        (self.suite / 'link').symlink_to(self.policy)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            _policy(self.policy, self.gate.root, self.forbidden)
        (self.suite / 'link').unlink()
        (self.suite / 'large').write_bytes(b'x' * 2000001)
        with self.assertRaisesRegex(ValueError, '2 MB'):
            _policy(self.policy, self.gate.root, self.forbidden)
        self.policy.write_bytes(b'x' * 64001)
        with self.assertRaisesRegex(ValueError, '64 KB'):
            _policy(self.policy, self.gate.root, self.forbidden)

    def test_bridge_requires_configured_holdout_before_proposing(self):
        gate = self.gate
        gate.bridge.holdout_policy = str(self.policy)
        params = dict(workspaceId=gate.candidate['id'], investigationId=gate.investigation,
                      baselineRunId=gate.baseline, candidateRunId=gate.fixed, regressionRunIds=[gate.regression],
                      evidenceIds=[gate.intent], rationale='Preserve behavior')
        with patch.object(gate.bridge, '_workspace_manager', return_value=gate.manager):
            with patch.object(gate.manager, '_docker_run', return_value=self.output(returncode=1)):
                with self.assertRaisesRegex(ValueError, 'holdout rejected'):
                    gate.call('workspace_propose', **params)
            with gate.pil._session() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM patch_proposals').fetchone()[0], 0)
            with patch.object(gate.manager, '_docker_run', return_value=self.output()):
                self.assertTrue(gate.call('workspace_propose', **params)['holdout']['passed'])

    def test_ranking_only_accepts_current_gate_qualified_proposals(self):
        gate = self.gate
        proposal = gate.propose()
        actor = gate.bridge._actor(gate.project, 'diagnose')
        result = diagnose(gate.pil, gate.project, actor, 'rank-patches', [proposal['evidence_id']])
        self.assertEqual(result['result']['candidates'][0]['changed_lines'], 2)
        notes = masking_notes('+except Exception:\n+    return 1\n+pytest.skip("input")\n')
        self.assertEqual(len(notes), 3)
        with self.assertRaises(ValueError):
            diagnose(gate.pil, gate.project, actor, 'rank-patches', [])
        (Path(gate.candidate['path']) / 'source.py').write_text('VALUE = 4\n')
        with self.assertRaisesRegex(ValueError, 'candidate bytes'):
            diagnose(gate.pil, gate.project, actor, 'rank-patches', [proposal['evidence_id']])

    def test_benchmark_uses_recorded_runs_and_rejects_unpaired_or_mutated_inputs(self):
        gate = self.gate
        before, after = [], []
        for _ in range(8):
            a = gate.record(gate.original, ['python', 'benchmark.py'], 'passed')
            b = gate.record(gate.candidate, ['python', 'benchmark.py'], 'passed')
            gate.result(a, duration_seconds=10)
            gate.result(b, duration_seconds=8)
            before.append(a)
            after.append(b)
        result = compare_recorded(gate.manager, gate.project, before, after, [[gate.preserved, gate.regression]], gate.actor)
        self.assertTrue(result['accepted'])
        self.assertEqual(len(result['input_evidence_ids']), 18)
        gate.result(after[0], stdout='changed output')
        self.assertFalse(compare_recorded(gate.manager, gate.project, before, after,
                                         [[gate.preserved, gate.regression]], gate.actor)['accepted'])
        gate.edit_run(after[0], image_id='another-image')
        with self.assertRaisesRegex(ValueError, 'immutable image'):
            compare_recorded(gate.manager, gate.project, before, after, [[gate.preserved, gate.regression]], gate.actor)
        with self.assertRaisesRegex(ValueError, 'distinct paired'):
            compare_recorded(gate.manager, gate.project, before, before, [], gate.actor)
        with self.assertRaisesRegex(ValueError, 'engine-recorded'):
            compare_recorded(gate.manager, gate.project, ['missing'] + before[1:], after,
                             [[gate.preserved, gate.regression]], gate.actor)
        gate.edit_run(after[0], status='failed')
        with self.assertRaisesRegex(ValueError, 'pass unchanged'):
            compare_recorded(gate.manager, gate.project, before, after, [[gate.preserved, gate.regression]], gate.actor)
