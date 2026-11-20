# Public historical evaluation

Measured locally on November 20, 2026 with Python 3.12, Linux Docker, and the native evaluation adapter. One supplied maintainer patch was replayed; no agent generated or discovered this fix during this evaluation.

## Case

- Repository: [pallets-eco/blinker](https://github.com/pallets-eco/blinker).
- Base: `0068cb4ce6a8d63f6628ba0a02450b7bae5110e9`.
- Independent fix: [fc537092961d79731ac8a4e020e852076d7c6256](https://github.com/pallets-eco/blinker/commit/fc537092961d79731ac8a4e020e852076d7c6256), restoring the deprecated `WeakNamespace` import.
- Candidate: maintainer's one-line `return _WeakNamespace` change in `src/blinker/base.py`.
- Focused probe: import the deprecated public name, construct a namespace, and verify its named signal. Deprecation warnings are suppressed in both runs.
- Preservation: connect a Signal receiver, verify send results, disconnect it, and verify no receiver remains.
- Labels and probes: [JSONL fixture](../examples/blinker-pr149.jsonl). The fixture contains the patch and explicit command arrays.

## Reproduce from a checkout

Choose an empty temporary directory. Replace the absolute example paths with your Octane checkout and temporary directory. Clone/fetch and image preparation use the host network; case checks run offline in containers.

```sh
git clone https://github.com/pallets-eco/blinker.git /tmp/octane-eval-blinker
cp /absolute/path/to/Octane/examples/blinker-pr149.jsonl /tmp/octane-eval-blinker/cases.jsonl
docker pull python:3.12-slim
python3 -m octane native init /tmp/octane-eval-blinker --name blinker-history --database /tmp/octane-eval-state/pil.sqlite3
# Use the returned project ID:
python3 -m octane native evaluate PROJECT_ID cases.jsonl --image python:3.12-slim --limit 1 --database /tmp/octane-eval-state/pil.sqlite3 --workspace-root /tmp/octane-eval-state/workspaces
```

The Git repository must retain the base commit. The adapter archives its bytes without changing the original checkout, applies the supplied patch only to the candidate, and records four execution runs with an immutable image ID. Run from the Octane checkout or its installed Python environment.

## Recorded result

```json
{
  "case_count": 1,
  "resolved": 1,
  "resolved_rate": 1.0,
  "localization_mrr": 1.0,
  "localization_top1": 1.0,
  "case": {
    "instance_id": "blinker-pr149-weaknamespace",
    "base_commit": "0068cb4ce6a8d63f6628ba0a02450b7bae5110e9",
    "resolved": true,
    "localization_rank": 1,
    "ranked_files": [
      "src/blinker/base.py",
      "src/blinker/__init__.py",
      "src/blinker/_utilities.py"
    ],
    "image_id": "sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b",
    "baseline_run_id": "RUN-ea0df429956247df9146bbe570deb8f0",
    "candidate_run_id": "RUN-9fd3cc90c5554197aa5f2a62477d08b1",
    "preservation_run_ids": [
      "RUN-7b687063a39043668697876c258b318c",
      "RUN-6202b1c0487a46a8b5685e50460fb2bf"
    ],
    "evidence_ids": [
      "EV-c1e0951618ba4195b7d5836964214eee",
      "EV-36f43eeee0b14df38b0feb826721fde2",
      "EV-e6fa4d1301444747aa94390cc04fe25a",
      "EV-c297c3cc5167492e8bf96c8edf9814bf"
    ]
  }
}
```

The focused baseline exited 1; the candidate and both preservation runs exited 0. The four run/evidence IDs identify this local validation session; the temporary source workspaces and test database were removed after validation. Image tags can change, so compare the recorded immutable image ID when reproducing. Source localization ranked the independently labeled file first on this case. These one-case metrics validate the adapter and cannot estimate general localization or repair accuracy. The synthetic suite deliberately permits an unlocalized but successfully repaired case, keeping execution success separate from localization scores.

## 0.3.0 replay

The same public maintainer patch was replayed again with the new evaluation schema on November 20, 2026. The focused baseline failed, the supplied correction passed, both preservation runs passed, and the expected file ranked first (top-1/top-5 1/1, zero execution errors). The first produced hypothesis took 1.657 seconds including snapshot/setup and the preceding checks, on the immutable Python image above. This remains supplied-patch replay, not autonomous fix discovery. Schema v2 includes top-5 and counts labeled execution errors as misses rather than dropping them from the denominator.

The separate [seeded acceptance harness](accuracy-workflow.md#reproducible-measurements) tests line localization, withheld inputs, impacted-first ordering, and optimization variance in Docker CI. Its measurements must not be pooled with this historical file-level case as a general accuracy claim.
