# Implementation and limitations

## Local evidence and static analysis

The Octane package includes a checkout-only Python 3.9+ standard-library CLI (pip installation requires Python 3.11+) under `octane/ledger/`. It records evidence and claims in a project-local SQLite ledger, quarantines low-trust claims, verifies an anchored SHA-256 event chain, and compares materialized state with event replay. Evidence stays `unverified` in this ledger. Run checks with the host's authorized tools and record the observation with its source and revision. See the [portable command reference](../SKILL.md).

| Command | What it reports |
| --- | --- |
| `languages` | Bounded inventory by extension. |
| `graph` | Candidate project-local file dependencies with parser coverage and unresolved imports. It uses Python AST, optional Tree-sitter, and text recognizers. |
| `locate --trace-file failure.log` | Ranked file suspects from a saved trace, query terms, and recent Git changes. It includes traceback and recent-change baselines. |
| `brief` | Ranked claims and open questions. The token estimate covers selected claim records. Other response fields and pretty-print whitespace can increase the output. |

Revision hashing can be expensive on large trees. SQLite WAL expects local storage. The hash chain and local anchor detect alterations under the CLI's integrity model. Shell or database access can modify both. Keep credentials out of descriptions and evidence payloads.

## OpenClaw investigation tools

The OpenClaw plugin adds checkout triage, candidate function relationships, runtime traces for specific probes, and a separate SQLite store for revision-aware investigations. A scan reports parser and budget gaps. A saved checkpoint carries its project revision, evidence references, and exploration position. Scan and revalidate before reusing a checkpoint after the checkout changes. See the [native workflow](openclaw.md).

- Static edges describe potential calls or imports. Resolve ambiguity from source and test execution. Dynamic dispatch, overloads, and cross-language calls can leave gaps.
- Runtime traces observe the selected command and supported process. Sampling may miss short calls. A passing test establishes the behavior its assertions checked.
- Patch proposals require current intent evidence, a matched failing-to-passing reproduction, and passing checks for preserved behavior. Proposals cover text edits to existing files. Review the diff and test coverage before applying it to the requested checkout.
- Docker or configured rootless Podman experiments require an eligible local image with the needed tools and dependencies. Container networking is disabled. OpenClaw tool policy and allowed checkout roots govern access.

## Portable localization benchmark

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/benchmark_localization.py
```

The three authored Python, TypeScript, and Go cases test file-ranking mechanics.

| Ranker | Top-1 | Mean reciprocal rank |
| --- | ---: | ---: |
| Octane heuristic | 1.00 | 1.00 |
| Traceback-first | 1.00 | 1.00 |
| Recent-change-first | 0.00 | 0.00 |

The labels and traces were authored with the fixtures. These scores apply to the included cases. Assess bug-fix accuracy separately with representative defects and independent expected behavior.

## Validation

See the [project review](project-review.md) for regression coverage and GitHub Actions results. The [native Docker demonstration](openclaw.md#reproduce-the-native-demonstration) exercises a failing reproduction, an overbroad patch that breaks documented behavior, and a smaller candidate that passes both checks.

## Integration verification boundaries

The full integration roadmap is implemented as optional, explicit adapters. The [integration guide](integrations.md) distinguishes live validation from mocked transport tests. Semantic Python/TypeScript imports, MCP stdio calls, offline Ed25519 signatures, Docker report capture, mutation reversion, bisect, and property probes have executable regression checks. Podman and gVisor policy/argument tests run without those backends; live deployment checks remain necessary. GitHub write transport is tested without sending third-party comments. No hosted remote sandbox, live telemetry subscription, full-language semantic analysis, or autonomous SWE-bench solver is claimed.

The [public Blinker replay](evaluation.md) exercises one historical maintainer patch with authored reproduction and preservation probes. It validates execution/evaluation mechanics and provides no general repair accuracy estimate. Imported reports are unverified; runtime reports bind to their recorded run. A mutation score is bounded to selected operators. Signed statements prove the trusted key signed the recorded claim, not that its semantic conclusions were correct.

## 0.2.0 validation changes

Build-file overrides are distinct from pure test-file exceptions. Recognized structured test settings remain fixed, while XML/Gradle formats record an opaque text review and require independent inspection of test selection. Unsupported mutation languages report unavailable with no score; partial support and timeouts are explicit. [Follow-up review](follow-up-review.md).

SARIF export preserves recorded verification/stale status. Language servers receive an explicit environment allowlist and bounded failure diagnostics. MCP identity is derived from self-reported handshake metadata and is not authentication. `verify-provenance --rebuild` checks signed ledger fields; predicate v2 covers more recorded payloads than historical v1. Keyless OIDC signing through external Cosign was not exercised live; its verification adapter has controlled transport/policy checks.

## 0.2.1 enforcement

The complexity gate now covers all production Python, with a non-growing baseline for existing functions above 30. Average and maximum scores fall; total project complexity does not. The [polish review](polish-review.md) and changelog report measured values and remaining debt.

A minimal fake LSP peer exercises host transport and semantic paths without external providers, and CI enforces 85% language-server coverage. SARIF lists IDs through a public, bounded store API. Cosign verification requires 3.0.5+ and can take an operator-authenticated local TrustedRoot JSON; complete offline bundles and independently prepared trust material are required. Live keyless signing and Podman/runsc deployment testing remain separate.

## 1.0.0 diagnosis and final validation

The [accuracy workflow](accuracy-workflow.md) documents ordered Python numeric traces, language-neutral imported spectra/ordered traces, flaky-test exclusion, analyzer attribution, low-confidence syntax patterns, test ordering, operator-owned holdout checks, current-proposal ranking, and exact paired-median optimization decisions. The same twelve tools expose them. The executed seeded harness checks mechanics; broad real-world accuracy and the secondary research proposals remain unmeasured or unimplemented.
