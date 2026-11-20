"""MIT-licensed seeded mechanics cases; deliberately not an external accuracy dataset."""
CASES = [
    {'id': 'magnitude', 'source': 'def f(n):\n    if n < 0:\n        return -n + 1\n    return n\n',
     'fixed': 'return -n', 'cases': [[-2, 2], [1, 1], [0, 0]]},
    {'id': 'zero', 'source': 'def f(n):\n    if n == 0:\n        return 1\n    return n\n',
     'fixed': 'return 0', 'cases': [[0, 0], [1, 1], [2, 2]]},
    {'id': 'last', 'source': 'def f(items):\n    if len(items) > 1:\n        return items[0]\n    return items[0] if items else None\n',
     'fixed': 'return items[-1]', 'cases': [[[1, 2], 2], [[1], 1], [[], None]]},
    {'id': 'spaces', 'source': 'def f(text):\n    if text.startswith(" "):\n        return text.rstrip()\n    return text\n',
     'fixed': 'return text.strip()', 'cases': [[' hi ', 'hi'], ['hi', 'hi'], ['', '']]},
    {'id': 'lookup', 'source': 'def f(key):\n    if key not in {"a": 1}:\n        return 0\n    return {"a": 1}[key]\n',
     'fixed': 'return None', 'cases': [['b', None], ['a', 1], ['a', 1]]},
    {'id': 'nonnegative', 'source': 'def f(n):\n    if n < 0:\n        return True\n    return True\n',
     'fixed': 'return False', 'cases': [[-1, False], [0, True], [1, True]]},
    {'id': 'half', 'source': 'def f(n):\n    if n > 1:\n        return n // 2\n    return n\n',
     'fixed': 'return (n + 1) // 2', 'cases': [[3, 2], [1, 1], [0, 0]]},
    {'id': 'milliseconds', 'source': 'def f(n):\n    if n > 0:\n        return n * 100\n    return n\n',
     'fixed': 'return n * 1000', 'cases': [[2, 2000], [0, 0], [-1, -1]]},
]

DRIVER = '''
import importlib, json, os, sys
from pathlib import Path
cases = json.loads(Path("cases.json").read_text())
rows = []
for case in cases:
    module = importlib.import_module(case["id"])
    for number, (value, expected) in enumerate(case["cases"]):
        lines = set()
        def record(frame, event, arg):
            if event == "line" and frame.f_code.co_filename.endswith("/" + case["id"] + ".py"):
                lines.add(frame.f_lineno)
            return record
        sys.settrace(record)
        try:
            actual = module.f(value)
        finally:
            sys.settrace(None)
        rows.append({"id": case["id"] + ":" + str(number),
                     "status": "passed" if actual == expected else "failed",
                     "coverage": {case["id"] + ".py": sorted(lines)}})
Path(os.environ["OCTANE_REPORT_PATH"]).write_text(json.dumps({"test_spectra": rows}))
sys.exit(1 if any(row["status"] == "failed" for row in rows) else 0)
'''
