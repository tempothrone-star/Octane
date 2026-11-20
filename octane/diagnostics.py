"""Evidence-linked diagnosis available through CLI, OpenClaw and MCP."""
from __future__ import annotations

from pathlib import Path

from .bug_patterns import scan_patterns
from .finding_fusion import fuse_findings
from .localization import normalize_spectrum, select_tests, source_path, spectrum_localize, stable_tests, trace_diff
from .trace_events import validate_timeline

KINDS = ('trace-diff', 'localize', 'fuse', 'select-tests', 'patterns', 'rank-patches')


def _inputs(intelligence, project_id, evidence_ids):
    if (not isinstance(evidence_ids, list) or len(evidence_ids) > 100
            or any(not isinstance(key, str) for key in evidence_ids) or len(set(evidence_ids)) != len(evidence_ids)):
        raise ValueError('provide at most 100 unique evidence IDs')
    project = intelligence.get_project(project_id)
    if project is None:
        raise KeyError(project_id)
    records = []
    for identifier in evidence_ids:
        record = intelligence.retrieve_evidence(identifier)
        if not record or record['project_id'] != project_id:
            raise ValueError('diagnostic inputs must belong to this project')
        if record['stale'] or record['revision'] != project['revision']:
            raise ValueError('revalidate stale or revision-incompatible diagnostic inputs')
        records.append(record)
    return project, records


def _report(record):
    data = record['payload']
    return data.get('report') or data


def _ordered(record):
    data = record['payload']
    trace = data.get('python_trace') or _report(record).get('ordered_trace')
    if trace is None:
        raise ValueError('evidence does not contain an ordered trace')
    return validate_timeline(trace)


def _patterns(root, options):
    path = source_path(options['path'])
    candidate = root / path
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
        raise ValueError('pattern source must stay in the project')
    with candidate.open('rb') as stream:
        content = stream.read(256001)
    if len(content) > 256000:
        raise ValueError('pattern source exceeds 256 KB')
    return scan_patterns(path, options['language'], content.decode('utf-8'))


def _spectra(records):
    rows = [row for record in records for row in _report(record).get('test_spectra', [])]
    return normalize_spectrum({'test_spectra': rows})


def _analyze(intelligence, project, records, kind, options):
    if kind == 'trace-diff':
        if len(records) != 2:
            raise ValueError('trace-diff needs passing then failing trace evidence IDs')
        return trace_diff(_ordered(records[0]), _ordered(records[1]))
    if kind == 'localize':
        return spectrum_localize(_spectra(records), options.get('limit', 50))
    if kind == 'select-tests':
        return select_tests(_spectra(records), options['changedLines'])
    if kind == 'patterns':
        return _patterns(Path(project['root']).resolve(), options)
    if kind == 'rank-patches':
        from .patch_ranking import rank_patches
        return rank_patches(intelligence, project['id'], records)
    return _fuse(records, options)


def _fuse(records, options):
    failing = {path: set(lines) for path, lines in options.get('failingLines', {}).items()}
    spectra = [row for record in records for row in _report(record).get('test_spectra', [])]
    localization = spectrum_localize(spectra) if spectra else None
    if spectra:
        stable, _, _ = stable_tests(spectra)
        for row in stable:
            if row['status'] == 'failed':
                for path, lines in row['coverage'].items():
                    failing.setdefault(path, set()).update(lines)
    trace_evidence = []
    for record in records:
        data = record['payload']
        if record['evidence_type'] == 'diagnosis' and data.get('kind') == 'trace-diff':
            divergence = data['result'].get('first_divergence') or {}
            location = divergence.get('failing')
            if location:
                failing.setdefault(source_path(location['path']), set()).add(location['line'])
                trace_evidence.append(record['id'])
    result = fuse_findings([(record['id'], _report(record)) for record in records],
                           failing_lines={path: sorted(lines) for path, lines in failing.items()},
                           changed_lines=options.get('changedLines'), limit=options.get('limit', 100))
    result.update(spectrum=localization, trace_difference_evidence_ids=trace_evidence)
    return result


def diagnose(intelligence, project_id, agent_id, kind, evidence_ids, options=None):
    if kind not in KINDS or (options is not None and not isinstance(options, dict)):
        raise ValueError('unsupported diagnostic kind or options')
    project, records = _inputs(intelligence, project_id, evidence_ids)
    result = _analyze(intelligence, project, records, kind, options or {})
    payload = {'schema': 'octane.diagnosis.v1', 'kind': kind, 'input_evidence_ids': evidence_ids,
               'result': result, 'options': options or {}, 'model_calls': False}
    with intelligence._transaction():
        evidence = intelligence.submit_evidence(project_id, 'diagnosis', payload,
            'Deterministic ' + kind + ' analysis; inspect the recorded limits', agent_id,
            project['revision'], verification_status='inferred')
        for identifier in evidence_ids:
            intelligence.link_evidence(evidence['id'], identifier, 'context', agent_id)
        if kind == 'patterns':
            for finding in result['findings']:
                hypothesis = intelligence.create_hypothesis(project_id, finding['message'], agent_id=agent_id, confidence=0.2)
                intelligence.attach_evidence(hypothesis['id'], evidence['id'], 'context', agent_id)
    return {'evidence_id': evidence['id'], **payload}
