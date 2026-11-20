# Follow-up to PR #6

This release addresses the nine-item review of merged commit `ffbb45e`, retaining one Octane project and twelve tool names. The earlier review in `project-review.md` records the previous snapshot; this document describes the 0.2.0 changes.

| Review item | Result |
| --- | --- |
| Build-file guard | Pure tests remain read-only. Build edits require a run/proposal override and justification; parsed changed-key evidence is bound to the candidate runs. Recognized test settings cannot be weakened by the build override. |
| Language-server environment | Minimal inherited variables, explicit per-provider extension, bounded scrubbed stderr. A fake server verifies a planted secret is absent. |
| Host Git | Minimal environment, disabled user/system config, hooks/fsmonitor/pack hook overrides, Git 2.32+ for native archives/apply/bisect. Portable checkout Git calls use the same reduced environment. |
| Mutation capabilities | Explicit per-path support and unavailable/inconclusive statuses, plus actual JS/TS syntax operators. No unsupported-language passing score. |
| Provenance rebuild | Consistent ledger re-derivation after signature verification, with result/evidence hashes in predicate v2 and historical v1 compatibility. |
| MCP identity | Client name/version-derived actors and optional session suffix; fixed fallback. Distinct SDK clients write evidence under different actors. |
| Coverage | Higher overall and module floors; Docker execution/mutation measured separately. |
| Complexity | Reviewed targets: PythonAdapter.scan 25, WorkspaceManager._docker_run 30, ledger CLI _run 20, repair_focus 17, ProjectScanner.scan 29. CI enforces each at 30 or below. |
| Publication metadata | Version 0.2.0, public package description, Python lint-target explanation, changelog and release tag. |

Database graph, evidence, and investigation stores share the existing transaction machinery through separate modules. Evidence/memory bridge operations are also separated from workspace handlers. No data reset or new database schema version is required.

The integration audit confirmed existing GitHub issue/CI import and explicit publication, Sentry/OTEL imports, and historical-case evaluation. SARIF output was added alongside existing import. Keyless signing uses externally installed Cosign to sign the exported statement; Octane's optional Cosign verification adapter applies an exact identity/issuer policy and can rebuild the ledger statement afterward. This does not initiate model-facing publication or weaken sandbox networking.

Read [integration configuration](integrations.md) before enabling providers. Docker, live language servers, and offline cryptographic checks are tested locally. Podman/runsc still need deployment validation. The Cosign adapter's transport and policy checks are tested with controlled responses; a live OIDC signing session was not performed.

## Validation before publication

The comprehensive local run passed 200 tests with two expected dependency-absence skips, with Docker, the optional parser, live Python/TypeScript language servers, and cryptography enabled. The isolated quality environment passed 198 tests with 13 integration/dependency skips. An additional nine-test hardening run passed after adding deterministic checks for existing SQLite sidecar permissions. All 46 Python 3.9 checkout CLI tests and six Node/MCP tests passed.

Coverage measured 78% overall in the isolated quality run; the proposal gate, original-test protection, and storage helpers reach 100%. Provenance is 96% and bounded host transport 92%. The Docker run measures workspace execution at 80% and mutation at 96%. The new CI floors are separately enforced per module.

OpenClaw 2026.11.3 loaded twelve tools and found one eligible Octane skill. A clean installation of the 0.2.0 wheel passed native registration/scanning and project-local ledger initialization. Package checks include the changelog and new integration modules. Live Podman/runsc and OIDC signing remain outside these results; the adapters retain the explicit verification limits described above. Hosted CI results belong to the resulting PR and must be checked at its final commit.
