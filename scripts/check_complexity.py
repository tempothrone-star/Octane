"""Ratchet complexity across all production Python functions and methods."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radon.complexity import cc_visit

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'scripts/complexity-baseline.json'


def _functions(blocks, prefix=''):
    for block in blocks:
        if not prefix and getattr(block, "classname", None):
            continue  # Radon also exposes class methods in its flat result list.
        name = prefix + block.name
        if hasattr(block, 'methods'):
            yield from _functions(block.methods, name + '.')
            yield from _functions(block.inner_classes, name + '.')
        else:
            yield name, block.complexity
            yield from _functions(block.closures, name + '.<locals>.')


def source_scores(source):
    scores, occurrences = {}, {}
    for name, score in _functions(cc_visit(source)):
        occurrences[name] = occurrences.get(name, 0) + 1
        key = name if occurrences[name] == 1 else name + '#' + str(occurrences[name])
        scores[key] = score
    return scores


def measure(root):
    scores = {}
    for directory in ('octane', 'scripts'):
        for path in sorted((root / directory).rglob('*.py')):
            for name, score in source_scores(path.read_text()).items():
                scores[path.relative_to(root).as_posix() + ':' + name] = score
    return scores


def violations(scores, baseline, limit=30):
    errors = []
    for name, score in scores.items():
        ceiling = baseline.get(name, limit)
        if score > ceiling:
            errors.append(f'{name}: {score} exceeds {ceiling}')
    for name, ceiling in baseline.items():
        score = scores.get(name)
        if type(ceiling) is not int or ceiling <= limit:
            errors.append(f'{name}: baseline entries must exceed {limit}')
        elif score is None or score < ceiling:
            errors.append(f'{name}: lower or remove the baseline entry ({ceiling} -> {score})')
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path, default=BASELINE)
    parser.add_argument('--metrics', action='store_true', help='also report counts and aggregate scores')
    args = parser.parse_args(argv)
    document = json.loads(args.baseline.read_text())
    if document.get('limit') != 30 or not isinstance(document.get('exceptions'), dict):
        parser.error('baseline must define limit 30 and an exceptions object')
    scores = measure(args.root)
    errors = violations(scores, document['exceptions'], document['limit'])
    if args.metrics:
        total = sum(scores.values())
        print(json.dumps({'functions':len(scores), 'total_complexity':total,
                          'average_complexity':round(total / len(scores), 4) if scores else 0,
                          'decision_points':sum(score - 1 for score in scores.values()),
                          'maximum':max(scores.values(), default=0)}, sort_keys=True))
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f'Checked {len(scores)} production functions/methods: limit 30, {len(document["exceptions"])} non-growing exceptions')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
