"""Controlled diagnosis accuracy, invalid inputs, provenance and tool wiring."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from octane.bug_patterns import scan_patterns, _matches
from octane.diagnostics import diagnose
from octane.finding_fusion import fuse_findings
from octane.localization import normalize_spectrum, select_tests, source_path, spectrum_localize, trace_diff
from octane.openclaw_bridge import OctaneBridge
from octane.performance import compare_samples, median_interval
from octane.reports import parse_report
from octane.trace_events import validate_timeline


def spectra():
    return [{'id': 'negative', 'status': 'failed', 'coverage': {'calc.py': [1, 2, 3]}},
            {'id': 'positive', 'status': 'passed', 'coverage': {'calc.py': [1, 2, 4]}},
            {'id': 'zero', 'status': 'passed', 'coverage': {'calc.py': [1, 2, 4]}}]


def event(value=1, **fields):
    return {'path': 'calc.py', 'function': 'magnitude', 'line': 3, 'event': 'return', 'values': {'result': value}, **fields}


def ordered(*events, truncated=False):
    return {'timeline': list(events), 'timeline_truncated': truncated}


class LocalizationTests(unittest.TestCase):
    def test_ochiai_counts_ties_and_flaky_policy(self):
        rows = spectra()
        result = spectrum_localize(rows)
        self.assertTrue(result['available'])
        self.assertEqual(result['suspects'][0], {'path': 'calc.py', 'line': 3, 'score': 1.0,
                                                'failed_covering': 1, 'passed_covering': 0})
        self.assertAlmostEqual(result['suspects'][1]['score'], 1 / math.sqrt(3))
        rows += [{**rows[0], 'status': 'passed'}, {**rows[1], 'coverage': {'calc.py': [6]}}]
        result = spectrum_localize(rows)
        self.assertEqual(result['excluded_flaky'], ['negative'])
        self.assertEqual(result['excluded_unstable_coverage'], ['positive'])
        self.assertFalse(result['available'])
        self.assertEqual(result['suspects'], [])
        with self.assertRaises(ValueError):
            spectrum_localize(spectra(), True)

    def test_selection_preserves_the_full_suite_and_falls_back_when_uncertain(self):
        result = select_tests(spectra(), {'calc.py': [3]})
        self.assertEqual(result['first'], ['negative'])
        self.assertEqual(result['remainder'], ['positive', 'zero'])
        self.assertTrue(result['full_suite_required'])
        self.assertFalse(result['fallback_to_full'])
        self.assertTrue(select_tests(spectra(), {'new.py': [1]})['fallback_to_full'])
        self.assertTrue(select_tests(spectra(), {'calc.py': [99]})['fallback_to_full'])
        flaky = spectra() + [{**spectra()[0], 'status': 'passed'}]
        self.assertTrue(select_tests(flaky, {'calc.py': [3]})['fallback_to_full'])
        with self.assertRaises(ValueError):
            select_tests(spectra(), {'calc.py': [-1]})

    def test_spectrum_and_paths_are_bounded(self):
        for path in ('../escape', '/tmp/file', '', 'a\\b', '.', 'a\nfile'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                source_path(path)
        for data in ({}, {'test_spectra': []}, {'test_spectra': [spectra()[0]] * 2001},
                     {'test_spectra': [{**spectra()[0], 'id': ''}]},
                     {'test_spectra': [{**spectra()[0], 'status': 'unknown'}]},
                     {'test_spectra': [{**spectra()[0], 'coverage': {'calc.py': [True]}}]},
                     {'test_spectra': [{**spectra()[0], 'coverage': {'calc.py': [1] * 200001}}]}):
            with self.subTest(data=str(data)[:40]), self.assertRaises(ValueError):
                normalize_spectrum(data)
        rows = spectra() + [{'id': 'skip', 'status': 'skipped', 'coverage': {}}]
        self.assertEqual(spectrum_localize(rows)['failed_tests'], 1)

    def test_first_observed_divergence_reports_values_and_control(self):
        result = trace_diff(ordered(event(2)), ordered(event(3)))
        self.assertEqual(result['first_divergence']['kind'], 'values')
        self.assertEqual(result['first_divergence']['passing']['values'], {'result': 2})
        result = trace_diff(ordered(event(2)), ordered(event(2), event(4)))
        self.assertEqual(result['first_divergence']['index'], 1)
        self.assertEqual(result['first_divergence']['kind'], 'control_flow')
        self.assertEqual(trace_diff(ordered(event()), ordered(event(line=4)))['first_divergence']['kind'], 'control_flow')
        self.assertIsNone(trace_diff(ordered(event()), ordered(event(), truncated=True))['first_divergence'])
        self.assertFalse(trace_diff(ordered(event()), ordered(event(), truncated=True))['complete'])
        for data in ({}, ordered(), ordered(*([event()] * 513))):
            with self.assertRaises(ValueError):
                trace_diff(data, ordered(event()))

    def test_numeric_capture_omits_secrets_objects_and_invalid_namespace_keys(self):
        from types import SimpleNamespace
        from octane.trace_runner import _numeric_values, _safe_scalar
        class NoRepresentation:
            def __repr__(self):
                raise AssertionError('project repr must never run')
        frame = SimpleNamespace(f_locals={'password': 42, 'api_key': 1, 'text': 'private', 'object': NoRepresentation(),
                                         'number': 7, 'fraction': 1.5, 'empty': None, 'flag': True,
                                         'huge': 2 ** 100, 'nan': float('nan'), 'infinite': float('inf'),
                                         12: 42, 'bad\nname': 5, 'n' * 81: 1})
        self.assertEqual(_numeric_values(frame), {'number': 7, 'fraction': 1.5, 'empty': None, 'flag': True})
        self.assertLessEqual(len(_numeric_values(SimpleNamespace(f_locals={str(n): n for n in range(64)}))), 8)
        for value in (None, True, 1, 1.5):
            self.assertTrue(_safe_scalar(value))
        for value in ('text', NoRepresentation(), 2 ** 100, float('inf')):
            self.assertFalse(_safe_scalar(value))

    def test_trace_writer_respects_the_artifact_byte_budget(self):
        import io
        from octane.trace_runner import _write_trace
        values = {str(n) + 'x' * 79: n for n in range(8)}
        payload = {'timeline': [event(values=values, function='f' * 200, path='p' * 350)] * 512,
                   'timeline_truncated': False, 'calls': [], 'entries': [], 'exceptions': [], 'events': 1, 'truncated': False}
        stream = io.StringIO()
        _write_trace(stream, payload)
        self.assertLessEqual(len(stream.getvalue().encode()), 390000)
        result = json.loads(stream.getvalue())
        self.assertTrue(result['timeline_truncated'])
        validate_timeline(result)
        payload.update(timeline=[], calls=[{'large': 'x' * 10000}] * 150)
        stream = io.StringIO()
        _write_trace(stream, payload)
        self.assertLessEqual(len(stream.getvalue().encode()), 390000)

    def test_timeline_rejects_objects_nonfinite_values_and_escape_paths(self):
        for data in ({}, ordered(event(values={'x': object()})), ordered(event(values={'x': float('nan')})),
                     ordered(event(values={'x': 10 ** 1000})), ordered(event(path='../escape')),
                     ordered(event(line=0)), ordered(event(function='')), ordered(event(event='unknown')),
                     ordered(event(values={str(n): n for n in range(9)})), ordered(event(values={'': 1})),
                     ordered({'unknown': 1})):
            with self.subTest(data=str(data)[:40]), self.assertRaises(ValueError):
                validate_timeline(data)
        self.assertEqual(validate_timeline(ordered(event(values={'x': None, 'y': True})))['timeline'][0]['values']['y'], True)


class FusionPatternPerformanceTests(unittest.TestCase):
    def test_two_analyzers_are_attributed_deduplicated_and_ranked(self):
        def report(tool, path, line):
            return {'runs': [{'tool': {'driver': {'name': tool, 'version': '1'}}, 'results': [
                {'ruleId': 'rule', 'message': {'text': 'inspect'}, 'locations': [{'physicalLocation': {
                    'artifactLocation': {'uri': path}, 'region': {'startLine': line}}}]}]}]}
        ruff = parse_report(json.dumps(report('Ruff', 'calc.py', 3)).encode(), 'sarif', Path.cwd())
        semgrep = parse_report(json.dumps(report('Semgrep', 'other.py', 1)).encode(), 'sarif', Path.cwd())
        result = fuse_findings([('ev1', ruff), ('ev2', semgrep), ('ev3', ruff)], failing_lines={'calc.py': [3]})
        first = result['findings'][0]
        self.assertEqual(first['tool'], 'Ruff')
        self.assertEqual(first['evidence_ids'], ['ev1', 'ev3'])
        self.assertEqual(first['score'], 5)
        self.assertEqual(fuse_findings([('ev', ruff)], changed_lines={'calc.py': [3]})['findings'][0]['score'], 3)
        self.assertEqual(result['findings'][1]['tool'], 'Semgrep')
        self.assertTrue(fuse_findings([('ev', ruff), ('ev', semgrep)], limit=1)['truncated'])
        for options in ({'limit': 0}, {'failing_lines': {'../file': [1]}}, {'changed_lines': {'calc.py': [-1]}}):
            with self.assertRaises(ValueError):
                fuse_findings([], **options)
        with self.assertRaisesRegex(ValueError, "aggregate"):
            fuse_findings([('ev', {'findings': [{'path': None}] * 10001})])
        self.assertEqual(fuse_findings([('x', {'findings': [{'path': None}]})])['findings'], [])

    def test_patterns_are_hypotheses_with_content_cache_invalidation(self):
        source = 'def append(x, items=[]):\n    try:\n        items.append(x)\n    except Exception:\n        pass\n'
        result = scan_patterns('calc.py', 'python', source)
        self.assertEqual({f['rule'] for f in result['findings']}, {'mutable-default', 'swallowed-exception'})
        self.assertTrue(all(f['confidence'] == 0.2 for f in result['findings']))
        clean = scan_patterns('calc.py', 'python', 'def append(x, items=None):\n    return x\n')
        self.assertNotEqual(clean['sha256'], result['sha256'])
        self.assertEqual(clean['findings'], [])
        _matches.cache_clear()
        with patch('octane.bug_patterns.ast_backend.parse_tree', return_value=None):
            self.assertEqual(scan_patterns('calc.py', 'python', source)['backend'], 'python_ast')
            self.assertFalse(scan_patterns('app.z', 'unavailable', 'x')['available'])
            self.assertFalse(scan_patterns('bad.py', 'python', 'def !')['available'])
        with self.assertRaises(ValueError):
            scan_patterns('x.py', 'python', 'x' * 256001)
        from octane.ledger import ast_backend
        if ast_backend.available():
            for language, text in (('javascript', 'try { f(); } catch (e) {}'),
                                   ('typescript', 'try { f(); } catch (e) {}'),
                                   ('java', 'class App { void f() { try { f(); } catch (Exception e) {} } }')):
                with self.subTest(language=language):
                    found = scan_patterns('App.src', language, text)
                    self.assertEqual(found['backend'], 'tree_sitter_queries')
                    self.assertEqual(found['findings'][0]['rule'], 'empty-catch')
            self.assertEqual(scan_patterns('App.js', 'javascript', 'try { f(); } catch (e) { throw e; }')['findings'], [])
            self.assertEqual(scan_patterns('broken.js', 'javascript', 'try {')['backend'], 'syntax_error')

    def test_performance_interval_requires_significance_and_preservation(self):
        fast = compare_samples([10] * 8, [8] * 8, preservation_passed=True)
        self.assertTrue(fast['accepted'])
        self.assertGreaterEqual(fast['median_reduction_interval']['confidence_at_least'], .95)
        self.assertFalse(compare_samples([10] * 8, [8] * 8, preservation_passed=False)['accepted'])
        self.assertFalse(compare_samples([10] * 8, [9.9] * 8, preservation_passed=True)['accepted'])
        self.assertFalse(compare_samples([10] * 8, [8, 12] * 4, preservation_passed=True)['accepted'])
        self.assertFalse(compare_samples([10] * 8, [12] * 8, preservation_passed=True)['accepted'])
        self.assertAlmostEqual(median_interval(list(range(20)))['lower'], 5)
        for a, b in (([1], [1]), ([0] * 8, [1] * 8), ([1] * 8, [float('inf')] * 8), ([1] * 8, [1] * 9)):
            with self.assertRaises(ValueError):
                compare_samples(a, b, preservation_passed=True)
        with self.assertRaises(ValueError):
            compare_samples([1] * 8, [1] * 8, preservation_passed=1)


class DiagnosisWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        (self.root / 'calc.py').write_text('def f(items=[]):\n    return items\n')
        self.bridge = OctaneBridge(str(self.base / 'db.sqlite3'), [str(self.root)], str(self.base / 'workspaces'))
        self.pil = self.bridge.intelligence
        self.project = self.bridge.dispatch({'operation': 'project_create', 'params': {'name': 'diagnose', 'root': str(self.root)}})['id']
        self.actor = self.bridge._actor(self.project, 'diagnose')

    def evidence(self, payload, project=None, **fields):
        return self.pil.submit_evidence(project or self.project, 'structured_report', payload, 'fixture', **fields)['id']

    def analyze(self, kind, ids=None, options=None):
        return self.bridge.dispatch({'operation': 'diagnose', 'params': {'projectId': self.project, 'analysisKind': kind,
                                      'evidenceIds': ids or [], 'analysisOptions': options or {}}})

    def test_import_localize_select_and_trace_links_both_runs(self):
        raw = json.dumps({'test_spectra': spectra()}).encode()
        report = parse_report(raw, 'spectrum', self.root)
        key = self.evidence(report)
        result = self.analyze('localize', [key])
        self.assertEqual(result['result']['suspects'][0]['line'], 3)
        saved = self.pil.retrieve_evidence(result['evidence_id'])
        self.assertEqual(saved['verification_status'], 'inferred')
        self.assertEqual(saved['relationships'][0]['related_evidence_id'], key)
        selected = self.analyze('select-tests', [key], {'changedLines': {'calc.py': [3]}})
        self.assertEqual(selected['result']['first'], ['negative'])
        keys = [self.evidence(parse_report(json.dumps(ordered(event(v))).encode(), 'ordered-trace', self.root)) for v in (2, 3)]
        traced = self.analyze('trace-diff', keys)
        self.assertEqual(traced['input_evidence_ids'], keys)
        self.assertEqual(len(self.pil.retrieve_evidence(traced['evidence_id'])['relationships']), 2)
        self.assertEqual(self.analyze('fuse', [key])['result']['findings'], [])
        finding = self.evidence({'findings': [{'tool': 'Ruff', 'path': 'calc.py', 'line': 3, 'rule': 'test', 'message': 'inspect'}]})
        fused = self.analyze('fuse', [key, traced['evidence_id'], finding])
        self.assertEqual(fused['result']['findings'][0]['score'], 5)
        self.assertEqual(fused['result']['spectrum']['suspects'][0]['line'], 3)
        self.assertEqual(fused['result']['trace_difference_evidence_ids'], [traced['evidence_id']])

    def test_pattern_hypotheses_are_proposed_with_low_confidence(self):
        result = self.analyze('patterns', options={'path': 'calc.py', 'language': 'python'})
        self.assertEqual(len(result['result']['findings']), 1)
        hypotheses = self.pil.retrieve_hypotheses(self.project)
        self.assertEqual(hypotheses[0]['confidence'], .2)
        self.assertEqual(hypotheses[0]['status'], 'proposed')
        (self.root / 'escape.py').symlink_to(self.base / 'outside.py')
        with self.assertRaises(ValueError):
            self.analyze('patterns', options={'path': 'escape.py', 'language': 'python'})

    def test_rejects_stale_cross_project_invalid_and_aggregate_inputs(self):
        key = self.evidence({'test_spectra': spectra()})
        second = self.pil.create_project('second', self.root)['id']
        other = self.evidence({}, project=second)
        for kind, ids, options in (('localize', [other], {}), ('localize', ['missing'], {}),
                                   ('localize', [key, key], {}), ('trace-diff', [key], {}),
                                   ('trace-diff', [key, other], {}), ('rank-patches', [key], {}),
                                   ('invalid', [], {})):
            with self.subTest(kind=kind, ids=ids), self.assertRaises(ValueError):
                self.analyze(kind, ids, options)
        self.pil.invalidate_evidence(key)
        with self.assertRaisesRegex(ValueError, 'revalidate'):
            self.analyze('localize', [key])
        with self.assertRaises(KeyError):
            diagnose(self.pil, 'missing', self.actor, 'localize', [])
        with self.assertRaises(ValueError):
            diagnose(self.pil, self.project, self.actor, 'fuse', [], [])
        old = self.evidence({'test_spectra': spectra()}, revision='old')
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.analyze('localize', [old])
        aggregate = self.evidence({'python_trace': {'calls': [], 'available': True}})
        with self.assertRaises(ValueError):
            self.analyze('trace-diff', [aggregate, self.evidence({})])


class EvaluationMetricTests(unittest.TestCase):
    def test_execution_errors_are_localization_misses_and_top5_is_published(self):
        from octane.evaluation import _evaluation_summary
        cases = [{'expected_files': ['a.py']}] * 3
        outcomes = [{'resolved': True, 'localization_rank': 4}, {'resolved': False, 'error': 'setup failed'},
                    {'resolved': False, 'localization_rank': 1}]
        report = _evaluation_summary(cases, outcomes, 3)
        self.assertEqual(report['localization_top1'], 1 / 3)
        self.assertEqual(report['localization_top5'], 2 / 3)
        self.assertEqual(report['error_count'], 1)
        self.assertEqual(report['localization_labeled_cases'], 3)
