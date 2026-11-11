# Optional Octane OpenClaw extension

Octane's primary deliverable is the portable, standard-library skill in [`../octance/`](../octance/). This optional native extension ships the same skill through `openclaw.plugin.json` and additionally registers optional PIL-backed tools. The extension and the skill CLI are separate interfaces: installing the skill does not require this plugin, and the plugin's Python engine requires Python 3.11 or newer.

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

`typebox` is a package dependency and is installed by npm; OpenClaw itself is an optional peer supplied by the Gateway runtime. The plugin manifest bundles the current skill from `octance/`. The historical `openclaw/skills/` directory is not listed as an installable skill root.

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

All tools are optional and should be separately authorized by operator policy. Plugin installation, activation, filesystem access, and tool authorization are distinct decisions. The plugin is trusted Gateway-process code; it is not a sandbox. Review and pin the package before deployment.

## Boundaries and validation

The plugin resolves configured absolute `allowedProjectRoots` before calls and independently enforces containment in its Python bridge. Docker execution is denied unless the exact image string appears in `allowedDockerImages`; it has no host-execution fallback. Docker is not a VM boundary. The plugin's PIL engine is separate from Octance's standard-library ledger and project-local `.octance/` database.

Local checks include `node --check openclaw/index.js`, JSON validation of the manifests, and the Python test suite. They do not establish that a plugin loads in a live OpenClaw Gateway. Gateway behavior remains unverified until tested against the deployed, pinned version.
