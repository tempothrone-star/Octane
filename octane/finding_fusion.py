"""Fuse analyzer observations with path evidence without declaring them confirmed bugs."""
from __future__ import annotations

import hashlib
import json
from .localization import source_path


def finding_key(finding):
    fields = {key: finding.get(key) for key in ('tool', 'rule', 'path', 'line', 'message')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def fuse_findings(reports, *, failing_lines=None, changed_lines=None, limit=100):
    failing_lines = failing_lines or {}
    changed_lines = changed_lines or {}
    for mapping in (failing_lines, changed_lines):
        for path, lines in mapping.items():
            source_path(path)
            if not isinstance(lines, list) or any(type(line) is not int or line < 1 for line in lines):
                raise ValueError('path evidence needs positive integer line lists')
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('finding limit must be 1 to 1000')
    merged, count = {}, 0
    for evidence_id, report in reports:
        for finding in report.get('findings', []):
            count += 1
            if count > 10000:
                raise ValueError('finding fusion exceeds the aggregate 10000-record budget')
            path = finding.get('path')
            if path is None:
                continue
            source_path(path)
            key = finding_key(finding)
            entry = merged.setdefault(key, {**finding, 'fingerprint': key, 'evidence_ids': [], 'signals': []})
            if evidence_id not in entry['evidence_ids']:
                entry['evidence_ids'].append(evidence_id)
    for entry in merged.values():
        score, signals = 0, []
        for mapping, label, weight in ((failing_lines, 'failing_path', 4), (changed_lines, 'changed_path', 2)):
            if entry['path'] in mapping:
                exact = entry.get('line') in mapping[entry['path']]
                score += weight + int(exact)
                signals.append({'kind': label, 'same_line': exact, 'weight': weight + int(exact)})
        entry.update(score=score, signals=signals, confidence='unverified_analyzer_observation')
    ranked = sorted(merged.values(), key=lambda row: (-row['score'], row['path'], row.get('line') or 0, row['fingerprint']))
    return {'findings': ranked[:limit], 'truncated': len(ranked) > limit,
            'weights': {'failing_path': 4, 'changed_path': 2, 'same_line_bonus': 1},
            'basis': 'Deterministic path proximity; analyzer findings can describe intentional behavior'}
