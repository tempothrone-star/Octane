# Optional Octane OpenClaw extension

The native extension registers PIL tools for multilingual project indexing, resumable investigations, and Docker experiments. It bundles the native `octane` skill and the separately installable portable `octance` skill.

## Requirements

- OpenClaw `>=2026.5.17` (plugin APIs are experimental; validate against the Gateway version you deploy).
- Node.js `>=24.22.1` or `>=26.12.1`.
- Python `>=3.11` on the Gateway host for native plugin tools.
- Docker only when using isolated workspace command execution.

## Install from a local checkout

From the repository root, install the declared runtime dependency, then link the reviewed package:

```sh
npm install --omit=peer
openclaw plugins install --link . --force
```

`typebox` is a package dependency and is installed by npm; OpenClaw itself is an optional peer supplied by the Gateway runtime. The plugin manifest registers both skills. Use `/octane` to start the native investigation workflow.

Configure the plugin and explicitly allow only the tools needed by the agent. For example, in `openclaw.json`:

```json5
{
  tools: {
    alsoAllow: ["octane_project", "octane_context", "octane_scan", "octane_graph",
      "octane_evidence", "octane_hypothesis", "octane_investigation"]
  },
  plugins: {
    entries: {
      octane: {
        enabled: true,
        config: {
          allowedProjectRoots: ["/home/alex/work"],
          // Required only for workspace command execution; exact-match allowlist.
          allowedDockerImages: ["python:3.12-slim"],
          // Optional; defaults are under OPENCLAW_STATE_DIR/octane.
          databasePath: "/home/alex/.openclaw/octane/pil.sqlite3",
          workspaceRoot: "/home/alex/.openclaw/octane/workspaces",
          pythonExecutable: "python3",
          allowAgentProvisioning: false
        }
      }
    }
  }
}
```

On repair requests, `octane_context` action `triage` uses the current allowed checkout to identify baseline checks and related tests before a project has been registered. It reports candidate commands without running them. On OpenClaw versions that support Tool Search, this entry point stays directly visible to agents that can call it. A bare request such as “Fix this file” can use the same entry point as `/octane`.

`octane_context` action `focus` takes a `path`, a function name in `query`, or both. It analyzes the working copy, returns bounded static relationships and a Mermaid plot, and requires no project registration. A path confines analysis to the named file. A symbol-only request reads up to 64 eligible source files and 2 MiB; use a path when the result reports a partial scope. Triage offers a compact Mocha command for direct npm scripts and lists common spellings of project documentation.

For OpenClaw Docker turns, triage resolves `/workspace` against the current host workspace and enforces the configured roots. In a PHP source checkout, triage returns the remaining CLI build steps and a PHPT command for the selected test, extension, or core smoke suite. Run those steps in an isolated writable build copy.

To put the repair workflow in the initial prompt on OpenClaw releases with authorized prompt enrichment, set `plugins.entries.octane.hooks.allowConversationAccess` to `true`. OpenClaw requires that opt-in for the hook. Octane discards the hook's conversation input and contributes static instructions only when the current turn can call `octane_context`.

Add `octane_workspace` to the tool policy for agents authorized to run Docker experiments. Pull the approved image locally before execution. If the agent is sandboxed, include the required exact tool IDs in `tools.sandbox.tools.alsoAllow` as well. Add memory, audit, scheduling, and agent registry tools as needed.

## Boundaries and validation

The plugin resolves configured absolute `allowedProjectRoots` before calls and independently enforces containment in its Python bridge. Docker execution is denied unless the exact image string appears in `allowedDockerImages`; it has no host-execution fallback. Docker is not a VM boundary. The plugin's PIL engine is separate from Octance's standard-library ledger and project-local `.octance/` database.

Run `openclaw plugins inspect octane --runtime --json` to check registration and `openclaw skills list` to confirm discovery. Use the [native workflow guide](../docs/openclaw.md) to run the three-agent Docker demonstration and see feature boundaries.

## Multilingual function graphs and traces

`octane_scan` includes Python's lexical call analysis. To enable additional function and call candidates, install the pinned `ast` extra into the Python environment configured by `pythonExecutable`. From the checkout, use `python -m pip install '.[ast]'` in that environment. Tree-sitter call rules cover Bash, C, C++, C#, Erlang, Fortran, Go, Haskell, Java, JavaScript, Julia, Kotlin, OCaml, Perl, PHP, R, Ruby, Rust, Scala, and TypeScript including TSX. The scan reports parser coverage and grammar or scan-budget gaps. Other languages retain the available file and import graph. Static edges represent candidate relationships. Dynamic dispatch, overloaded names, and cross-language calls may require a runtime trace or additional source evidence.

Use `octane_graph` action `find` with a name and optional `path` or `language`, then `connections` with the returned `node_key`. Call relationships include `mermaid` for rendering the bounded neighborhood and source lines for inspection.

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
