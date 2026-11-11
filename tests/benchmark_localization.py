"""Synthetic, controlled ranking comparison; not a real-world accuracy claim."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "octance" / "scripts"))
from octance.source import locate_suspects  # noqa: E402

CASES = [
    {
        "id": "python-wrapper-vs-handler",
        "files": ["tests/harness.py", "src/handler.py", "docs/guide.md"],
        "trace": 'Traceback (most recent call last):\n  File "tests/harness.py", line 10, in run\n  File "src/handler.py", line 40, in handle',
        "recent": ["tests/harness.py"], "expected": "src/handler.py",
    },
    {
        "id": "typescript-shell-vs-router",
        "files": ["app.ts", "web/router.ts", "db/query.sql"],
        "trace": "at validate (web/router.ts:41:8)\nat dispatch (app.ts:20:3)",
        "recent": ["app.ts"], "expected": "web/router.ts",
    },
    {
        "id": "go-entry-vs-cache",
        "files": ["cmd/main.go", "internal/cache.go", "docs/guide.md"],
        "trace": "cache.get(/repo/internal/cache.go:31 +0x10)\ncmd.main(/repo/cmd/main.go:8 +0x10)",
        "recent": ["cmd/main.go"], "expected": "internal/cache.go",
    },
]


def reciprocal_rank(order: list[str], expected: str) -> float:
    try:
        return 1.0 / (order.index(expected) + 1)
    except ValueError:
        return 0.0


def main() -> int:
    outcomes = []
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix="octance-loc-bench-") as directory:
            root = Path(directory)
            for relative in case["files"]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("// synthetic fixture; deliberately not executable\n", encoding="utf-8")
            with patch("octance.source.recent_project_changes", return_value=(case["recent"], None)):
                result = locate_suspects(root, case["trace"])
            ranked = [item["path"] for item in result["suspects"]]
            traceback_baseline = result["baselines"]["traceback_primary_order"]
            recent_baseline = result["baselines"]["recent_change_order"]
            outcomes.append({
                "case": case["id"], "expected_suspect": case["expected"],
                "ranked": ranked, "traceback_display_order": result["baselines"]["traceback_frame_order"],
                "traceback_baseline": traceback_baseline,
                "traceback_order_convention": result["baselines"]["traceback_order_convention"],
                "recent_change_baseline": recent_baseline,
                "ranked_top1": bool(ranked and ranked[0] == case["expected"]),
                "traceback_top1": bool(traceback_baseline and traceback_baseline[0] == case["expected"]),
                "recent_change_top1": bool(recent_baseline and recent_baseline[0] == case["expected"]),
                "ranked_rr": reciprocal_rank(ranked, case["expected"]),
                "traceback_rr": reciprocal_rank(traceback_baseline, case["expected"]),
                "recent_change_rr": reciprocal_rank(recent_baseline, case["expected"]),
            })
    count = len(outcomes)
    report = {
        "benchmark": "synthetic controlled trace/change ranking fixtures",
        "cases": count,
        "source_execution": False,
        "metrics": {
            method: {
                "top1": sum(bool(item[method + "_top1"]) for item in outcomes) / count,
                "mrr": sum(float(item[method + "_rr"]) for item in outcomes) / count,
            }
            for method in ("ranked", "traceback", "recent_change")
        },
        "outcomes": outcomes,
        "limitations": [
            "Three synthetic fixtures are a mechanics regression check, not a representative real-project dataset.",
            "Expected files are authored by the fixture; no independent root-cause labels or replayed checks are present.",
            "Each trace has one frame per candidate and uses realistic order conventions: Python outer-to-inner, Node/Go innermost-first.",
            "Results do not establish localization quality on user projects or across the listed languages.",
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
