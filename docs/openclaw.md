# Native Octane integration

Octane gives OpenClaw agents a shared SQLite Project Intelligence Layer (PIL). It stores evidence, interpretations, an evolving graph, investigation checkpoints, and controlled execution records. The OpenClaw agent remains responsible for planning, evaluating evidence, and interpreting results. The root `octane` skill also documents local evidence and static analysis through `scripts/octane.py`.

The [installation guide](../openclaw/README.md) shows the native plugin configuration. From an allowed checkout, ask OpenClaw “Fix a bug.” Use `/skill octane Fix a bug.` to invoke the workflow explicitly.

## Start from a checkout

The agent begins with `octane_context` on the current working copy. Triage and focus are available before project registration. Add `root` when the Gateway needs an explicit allowed checkout path.

Use the `triage` action to get baseline candidates and documentation locations.

```json
{"action": "triage"}
```

After a reproduction identifies a suspect, call `octane_context` again with the path, symbol, or both.

```json
{"action": "focus", "path": "src/parser.py", "query": "parse_input"}
```

Focus returns source locations and a Mermaid view of candidate edges. For a cross-file investigation, register and scan the project, then pass a symbol from `octane_graph` action `find` to `connections` as `nodeKey`. Confirm the relevant path with a focused test or trace.

## Investigation workflow

| Step | Tool | Stored result |
| --- | --- | --- |
| Find project knowledge and check the source revision | `octane_project`, `octane_context` with `snapshot`, `octane_scan` | Project, file hashes, changed paths, graph, and static evidence |
| Investigate a call or import path | `octane_graph` with `find` or `connections`, `octane_investigation` with `explore` | Named incoming and outgoing connections with locations, a Mermaid plot for calls, visited nodes, pending queue, and checkpoint |
| Record an explanation and conflicting findings | `octane_hypothesis`, `octane_evidence` | Hypothesis with explicit evidence relationships |
| Hand off and check revision compatibility | `octane_investigation checkpoint`, `resume`, `revalidate` | Attributed checkpoints and revalidation rationale |
| Run the reproduction and preservation checks | `octane_workspace run` | Exit status, bounded output, snapshot hashes, and optional runtime call graphs |
| Prepare a patch for review | `octane_workspace propose`, `octane_investigation report` | Evidence-linked diff, run IDs, and investigation summary |

Exploration follows stored graph relationships in the chosen direction. Set `startingNodes`, `relationships`, `direction`, `depthLimit`, `nodeBudget`, and `timeBudgetSeconds`. Use the returned `exploration_id` as `explorationId` to continue a saved run. Use separate runs to explore independent starting points. A changed revision requires revalidation with current evidence and a new exploration.

## Test and propose a patch

1. Create an `observation` workspace for the unchanged baseline and an `experimentation` workspace for the candidate, both from the same checkout revision. Pass `workspaceId`, matching `mode`, and `investigationId` to each `octane_workspace` run.
2. Record the focused reproduction failing on the baseline. Record at least one separate check that passes and exercises a behavior the patch should preserve.
3. Edit the candidate, then rerun both checks with the same inputs, commands, immutable image, and trace settings. A requested trace must be available for the paired run to qualify.
4. Call `octane_workspace` with action `propose`, supplying the two focused run IDs as `baselineRunId` and `candidateRunId` and the candidate preservation run IDs as `regressionRunIds`. Cite current `user_requirement`, `documented_behavior`, `design_decision`, or `intent_contract` evidence in `evidenceIds`, then explain the expected behavior in `rationale`.

Octane pairs the preservation runs with recorded baseline runs. Review each `preservation_pairs` entry, including `output_match`, and inspect `review_notes` and changed paths. An `output_match` of `null` means the captured output cannot be compared fully. Set `strictPreservationOutput` for deterministic output. The proposal stores a text diff for review and identifies changed paths seen in the focused traces when available. Retrieve the diff by evidence ID after workspace cleanup.

### Python trace example

Set `pythonTrace: true` on a Docker workspace run such as `["python", "-B", "-m", "unittest", "test_pipeline.Focused"]` or `["python", "check.py"]`. The result and execution evidence include project-local function entries, caller and callee names and source locations, and exception types and locations. The tracer omits argument values, return values, and exception messages. It observes the main Python thread and can include generator or coroutine re-entry. It stops at 50,000 events and keeps at most 150 distinct rows. Traced runs have a 512 KiB per-file write limit. Use the trace to check what executed for that input and keep ordinary test output for assertion details. The installation guide lists the [native, Node, Ruby, and Speedscope trace options](../openclaw/README.md#multilingual-function-graphs-and-traces).

## Architecture and limits

- `openclaw/index.js` registers the optional tools using OpenClaw's [tool factory API](https://docs.openclaw.ai/plugins/building-plugins). It passes runtime agent IDs from the host context to the bridge. A model-supplied parameter cannot choose the runtime identity. Attribution identifies the OpenClaw agent and a permission-scoped bridge actor when available. Explicit OpenClaw tool policy remains the authorization boundary.
- `octane/scanner.py` uses the shared `octane/ledger/` parser. It persists multilingual file dependencies, Python call candidates resolved from lexical scopes and imports, and syntax-level function and call candidates for the supported Tree-sitter grammars when the optional backend is installed. Run an explicit scan after upgrading to rebuild saved graph edges with the new resolver.
- `octane/investigation.py` stores exploration frontier and evidence atomically, and `octane/db.py` migrates the existing PIL database to schema 5. Existing evidence, checkpoints, and graph history are retained.
- `octane/workspace.py` copies an approved project, excludes common credential paths, and keeps an unmounted baseline. Docker receives only the disposable copy, the selected local image, and an explicit command. The container has networking disabled, resource limits, and a timeout. Keep credentials outside the source tree. The Docker daemon and approved images are trusted infrastructure.
- Native evidence verification requires a separate recorded passing run on an unchanged snapshot at the current indexed revision. A passing command establishes the behavior it actually checked. Octane leaves semantic interpretation to the investigator.
- The agent judges intent and causation from the evidence. Runtime coverage follows the selected probe and tracer. For another process or runtime, use a profiler with source-backed Speedscope output.

## Reproduce the native demonstration

The fixture converts numeric zero to `None`, which incorrectly triggers the documented missing-input sentinel. The explorer records a call relationship and a saved queue. A fresh bridge instance resumes work as the tester and traces the failing reproduction in Docker. A broad patch passes the zero-input check but changes the sentinel and fails preservation. The one-line candidate passes both, and the report links the exact before-and-after runs. The fixture checks the proposed diff with `git apply --check`.

```sh
docker pull python:3.12-slim
OCTANE_TEST_DOCKER_IMAGE=python:3.12-slim python3 -m unittest tests.test_native_workflow -v
npm test
```

The packaged plugin registered twelve optional tools under the [OpenClaw tool plugin contract](https://docs.openclaw.ai/plugins/tool-plugins) when inspected with OpenClaw 2026.11.3. Local bridge checks and the Docker demonstration require no model credentials.
