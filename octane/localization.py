"""Deterministic line localization and test ordering; scores are not probabilities."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import PurePosixPath

MAX_TESTS = 2000
MAX_LINES = 200000


def source_path(value):
    if not isinstance(value, str) or not value or len(value) > 350 or '\\' in value:
        raise ValueError('source path must be a bounded checkout-relative POSIX path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or not value.isprintable():
        raise ValueError('source path must stay in the checkout')
    return path.as_posix()


def normalize_spectrum(data):
    rows = data.get('test_spectra') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TESTS:
        raise ValueError('test_spectra needs 1 to 2000 per-test coverage records')
    result, budget = [], 0
    for row in rows:
        name, status, coverage = row['id'], row['status'], row['coverage']
        if not isinstance(name, str) or not 1 <= len(name) <= 300 or not name.isprintable():
            raise ValueError('invalid test ID')
        if status not in {'passed', 'failed', 'skipped'} or not isinstance(coverage, dict):
            raise ValueError('invalid per-test coverage status or map')
        checked = {}
        for path, lines in coverage.items():
            if not isinstance(lines, list) or any(type(n) is not int or not 1 <= n <= 10000000 for n in lines):
                raise ValueError('coverage lines must be positive integers')
            budget += len(lines)
            if budget > MAX_LINES:
                raise ValueError('per-test coverage exceeds the aggregate line budget')
            checked[source_path(path)] = sorted(set(lines))
        result.append({'id': name, 'status': status, 'coverage': checked})
    return result


def stable_tests(rows):
    grouped = defaultdict(list)
    for row in normalize_spectrum({'test_spectra': rows}):
        grouped[row['id']].append(row)
    stable, flaky, unstable_coverage = [], [], []
    for name, runs in sorted(grouped.items()):
        statuses = {r['status'] for r in runs}
        if len(statuses) != 1:
            flaky.append(name)
        elif any(r['coverage'] != runs[0]['coverage'] for r in runs):
            unstable_coverage.append(name)
        elif runs[0]['status'] != 'skipped':
            stable.append(runs[0])
    return stable, flaky, unstable_coverage


def spectrum_localize(rows, limit=50):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('localization limit must be 1 to 1000')
    stable, flaky, unstable = stable_tests(rows)
    counts = {'failed': Counter(), 'passed': Counter()}
    failures = sum(row['status'] == 'failed' for row in stable)
    passes = len(stable) - failures
    for row in stable:
        counts[row['status']].update((path, line) for path, lines in row['coverage'].items() for line in lines)
    ranked = []
    for (path, line), failed in counts['failed'].items():
        passed = counts['passed'][path, line]
        score = failed / math.sqrt(failures * (failed + passed))
        ranked.append({'path': path, 'line': line, 'score': score,
                       'failed_covering': failed, 'passed_covering': passed})
    ranked.sort(key=lambda row: (-row['score'], row['path'], row['line']))
    return {'method': 'ochiai', 'suspects': ranked[:limit], 'failed_tests': failures, 'passed_tests': passes,
            'excluded_flaky': flaky, 'excluded_unstable_coverage': unstable, 'truncated': len(ranked) > limit,
            'available': failures > 0 and passes > 0,
            'basis': 'Per-test executed lines; correlation, not proof of causation or a correctness probability'}


def select_tests(rows, changed_lines):
    """Order impacted tests first and retain every other test as a required remainder."""
    changed = {source_path(path): set(lines) for path, lines in changed_lines.items()}
    if any(any(type(n) is not int or n < 1 for n in lines) for lines in changed.values()):
        raise ValueError('changed lines must be positive integers')
    stable, flaky, unstable = stable_tests(rows)
    first, rest, observed = [], [], defaultdict(set)
    for row in stable:
        impacted = False
        for path, lines in row['coverage'].items():
            observed[path].update(lines)
            if path in changed and (not changed[path] or set(lines) & changed[path]):
                impacted = True
        (first if impacted else rest).append(row['id'])
    missing = [path for path, lines in changed.items() if path not in observed or not lines <= observed[path]]
    all_ids = sorted({row['id'] for row in rows})
    unknown = bool(missing or flaky or unstable)
    return {'first': all_ids if unknown else first,
            'remainder': [] if unknown else sorted(set(all_ids) - set(first)),
            'full_suite_required': True, 'fallback_to_full': unknown, 'unmapped_changes': sorted(missing),
            'excluded_flaky': flaky, 'excluded_unstable_coverage': unstable,
            'basis': 'Coverage is historical; ordering is a fail-fast hint and never permits dropping preservation checks'}


def trace_diff(passing, failing):
    for trace in (passing, failing):
        if not isinstance(trace.get('timeline'), list) or not trace['timeline']:
            raise ValueError('ordered traces are required; sampled/aggregated call graphs cannot establish first divergence')
        if len(trace['timeline']) > 512:
            raise ValueError('trace timeline exceeds 512 events')
    left, right = passing['timeline'], failing['timeline']
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a != b:
            kind = 'control_flow' if a is None or b is None else 'values'
            if a and b and any(a.get(k) != b.get(k) for k in ('path', 'function', 'line', 'event')):
                kind = 'control_flow'
            return {'first_divergence': {'index': index, 'kind': kind, 'passing': a, 'failing': b},
                    'complete': not (passing.get('timeline_truncated') or failing.get('timeline_truncated')),
                    'basis': 'First observed ordered-event difference; input differences and scheduling can also cause divergence'}
    return {'first_divergence': None, 'complete': not (passing.get('timeline_truncated') or failing.get('timeline_truncated')),
            'basis': 'No divergence in the recorded prefix; truncated traces do not establish equivalence'}
