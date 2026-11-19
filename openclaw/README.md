# Octane OpenClaw plugin

Octane registers tools for multilingual project indexing, resumable investigations, and Docker experiments. Its manifest loads one `octane` skill from the repository root. The same package supplies the local evidence CLI.

## Requirements

- OpenClaw `>=2026.5.17` (plugin APIs are experimental; validate against the Gateway version you deploy).
- Node.js `^24.22.1` or `>=26.12.1`.
- Python `>=3.11` on the Gateway host for native plugin tools.
- Docker only when using isolated workspace command execution.

## Install from a local checkout

From the repository root, install the declared runtime dependency, then link the reviewed package:

```sh
npm ci --omit=peer
openclaw plugins install --link .
```

`typebox` is installed by npm. The Gateway supplies OpenClaw itself. The plugin loads the root `octane` skill. Invoke the native workflow with `/skill octane Fix a bug.` or `/octane` where direct skill commands are available. [OpenClaw skill commands](https://docs.openclaw.ai/tools/slash-commands#skill-commands)

The following `openclaw.json` excerpt enables a repair workflow with Python Docker experiments. Use an existing absolute checkout path in `allowedProjectRoots`. Choose the exact trusted image needed for the project's checks and pull it onto the Gateway host before running them.

```json5
{
  tools: {
    alsoAllow: ["octane_project", "octane_context", "octane_scan", "octane_graph",
      "octane_evidence", "octane_hypothesis", "octane_investigation", "octane_workspace"]
  },
  plugins: {
    entries: {
      octane: {
        enabled: true,
        config: {
          allowedProjectRoots: ["/home/alex/work/sample-project"],
          allowedDockerImages: ["python:3.12-slim"]
        }
      }
    }
  }
}
```

If you enable execution for a sandboxed agent, allow the required tool IDs in `tools.sandbox.tools.alsoAllow`. For graph-only use, omit `octane_workspace` and the Docker image configuration. `pythonExecutable` selects the Gateway-side interpreter. `databasePath` and `workspaceRoot` default under `$OPENCLAW_STATE_DIR/octane` or `~/.openclaw/octane`. Keep both outside any checkout you plan to snapshot. Agent provisioning is disabled by default.

## Checkout triage and symbol lookup

On repair requests, `octane_context` action `triage` uses the current allowed checkout to identify baseline checks and related tests before a project has been registered. It reports candidate commands without running them. On OpenClaw versions that support Tool Search, this entry point stays directly visible to agents that can call it. A bare request such as “Fix this file” can use the same entry point as `/octane`.

`octane_context` action `focus` takes a `path`, a function name in `query`, or both. It analyzes the working copy and returns bounded static relationships and a Mermaid plot before registration. A path confines analysis to that file, up to 256 KiB. A symbol-only request reads up to 64 eligible source files and 2 MiB. Check `truncated` and the parser coverage before treating an absent edge as meaningful. Use a known path for narrower analysis or scan the project for a broader graph. Triage offers a compact Mocha command for direct npm scripts and lists common documentation locations.

For OpenClaw Docker turns, triage resolves `/workspace` against the current host workspace and enforces the configured roots. In a PHP source checkout, triage returns the remaining CLI build steps and a PHPT command for the selected test, extension, or core smoke suite. Run those steps in an isolated writable build copy.

For operators who want the repair guidance available in the initial prompt, Octane registers it when `plugins.entries.octane.hooks.allowConversationAccess` is `true` and `allowPromptInjection` is permitted. It contributes static guidance only when the turn can call `octane_context`. The explicit skill command works without enabling this hook.

## Boundaries and validation

The plugin resolves configured absolute `allowedProjectRoots` before calls and independently enforces containment in its Python bridge. Docker execution is denied unless the exact image string appears in `allowedDockerImages`; it has no host-execution fallback. Docker is not a VM boundary. Gateway investigation records live in the configured PIL database. Local CLI evidence uses `.octane/ledger.db` in the analyzed project. Existing ledgers are reused as described in the [upgrade notes](../README.md#storage-and-upgrades).

Run `openclaw plugins inspect octane --runtime --json` to check registration and `openclaw skills list` to confirm discovery. The [native workflow guide](../docs/openclaw.md) shows the tool calls and the Docker demonstration.

## Multilingual function graphs and traces

`octane_scan` includes Python's lexical call analysis. To enable additional function and call candidates, install the pinned `ast` extra into the Python environment configured by `pythonExecutable`. From the checkout, use `python -m pip install '.[ast]'` in that environment. Tree-sitter call rules cover Bash, C, C++, C#, Erlang, Fortran, Go, Haskell, Java, JavaScript, Julia, Kotlin, OCaml, Perl, PHP, R, Ruby, Rust, Scala, and TypeScript including TSX. The scan reports parser coverage and grammar or scan-budget gaps. Other languages retain the available file and import graph. Static edges represent candidate relationships. Dynamic dispatch, overloaded names, and cross-language calls may require a runtime trace or additional source evidence.

Use `octane_graph` action `find` with a name and optional `path` or `language`, then `connections` with the returned `node_key` as the `nodeKey` argument. For `relationship: "calls"`, the result includes `mermaid` and the source lines of candidate calls.

For a checkout with large generated source files, reuse any explicit `maxFileBytes` and `maxTotalBytes` limits from the scan when creating the investigation and its workspaces.

Use one trace option per `octane_workspace` action `run`. Each option uses an approved Docker image and reports a Mermaid graph of project-local frames.

| Option | Probe and output |
| --- | --- |
| `pythonTrace: true` | Run a Python script or module. Records calls and exception locations. |
| `runtimeTrace: "node"` | Run `node` directly. Samples JavaScript or TypeScript executed by that Node process. A short call may fall between samples. |
| `runtimeTrace: "ruby"` | Run `ruby` directly. Records method call stacks through TracePoint. |
| `nativeTraceSymbols: ["zend_compare"]` | Run a checkout-relative debug executable with GDB and Python support in the image. Supports simple and qualified names, including C++, Rust, and Go names tested with GDB. |
| `runtimeTrace: "speedscope"` | Run an explicit profiler or converter that writes sampled or evented Speedscope JSON to `OCTANE_TRACE_PATH`. This also accepts profiles captured from toolchains without a bundled runner. |

Use project-relative frame paths or paths inside `/workspace`. Profiler output is bounded to 512 KB. Use a direct PHP CLI probe with runtime arguments when tracing the interpreter, since PHPT can run a child process and a literal expression can be evaluated during compilation. Build debug binaries with source paths that resolve inside the workspace. Reuse the same probe, image, and trace settings for candidate runs. Validate a correction with a failing original check, a passing candidate check, and matching preservation checks.

Patch comparisons and proposals flag removed literals for common HTTP security headers so the reviewer can check actual response behavior. Each paired preservation check reports whether complete stdout and stderr match. Use `strictPreservationOutput: true` to require exact matching output for deterministic checks. Leave the setting off for runners whose output includes timings or other variable text.
