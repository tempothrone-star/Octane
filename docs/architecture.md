# Internal project-intelligence engine

## Role in the product

The primary product is the OpenClaw plugin and companion skill described in [`openclaw.md`](openclaw.md). This document describes the bundled Python/SQLite engine that implements the plugin's persistence, retrieval, static scanning, revision tracking, and workspace operations. It is maintained as an internal component of that integration, not presented as a standalone autonomous agent or as an independent product surface. The OpenClaw Gateway and its configured agent remain responsible for model execution and tool policy.

## Persistence and migrations

`ProjectIntelligence` opens a file-backed SQLite database, enables foreign keys and a busy timeout, and uses WAL. Writes use `BEGIN IMMEDIATE` and roll back on exceptions. Schema migration records are stored in `schema_migrations`; migrations 1–4 execute transactionally and rerunning initialization is safe.

The schema's main ownership boundary is `project_id`. Projects own agents, files, graph nodes and edges, evidence, memories, investigations, hypotheses, revisions, workspaces, and audit/activity events. Foreign keys prevent dangling references. Project source files are never copied into SQLite; only hashes, paths, locations, and analysis metadata are stored.

## Evidence, memory, and hypotheses

Evidence rows preserve a structured payload, description, source agent, revision, optional relative path/line, verification status, and stale state. Evidence links are separate rows and can express support, contradiction, context, supersession, or verification. The `verified` state is only assigned by `mark_evidence_verified()` when a distinct, observed test/runtime/verification record is attached and the caller has `verify_evidence` permission.

Memories are interpreted/shared knowledge, distinct from evidence. They can link to supporting evidence; when a referenced file changes, evidence and linked active memory are marked stale, not deleted. Hypotheses are separate records with explicit statuses and confidence estimates; confidence is a heuristic value, not a calibrated probability. Updating a hypothesis records old/new status and confidence in the audit log. Supporting and contradicting evidence links remain independently queryable.

## Graph and exploration

`update_graph()` atomically upserts nodes and typed relationships. Relationships carry a confidence estimate and optional agent/evidence provenance. When an edge changes, its previous revision, confidence, metadata, and provenance are copied into `graph_edge_history`. The Python adapter discovers files, classes/functions, local imports, and conservative unique-name calls. `Tentacle` workers traverse already-recorded `calls` or `imports` edges under node/depth/time budgets, avoid repeating current-revision discoveries, and record their own evidence. Graph syntax findings are not a claim of full semantic understanding.

## Investigations and handover

An investigation stores objective, creation/owner information, revision, current context, state, and an append-only ordered list of checkpoints. Checkpoint context holds completed work, unresolved questions, evidence/hypothesis IDs, relevant paths, and next action. `resume_investigation()` compares the investigation revision with the current project revision before changing state. A mismatch is returned as `resumed: false`; the stored checkpoint remains intact for revalidation. Lifecycle states are constrained; completed and abandoned investigations are terminal.

The CLI demo and tests are developer fixtures for the bundled engine, not a separate multi-agent product. The fixture simulates three Octane permission records handing off an investigation, then leaves it blocked because the example's runtime behavior was not tested. A disposable second project demonstrates changed-file invalidation without editing the checked-in sample.

## Revisions and stale knowledge

The Python adapter calculates SHA-256 for Python files and records `mtime_ns` as supplementary metadata. In a Git checkout the revision label includes HEAD, working-tree status, and scanned-source hashes; outside Git it is derived from scanned file hashes. Each detected revision stores changed/removed paths and before/after hashes. Graph nodes/edges and file-sourced evidence are marked stale before the new scan refreshes current graph entries. Historical evidence and changed-edge snapshots are retained. Staleness is conservative; PIL does not automatically revalidate prior interpretations.

## Permissions and audit

Agents have JSON-encoded capabilities and permissions. Mutating APIs with an agent identity check project membership and the required permission. Read APIs accept `requesting_agent_id` and enforce the corresponding read permission. Omitting the actor ID means a trusted, privileged local caller; it is not appropriate for an untrusted network-facing agent. Each important mutation writes an `audit_logs` row and an `agent_activity` row in the same transaction; callers can retrieve bounded audit results with `search_audit_logs()`.

Agent-provided configuration keys that resemble password, secret, token, API-key, credential, or private-key fields are rejected. Git remote URLs are stripped of userinfo, query, and fragment components before persistence. Do not put secrets in project configuration or evidence payloads.

## Workspace boundary

Snapshots are created outside the project root, ignore VCS/virtualenv/cache content and symlinks/special files, and are cleaned if setup fails. Candidate edits are allowed only in experimentation snapshots. Project commands run only through Docker with networking disabled, a read-only observation mount or writable experiment mount, resource/time limits, capped output, and timeout cleanup attempts. Docker is a container boundary, not a VM; the daemon and selected image remain trusted infrastructure. Runtime execution was not available in this environment and has not been claimed as verified.

## Engine limits and maintenance

The engine intentionally has no model runtime or autonomous repair loop; model execution and the user-facing agent experience belong to OpenClaw. Its Python AST adapter is conservative and does not provide full-text/semantic search, generic multi-language parsing, complete data/control-flow analysis, runtime instrumentation, or automatic root-cause repair. Investigations track both creator and current assignee; resuming hands assignment to the receiving agent. New database changes must add a migration beyond version 4.
