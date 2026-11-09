# Legacy Octane OpenClaw plugin notes (archived)

> These notes describe the superseded native `octane` plugin implementation. They are retained for historical context only and do not describe the current skill-first `octance` deliverable. The supported Phase 1 artifact is [`../octance/`](../octance/); do not use the plugin install instructions or treat its capabilities as implemented by Octance.

## Research snapshot

Reviewed OpenClaw's official documentation on [skills](https://docs.openclaw.ai/tools/skills), [creating skills](https://docs.openclaw.ai/tools/creating-skills), [building plugins](https://docs.openclaw.ai/plugins/building-plugins), [tool plugins](https://docs.openclaw.ai/plugins/tool-plugins), [plugin manifests](https://docs.openclaw.ai/plugins/manifest), [plugin SDK stability](https://docs.openclaw.ai/plugins/sdk-overview), [plugin installation](https://docs.openclaw.ai/tools/plugin), and [prompt-injection defenses](https://docs.openclaw.ai/gateway/security/prompt-injection).

The relevant contracts and implications are:

1. OpenClaw skills are `SKILL.md` instruction packages discovered from configured roots. Plugins can ship skill directories by declaring them in `openclaw.plugin.json`; the skill teaches a workflow but does not grant the tools it references.
2. New runtime capabilities require a native plugin. Tools registered at runtime must have corresponding `contracts.tools` ownership in the manifest; optional tools should be marked optional in both the registration and `toolMetadata` and explicitly allowlisted by the operator.
3. Native plugins are trusted Gateway-process code, not a sandbox for third-party code. Installation, enabling, and tool authorization are separate decisions. The Octane package intentionally ships a skill plus an optional-tools plugin rather than pretending a skill alone can add persistent storage or capabilities.
4. OpenClaw's plugin SDK is documented as experimental, so package compatibility is conservative and deployments must inspect and exercise the actual runtime against their pinned Gateway version.
5. Repository source, tool results, logs, and fetched documents can contain prompt-injection text. The skill treats those contents as untrusted; actual security boundaries remain tool policy, narrow project roots, Docker restrictions, and operator review.

## Integration structure

- `openclaw.plugin.json` is the static discovery/validation manifest and declares all twelve tool names, optional status, skill root, and configuration schema.
- `package.json` makes this same repository a Node ESM plugin package and includes the existing Python package as its single source of truth. The OpenClaw plugin implementation is `openclaw/index.js`. This first integration is deliberately a tool-and-skill adapter, not an exclusive memory-slot/context-engine replacement or automatic prompt injector: the skill guides the agent to retrieve scoped project knowledge when working on the project.
- `openclaw/skills/octane/SKILL.md` is the installable agent workflow guide.
- `octane/openclaw_bridge.py` is a narrow JSON bridge around existing PIL APIs. It accepts only named operations. The Node plugin starts it with a fixed argument vector (`shell: false`) and JSON over stdin, not a user-constructed shell command.
- The existing SQLite database and Python APIs remain authoritative. There is no second JavaScript database or copied schema.

## Tool permission and data boundaries

All twelve registered tools are optional. An administrator must enable the plugin and explicitly allow the required tool names (or the plugin's tools under their OpenClaw policy). Plugin configuration requires one or more absolute, existing `allowedProjectRoots`. The plugin resolves them before each call; the Python bridge independently enforces containment when registering or accessing stored projects and filters project listings to those roots. A record created before an allowlist change is not automatically accessible if its root is no longer allowed. Docker execution is denied unless the exact image string is also in the optional `allowedDockerImages` configuration; pin reviewed images by digest where possible.

The tool families cover project metadata and bounded context, Python scanning, evidence and interpreted memory, Octane agent permission records, audit search, revision-aware investigations, graph queries/updates/traversal, hypotheses, exploration scheduling, and optional disposable workspaces. `octane_context` is a bounded retrieval helper, not automatic prompt injection; `octane_schedule` prioritizes caller-supplied candidates using heuristics, not correctness or impact estimates.

`octane_agents` operates only on Octane's project-scoped agent records; it does not create or configure OpenClaw Gateway agents. Creation is disabled by default and, when explicitly enabled with `allowAgentProvisioning`, permits only fixed least-privilege role presets. Bridge operations currently use fixed capability-scoped Octane actors. Audit attribution therefore identifies the bridge capability, not a verified OpenClaw runtime agent or human caller; it must not be presented as trustworthy per-agent identity.

The tool-facing `octane_evidence` default is `unverified`; a model cannot set the database's reserved `verified` value through the ordinary evidence-add action. Verification uses the existing check that requires separate, observed verification evidence plus verifier permission. Per-operation internal agents are registered with only the matching PIL permission. Project source code is scanned but never run by `octane_scan`.

`octane_workspace` is optional and should remain outside the allowlist unless required. It only uses the existing Docker-backed execution path, requires an explicitly allowlisted image and command, enforces a 300-second maximum for bridge calls, disables Docker networking, and has no host execution fallback. Docker is not a VM boundary; the host, Docker daemon, and selected image remain trusted infrastructure. Runtime behavior was not tested in this development environment.

## Local developer checks

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
node --check openclaw/index.js
python -m json.tool openclaw.plugin.json >/dev/null
python -m json.tool package.json >/dev/null
```

A successful local syntax/bridge test does not establish that the plugin loads in OpenClaw. On a supported and pinned Gateway, review the source, link/install it locally, inspect `openclaw plugins inspect octane --runtime --json`, list skills, and make a scoped tool call. The development container has Node 22.22.3, below the currently documented plugin tool runtime floor, and has no OpenClaw Gateway. Do not claim an actual Gateway test until it is run in that environment.
