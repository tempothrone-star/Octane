"""Conservative performance decisions from paired recorded sandbox executions."""
from __future__ import annotations

import json
import math
import statistics


def median_interval(values):
    """Exact distribution-free >=95% median interval using binomial order statistics."""
    values = sorted(values)
    n = len(values)
    if not 8 <= n <= 50:
        raise ValueError('performance comparison needs 8 to 50 independent paired samples')
    k = 1
    while k < n // 2 and 2 * sum(math.comb(n, j) for j in range(k + 1)) / 2 ** n <= 0.05:
        k += 1
    coverage = 1 - 2 * sum(math.comb(n, j) for j in range(k)) / 2 ** n
    return {'lower': values[k - 1], 'upper': values[n - k], 'confidence_at_least': coverage}


def compare_samples(before, after, *, preservation_passed, minimum_improvement=0.05):
    if len(before) != len(after) or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                                       for v in [*before, *after]):
        raise ValueError('paired durations must be finite positive numbers of equal length')
    if type(preservation_passed) is not bool or type(minimum_improvement) not in (int, float) or not 0 <= minimum_improvement < 1:
        raise ValueError('invalid preservation result or improvement threshold')
    # Fractional reduction, paired to reduce host-load drift. No normality assumption.
    reductions = [(old - new) / old for old, new in zip(before, after)]
    interval = median_interval(reductions)
    accepted = preservation_passed and interval['lower'] > minimum_improvement
    return {'accepted': accepted, 'median_reduction': statistics.median(reductions), 'median_reduction_interval': interval,
            'before_mean': statistics.mean(before), 'after_mean': statistics.mean(after),
            'before_stdev': statistics.stdev(before), 'after_stdev': statistics.stdev(after),
            'minimum_improvement': minimum_improvement, 'sample_count': len(before),
            'preservation_passed': preservation_passed,
            'basis': 'Exact paired median interval; independent samples assumed. Container startup/noise included; no automatic patch application'}


def _run(db, project_id, identifier):
    row = db.execute('SELECT * FROM execution_runs WHERE id=? AND project_id=?', (identifier, project_id)).fetchone()
    if row is None:
        raise ValueError('performance input needs an engine-recorded run in this project')
    output = json.loads(row['result_json'])
    if (row['status'] != 'passed' or row['before_hash'] != row['after_hash'] or output.get('cleanup_warning')
            or output.get('timed_out') or output.get('output_truncated') or output.get('private_validation')):
        raise ValueError('performance inputs must pass unchanged without truncation or cleanup problems')
    return dict(row), output


def compare_recorded(manager, project_id, before_ids, after_ids, preservation_pairs, agent_id, minimum_improvement=0.05):
    identifiers = [*before_ids, *after_ids, *(key for pair in preservation_pairs for key in pair)]
    if (len(identifiers) != len(set(identifiers)) or not preservation_pairs
            or len(before_ids) != len(after_ids) or not 8 <= len(before_ids) <= 50
            or any(len(pair) != 2 for pair in preservation_pairs)):
        raise ValueError('supply distinct paired timing runs and separate paired preservation checks')
    with manager.intelligence._session() as db:
        before = [_run(db, project_id, key) for key in before_ids]
        after = [_run(db, project_id, key) for key in after_ids]
        checks = [(_run(db, project_id, a), _run(db, project_id, b)) for a, b in preservation_pairs]
    from .experiments import _same_output, _trace_settings
    pairs = [*zip(before, after), *checks]
    image = before[0][0]['image_id']
    left_hash, right_hash = before[0][0]['after_hash'], after[0][0]['after_hash']
    valid = True
    for (a, out_a), (b, out_b) in pairs:
        if (not image or a['image_id'] != image or b['image_id'] != image or a['command_json'] != b['command_json']
                or a['after_hash'] != left_hash or b['after_hash'] != right_hash
                or _trace_settings(a) != _trace_settings(b)):
            raise ValueError('performance pairs must use fixed snapshots, the same immutable image, command and trace settings')
        valid = valid and _same_output(out_a, out_b) is True and out_a.get('test_integrity') == out_b.get('test_integrity')
    durations = lambda runs: [out['duration_seconds'] for _, out in runs]
    result = compare_samples(durations(before), durations(after), preservation_passed=valid,
                             minimum_improvement=minimum_improvement)
    inputs = [row['evidence_id'] for group in (before, after) for row, _ in group]
    inputs += [row['evidence_id'] for pair in checks for row, _ in pair]
    payload = {'schema': 'octane.performance.v1', **result, 'input_evidence_ids': inputs,
               'baseline_hash': left_hash, 'candidate_hash': right_hash, 'image_id': image}
    evidence = manager.intelligence.submit_evidence(project_id, 'performance_comparison', payload,
        'Paired sandbox timing and preservation decision', agent_id, manager.intelligence.get_project_revision(project_id))
    for key in inputs:
        manager.intelligence.link_evidence(evidence['id'], key, 'context', agent_id)
    return {'evidence_id': evidence['id'], **payload}
