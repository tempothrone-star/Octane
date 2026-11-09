# Octane — Project Intelligence Layer (foundation)

Octane is a local, provider-independent foundation for persistent, evidence-driven software investigations. This repository is currently a deliberately small Python implementation, not a claim of complete autonomous repair or universal language support. It works without an LLM and does not modify the project being analyzed.

## Requirements and setup

- Python 3.11 or newer
- SQLite (included with Python)
- No third-party runtime dependencies
- Docker is required only to execute project code in the disposable workspace. Octane has **no host-process fallback**.

From the repository root, run the tests with:

```sh
python -m unittest discover -s tests -v
```

Run a real local demonstration:

```sh
python -m octane.cli demo
```

The demonstration uses SQLite and the checked-in `examples/sample_project`. Three agents hand off a paused investigation; Agent C retrieves the checkpoint and supporting evidence, verifies a static call-graph assertion, and records the scope of that verification. A separate disposable project copy demonstrates file-change detection and stale evidence/memory preservation. It deliberately does not execute the example: runtime execution needs a user-selected Docker image and explicit command. The example contains a potentially incorrect lowercasing behavior and a corresponding expectation; the demo does not claim that static evidence proves a runtime defect.

Install as a local command (optional):

```sh
python -m pip install -e .
octane init /path/to/project --database .octane/pil.sqlite3
octane scan PROJECT_ID --database .octane/pil.sqlite3
```

Without installation, invoke `python -m octane.cli` with the same subcommands. `init` registers a project and prints its ID; `scan` hashes and analyzes Python files and updates the SQLite graph. `.octane/pil.sqlite3` is runtime data and should not be committed.

## Current architecture

- `octane/db.py`: SQLite schema migrations through version 4, WAL, foreign keys, transactions, audit/activity records, project metadata, scoped agent permissions, provenance-bearing evidence, evidence-linked memory/hypotheses, lifecycle-aware investigations, revision history, and graph-edge history.
- `octane/exploration.py`: deterministic Python AST adapter, file hashing/revision detection, typed `defines` / `calls` / `imports` graph links when statically resolvable, stale evidence and supported-memory marking, a bounded graph-traversal `Tentacle`, and configurable budgeted candidate scheduling.
- `octane/workspace.py`: disposable snapshots outside the source tree, safe relative candidate-file edits in experimentation mode, diff generation, and a Docker-only command runner with networking disabled, read-only source snapshots for observation, resource limits, a timeout, forced container cleanup attempts on timeout, and bounded captured output (up to 1 MB per stream).
- `examples/sample_project`: small analysis fixture with a suspected normalization behavior.
- `tests`: standard-library `unittest` coverage for schema upgrades, project metadata/isolation, rollback/concurrent writes, permissions, evidence provenance, three-agent handover, graph history, scheduler budgets, revision staleness, and workspace protection.

Basic library use:

```python
from octane import ProjectIntelligence, PythonAdapter

pil = ProjectIntelligence(".octane/pil.sqlite3")
project = pil.create_project("my-project", "/path/to/project")
result = PythonAdapter().scan(pil, project["id"], project["root"])
print(result)
```

Example cross-agent checkpoint flow:

```python
explorer = pil.register_agent(project["id"], "explorer", "code-explorer",
                              permissions=["write_evidence", "checkpoint_investigation"])
investigator = pil.register_agent(project["id"], "investigator", "bug-investigator",
                                  permissions=["investigate", "checkpoint_investigation", "read_evidence", "read_investigation"])
finding = pil.submit_evidence(project["id"], "static_finding", {"symbol": "parse_input"},
                              "Found the input parser", agent_id=explorer["id"])
task = pil.create_investigation(project["id"], "Trace invalid input handling", investigator["id"],
                                {"evidence_ids": [finding["id"]], "next_action": "inspect callers"})
pil.pause_investigation(task["id"], investigator["id"])
continuation = pil.resume_investigation(task["id"], investigator["id"])
```

Use `requesting_agent_id` on read APIs when acting as an agent. A resume returns `resumed=False` and leaves the checkpoint unchanged if the project revision no longer matches.

Agents must be registered with explicit permissions before they can write evidence, memories, graph updates, investigations, hypotheses, or checkpoints. Agent-scoped read methods enforce permissions when passed `requesting_agent_id`; calls without an agent ID are privileged local API calls. Evidence records source revision/location and verification status; the AST scanner, tentacles, and Docker runner register internal agents. `submit_evidence()` still permits `agent_id=None` for external/user-supplied records. `verified` can only be assigned through `mark_evidence_verified()` with a linked, observed test/runtime record and verifier permission. Hypothesis confidence values are heuristics, not calibrated probabilities. Memory support links drive stale marking. Historical evidence is preserved and queryable with `include_stale=True`.

## Runtime safety

Workspace execution is always mediated by the installed Docker CLI. Octane never invokes a project command directly on the host. It disables container networking and mounts only the disposable copy; observation mode is read-only, while experimentation mode is writable. Docker is not a complete VM boundary and the Docker daemon itself is privileged infrastructure: use only a trusted, configured daemon and image. No image is selected implicitly. Workspace cleanup is explicit through `WorkspaceManager.destroy_workspace()`.

## Limitations and next work

- Only Python syntax, basic symbols, imports, and conservative static call links are analyzed. Dynamic dispatch, aliases, scopes, full control-flow/dataflow, types, other languages, and complete semantics are not inferred.
- Revision hashes outside Git cover Python source files only. In Git repositories, the current HEAD, working-tree status, and scanned Python source hashes participate in the revision label. An edit within a changed file invalidates its old sourced evidence and evidence-backed memory; there is no automatic semantic revalidation.
- Scheduler scores are configurable heuristics. Tentacles traverse the recorded graph; they do not yet perform full static data/control-flow analysis or model-based root-cause reasoning.
- SQLite migrations currently upgrade schema versions 1 through 4; future schema changes still need new migrations. FTS5, an HTTP API, provider adapters, runtime trace instrumentation, generalized test orchestration, patch approval/transfer, and broader conflict-resolution workflows are not implemented.
- Docker-based runtime execution and experiments are implemented but require an available Docker daemon and an explicit image/command. The automated test suite does not execute untrusted project code.
- Graph and evidence APIs are a local Python interface. A broader public API and richer permission policy are future work.

The implementation favors reproducible local persistence and explicit uncertainty over simulated completion of unsupported capabilities.
