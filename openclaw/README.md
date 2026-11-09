# Octane for OpenClaw

Octane is distributed as a native OpenClaw plugin **and** a companion `octane` skill. The plugin exposes the existing Python PIL through twelve optional OpenClaw tools; the skill teaches the agent to use those tools with provenance, uncertainty, revision awareness, and safe handoff practices.

## Requirements

- OpenClaw `>=2026.5.17` (plugin APIs are experimental; validate against the Gateway version you deploy).
- Node.js `>=24.22.1` or `>=26.12.1`, per the current OpenClaw plugin requirements.
- Python `>=3.11` on the Gateway host.
- Docker only when using isolated workspace command execution.

## Install from a local checkout

From the repository root, install the plugin dependency and then link the reviewed local package:

```sh
npm install --omit=peer
openclaw plugins install --link . --force
```

The plugin package root is the repository root. The `--force` flag acknowledges a local source; review the source before installing. OpenClaw's plugin runtime is trusted code running in the Gateway process.

Configure the plugin and explicitly allow only the tools needed by the agent. For example, in `openclaw.json`:

```json5
{
  plugins: {
    entries: {
      octane: {
        enabled: true,
        config: {
          allowedProjectRoots: ["/home/alex/work"],
          // Required only for workspace command execution; exact-match allowlist.
          allowedDockerImages: ["python:3.12-slim"],
          // Optional; default is $OPENCLAW_STATE_DIR/octane/pil.sqlite3
          databasePath: "/home/alex/.openclaw/octane/pil.sqlite3",
          // Optional; default is $OPENCLAW_STATE_DIR/octane/workspaces
          workspaceRoot: "/home/alex/.openclaw/octane/workspaces",
          // Optional; default is python3 (python on Windows)
          pythonExecutable: "python3",
          // Optional; agent records use fixed Octane roles, not OpenClaw runtime identity.
          // Keep false unless this Gateway is authorized to provision Octane agent records.
          allowAgentProvisioning: false
        }
      }
    }
  },
  tools: {
    allow: [
      "octane_project", "octane_context", "octane_scan", "octane_evidence",
      "octane_memory", "octane_agents", "octane_audit", "octane_investigation",
      "octane_graph", "octane_hypothesis", "octane_schedule"
      // Add "octane_workspace" only if isolated workspace actions are wanted.
    ]
  }
}
```

`allowedProjectRoots` is required, must name existing directories, and is enforced in both the plugin and Python bridge. Project registration, listing, scanning, and stored-project access are limited to those roots. Use a narrow directory; do not set it to `/`, a home directory containing secrets, or another unnecessarily broad location. Configure a project-specific agent allowlist as well if other agents should not call Octane. Workspace command execution is denied unless `allowedDockerImages` contains the exact selected image reference; pin reviewed images by digest where possible. Keep `octane_workspace` out of the tool allowlist unless needed.

Enable the plugin if it is not already enabled, then inspect the live registration and use a tool call to smoke-test it:

```sh
openclaw plugins enable octane
openclaw plugins inspect octane --runtime --json
openclaw skills list
```

If `plugins.allow` is configured, add `octane` to it. Tool registration does not bypass OpenClaw's tool allowlists. A plugin skill is loaded only while the plugin is enabled; a skill does not itself grant tool permissions.

## Tool inventory

- `octane_project`: list/create/show/update projects and inspect revision changes.
- `octane_context`: retrieve a bounded project snapshot across revisions, changes, evidence, memories, investigations, hypotheses, and graph nodes; optionally refresh the static scan.
- `octane_scan`: deterministic Python AST scan and file hashing; does not execute source.
- `octane_evidence`: search/read/add/link evidence and verify a claim against a separate observed verification record.
- `octane_memory`: search/read/add/status for interpreted knowledge linked to evidence.
- `octane_agents`: list/show Octane's project-scoped permission records, check a permission, and optionally provision a fixed reader/explorer/investigator/verifier role. Provisioning is disabled unless `allowAgentProvisioning` is explicitly true.
- `octane_audit`: search a bounded project audit trail.
- `octane_investigation`: create/search/read/update/checkpoint/resume/complete/abandon revision-aware investigations.
- `octane_graph`: query/update the graph, inspect dependencies/history, and perform bounded traversal.
- `octane_hypothesis`: manage explicit hypotheses and support/contradiction links.
- `octane_schedule`: prioritize supplied candidate exploration work under explicit budgets using PIL heuristics.
- `octane_workspace`: create disposable snapshots, patch experimentation copies, compare diffs, run a command through Docker only, and destroy workspaces. This is optional and should only be allowlisted when needed.

The tools call the Python package in a fresh child process with JSON over stdin/stdout. The plugin uses fixed argument vectors and never builds a shell command. It does not embed a second copy of Octane's SQLite implementation. SQLite remains the single persistence source of truth. Python source is packaged alongside the plugin.

## Development and verification

Python functionality and the bridge are tested with:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

Check the plugin module syntax with:

```sh
node --check openclaw/index.js
```

For actual OpenClaw verification, run the install, runtime inspect, and a minimal tool invocation on a Gateway version supported by your deployment. Plugin SDK APIs are experimental; this repository has no OpenClaw Gateway checkout/runtime, and Node 22 in the development environment is below OpenClaw's currently documented plugin requirement. Do not represent static checks as a live Gateway test.

## Current boundaries

The integration makes OpenClaw's existing agent capable of using Octane's local, persistent PIL; it does **not** add an LLM or turn deterministic scanning into autonomous root-cause analysis. The `octane_agents` tool manages Octane's own permission records, not OpenClaw Gateway agent configuration. Bridge operations currently use fixed capability-scoped Octane actors, so audit attribution identifies the bridge capability rather than a verified OpenClaw runtime agent or an independently verified human. Do not treat it as proof of who initiated a tool call. It is a tool-and-skill integration, not a replacement OpenClaw context engine or automatic memory injector: the agent uses the skill-guided tools to retrieve project knowledge, preserving compatibility with the operator's existing memory system and avoiding implicit cross-project prompt injection. Python AST coverage remains conservative. Runtime experimentation is Docker-only, requires an explicit image and command, disables container networking, and is not a VM boundary. The plugin and Gateway are trusted code; review changes and use narrow roots, tool allowlists, and a trusted Docker daemon.
