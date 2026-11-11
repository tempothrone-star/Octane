# Octance Phase 1 architecture

## Product boundary

The product is the portable OpenClaw skill in `octance/`: a concise workflow plus a deterministic Python CLI. The CLI has no model SDK, makes no network calls, and uses only Python 3.9+ standard-library modules. OpenClaw's host agent supplies reasoning; its configured tools and sandbox policy determine whether the CLI can read a project or execute.

The skill directory contains `SKILL.md`, a self-contained `scripts/` implementation, and numbered SQL migrations. `scripts/octance.py` at the repository root is a development convenience wrapper that invokes the bundled skill CLI.

## Project layout and identity

Each analyzed root stores runtime state under `.octance/`:

- `octance.db`: SQLite WAL database, schema version 1.
- `project.json`: project UUID, canonical root, and a last-committed log-head anchor.
- `config.json`: default trust tier and an initially empty execution allowlist reserved for later phases.

Initialization chooses `git rev-parse --show-toplevel` when Git is available; otherwise it uses the requested/current directory. All commands discover the nearest initialized root, compare its canonical path and project UUID with both the database and JSON metadata, and refuse if they disagree. `.gitignore` is never changed unless `init --update-gitignore` is explicitly provided. The repository itself is never used as a test project; tests initialize temporary roots.

The revision recorded with evidence and claims is a SHA-256 digest of relative file paths and file bytes, excluding `.git`, `.octance`, and common generated/cache directories. No file contents are stored in the database. Git is optional and the hash fallback needs no Git command.

## Append-only ledger

Every phase-1 write appends an event containing sequence, project, agent, operation, canonical JSON payload, idempotency key, previous hash, and UTC timestamp. The hash is SHA-256 over the previous hash concatenated with canonical JSON for the event without its hash. The log has SQLite update/delete triggers; verification checks sequence continuity, project identity, predecessor links, event hashes, and the external `project.json` head anchor. `verify-log` detects edits, deletions, and reordering in tests.

The anchor is updated after a committed event. As this update spans a database and a JSON file, a process interruption can leave it behind the database head; a verified anchor prefix is accepted. This is tamper evidence, not tamper prevention: an operator with raw shell access can alter the database and the anchor. SQLite permissions are cooperative guardrails, not a security boundary.

Idempotency keys supplied by callers are honored. Otherwise the CLI derives a deterministic SHA-256 key from agent, operation, and canonical request payload. A retry returns the original event record; reuse of an explicit key for different content is rejected. Counters allocate `EV-0001` and `CL-0001` transactionally per project. Agent IDs use `AG-<name>`; initialization creates `AG-owner` with the local admin role.

Materialized project, agent, evidence, claim, link, and counter tables are written in the same `BEGIN IMMEDIATE` transaction as their event. `rebuild --check` replays supported phase-1 event types into an expected view and compares it to the live tables. It is a comparison only; a mutating rebuild is not implemented.

## Evidence and claims

Evidence and claims are distinct. Evidence is immutable and carries agent, type, description, structured payload, content hash, UTC time, revision, optional relative file/symbol/line range, method, verification status, and redaction count. Phase 1 accepts only `unverified` evidence; no command can mark evidence executed or replayed. Evidence links are append-only events.

Claims use the kinds `observation`, `inference`, `hypothesis`, `intent`, `decision`, `requirement`, `limitation`, and `open_question`. They cite supporting/contradicting evidence and dependencies, carry a revision and a derived ordinal evidence basis, and have no numeric confidence. Low-trust agents' claims enter as `quarantined` and are excluded from `brief`. Claim text remains immutable; `claim status` appends a status-transition event without rewriting the original claim. Content corrections still require a new claim and appropriate links. Only event reducers and CLI validators write the materialized tables; raw database access can bypass those cooperative rules.

Common secret-like keys and values are redacted during evidence and claim ingest. This is best-effort pattern filtering, not a credential scanner or guarantee; users should not supply credentials. `brief` ranks keyword matches, includes open questions as unknowns, excludes quarantined claims, and obeys an estimated token budget of characters divided by four. It is not semantic retrieval. FTS5 is probed by `doctor` but phase-1 search uses bounded SQLite `LIKE` queries.

## SQLite and filesystem

Connections enable foreign keys and a 5-second busy timeout. The database uses WAL and numbered transactional migrations. The CLI refuses a schema version newer than itself. `doctor` checks Python/SQLite versions, FTS5 availability, journal mode, log integrity, Git presence, and filesystem type where it can be detected. SQLite WAL is unsupported on some network filesystems; on platforms or mounts that cannot be identified, `doctor` warns rather than assuming local storage. Wall-clock timestamps are UTC ISO-8601; leases are not implemented yet, so clock-skew behavior remains future work.

No project command execution, network access, telemetry, patch application, or source modification exists in Phase 1. The skill cannot guarantee shell isolation. OpenClaw's sandbox is off by default and configured separately; `exec` may run on the Gateway host unless policy routes it to a sandbox. Operators should restrict tools and configure sandbox workspace access appropriately.

## Multilingual source triage prototype

`languages` walks a project without following symlinks, skips common generated/vendor trees, and classifies common source extensions (including Python, JavaScript/TypeScript, Java, Go, Rust, C/C++, C#, PHP, Ruby, Swift, Kotlin, SQL, HTML/CSS, and several others). It applies file-count and per-file-size limits, reports unsupported extensions, and does not read or parse source contents.

`graph` extracts best-effort internal file dependencies and identifies cross-language edges where a declaration resolves to a file with a different extension language. Python imports use the standard-library AST. By default, an optional offline Tree-sitter backend is used when installed for its bundled grammar set; otherwise language-specific recognizers and a generic line-oriented import fallback are used. Install the optional `.[ast]` extra and pass `--parser tree-sitter` to require this backend. Tree-sitter produces concrete syntax trees, not compiler-semantic ASTs; unsupported grammars fall back to heuristics. The graph observes file-count, per-file, and aggregate byte limits, skips symlinks through the inventory boundary, and emits unresolved references and parser coverage. It does not implement grammars for every language, complete call graphs, or runtime dependency resolution; matches may be false positives, and cross-language edges are not evidence of execution or causation.

`locate --trace-file` accepts an existing project-local text trace, parses common Python, JavaScript/TypeScript, Go, Java, Rust, and C/C++ file/line frames, and ranks matching source files using repeated/early frames, path query terms, and recent Git changes when Git is available. The output includes rationale and simple traceback-order/recent-change baselines. This is a file-level ranking heuristic only: it does not resolve functions, parse complete language grammars, replay a check, prove causation, or set evidence to verified. Trace paths are untrusted input, constrained to the project root, and never executed. Ranking is bounded by source-file count/size limits and does not persist scan results.

A 3-case controlled synthetic benchmark is provided in `tests/benchmark_localization.py`. It reports top-1 and mean reciprocal rank against traceback-first and recent-change-first baselines. Its cases are authored mechanics fixtures, not independent real-project ground truth; the measured score is not evidence of production localization quality and does not open the next phase gate.

## Phase status

Implemented: setup, project identity checks, owner and fixed-role agent registration, evidence/claim ingest and retrieval, secret redaction, hash-chain verification, idempotency, compact briefing, status/doctor, rebuild comparison, read-only multilingual extension inventory, a bounded best-effort internal file dependency graph with cross-language edges, and heuristic file-level trace ranking. Tests cover the Phase 1 ledger and current static-triage behavior.

Not implemented: predeclared replayable check definitions/execution, runtime trace capture, budgeted `explore`, function/symbol localization, representative real-project benchmark and measured go/no-go result, semantic ASTs or bundled grammars for every language (the optional Tree-sitter extra covers a subset), complete cross-language call graph, symbol footprints/staleness, investigation rows, leases, handoffs, optimistic versions, patch proposals, reports, export, migrations after version 1, FTS5-backed retrieval, or full CLI command contract. The controlled synthetic benchmark is preliminary and does not qualify as the required real-project gate. Do not treat old `octane/` or `openclaw/` code as evidence these Octance features exist.
