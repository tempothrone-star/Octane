# Changelog

## Unreleased — audit hardening

- Share bounded-pass redaction across both stores; cover quoted JSON/assignment values, Basic authentication, Slack/Google keys and megabyte blobs.
- Name/label container jobs and add SIGTERM cleanup, independent crash deadlines and owned expired-run recovery. Replace the trace-only 512 KiB process-wide file cap with a documented 64 MiB per-file cap.
- Isolate resource-exhausted Python files as parse errors, require UTF-8 XML without entity declarations, and bound stack-frame matching while preserving JS symbols.
- Require explicit trusted-project opt-in for host language servers, report queue overflow promptly, and signal process groups before reaping their leader.
- Check ledger anchor/tail during append, retain explicit full verification, and skip schema write transactions when opening current stores.
- Log skipped archive symlinks, include added/deleted regular text files in proposals, and default MCP attribution to fresh connection IDs.
- Migrate to bundled `tree-sitter-language-pack==0.13.0` / `tree-sitter==0.25.2`; add Python 3.13/3.14 parser checks and Windows/macOS portable-host CI. Keep package metadata at 1.0.0 pending a release decision.


## 1.0.0 — 2026-11-20

- Compare bounded ordered Python numeric traces and import ordered traces/per-test spectra from other runtimes. Record first observed divergence and Ochiai line ranking with explicit flaky/unstable exclusions.
- Attribute and fuse SARIF findings by failing/changed-path proximity. Add reviewed Tree-sitter query hypotheses with Python AST fallback, source hashing and bounded content caching.
- Order impacted tests first while retaining the required full suite. Rank current gate-qualified proposals by masking warnings and diff size.
- Require an optional operator-owned withheld suite before bridge proposals. Freeze/mount it read-only, bind the current candidate/image, and withhold command output before persistence; include validation metadata in the proposal payload.
- Add a paired optimization decision with exact >=95% median intervals, variance, preserved output and distinct recorded executions.
- Run seeded localization, holdout and latency acceptance checks in Docker CI. Publish measured scope/limits in [accuracy workflow](docs/accuracy-workflow.md). Public-history evaluation now includes top-5 and first-hypothesis timing; execution errors count as localization misses.


## 0.2.1 — 2026-11-20

- Replace the five-function complexity check with a production-wide Python gate and committed non-growing baseline. New functions above 30 fail; reduced exceptions must be ratcheted down. Methods, nested functions, and repeated names are included without class-score double counting.
- Split import recognition by language (91 -> 4), candidate resolution (47 -> 27), and mutation-evidence validation (41 -> two functions at 21). Correct C/C++ and multiline fallback import line locations discovered by new behavior tests.
- Add a minimal protocol-speaking fake LSP peer, timeout/framing/cleanup and semantic graph tests, and an 85% language-server coverage gate. The fake-server suite measures 95% without real providers.
- Require Cosign 3.0.5+ at verification time, test exact identity/issuer mismatch rejection, and support an operator-supplied trusted root. Document signing identity, issuer, trust-root preparation, and offline failure handling.
- Use public `list_evidence_ids` for SARIF export with bounded project/type/permission-aware queries.
- Retain and verify the Tree-sitter Dependabot exclusion and intentional Ruff Python 3.9 comment. Clear redundant parentheses (`UP034`) and enforce that rule; `B904` was already clean and covered by the existing B rules.

Complexity comparison uses Radon 6.0.1, the same production scopes (`octane/**/*.py`, `scripts/**/*.py`), functions/methods/nested functions, and no aggregate class scores:

| Snapshot | Functions | Total CC | Mean CC | Sum of (CC − 1) | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before PR #12 (`ffbb45e`) | 411 | 3238 | 7.8783 | 2827 | 91 |
| After PR #12 (`7ef5590`) | 447 | 3461 | 7.7427 | 3014 | 91 |
| 0.2.1 polish | 468 | 3515 | 7.5107 | 3047 | 40 |

These measurements show lower mean and peak complexity, **not a reduction in total project complexity**: the releases also add functionality and helper functions. For the matched PythonAdapter class, the earlier refactor changed 2 functions / total 69 / mean 34.5 to 7 functions / total 73 / mean 10.4286; the sum of (CC − 1) fell only from 67 to 66. Native database methods across the original file and extracted stores remained 57 / total 341 / mean 5.9825, so that split moved code by concern rather than removing decisions. The new public listing method adds one method and ten CC in this release. [Measurement method and remaining baseline](docs/polish-review.md).

## 0.2.0 — 2026-11-20

### Repair validation

- Separate build configuration from pure test files. Build edits require `allowBuildChanges` and `buildChangesRationale` on candidate execution and proposal calls; evidence records parsed changed keys where supported. Recognizable test settings remain protected. Opaque build formats require independent review of test selection.
- Prevent imported no-coverage or timed-out mutation results from inflating a passing score.
- Report mutation support per changed source path. Unsupported languages, including Go, return `unavailable` and no score; timeouts remain inconclusive. Add bounded JavaScript/TypeScript syntax-tree operators through the existing optional parser.
- Rebuild signed provenance from the ledger, detecting changed runs, results, or referenced evidence. New predicate v2 binds result and observation hashes; v1 rebuild verification remains available for historical signed fields. Support encrypted PEM passwords through an environment-variable name or interactive prompt.

### Integration boundaries

- Scrub language-server environments, support explicit per-provider environment allowlists, and retain bounded, scrubbed stderr diagnostics.
- Use minimal environments and explicit config/hook/fsmonitor overrides for host Git calls. Native archives, patch application, and bisect require Git 2.32+.
- Attribute MCP calls to sanitized, stable client name/version identities, with an optional operator-selected session suffix. These are self-reported provenance labels, not authentication.
- Export attributed SARIF 2.1.0 results from recorded findings. Verify externally keyless-signed statement bundles through Cosign with exact signer identity and issuer policy and optional ledger rebuild.

### Maintenance

- Split database evidence/investigation stores and bridge evidence handlers without changing the twelve public tools or persistent store identity.
- Refactor the five reviewed orchestration hotspots and enforce a Radon complexity limit of 30 for them.
- Raise overall coverage to 75%; require 100% for the proposal gate, original-test protection, and private-storage helpers, and 90% for provenance and bounded host transport. Docker CI gates workspace coverage at 78% and mutation coverage at 95%.
- Publish consistent 0.2.0 metadata and clarify why checkout-only lint targets Python 3.9 while package installation requires Python 3.11+.

## 0.1.0

Unified Octane engine and skill, legacy ledger migration, constrained execution, persistent investigations, original-test overlays, MCP, LSP providers, structured evidence, mutation/property checks, Git bisect, offline signatures, sandbox adapters, and historical-case evaluation. See PRs #5 and #6 for the earlier implementation and validation.
