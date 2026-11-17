# Octane for OpenClaw

Octane is an OpenClaw plugin for investigating and fixing bugs in a source checkout. It finds project test commands, follows candidate caller and callee relationships, and records what ran before and after a patch. The OpenClaw agent uses that evidence to identify the intended behavior and make the smallest correction it can verify.

This repository also ships **Octance**, a standalone skill and Python CLI for keeping a project-local evidence ledger and performing static triage.

| Install | Use it for | Requirements |
| --- | --- | --- |
| **Octane plugin** | Repair workflows, function graphs, runtime traces, resumable investigations, and evidence-linked patch proposals | OpenClaw 2026.5.17+, Node.js `^24.22.1` or `>=26.12.1`, and Python 3.11+. Docker for isolated execution. |
| **Octance skill** | Evidence, claims, briefings, file dependency maps, and ranking from an existing failure trace | Python 3.9+ and the standard library. OpenClaw is optional when using the CLI directly. |

## Install the Octane plugin

From a local checkout of this repository:

```sh
npm ci --omit=peer
openclaw plugins install --link .
```

Set `plugins.entries.octane.config.allowedProjectRoots` in `openclaw.json` to the absolute directories the plugin may inspect. Allow the relevant `octane_*` tools for the agent. For Docker experiments, allow `octane_workspace`, add the exact trusted image references to `allowedDockerImages`, and pull those images locally. The [plugin setup guide](openclaw/README.md) includes a configuration example and instructions for sandboxed agents.

Check registration and skill discovery:

```sh
openclaw plugins inspect octane --runtime --json
openclaw skills list
```

The plugin bundles both skills. Use the standalone install below if you only need the portable ledger.

## Fix a bug

With OpenClaw working in an allowed checkout, ask **“Fix a bug.”** To invoke the workflow explicitly, use `/skill octane Fix a bug.` OpenClaw can also expose it as `/octane`. [OpenClaw skill commands](https://docs.openclaw.ai/tools/slash-commands#skill-commands)

1. **Get a baseline.** `octane_context` with `triage` identifies relevant tests and their commands from the working copy without running them. When the request gives no symptom and the baseline passes, the skill probes an uncovered case implied by an existing test, document, or caller. If it finds no reproducible failure, it reports what it checked.
2. **Trace the behavior.** Use `focus` with a path or function name for a bounded Mermaid call graph with source locations. For a wider investigation, use `octane_scan` and `octane_graph` to follow callers, callees, or imports. Run a focused reproduction with runtime tracing where available.
3. **Preserve what works.** Use existing tests, documentation, or user requirements to establish the intended behavior. Keep the patch small and rerun the same reproduction and separate preservation checks on the candidate.
4. **Review the evidence.** With `octane_workspace`, a patch proposal pairs a failing baseline reproduction with a passing candidate. Preservation checks must pass on both snapshots. The proposal records the diff, intent evidence, and paired runs. It can flag removed HTTP security-header references and enforce exact output matching for deterministic preservation checks.

The [native workflow guide](docs/openclaw.md) shows how to checkpoint an investigation, resume it after a session change, revalidate evidence, and reproduce the one-line repair demonstration.

### Language and runtime support

- **File and import graphs** recognize extensions across many ecosystems. The portable CLI accepts additional suffixes and import rules in a data-only `.octance/languages.json` via `--language-config`.
- **Static function graphs** use Python's built-in AST and lexical analysis. Install the optional Tree-sitter extra for syntax-level call candidates in languages such as C, C++, C#, Go, Java, JavaScript, PHP, Ruby, Rust, and TypeScript. Coverage varies by grammar and scan scope. Dynamic dispatch and cross-language calls may need runtime evidence.
- **Runtime traces** support Python calls and exceptions, Node.js sampling, Ruby method calls, GDB traces for debug binaries, and compatible Speedscope profiles. Each trace covers the selected probe. See the [supported grammars and trace options](openclaw/README.md#multilingual-function-graphs-and-traces) for setup and limits.

To enable Tree-sitter, install the optional extra from the checkout with the same Python environment configured as `pythonExecutable`:

```sh
python3 -m pip install '.[ast]'
```

## Use the standalone Octance skill

Install the skill from Git:

```sh
openclaw skills install git:tempothrone-star/Octane
```

For a manual workspace install, copy the complete `octance/` directory so its `SKILL.md`, references, and helper script stay together:

```sh
mkdir -p "$OPENCLAW_WORKSPACE/skills"
cp -R ./octance "$OPENCLAW_WORKSPACE/skills/"
```

Run the bundled CLI with the target checkout as your working directory. Set `OCTANCE_CLI` to the absolute path to `scripts/octance.py` in the Octane checkout or in the installed skill:

```sh
python3 "$OCTANCE_CLI" init
python3 "$OCTANCE_CLI" status
python3 "$OCTANCE_CLI" evidence add \
  --type source_observation \
  --description "The parser calls normalize_input before validation" \
  --file src/parser.py --symbol parse_input
python3 "$OCTANCE_CLI" claim add \
  --kind hypothesis --text "Normalization may change the value before validation" \
  --evidence EV-0001
python3 "$OCTANCE_CLI" brief --task "trace parser validation" --budget-tokens 1200
```

Replace `EV-0001` with the ID returned by `evidence add`. Use `languages` for a source inventory, `graph` for file dependencies, and `locate --trace-file failure.log` to rank files from a saved failure trace. CLI output defaults to JSON. See the [portable skill command reference](octance/SKILL.md) for output options and ledger maintenance.

## Data and execution

- Octane stores its native SQLite database and disposable workspaces under `$OPENCLAW_STATE_DIR/octane`, or `~/.openclaw/octane` when the variable is unset. OpenClaw tool policy and configured project roots govern access. Docker runs use disposable snapshots, disabled container networking, and time and resource limits.
- Octance stores its portable ledger under `.octance/` in the target project. `init` changes `.gitignore` only when given `--update-gitignore`. The portable CLI records observations and claims separately, analyzes files, and reads saved traces. Run tests through the host's approved tools.
- Keep credentials outside analyzed trees and evidence records. Octane and Octance make no model calls directly. The OpenClaw agent uses the model provider configured in OpenClaw.

## Development

From the repository root, use Python 3.11+ for the full suite:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
npm test
```

[CI](.github/workflows/tests.yml) tests the portable skill on Python 3.9, the full suite on Python 3.11, 3.12, and 3.14, the optional parser, the Node bridge and package, and the Docker repair demonstration.

See the [ledger architecture](docs/architecture.md) and [portable triage benchmark and limitations](docs/implementation-and-limitations.md) for implementation details.
