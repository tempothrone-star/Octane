# Project architecture

Octane is one OpenClaw plugin and skill backed by a Python package. Its tools and CLI share the source analyzer. The OpenClaw agent supplies reasoning.

| Component | Entry point | State |
| --- | --- | --- |
| OpenClaw tools | `openclaw/index.js` registers tools and `openclaw/bridge.js` invokes the Python 3.11+ bridge | A Gateway-side Project Intelligence Layer (PIL) database and disposable workspaces |
| Local evidence CLI | `scripts/octane.py` and `octane/ledger/` | A project-local `.octane/` ledger |

## Native project intelligence

- `octane/repair.py` suggests baseline checks from checkout metadata. `octane/focus.py` builds a bounded, read-only symbol view from the working copy. Both accept an allowed checkout before it has a saved index.
- `octane/scanner.py` builds the persistent index from source file hashes, import edges, and static call candidates. It reuses the portable dependency parser, adds Python lexical analysis, and can use Tree-sitter for other languages. The native database tracks revisions, evidence, hypotheses, graph history, and investigation checkpoints. Schema 5 adds saved exploration queues, execution records, and patch proposals.
- `octane/workspace.py` copies an allowed checkout and keeps an unmounted baseline. Observation commands mount the snapshot read-only. Experimentation runs and candidate edits use a writable copy. Docker receives an explicit command and an allowlisted local image. The run records the immutable image ID, exit status, output, snapshot hashes, and any requested trace.
- `octane/experiments.py` pairs the failing original check, passing candidate, and separate preservation checks. It binds the proposal to the current investigation, tested snapshots, and evidence of expected behavior. The proposal is a reviewable diff stored as evidence.

The Gateway supplies agent identity and tool permissions. Configure allowed checkout roots, keep database and workspace storage outside analyzed projects, and authorize Docker images through the [plugin setup guide](../openclaw/README.md). For a full repair example, see the [native workflow](openclaw.md).

## Portable ledger

The root `SKILL.md` describes the repair workflow and CLI fallback. `scripts/octane.py`, `python -m octane`, and the installed `octane` command use one dispatcher. Local evidence commands use `octane/ledger/`, which also provides the source and dependency analyzer used by native scans. `octane native` exposes Gateway database commands.

## Project layout and identity

Each analyzed root stores runtime state under `.octane/`:

- `ledger.db`: SQLite WAL database, schema version 1.
- `project.json`: project UUID, canonical root, and a last-committed log-head anchor.
- `config.json`: default trust tier and an initially empty execution allowlist reserved for later phases.

Initialization starts from the requested directory or the current working directory and uses the enclosing Git top-level when available. Ledger commands discover the nearest initialized project and check the canonical path and project UUID before reading or writing records. Static `languages`, `graph`, and `locate` commands also work on a checkout without an initialized ledger. Pass `init --update-gitignore` to add the state directory to `.gitignore`. Tests initialize temporary roots.

The revision recorded with evidence and claims is a SHA-256 digest of relative file paths and file bytes, excluding symlinks, `.git`, `.octane`, and common generated/cache directories. Hashing does not copy file contents into the database. Git is optional.

## Append-only ledger

Every portable ledger write appends an event containing sequence, project, agent, operation, canonical JSON payload, idempotency key, previous hash, and UTC timestamp. The hash is SHA-256 over the previous hash concatenated with canonical JSON for the event without its hash. The log has SQLite update/delete triggers; verification checks sequence continuity, project identity, predecessor links, event hashes, and the external `project.json` head anchor. `verify-log` detects edits, deletions, and reordering in tests.

The anchor is updated after a committed event. As this update spans a database and a JSON file, a process interruption can leave it behind the database head; a verified anchor prefix is accepted. This is tamper evidence, not tamper prevention: an operator with raw shell access can alter the database and the anchor. SQLite permissions are cooperative guardrails, not a security boundary.

Idempotency keys supplied by callers are honored. Otherwise the CLI derives a deterministic SHA-256 key from agent, operation, and canonical request payload. A retry returns the original event record; reuse of an explicit key for different content or a different agent is rejected. Explicit claim-status retries return the original transition even if a later transition has changed the current status; they never reapply the old state. Counters allocate `EV-0001` and `CL-0001` transactionally per project. Agent IDs use `AG-<name>`; initialization creates `AG-owner` with the local admin role.

Materialized project, agent, evidence, claim, link, and counter tables are written in the same `BEGIN IMMEDIATE` transaction as their event. `rebuild --check` replays supported phase-1 event types into an expected view and compares it to the live tables within one read transaction. Concurrent writers cannot produce a false replay mismatch. Log verification reads the external anchor before the event rows, accepting a verified prefix if another writer advances the log. It is a comparison only; a mutating rebuild is not implemented.

## Evidence and claims

Evidence and claims are distinct. Evidence is immutable and carries agent, type, description, structured payload, content hash, UTC time, revision, optional relative file/symbol/line range, method, verification status, and redaction count. Phase 1 accepts only `unverified` evidence; no command can mark evidence executed or replayed. Evidence links are append-only events.

Claims use the kinds `observation`, `inference`, `hypothesis`, `intent`, `decision`, `requirement`, `limitation`, and `open_question`. They cite supporting/contradicting evidence and dependencies, carry a revision and a derived ordinal evidence basis, and have no numeric confidence. Low-trust agents' claims enter as `quarantined` and are excluded from `brief`. Claim text remains immutable; `claim status` appends a status-transition event without rewriting the original claim. Content corrections still require a new claim and appropriate links. Only event reducers and CLI validators write the materialized tables; raw database access can bypass those cooperative rules.

Common secret-like keys and values are redacted during evidence and claim ingest. This is best-effort pattern filtering, not a credential scanner or guarantee; users should not supply credentials. `brief` ranks keyword matches, includes open questions as unknowns, excludes quarantined claims, and budgets the selected claim records using an estimate of characters divided by four. Its envelope, task text, duplicated unknowns, and pretty-print whitespace are outside that estimate. It is not semantic retrieval. Evidence and claim search use FTS5 when available, with SQLite `LIKE` candidate filtering and whole-term post-filtering otherwise. Evidence search includes descriptions, structured payloads, files, and symbols. Results and query sizes are limited; fallback candidate scanning is not bounded by the result limit, and Unicode tokenization can differ between backends.

## SQLite and filesystem

Connections enable foreign keys and a 5-second busy timeout. The database uses WAL and numbered transactional migrations. The CLI refuses a schema version newer than itself. `doctor` checks Python/SQLite versions, FTS5 availability, journal mode, log integrity, Git presence, and filesystem type where it can be detected. SQLite WAL is unsupported on some network filesystems; on platforms or mounts that cannot be identified, `doctor` warns rather than assuming local storage. Wall-clock timestamps are UTC ISO-8601; leases are not implemented yet, so clock-skew behavior remains future work.

The portable CLI reads source and saved traces locally. Running tests through OpenClaw follows the host's tool and sandbox policy. The native plugin's Docker execution path has its own image allowlist and workspace boundaries.

## Multilingual source triage prototype

`languages` walks a project without following symlinks, skips common generated/vendor trees, and classifies common source extensions (including Python, JavaScript/TypeScript, Java, Go, Rust, C/C++, C#, PHP, Ruby, Swift, Kotlin, SQL, HTML/CSS, and several others). It applies file-count and per-file-size limits, reports unsupported extensions, and does not read or parse source contents.

`graph` extracts best-effort internal file dependencies and identifies cross-language edges where a declaration resolves to a file with a different extension language. Python imports use the standard-library AST. By default, an optional offline Tree-sitter backend is used when installed for its bundled grammar set; otherwise language-specific recognizers and a generic line-oriented import fallback are used. Install the optional `.[ast]` extra and pass `--parser tree-sitter` to require this backend. Tree-sitter produces concrete syntax trees, not compiler-semantic ASTs; unsupported grammars fall back to heuristics. The graph observes file-count, per-file, and aggregate byte limits, skips symlinks through the inventory boundary, and emits unresolved references and parser coverage. It does not implement grammars for every language, complete call graphs, or runtime dependency resolution; matches may be false positives, and cross-language edges are not evidence of execution or causation.

`locate --trace-file` accepts an existing project-local text trace, parses common Python, JavaScript/TypeScript, Go, Java, Rust, and C/C++ file/line frames, and ranks matching source files using repeated/early frames, path query terms, and recent Git changes when Git is available. The output includes rationale and simple traceback-order/recent-change baselines. This is a file-level ranking heuristic only: it does not resolve functions, parse complete language grammars, replay a check, prove causation, or set evidence to verified. Trace paths are untrusted input, constrained to the project root, and never executed. Ranking is bounded by source-file count/size limits and does not persist scan results.

A three-case synthetic benchmark in `tests/benchmark_localization.py` reports top-1 and mean reciprocal rank against traceback-first and recent-change-first baselines. The cases check the ranking mechanics on authored fixtures. See the [implementation and limitations report](implementation-and-limitations.md) for results and scope.

## Portable scope

The portable CLI supports ledger operations and static file-level triage. Reopen source and rerun relevant checks before relying on old claims. Native investigation checkpoints, runtime traces, and patch proposals use the separate PIL store. The portable ledger has one numbered migration, with compatibility tables and full-text indexes managed at startup.

## Compatibility

New local ledgers use `.octane/ledger.db`. Existing `.octance/octance.db` records are opened in place, preserving project identity, idempotency keys, and the anchored event history. If both ledger locations exist, the CLI reports `project_conflict`. The former script filename and agent environment variable remain aliases for existing integrations.

## Native adapters and separation of concerns

`openclaw/tools.js` owns the twelve public schemas shared by OpenClaw registration and `mcp/server.js`. Both call the bounded JavaScript/Python bridge. `bridge_workspace.py` groups execution operations; bridge validation helpers are in `bridge_support.py`. The native store delegates graph queries to `db_graph.py`, schema migrations to `db_schema.py`, and privacy/identity helpers to `db_support.py`, using the same transaction API throughout. Native schema 6 adds archived `source_revision`; local ledger schema 1 is separate and unchanged.

The scanner separates snapshot reading, analysis, consistency verification, and atomic reconciliation. Investigation validation and adjacency construction are separate from persisted exploration. Execution separates command/trace validation, engine selection, container arguments, runtime capture, and database recording. The proposal gate separates argument, diff, run pairing, intent, report, mutation, and scope validation.

`reports.py` normalizes bounded external evidence. `mutation.py`, `property_checks.py`, `bisect.py`, and `evaluation.py` delegate all project execution to `WorkspaceManager`. `source_archive.py` exports bounded safe Git trees. `github.py` uses an explicitly authenticated host CLI; `process.py` bounds integration output/deadlines and cleans POSIX child groups. `provenance.py` exports statements and optional offline signatures; signing keys remain outside tool inputs. [Integration setup and limits](integrations.md).

## Follow-up module boundaries

`db_evidence.py` and `db_investigations.py` join `db_graph.py` as mixins over the same SQLite sessions and nested transactions. `bridge_evidence.py` groups evidence/memory operations. `build_config.py` classifies and reviews explicit build edits; `mutation_support.py` reports per-language availability and existing-grammar JS/TS operators. `host_environment.py` owns environment/command boundaries. `sarif.py` exports attributed findings. `sigstore.py` verifies externally signed bundles through an operator-installed Cosign; signing/OIDC stays outside model tools. New provenance predicate v2 binds runtime results and observations and rebuilds from the ledger; existing database schema 6 and ledger schema 1 remain unchanged.

## Global enforcement and import decomposition

`scripts/check_complexity.py` now measures production Python rather than five selected functions; its reviewed baseline is committed in `scripts/complexity-baseline.json`. Language-specific import recognition and candidate-path helpers keep the dispatchers bounded. The public `EvidenceStore.list_evidence_ids` API isolates SARIF from transaction internals. Protocol fixtures cover LSP callbacks, source edges, and failure handling; they are test peers, not additional host integrations. See [0.2.1 measurements](polish-review.md).

## Diagnosis layer (0.3.0)

`diagnostics.py` links current project-scoped evidence to deterministic functions in `localization.py`, `finding_fusion.py`, `bug_patterns.py`, and `patch_ranking.py`. `trace_events.py` validates ordered numeric events. `holdout.py` loads only operator configuration; private command output is withheld inside `WorkspaceManager` before persistence. `performance.py` consumes recorded runs rather than agent-supplied pass/fail or timing claims. OpenClaw and MCP share the existing schemas; native CLI registration stays in `diagnostic_cli.py`. [Accuracy workflow](accuracy-workflow.md).
