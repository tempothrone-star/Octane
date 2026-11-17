# Project architecture

Octane and Octance have distinct entry points and SQLite stores. The OpenClaw agent supplies reasoning for both.

| Component | Entry point | State |
| --- | --- | --- |
| Native Octane plugin | `openclaw/index.js` registers tools and `openclaw/bridge.js` invokes the Python 3.11+ bridge | A Gateway-side Project Intelligence Layer (PIL) database and disposable workspaces |
| Portable Octance skill | `octance/SKILL.md` and the bundled Python 3.9+ CLI | A project-local `.octance/` ledger |

## Native project intelligence

- `octane/repair.py` suggests baseline checks from checkout metadata. `octane/focus.py` builds a bounded, read-only symbol view from the working copy. Both accept an allowed checkout before it has a saved index.
- `octane/scanner.py` builds the persistent index from source file hashes, import edges, and static call candidates. It reuses the portable dependency parser, adds Python lexical analysis, and can use Tree-sitter for other languages. The native database tracks revisions, evidence, hypotheses, graph history, and investigation checkpoints. Schema 5 adds saved exploration queues, execution records, and patch proposals.
- `octane/workspace.py` copies an allowed checkout and keeps an unmounted baseline. Observation commands mount the snapshot read-only. Experimentation runs and candidate edits use a writable copy. Docker receives an explicit command and an allowlisted local image. The run records the immutable image ID, exit status, output, snapshot hashes, and any requested trace.
- `octane/experiments.py` pairs the failing original check, passing candidate, and separate preservation checks. It binds the proposal to the current investigation, tested snapshots, and evidence of expected behavior. The proposal is a reviewable diff stored as evidence.

The Gateway supplies agent identity and tool permissions. Configure allowed checkout roots, keep database and workspace storage outside analyzed projects, and authorize Docker images through the [plugin setup guide](../openclaw/README.md). For a full repair example, see the [native workflow](openclaw.md).

## Portable ledger

The skill directory contains `SKILL.md`, a self-contained `scripts/` implementation, and numbered SQL migrations. `scripts/octance.py` at the repository root is a development convenience wrapper that invokes the bundled skill CLI.

## Project layout and identity

Each analyzed root stores runtime state under `.octance/`:

- `octance.db`: SQLite WAL database, schema version 1.
- `project.json`: project UUID, canonical root, and a last-committed log-head anchor.
- `config.json`: default trust tier and an initially empty execution allowlist reserved for later phases.

Initialization starts from the requested directory or the current working directory and uses the enclosing Git top-level when available. Ledger commands discover the nearest initialized project and check the canonical path and project UUID before reading or writing records. Static `languages`, `graph`, and `locate` commands also work on a checkout without an initialized ledger. Pass `init --update-gitignore` to add the state directory to `.gitignore`. Tests initialize temporary roots.

The revision recorded with evidence and claims is a SHA-256 digest of relative file paths and file bytes, excluding symlinks, `.git`, `.octance`, and common generated/cache directories. Hashing does not copy file contents into the database. Git is optional.

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
