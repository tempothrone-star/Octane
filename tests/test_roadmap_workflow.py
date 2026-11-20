"""Container-backed verification of roadmap actions and their shared boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from octane.evaluation import _command, apply_patch
from octane.mutation import mutation_candidates
from octane.openclaw_bridge import OctaneBridge
from tests.test_native_workflow import BUGGY, CHECKS, FIXED


class RoadmapWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        (self.root / 'pipeline.py').write_text(BUGGY)
        (self.root / 'test_pipeline.py').write_text(CHECKS)
        self.image = os.environ.get('OCTANE_TEST_DOCKER_IMAGE', 'python:3.12-slim')
        self.property_image = os.environ.get('OCTANE_TEST_PROPERTY_IMAGE', '')
        self.bridge = OctaneBridge(str(self.base / 'pil.sqlite3'), [str(self.root)], str(self.base / 'workspaces'),
                                  [self.image, self.property_image, os.environ.get('OCTANE_TEST_NODE_PROPERTY_IMAGE', '')])
        self.project = self.call('project_create', root=str(self.root), name='roadmap')['id']
        self.call('scan', projectId=self.project)

    def call(self, operation, **params):
        return self.bridge.dispatch({'operation': operation, 'params': params, 'context': {'agentId': 'roadmap'}})

    def workspace(self):
        return self.call('workspace_create', projectId=self.project, mode='experimentation')

    def execute(self, workspace, command, **params):
        return self.call('workspace_run', workspaceId=workspace['id'], command=command, image=self.image,
                         mode='experimentation', **params)

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'user.name=Octane tests', '-c',
            'user.email=test@example.invalid', *args], cwd=self.root).decode().strip()

    def test_mutation_limit_preserves_all_candidate_edits(self):
        (self.root / 'second.py').write_text('VALUE = 1\n')
        self.call('scan', projectId=self.project)
        workspace = self.workspace()
        self.call('workspace_patch', workspaceId=workspace['id'], relativePath='pipeline.py', content=FIXED)
        self.call('workspace_patch', workspaceId=workspace['id'], relativePath='second.py', content='VALUE = 2\n')
        _, _, patches, mutants = mutation_candidates(self.bridge._workspace_manager(), workspace['id'], 1)
        self.assertEqual(set(patches), {'pipeline.py', 'second.py'})
        self.assertEqual(len(mutants), 1)
        self.assertEqual(mutants[0]['operator'], 'original-source')

    def test_evaluation_rejects_invalid_patches_and_requires_both_checks(self):
        with self.assertRaisesRegex(ValueError, 'commands'):
            _command({}, 'test_command', 'FAIL_TO_PASS')
        self.assertEqual(_command({'FAIL_TO_PASS': '["test_pipeline.py"]'}, 'test_command', 'FAIL_TO_PASS'),
                         ['python', '-B', '-m', 'pytest', '-q', 'test_pipeline.py'])
        for value in ('GIT binary patch', 'new file mode 120000', 'invalid patch'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    apply_patch(self.root, value)
        (self.root / 'empty.jsonl').write_text('')
        with self.assertRaisesRegex(ValueError, 'at least one'):
            self.call('workspace_evaluate', projectId=self.project, datasetPath='empty.jsonl', image=self.image)
        with self.assertRaisesRegex(ValueError, 'within'):
            self.call('workspace_evaluate', projectId=self.project, datasetPath='../escape', image=self.image)

    @unittest.skipUnless(os.environ.get('OCTANE_TEST_DOCKER_IMAGE'), 'set OCTANE_TEST_DOCKER_IMAGE')
    def test_real_mutation_reversion_is_killed_by_original_reproduction(self):
        investigation = self.call('investigation_create', projectId=self.project, objective='Preserve numeric contract')['id']
        workspace = self.workspace()
        command = ['python', '-B', '-m', 'unittest', 'test_pipeline.Focused', '-q']
        original = self.call('workspace_create', projectId=self.project, mode='observation')
        baseline = self.call('workspace_run', workspaceId=original['id'], command=command, image=self.image,
                             mode='observation', investigationId=investigation)
        self.assertEqual(baseline['returncode'], 1)
        preservation = ['python', '-B', '-m', 'unittest', 'test_pipeline.Regression', '-q']
        self.call('workspace_run', workspaceId=original['id'], command=preservation, image=self.image,
                  mode='observation', investigationId=investigation)
        self.call('workspace_patch', workspaceId=workspace['id'], relativePath='pipeline.py', content=FIXED)
        candidate = self.execute(workspace, command, investigationId=investigation)
        self.assertEqual(candidate['returncode'], 0)
        result = self.call('workspace_mutate', workspaceId=workspace['id'], candidateRunId=candidate['run_id'], mutationLimit=3)
        self.assertTrue(result['original_bug_detected'])
        self.assertEqual(result['mutants'][0]['status'], 'killed')
        self.assertTrue(0 <= result['score'] <= 1)
        regression = self.execute(workspace, preservation, investigationId=investigation)
        intent = self.call('evidence_add', projectId=self.project, evidenceType='documented_behavior',
                           payload={'contract': 'Zero is numeric; None preserves the missing-input sentinel'},
                           description='The existing numeric and sentinel tests define the contract')['id']
        proposal = self.call('workspace_propose', projectId=self.project, workspaceId=workspace['id'],
                            investigationId=investigation, baselineRunId=baseline['run_id'], candidateRunId=candidate['run_id'],
                            regressionRunIds=[regression['run_id']], rationale='Preserve numeric zero without changing the sentinel',
                            evidenceIds=[intent, result['evidence_id']])
        self.assertEqual(proposal['mutation_results'][0]['evidence_id'], result['evidence_id'])
        self.assertEqual((self.root / 'pipeline.py').read_text(), BUGGY)

    @unittest.skipUnless(os.environ.get('OCTANE_TEST_DOCKER_IMAGE'), 'set OCTANE_TEST_DOCKER_IMAGE')
    def test_container_captures_structured_report(self):
        workspace = self.workspace()
        script = 'import os; from pathlib import Path; Path(os.environ["OCTANE_REPORT_PATH"]).write_text(\'<testsuite><testcase name="original"/></testsuite>\')'
        result = self.execute(workspace, ['python', '-B', '-c', script], reportFormat='junit')
        self.assertEqual(result['returncode'], 0)
        self.assertIsNone(result['report_error'])
        self.assertEqual(result['report']['tests'][0]['status'], 'passed')
        invalid = self.execute(workspace, ['python', '-B', '-c', 'pass'], reportFormat='junit')
        self.assertTrue(invalid['report_error'])

    @unittest.skipUnless(os.environ.get('OCTANE_TEST_DOCKER_IMAGE'), 'set OCTANE_TEST_DOCKER_IMAGE')
    def test_real_bisect_and_evaluation_preserve_original_checkout(self):
        self.git('init', '-q')
        (self.root / 'pipeline.py').write_text(FIXED)
        self.git('add', '.')
        self.git('commit', '-qm', 'working')
        good = self.git('rev-parse', 'HEAD')
        (self.root / 'notes.txt').write_text('unrelated change\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'unrelated')
        (self.root / 'pipeline.py').write_text(BUGGY)
        self.git('add', '.')
        self.git('commit', '-qm', 'regression')
        first_bad = self.git('rev-parse', 'HEAD')
        (self.root / 'notes.txt').write_text('another unrelated change\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'later')
        bad = self.git('rev-parse', 'HEAD')
        self.call('scan', projectId=self.project)
        focused = ['python', '-B', '-m', 'unittest', 'test_pipeline.Focused', '-q']
        result = self.call('workspace_bisect', projectId=self.project, goodRevision=good, badRevision=bad,
                           command=focused, image=self.image, bisectBudget=10)
        self.assertTrue(result['completed'])
        self.assertEqual(result['first_bad'], first_bad)
        patch = self.git('diff', bad, good, '--', 'pipeline.py') + '\n'
        case = {'instance_id': 'numeric-regression', 'base_commit': bad, 'patch': patch,
                'test_command': focused, 'preservation_command': ['python', '-B', '-m', 'unittest', 'test_pipeline.Regression', '-q'],
                'expected_files': ['pipeline.py']}
        (self.root / 'cases.jsonl').write_text(json.dumps(case) + '\n')
        result = self.call('workspace_evaluate', projectId=self.project, datasetPath='cases.jsonl', image=self.image)
        self.assertEqual(result['resolved'], 1)
        self.assertTrue(0 <= result['localization_top1'] <= 1)
        self.assertEqual(self.git('rev-parse', 'HEAD'), bad)
        self.assertEqual((self.root / 'pipeline.py').read_text(), BUGGY)

    @unittest.skipUnless(os.environ.get('OCTANE_TEST_PROPERTY_IMAGE'), 'set OCTANE_TEST_PROPERTY_IMAGE with Hypothesis')
    def test_real_hypothesis_differential_check_detects_bug_and_passes_fix(self):
        workspace = self.workspace()
        specification = {'module': 'pipeline', 'function': 'calculate', 'reference': 'reference',
                         'rule': 'differential', 'strategy': 'integers', 'examples': 50}
        # Put an independently defined numeric contract in the same project module.
        reference = '\ndef reference(value): return value * 2\n'
        self.call('workspace_patch', workspaceId=workspace['id'], relativePath='pipeline.py', content=BUGGY + reference)
        params = dict(workspaceId=workspace['id'], propertySpec=specification, image=self.property_image, mode='experimentation')
        failed = self.call('workspace_property', **params)
        self.assertEqual(failed['returncode'], 1, failed['stderr'])
        self.call('workspace_patch', workspaceId=workspace['id'], relativePath='pipeline.py', content=FIXED + reference)
        passed = self.call('workspace_property', **params)
        self.assertEqual(passed['returncode'], 0, passed['stderr'])
        self.assertTrue(passed['property_evidence_id'])

    @unittest.skipUnless(os.environ.get('OCTANE_TEST_NODE_PROPERTY_IMAGE'), 'set OCTANE_TEST_NODE_PROPERTY_IMAGE with fast-check')
    def test_real_fast_check_differential_probe(self):
        source = "exports.actual = value => value || -1; exports.expected = value => value;\n"
        (self.root / 'app.cjs').write_text(source)
        self.call('scan', projectId=self.project)
        workspace = self.workspace()
        params = dict(workspaceId=workspace['id'], mode='experimentation', image=os.environ['OCTANE_TEST_NODE_PROPERTY_IMAGE'],
                      propertySpec={'runtime': 'node', 'file': 'app.cjs', 'function': 'actual', 'reference': 'expected',
                                    'rule': 'differential', 'examples': 100})
        failed = self.call('workspace_property', **params)
        self.assertEqual(failed['returncode'], 1, failed['stderr'])
        self.call('workspace_patch', workspaceId=workspace['id'], relativePath='app.cjs',
                  content=source.replace('value || -1', 'value'))
        passed = self.call('workspace_property', **params)
        self.assertEqual(passed['returncode'], 0, passed['stderr'])
