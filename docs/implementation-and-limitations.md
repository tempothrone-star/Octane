# Octance implementation and limitations report — Phase 1

## Implemented

Phase 1 is a portable OpenClaw skill in `octance/` backed by a deterministic Python 3.9+ standard-library CLI. It creates one SQLite/WAL database per project; writes UTC event records chained with SHA-256 and an external project JSON anchor; derives idempotency keys; verifies the event chain; and compares supported materialized tables against event replay.

Implemented commands: `init`, `doctor`, `status`, `verify-log`, `rebuild --check`; `agent register/show/whoami`; `evidence add/get/search/link`; `claim add/get/search/link`; and a bounded keyword `brief`. Project UUID/root mismatch is refused. Agent roles/permissions are checked by the CLI, low-trust claims are quarantined and excluded from briefings, and secret-like values are best-effort redacted at ingestion. No CLI command can set evidence to executed/replayed or a claim to verified.

The bundled skill has portable `SKILL.md`/`scripts`/`references` structure and declares only `python3` under `metadata.openclaw.requires.bins`. The CLI is also runnable from the repository root through `scripts/octance.py`.

## Phase 1 tests and output

Runner: Python standard-library `unittest`. The Octance Phase 1 tests exercise project identity/isolation, hash-chain edit/deletion/reorder detection, idempotency, immutability, replay comparison, role permissions, trust quarantine, secret redaction, schema-newer refusal, FTS5/LIKE search, concurrent process writers, transaction rollback, bounded brief, doctor reporting, and explicit `.gitignore` consent.

Final full-suite command: `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v` — **Ran 36 tests in 5.067s; OK**. This includes 11 Octance Phase 1 tests and 25 retained legacy PIL/plugin tests; the legacy suite verifies that the archived code still works, not that its capabilities belong to Octance. The separately run focused command `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_octance_phase1 -v` passed **11 tests**.

## Known limitations and not-yet-implemented items

The phase build order has not passed the pinpointing or benchmark gates. No replayable check runner, `explore`, `locate`, benchmark set/results, symbol footprints, stale/needs-review propagation, investigations, leases, checkpoints, resume protocol, optimistic concurrency, patch proposals, evidence-linked reports, export, or multi-language analysis is implemented. Do not claim root-cause localization, code verification, precise staleness, or automated handoff.

The log is tamper-evident, not tamper-proof. The project anchor is stored in local JSON beside the SQLite database, and an operator with shell access can edit both. SQLite WAL is unsupported on some network filesystems. Revision computation hashes file paths and contents (excluding `.git`, `.octance`, symlinks, and common generated directories) and may be expensive on very large projects. Secret pattern filtering is incomplete. No benchmark numbers or agent-level comparisons have been measured.

## OpenClaw and ClawHub assumptions

| Assumption | Status | Basis |
|---|---|---|
| Skill structure and load timing | **Verified in docs; not runtime-tested here.** A skill is a directory with `SKILL.md`; discovery metadata is read for the skill catalog/system prompt, and the instructions are supplied when the skill is active. Supporting references are loaded on demand by the agent. | [OpenClaw skill guide](https://docs.openclaw.ai/tools/skills), [Agent Skills specification](https://agentskills.io/specification) |
| Multiple agents/subagents | **Platform capability verified; target setup unverified.** OpenClaw documents independently queued subagents, a default max of eight concurrent child runs per spawning session, and configurable multi-agent entries. The operator's policy, tool visibility, and actual parallel availability have not been tested. | [Subagents](https://docs.openclaw.ai/tools/subagents), [Concurrency and recovery](https://docs.openclaw.ai/tools/subagents/operations) |
| Script/file access and sandboxing | **Docs verified; target runtime unverified.** A skill does not grant execution or filesystem access. `exec` is a shell surface, sandboxing is off by default, and sandbox/workspace policy changes what files it can access. The deployment must explicitly provide Python and authorize shell/file access to the target root. | [Exec tool](https://docs.openclaw.ai/tools/exec), [Workspace access](https://docs.openclaw.ai/gateway/sandboxing/workspace-access) |
| ClawHub file acceptance | **Verified in docs; no publication performed.** ClawHub accepts a folder containing `SKILL.md` and supporting regular files, including text-based `.py` and `.sql`; it documents a 50 MB bundle limit. This skill contains UTF-8 text only. | [ClawHub skill format](https://docs.openclaw.ai/clawhub/skill-format) |
| Skill `version` frontmatter | **Prompt differs from current docs; omitted.** The prompt template shows a top-level `version`, but the current OpenClaw/ClawHub frontmatter reference does not list it as a supported key and says publish version is set by the ClawHub CLI. The skill follows documented `name`, one-line `description`, and `metadata.openclaw.requires.bins`. | [Creating skills](https://docs.openclaw.ai/tools/creating-skills), [Skill format](https://docs.openclaw.ai/clawhub/skill-format) |
| ClawHub installation/publish validation | **Unverified.** The ClawHub CLI and a live OpenClaw Gateway are not installed/tested in this environment. Nothing has been published. | Not exercised |

The ClawHub format page requires `SKILL.md` (or accepted legacy names), allows optional supporting files, and says public skill versions are assigned on publish. No registry-authenticated operation was performed.
