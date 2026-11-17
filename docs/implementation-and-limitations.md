# Implementation and limitations

## Portable Octance

The standalone skill provides a Python 3.9+ standard-library CLI. It records evidence and claims in a project-local SQLite ledger, quarantines low-trust claims, verifies an anchored SHA-256 event chain, and compares materialized state with event replay. Evidence stays `unverified` in this ledger. Run checks with the host's authorized tools and record the observation with its source and revision. See the [portable command reference](../octance/SKILL.md).

| Command | What it reports |
| --- | --- |
| `languages` | Bounded inventory by extension. |
| `graph` | Candidate project-local file dependencies with parser coverage and unresolved imports. It uses Python AST, optional Tree-sitter, and text recognizers. |
| `locate --trace-file failure.log` | Ranked file suspects from a saved trace, query terms, and recent Git changes. It includes traceback and recent-change baselines. |
| `brief` | Ranked claims and open questions. The token estimate covers selected claim records. Other response fields and pretty-print whitespace can increase the output. |

Revision hashing can be expensive on large trees. SQLite WAL expects local storage. The hash chain and local anchor detect alterations under the CLI's integrity model. Shell or database access can modify both. Keep credentials out of descriptions and evidence payloads.

## Native Octane

The OpenClaw plugin adds checkout triage, candidate function relationships, runtime traces for specific probes, and a separate SQLite store for revision-aware investigations. A scan reports parser and budget gaps. A saved checkpoint carries its project revision, evidence references, and exploration position. Scan and revalidate before reusing a checkpoint after the checkout changes. See the [native workflow](openclaw.md).

- Static edges describe potential calls or imports. Resolve ambiguity from source and test execution. Dynamic dispatch, overloads, and cross-language calls can leave gaps.
- Runtime traces observe the selected command and supported process. Sampling may miss short calls. A passing test establishes the behavior its assertions checked.
- Patch proposals require current intent evidence, a matched failing-to-passing reproduction, and passing checks for preserved behavior. Proposals cover text edits to existing files. Review the diff and test coverage before applying it to the requested checkout.
- Docker experiments require an eligible local image with the needed tools and dependencies. Container networking is disabled. OpenClaw tool policy and allowed checkout roots govern access.

## Portable localization benchmark

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/benchmark_localization.py
```

The three authored Python, TypeScript, and Go cases test file-ranking mechanics.

| Ranker | Top-1 | Mean reciprocal rank |
| --- | ---: | ---: |
| Octance heuristic | 1.00 | 1.00 |
| Traceback-first | 1.00 | 1.00 |
| Recent-change-first | 0.00 | 0.00 |

The labels and traces were authored with the fixtures. These scores apply to the included cases. Assess bug-fix accuracy separately with representative defects and independent expected behavior.

## Validation

See the [project review](project-review.md) for regression coverage and GitHub Actions results. The [native Docker demonstration](openclaw.md#reproduce-the-native-demonstration) exercises a failing reproduction, an overbroad patch that breaks documented behavior, and a smaller candidate that passes both checks.
