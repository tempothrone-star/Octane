# Native Octane integration

Octane gives OpenClaw agents a shared SQLite Project Intelligence Layer (PIL). It stores evidence, interpretations, an evolving graph, investigation checkpoints, and controlled execution records. The OpenClaw agent remains responsible for planning, evaluating evidence, and interpreting results. The separately installable `octance` skill retains its portable ledger and its own database.

The [installation guide](../openclaw/README.md) shows the native plugin configuration. The manifest bundles the `octane` workflow and the portable `octance` skill. Use `/octane` for the native tools, or ask an authorized OpenClaw agent to investigate a registered project with those tools.

## Investigation workflow

| Step | Tool | Stored result |
| --- | --- | --- |
| Find project knowledge and check the source revision | `octane_project`, `octane_context`, `octane_scan` | Project, file hashes, changed paths, graph, and static evidence |
| Investigate a call or import path | `octane_graph find`, `connections`, `octane_investigation explore` | Named incoming and outgoing connections with locations, a Mermaid plot, visited nodes, pending queue, and checkpoint |
| Record an explanation and conflicting findings | `octane_hypothesis`, `octane_evidence` | Hypothesis with explicit evidence relationships |
| Hand off and check revision compatibility | `octane_investigation checkpoint`, `resume`, `revalidate` | Attributed checkpoints and revalidation rationale |
| Run the reproduction and preservation checks | `octane_workspace run` | Exit status, bounded output, snapshot hashes, and optional runtime call graphs |
| Prepare a patch for review | `octane_workspace propose`, `octane_investigation report` | Evidence-linked diff, run IDs, and investigation summary |

Exploration follows stored graph relationships in the chosen direction. Set `startingNodes`, `relationships`, `direction`, `depthLimit`, `nodeBudget`, and `timeBudgetSeconds`. Use the returned `exploration_id` as `explorationId` to continue a saved run. Use separate runs to explore independent starting points. A changed revision requires revalidation with current evidence and a new exploration.

A proposal requires a focused check that fails on the unchanged observation copy and passes on the candidate. Each separate preservation check must pass on both copies. Supply the candidate preservation runs using `regressionRunIds`. Octane finds the matching baseline runs in the same investigation using the command, immutable image ID, snapshot and trace setting. Cite current `user_requirement`, `documented_behavior`, `design_decision`, or `intent_contract` evidence and explain the expected behavior. Keep validation code and inputs identical across both runs. The proposal includes the paired runs, diff, and which changed paths were present in the focused traces. Proposals support text edits to existing files. Retrieve the full diff by evidence ID after workspace cleanup.

Set `pythonTrace: true` on a Docker workspace run such as `["python", "-B", "-m", "unittest", "test_pipeline.Focused"]` or `["python", "check.py"]`. The result and execution evidence include project-local function entries, caller and callee names and source locations, and exception types and locations. The tracer omits argument values, return values, and exception messages. It observes the main Python thread and can include generator or coroutine re-entry. It stops at 50,000 events and keeps at most 150 distinct rows. Traced runs have a 512 KiB per-file write limit. Use the trace to check what executed for that input and keep ordinary test output for assertion details. The installation guide lists the [native, Node, Ruby, and Speedscope trace options](../openclaw/README.md#multilingual-function-graphs-and-traces).

## Architecture and limits

- `openclaw/index.js` registers the optional tools using OpenClaw's [tool factory API](https://docs.openclaw.ai/plugins/building-plugins). It passes runtime agent IDs from the host context to the bridge. A model-supplied parameter cannot choose the runtime identity. Attribution identifies the OpenClaw agent and a permission-scoped bridge actor when available. Explicit OpenClaw tool policy remains the authorization boundary.
- `octane/scanner.py` reuses the bundled portable parser. It persists multilingual file dependencies, Python call candidates resolved from lexical scopes and imports, and syntax-level function and call candidates for the supported Tree-sitter grammars when the optional backend is installed. Run an explicit scan after upgrading to rebuild saved graph edges with the new resolver.
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
