# Integrations and controlled experiments

Octane uses one set of twelve tool schemas in OpenClaw and MCP. The host agent chooses a hypothesis and candidate patch. Octane records source relationships and runs explicit checks; it makes no model calls and grants no network access to project commands.

## Stdio MCP

From this checkout, run `npm ci --omit=peer`. Save an operator-owned configuration outside the analyzed repository:

```json
{
  "allowedProjectRoots": ["/srv/projects"],
  "allowedDockerImages": ["python:3.12-slim"],
  "pythonExecutable": "/srv/octane/.venv/bin/python",
  "databasePath": "/srv/octane-state/pil.sqlite3",
  "workspaceRoot": "/srv/octane-state/workspaces",
  "workspaceDir": "/srv/projects/example"
}
```

Configure an MCP client to launch this process:

```json
{
  "mcpServers": {
    "octane": {
      "command": "node",
      "args": ["/srv/Octane/mcp/server.js", "--config", "/srv/octane-config.json"]
    }
  }
}
```

The installed npm executable is `octane-mcp`. Optional `allowedTools` restricts the exposed tool names; omission exposes all twelve. Project containment, approved images, permission-scoped actors, immutable image IDs, and sandbox execution use the same Python bridge as OpenClaw. MCP derives a stable sanitized actor label from handshake client name/version, with a hash to distinguish names that sanitize alike. An absent client identity falls back to `mcp-stdio`. Optional `mcpSessionSuffix: true` adds a fresh process/session UUID; a short safe string selects an operator-provided suffix. Client metadata is self-reported attribution and grants no additional permissions; tool arguments cannot select the runtime actor. Protocol messages alone go to stdout. No HTTP server is started. SDK client tests cover tool discovery, real calls, invalid arguments, and policy denial. Individual MCP hosts have not all been certified; configure their request timeout to cover long mutation, bisect, or evaluation calls.

## Semantic Python and TypeScript relationships

Install language servers in an operator-controlled directory, outside project trees:

```sh
npm install --prefix /srv/octane-lsp pyright@1.1.414 typescript-language-server@6.1.0 typescript@5.9.3
```

Add to the OpenClaw plugin configuration or MCP configuration:

```json
{
  "languageServers": {
    "python": {
      "command": ["/srv/octane-lsp/node_modules/.bin/pyright-langserver", "--stdio"],
      "environmentAllowlist": ["XDG_CACHE_HOME"]
    },
    "typescript": {
      "command": ["/srv/octane-lsp/node_modules/.bin/typescript-language-server", "--stdio"],
      "initializationOptions": {
        "tsserver": {"path": "/srv/octane-lsp/node_modules/typescript/lib/tsserver.js"}
      }
    }
  }
}
```

`scan` and working-copy `focus` request document symbols and outgoing call hierarchy. Successful semantic results replace candidate call edges for the analyzed callers. Edges carry `basis: "LSP call hierarchy"` and the provider. Missing, unsupported, or timed-out providers retain explicitly labeled static fallback and coverage gaps. Providers have bounded requests and output, including a deadline when a server stops reading input; POSIX cleanup terminates its private process group. The server environment defaults to PATH, HOME, LANG, LC_ALL, TMPDIR, TMP, TEMP, SYSTEMROOT, and WINDIR when present. Per-provider `environmentAllowlist` explicitly forwards additional variable names (up to 32), without accepting secret values in plugin config. Bounded stderr diagnostics retain the last 8 KiB and are scrubbed before returning failure details. Servers are trusted host executables configured by the operator, may read project configuration, and are separate from project execution containers. Install and configure them only for trusted analysis environments. Octane does not discover executable commands from repository files. Dynamic calls, generated code, foreign-language boundaries, and incomplete server analysis can still leave gaps. Live tests resolve Python and TypeScript imports across files.

## Structured reports and per-line coverage

`octane_evidence` action `import` takes `projectId`, project-relative `reportPath`, and `reportFormat`. The native CLI equivalent is:

```sh
python3 -m octane native import-report PROJECT_ID reports/junit.xml --format junit --database /srv/octane-state/pil.sqlite3
```

Supported formats:

| `reportFormat` | Input |
| --- | --- |
| `junit`, `pytest`, `go-test`, `jest` | JUnit XML, pytest-json-report JSON, `go test -json` JSONL, Jest JSON |
| `coverage`, `go-coverage` | coverage.py JSON with executed/missing lines, Go coverprofile |
| `sarif` | SARIF results from tools such as CodeQL, Semgrep, Ruff, or Bandit |
| `mutmut`, `stryker`, `pit` | Mutant/status JSON, Stryker JSON, PIT XML |
| `sentry`, `otel` | Sentry event JSON, OTLP JSON resourceSpans |
| `github` | GitHub issue JSON |

Imports are unverified evidence. They do not certify that a check ran on this candidate. Runtime runs can capture one report by setting `reportFormat` and writing it to the exact `OCTANE_REPORT_PATH` environment variable. For example, in an approved image containing pytest:

```json
{
  "action": "run", "workspaceId": "WS-…", "mode": "experimentation",
  "image": "my-reviewed-pytest-image", "reportFormat": "junit",
  "command": ["python", "-B", "-c", "import os,pytest; raise SystemExit(pytest.main(['-q','--junitxml='+os.environ['OCTANE_REPORT_PATH']]))"]
}
```

The report file is a bounded output mount outside the source tree. Requested reports that are absent or malformed cannot qualify for a proposal. When a focused report identifies failing tests, the candidate report must show those same IDs passing. Paired runs must use the same report format. Captured line coverage adds `covered_changed_lines` and uncovered changed lines to the proposal's scope evidence; inspect these before accepting a repair. One run captures one format; use separate checks when both test results and coverage are needed.

Reports are limited to 4 MB and 10,000 top-level records. Coverage line expansion is bounded. XML entities and document types are rejected; mapped source paths stay within the project. Sentry imports retain exception type and source frames; OTEL imports retain span identity, name, status, and source path. Arbitrary request, user, and resource attributes are omitted. These are file imports, not live telemetry subscriptions.

SARIF export is available through `octane_evidence` action `sarif` (`projectId`, optional `evidenceIds`, optional `limit`), or the native CLI:

```sh
python3 -m octane native export-sarif PROJECT_ID --database /srv/octane-state/pil.sqlite3 --output /srv/reports/octane.sarif.json
```

Exported SARIF 2.1.0 contains recorded findings, bounded source locations, and properties for evidence ID, revision, verification status, and staleness. It preserves the evidence's verification level and does not promote imported findings into independently verified defects. No automatic code-scanning upload is performed.

## Original checks and mutation experiments

Candidate executions overlay baseline test directories, fixtures, and pure test configuration as read-only mounts. Every run records their fingerprint. Proposals reject test-like edits by default. `allowTestChanges: true` requires nonempty `testChangesRationale` and records affected paths; the candidate still runs against the original checks. Newly added tests belong in a separately reviewed baseline; the proposal gate supports text edits to existing files. A filename classifier cannot recognize every embedded assertion or custom test convention. Keep the independent reproduction and intent evidence explicit.

Build/configuration files are reviewed separately. Set `allowBuildChanges: true` and a specific `buildChangesRationale` on **each candidate run and the proposal**. JSON, TOML, and INI files produce parsed changed-key diffs; the proposal binds these to the recorded candidate executions. Recognizable test scripts, test-runner options, and test-target controls cannot be changed by this override. Ordinary dependency and build metadata edits can take effect in the candidate. Files under test directories remain pure tests even when named `package.json`.

`pom.xml` and Gradle formats currently receive an explicit text-diff review record rather than a structural key diff. Inspect their test selection independently and compare actual executed test IDs or traces. The override is recorded permission for the reviewed change; it cannot prove arbitrary build scripts preserved their checks. Malformed supported structured formats are rejected, rather than silently treated as opaque text.

After a passing candidate run tied to a current investigation:

```json
{"action":"mutate","workspaceId":"WS-…","candidateRunId":"RUN-…","mutationLimit":5}
```

Mutation experiments run in disposable snapshots using the candidate's immutable image, original checks, command, and trace/report settings. The bounded built-in operators revert supported changed source and mutate Python constants/comparisons/arithmetic or JavaScript/TypeScript syntax-tree operators and numeric literals. JS/TS uses the existing optional `ast` parser extra on the host; actual project checks still run inside the sandbox. The result records overall `completed`, `partial`, `unavailable`, or `inconclusive` status, per-path support, killed/survived/inconclusive outcomes, score, individual run IDs, and whether tested source reversions reproduce the failure. Attach the returned evidence ID to the proposal's `evidenceIds`. The gate validates candidate/image/test bindings and requires the tested original-source reversions to be detected. Unsupported source languages (including Go) produce `unavailable` and no score. A timed-out or otherwise inconclusive experiment also has no overall score. Mixed-language support is explicitly partial. Proposals report support even if no mutation run was attached. The score covers only the selected supported mutants, not all paths or all changed files. External mutmut/Stryker/PIT imports remain separately attributed report evidence. No-coverage mutants count as surviving; unknown/timed-out imported outcomes make the overall imported score inconclusive.

## Property and differential preservation checks

`octane_workspace` action `property` runs generated probes in the approved container; dependencies must already be installed in its image. Python uses Hypothesis and Node uses fast-check. An explicit project-local function, reference, input strategy, and rule define the contract:

```json
{
  "action":"property", "workspaceId":"WS-…", "mode":"experimentation",
  "image":"my-reviewed-hypothesis-image", "investigationId":"INV-…",
  "propertySpec":{"module":"normalizer","function":"normalize","rule":"idempotent","strategy":"integers","examples":100}
}
```

Rules are `idempotent`, `roundtrip`, and `differential`. The latter two require `reference` and optionally `referenceModule` in Python. Node uses `runtime: "node"`, `file`, and optional `referenceFile`. Strategies are bounded integers, text, and integer lists; examples range from 1 to 500. Python is deterministic without an example database; Node uses a fixed seed. Run the identical specification against baseline and candidate for preservation pairing. Reference behavior must have independent justification. These checks do not invent a program's intended behavior.

## Git bisect

```json
{
  "action":"bisect", "projectId":"PRJ-…", "goodRevision":"KNOWN_GOOD",
  "badRevision":"KNOWN_BAD", "command":["python","-B","check.py"],
  "image":"my-reviewed-image", "bisectBudget":20
}
```

Native archive/apply/bisect calls require Git 2.32+ (the release that introduced `GIT_CONFIG_GLOBAL`). Host Git receives a minimal environment, disables user/system configuration and terminal credential prompts, and overrides fsmonitor, hooks, and the pack-objects hook. Portable checkout Git calls use the same environment and command overrides. Git metadata and safe source archives are handled on the host in temporary storage. Every project check runs in the configured container, with bad-revision baseline tests overlaid across revisions and one pinned image. The good endpoint must exit 0 and the bad endpoint 1. Other exits, timeouts, and cleanup failures are skipped. Completion recognizes both older Git output and the quoted bad-term format used by newer Git. The result reports the first bad commit only when the search completes; ambiguous histories and exhausted budgets remain incomplete. Builds must fit the explicit command and offline image. No worktree is checked out over the user's project. Native CLI: `octane native bisect --help`.

## GitHub evidence and publication

Install and authenticate GitHub CLI separately on the host. Read-only issue and workflow-log ingestion:

```sh
python3 -m octane native github PROJECT_ID owner/repository --issue 149 --database /srv/octane-state/pil.sqlite3
python3 -m octane native github PROJECT_ID owner/repository --workflow RUN_ID --database /srv/octane-state/pil.sqlite3
```

Publication is an explicit operator CLI action, excluded from the model-facing tool schemas:

```sh
python3 -m octane native github PROJECT_ID owner/repository --pull-request 12 --evidence EV_ID --publish --database /srv/octane-state/pil.sqlite3
```

`--check-commit FULL_SHA` instead creates a neutral Check Run; credentials need repository Checks write permission. Without `--publish`, publication is refused. Only selected same-project evidence is summarized and publication is audited. Inspect the summary and obtain any authorization required by the host before sending it. Octane does not automatically comment on another project's issues or PRs. Transport output and deadlines are bounded; this adapter's write operations are tested against canned responses, not live third-party posts.

## Signed patch provenance

Install `python3 -m pip install '.[provenance]'` to sign and verify offline. Export a proposal's in-toto statement, optionally signing a DSSE envelope with an operator-owned Ed25519 PEM key:

```sh
python3 -m octane native provenance PROJECT_ID PROPOSAL_EVIDENCE_ID --database /srv/octane-state/pil.sqlite3 --key /srv/keys/signing.pem --output /srv/attestations/patch.dsse.json
python3 -m octane native verify-provenance /srv/attestations/patch.dsse.json --public-key /srv/keys/trusted-public.pem --patch-sha256 EXPECTED_DIFF_SHA256
```

Without `--key`, the export is an unsigned statement. The subject binds the exact patch bytes; the predicate records source/candidate hashes, image ID, execution statuses and command hashes, and proposal-evidence hash. `octane_evidence` action `attest` exports only an unsigned statement. Private keys are never tool parameters. Verification requires a separately selected trusted public key and detects altered payloads, wrong keys, and unexpected patch digests. This is in-toto/DSSE with a custom Octane predicate, not independent proof that the recorded observations were true.

Add `--rebuild --database /srv/octane-state/pil.sqlite3` to verification to re-derive the signed statement under a consistent ledger transaction. Verification fails when signed runs, results, proposal evidence, or referenced observations disagree with the ledger or are missing. New predicate v2 binds result JSON and observation payload digests. Historical predicate v1 remains verifiable within its original narrower signed fields; it did not sign full result/observation payload digests. Missing trust databases are refused rather than initialized.

Signing keys belong in operator-owned private files outside project roots (for example mode 0600 under a private directory). For encrypted PEM keys use `--key-password-prompt`, or `--key-password-env NAME` to read a password from an explicitly named host environment variable. Password values are never command arguments, model tool inputs, or evidence. Rotate by selecting a new trusted public key and keeping old public keys for historical envelopes; stop new signing with retired private keys. Octane does not maintain a global key revocation registry.

### Optional keyless Sigstore workflow

Install a trusted Cosign release **3.0.5 or newer** (the adapter enforces this minimum with `cosign version --json` and rejects unknown/prerelease builds). Keep newer security fixes applied. Export an unsigned Octane statement, then use Cosign directly for keyless signing and its OIDC interaction:

```sh
python3 -m octane native provenance PROJECT_ID PROPOSAL_EVIDENCE_ID --database /srv/octane-state/pil.sqlite3 --output /srv/attestations/statement.json
cosign sign-blob /srv/attestations/statement.json --bundle /srv/attestations/statement.sigstore.json
python3 -m octane native verify-provenance /srv/attestations/statement.json --sigstore-bundle /srv/attestations/statement.sigstore.json --certificate-identity EXPECTED_EXACT_IDENTITY --certificate-oidc-issuer EXPECTED_EXACT_ISSUER --rebuild --database /srv/octane-state/pil.sqlite3
```

Record the exact signer certificate identity and OIDC issuer alongside the bundle; obtain them from the authenticated signing identity/provider, not a claim copied from an untrusted bundle. For a GitHub Actions signer, the expected identity is the full workflow URI and ref (for example `https://github.com/OWNER/REPO/.github/workflows/release.yml@refs/tags/v0.2.1`) and the issuer is `https://token.actions.githubusercontent.com`. Interactive providers have their own exact identity and issuer; use the certificate and provider documentation to set the trust policy. Changing either policy value must cause verification to fail.

Default verification may need host networking to retrieve Sigstore's authenticated trust root through TUF. Before going offline, prepare a complete signature bundle and an independently authenticated, current TrustedRoot JSON in an online trusted environment. Transfer those artifacts and record their hashes through your trusted deployment process. Then use:

```sh
python3 -m octane native verify-provenance statement.json --sigstore-bundle statement.sigstore.json --certificate-identity EXPECTED_EXACT_IDENTITY --certificate-oidc-issuer EXPECTED_EXACT_ISSUER --trusted-root /srv/trust/trusted_root.json
```

`--trusted-root` is passed to Cosign over a frozen local copy. With a complete modern bundle, this avoids the default trust-root retrieval; the offline host should independently disable outbound networking. If verification requires missing proof or trust material, fail and prepare that material online rather than using insecure-ignore flags. Trust-root freshness and rotation belong to the operator; a root file from the same untrusted source as the bundle is not an independent trust anchor. [Cosign 3.0.5 verification flags](https://github.com/sigstore/cosign/blob/v3.0.5/doc/cosign_verify-blob.md), [version command](https://github.com/sigstore/cosign/blob/v3.0.5/doc/cosign_version.md).

The verification adapter uses Cosign `verify-blob` over frozen bounded copies, requires an exact identity and issuer, and retains standard trust and transparency-log verification. It provides no insecure-skip or identity-regex shortcuts. Host trust-root retrieval may use networking; project execution containers remain offline. Signing may publish certificate identity and artifact hashes to public transparency infrastructure and is an explicit operator action. The adapter has policy/transport regression tests using controlled responses; a live OIDC signing session has not been validated here. [Cosign blob signing](https://docs.sigstore.dev/cosign/signing/signing_with_blobs/), [verification](https://docs.sigstore.dev/cosign/verifying/verify/).

## Rootless Podman and gVisor

Default execution uses Docker. Set `containerEngine: "podman"` for an installed rootless Podman engine; Octane rejects rootful Podman and uses `--userns=keep-id`. Set `containerRuntime: "runsc"` only after configuring gVisor in that engine. Both retain disabled networking, resource limits, capability dropping, read-only roots, original-test overlays, and local image inspection. Engine/runtime are recorded and must match across paired checks. On platforms without host UID/GID mapping, execution is refused unless the operator explicitly enables `allowDefaultContainerUser`; that choice is recorded.

Docker was tested live on Linux. Podman and runsc argument/policy tests pass, but neither backend was installed on the validation host. Validate mount permissions, cgroups, image identity, and cleanup in your deployment. There is no remote sandbox implementation and no host execution fallback.

## Public historical evaluation

The local JSONL adapter accepts `instance_id`, `base_commit`, supplied `patch`, optional independent `test_patch`, focused `test_command`, and `preservation_command`. Standard SWE-bench-style `FAIL_TO_PASS` and `PASS_TO_PASS` lists or JSON strings map to pytest commands. Supply an offline image with all dependencies. It does not download SWE-bench datasets or generate patches.

```json
{"action":"evaluate","projectId":"PRJ-…","datasetPath":"cases.jsonl","image":"my-reviewed-image","caseLimit":5}
```

Each case exports the specified Git revision, installs independent tests before snapshotting, and runs failing baseline, passing baseline preservation, passing candidate, and passing candidate preservation. All use one immutable image and original test fingerprints. Source-mutating checks cannot count as resolved. Optional `expected_files` labels produce MRR and top-1 localization metrics; absent labels produce no localization claim. Errors and unsuccessful cases remain visible. Archived run evidence records its source commit separately from the registered project's indexed revision.

The November 20 validation replayed one real historical Blinker fix: [maintainer commit fc53709](https://github.com/pallets-eco/blinker/commit/fc537092961d79731ac8a4e020e852076d7c6256), restoring the deprecated WeakNamespace import with one line. The focused import check failed on parent `0068cb4ce6a8d63f6628ba0a02450b7bae5110e9`; it passed after the maintainer patch. Signal connect/send/disconnect preservation passed before and after. Localization ranked `src/blinker/base.py` first. The probes were authored for replay and the patch came from the maintainer; this is harness validation, not autonomous discovery or a measured general repair rate. See [reproduction and recorded result](evaluation.md).

## Primary protocol references

- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
- [LSP 3.17 specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/).
- [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md).
- [Podman run and user namespaces](https://docs.podman.io/en/latest/markdown/podman-run.1.html).

- [Git bisect completion implementation](https://github.com/git/git/blob/master/bisect.c).

- [Git 2.32 configuration environment changes](https://github.com/git/git/blob/v2.32.0/Documentation/RelNotes/2.32.0.txt).

## Evidence-driven diagnosis (0.3.0)

See the [accuracy workflow](accuracy-workflow.md) for `spectrum` and `ordered-trace` import schemas, SARIF tool attribution/fusion, existing-tool analysis actions, private operator holdout policy, candidate ranking and recorded performance comparison. Imported reports remain unverified. This extension does not install analyzer binaries or infer per-test coverage from aggregate reports.
