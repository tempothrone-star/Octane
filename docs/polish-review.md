# PR #12 closure and polish

This follow-up addresses the closure report for merged commit `7ef5590`. Version 0.2.1 keeps the twelve tools, one skill, existing database formats, and protected execution workflow.

## Global complexity ratchet

`python scripts/check_complexity.py --metrics` examines every Python source file under `octane/` and `scripts/`, including functions, methods, and nested functions. It excludes tests, generated build output, dependencies, and aggregate class scores. Repeated definitions receive distinct occurrence suffixes so one cannot hide another. `scripts/complexity-baseline.json` contains only existing scores above 30. A new function above 30 or any growth beyond an exception fails. Removing or reducing an exception requires deleting or lowering its baseline entry.

Regression tests exercise newly introduced source files, methods, nested functions, repeated names, increasing baseline scores, and stale baseline entries. The gate does not write or automatically increase its baseline.

The highest hotspot, `_heuristic_imports`, is split into language-specific recognition and fallback helpers, with no helper above 30. Its dispatcher drops from 91 to 4. `_candidate_paths` drops from 47 to 27; `_mutation_evidence` drops from 41 to 21 with a separate run-check helper at 21. Tests protect supported language declarations, ordering, and source locations. They also exposed and fixed whitespace-induced incorrect line numbers in C/C++ and multiline import fallbacks.

Nine existing exceptions remain: CLI main (37), Tentacle.explore (40), InvestigationWorkflow.explore (38), semantic_calls (31), Project.brief (35), tokenizer (36), dependency-graph construction (33), suspect ranking (35), and a nested Python resolver visitor (40). These are explicit maintenance debt and cannot grow unnoticed.

The [changelog](../CHANGELOG.md) records before/after totals with Radon 6.0.1 using the same scope and counting rules. The average and maximum improved while total project complexity grew. The matched legacy adapter lost only one per-function decision point; the database store extraction preserved the same method count and aggregate complexity. These were principally maintainability splits, not a claim of large algorithmic simplification.

## Host integrations and public boundaries

A tiny fixture server implements Content-Length framing, initialization, document symbols, outgoing calls, callbacks, ignored notifications, and failure modes. It tests successful semantic edges, planted-secret exclusion, stderr bounds/redaction, malformed headers, oversized/incomplete frames, server errors, early exit, and timeouts without external language servers. Real Pyright/TypeScript tests remain separate. The no-real-provider language-server coverage gate is 85%; the local protocol suite reaches 95%. The reader now consumes buffered responses through EOF even if a provider has just exited.

SARIF uses public `ProjectIntelligence.list_evidence_ids` rather than its transaction internals. The method validates bounded limits and optional types, isolates projects, supports a requesting agent's read permission, and preserves stable ordering and the existing inclusion of stale evidence. Export tests assert this boundary.

The Tree-sitter exclusion and Ruff compatibility comment requested in the report were already present in merged PR #12. They remain in effect; no incompatible parser update is enabled. `B904` was already clean under the B rules. This release clears and explicitly enables `UP034` (redundant parentheses), leaving broad strict/style rules disabled.

## Sigstore

The verifier checks an installed Cosign release version of at least 3.0.5. Older, prerelease, unknown, malformed, or failed version responses are refused. Exact signer identity and issuer are passed to verification; tests reject mismatches. `--trusted-root` supplies frozen, bounded operator-authenticated trust material for complete offline bundles. The [integration guide](integrations.md) provides the signing recipe, signer metadata, and trust preparation requirements.

Cosign 3.0.5's actual version output and CLI flags were checked against the checksum-verified release binary. Adapter policy tests use controlled verification responses; they do not claim a live OIDC signing session or independent cryptographic verification by a fake peer. There is no network access added to project execution containers. Podman/runsc deployment validation remains separate.

## Local validation

- Full suite: 219 tests passed, 12 expected optional/integration skips. The separate Docker/proposal/mutation suite passed 63 tests. These runs overlap and are not additive test counts.
- Overall coverage: 82% in the local environment with the optional parser installed. Proposal gate, private storage, and original-test protection stay at 100%; language-server coverage is 96% in the full suite and 95% in the focused fake-peer suite.
- Node/MCP/package checks: six passed. Python 3.9 checkout CLI: 46 passed. The production complexity ratchet and curated Ruff rules passed.
- OpenClaw 2026.11.3 loaded twelve tools and one eligible Octane skill. The 0.2.1 wheel passed native registration/scan and local ledger initialization from a clean environment.
- The minimum-version parser accepted actual checksum-verified Cosign 3.0.5 output. OIDC signing remains an explicit operator workflow and was not performed.

Hosted CI is checked on the resulting PR; the figures above describe the local validation environment and its actual optional dependencies.
