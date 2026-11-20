# Changelog

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
