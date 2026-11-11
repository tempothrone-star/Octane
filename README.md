# Octance

Octance is a portable OpenClaw skill backed by a deterministic, Python-standard-library CLI. It gives the host agent a project-local evidence ledger and a small briefing path that survives session resets. The host agent remains the reasoner: Octance makes no model calls and is not an autonomous repair agent, server, or security sandbox.

## Install the skill

For a Git-based install, the repository root now contains an OpenClaw-discoverable `SKILL.md`:

```sh
openclaw skills install git:https://github.com/tempothrone-star/Octane.git
```

For a manual workspace install, copy the complete `octance/` directory so its `SKILL.md`, references, and helper script stay together:

```sh
mkdir -p "$OPENCLAW_WORKSPACE/skills"
cp -R ./octance "$OPENCLAW_WORKSPACE/skills/"
```

Start a new OpenClaw session or refresh the skill catalog, then check that `octance` appears in `openclaw skills list`. This repository does not publish the skill to ClawHub automatically. For users who need the optional native plugin tools as well, install the declared npm dependency and link the repository package:

```sh
npm install --omit=peer
openclaw plugins install --link . --force
```

The plugin manifest bundles this same `octance/` skill. The native plugin tools are optional, use a separate PIL-backed store, and require Node.js and Python 3.11+; the portable skill CLI remains standard-library-only and supports Python 3.9+.

The skill declares `python3` as its only binary requirement. Keep the target project as the CLI working directory. From this repository, use `python3 scripts/octance.py`; from a separately installed skill, invoke `python3 "$OPENCLAW_WORKSPACE/skills/octance/scripts/octance.py"` while the shell working directory is the target project root.

## Phase 1 CLI

Initialize the project-local ledger and inspect it:

```sh
python3 scripts/octance.py init
python3 scripts/octance.py status
python3 scripts/octance.py doctor
```

Record a direct observation, then a separate claim citing its evidence ID:

```sh
python3 scripts/octance.py evidence add \
  --type source_observation \
  --description "The parser calls normalize_input before validation" \
  --file src/parser.py --symbol parse_input
python3 scripts/octance.py claim add \
  --kind hypothesis --text "Normalization may change the value before validation" \
  --evidence EV-0001
python3 scripts/octance.py brief --task "trace parser validation" --budget-tokens 1200
```

Output is JSON by default. Use `--human` for readable output and `--format compact` for compact JSON. Agent identity comes from `OCTANCE_AGENT` unless overridden by `--agent`; the default local identity is `owner`. Token budgeting estimates tokens as characters divided by four. The CLI also provides `languages` for bounded source-extension inventory and `locate --trace-file failure.log` for heuristic file-level ranking from supplied traces and recent Git changes. These are static triage aids, not language parsers or root-cause findings. A predeclared replayable check runner, budgeted exploration, and benchmark-quality localization have not passed their gates; no continuity features are being added.

## Multilingual triage prototype

Inventory common source files without parsing or running them:

```sh
python3 scripts/octance.py languages
```

After you run an approved test using the host's authorized tools, save its plain-text failure trace inside the project, then ask Octance to rank file-level suspects:

```sh
python3 scripts/octance.py locate --trace-file .octance/failure.log --query "parse request"
```

The inventory recognizes built-in extensions across mainstream, functional, scripting, systems, hardware-description, and data languages (including Zig, Nim, Fortran, COBOL, Ada, Pascal, OCaml, Solidity, Verilog/VHDL, PowerShell, and more). It applies count/size limits, skips common vendor/generated directories, and reports unsupported extensions. Add project-specific language suffixes with a data-only `.octance/languages.json` registry. Definitions may also declare safe import forms (`quoted_import_keywords`, `module_import_keywords`, and `from_import`), for example `{"languages":{"quartz":{"extensions":[".qz"],"quoted_import_keywords":["fetch"]}}}` for `fetch "helper"` imports. Load it with `--language-config .octance/languages.json` on `languages`, `graph`, and `locate`. No project code, regexes, or plugins are loaded.

By default `graph` uses Python's AST, the optional Tree-sitter backend when installed, existing language-specific recognizers, and a conservative generic fallback for line-oriented imports. The default CLI remains Python 3.9+ and standard-library-only; installing the parser extra uses this package's Python 3.11+ requirement. To opt into the bundled Tree-sitter grammar set, install the optional extra from the repository root and request it explicitly:

```sh
python3 -m pip install '.[ast]'
python3 scripts/octance.py graph --parser tree-sitter
```

The default CLI remains standard-library-only. Tree-sitter produces concrete syntax trees, not compiler-semantic ASTs; its pinned offline grammar bundle covers many common languages, while unsupported grammars fall back to static import recognizers. The generic fallback supports `import`, `include`, `require`, `use`, `mod`, `source`, `load`, and configured `from ... import` forms, resolving project-local paths using registered suffixes. The graph is a best-effort dependency map, not a complete call graph or proof of runtime reachability. These commands do not modify source or execute project code. `locate` is heuristic file ranking, not a root-cause result; see [`docs/implementation-and-limitations.md`](docs/implementation-and-limitations.md) for the synthetic benchmark and its limits.

## Data and safety

- Each project stores its database and metadata under `<project_root>/.octance/`. Git top-level is selected when available; otherwise `init` uses the current directory.
- A UUID in `project.json` must match the database project record. Commands refuse on mismatch or after the project root is moved.
- Octance never edits analyzed source files. `init` only edits `.gitignore` when explicitly passed `--update-gitignore`; otherwise it recommends the entry.
- SQLite WAL is intended for local filesystems, not network filesystems. `doctor` warns when a known network filesystem is detected or the type is uncertain.
- The event chain provides tamper evidence, not prevention. A user with raw shell access can edit the database and its project metadata. Permissions are cooperative guardrails.
- Claims are separate from evidence. Phase 1 has no replayable checks, so nothing can be marked verified. Secret-like data is redacted on ingest, but do not submit credentials.
- OpenClaw sandboxing, shell availability, and filesystem visibility depend on the operator's configuration. The skill declares no sandbox guarantee and grants no tool permission.

## Development and current phase

The repository is building in gated phases. **Phase 1: thin core** is implemented here; later pinpointing and benchmark work must pass its own tests and go/no-go gate before continuity features are added.

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

The bundled CLI requires Python 3.9+ and the standard library only. No network access or telemetry is used by Octance. See [`octance/SKILL.md`](octance/SKILL.md) for the concise agent workflow and [`docs/architecture.md`](docs/architecture.md) for the event ledger and Phase 1 data model. Do not interpret unimplemented roadmap items as working functionality.
