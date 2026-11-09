# Octane for OpenClaw

Octane is an **OpenClaw plugin with a companion skill**. It gives an OpenClaw agent optional tools for using project-scoped, persistent evidence, investigations, code-graph data, and isolated workspaces. The bundled Python/SQLite engine is an implementation component of this integration—not a separate autonomous agent or the product's primary interface.

Octane does not replace OpenClaw, supply its own model, or autonomously understand or repair a codebase. It provides bounded tools and persistence; the OpenClaw agent decides when and how to use them.

## Install and configure

The primary setup guide is [`openclaw/README.md`](openclaw/README.md). In brief, install dependencies and link the reviewed local checkout:

```sh
npm install --omit=peer
openclaw plugins install --link . --force
```

Configure at least one narrow, absolute `allowedProjectRoots` directory and explicitly allow only the Octane tools the agent needs in OpenClaw. Workspace execution is optional, Docker-only, and disabled unless an exact image is configured in `allowedDockerImages`. See the setup guide for a complete configuration, security guidance, and live Gateway smoke-test steps.

## What the plugin provides

Twelve optional tools expose the bundled engine's capabilities:

- Project registration, metadata, revision history, and bounded project context
- Conservative Python AST scanning and revision-aware graph data
- Provenance-bearing evidence, interpreted memories, and hypotheses
- Investigation checkpoints and handoffs
- Octane permission records and project audit search
- Heuristic prioritization of exploration candidates
- Optional disposable workspaces with Docker-only command execution

The companion `octane` skill guides the agent to preserve provenance, uncertainty, revision awareness, and safe handoffs. Skills do not grant tool permissions; OpenClaw policies determine which tools are available.

## Boundaries

- Source scanning is static and Python-only. It does not execute code or establish runtime behavior.
- The context tool retrieves bounded stored state; it is not automatic prompt injection or a replacement context engine.
- Scheduler scores are heuristics, not correctness, risk, or impact estimates.
- Workspace execution requires Docker and an explicit operator-allowlisted image. Docker is not a VM boundary; the daemon and image remain trusted infrastructure.
- The Octane agent tool manages Octane permission records, not OpenClaw Gateway agent configuration. Audit actors are fixed capability-scoped Octane identities, not verified OpenClaw agents or human initiators.
- The plugin and Gateway are trusted code. Review the package, use narrow project roots, and configure tool allowlists deliberately.

## Development

Run the Python engine and bridge tests and check the plugin package locally:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
node --check openclaw/index.js
python -m json.tool openclaw.plugin.json >/dev/null
npm pack --dry-run
```

A passing local test or syntax check is not a live OpenClaw validation. This repository has no Gateway runtime in its development environment; perform the install, runtime inspection, and scoped tool-call smoke test described in [`openclaw/README.md`](openclaw/README.md) on a supported Gateway before deployment.

For the bundled engine's data model and internal implementation details, see [`docs/architecture.md`](docs/architecture.md). The repository's current scope and limitations are documented in [`docs/openclaw.md`](docs/openclaw.md).
