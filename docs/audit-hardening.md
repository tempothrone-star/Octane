# November 20, 2026 audit closure

This follow-up checks the supplied audit against the merged Octane 1.0.0 source. Package versions remain 1.0.0; changes are recorded as unreleased. The engine still makes no model calls and the same twelve tools remain registered.

## Numbered findings

| Audit item | Resolution and regression coverage |
| --- | --- |
| 1. Quadratic scrubber | Both stores share `redaction.py`. Assignment recognition starts at token boundaries and processes each candidate once. A subprocess timeout guards regression tests against hangs on megabyte tokens. Local one-megabyte input took about 0.12 s. |
| 2. Killed bridge leaves containers | Containers have a unique name, storage-owner label, recorded run ID and Unix deadline. SIGTERM unwinds cleanup. An independent stdlib helper survives a killed bridge and removes the named container by the deadline. Before new execution, the engine recovers only expired jobs belonging to that storage root and closes their running rows, including rows whose auto-removed container is gone. The trusted watchdog preserves engine socket/context settings and a bounded host environment, without forwarding arbitrary secrets. Real Docker tests kill the parent with SIGTERM and SIGKILL. |
| 3. Missed secret formats | Quoted JSON keys/values, values with spaces and escaped quotes, Basic/Bearer headers, Slack and Google token formats are scrubbed. Existing GitHub/OpenAI/AWS/private-key and URL credential handling remains. Redaction tests cover both stores. |
| 4. Odd Python file aborts scan | Python parser entry points catch recursion and allocation failures. Native and portable scans retain neighboring valid files and record parse errors; mutation generation records a parse-error observation and leaves source reversion distinct. A 60,000-term file and injected allocation errors are covered. An iterative 128-depth/250,000-node AST budget protects recursive visitors even on Python 3.14, which accepts deeper parse trees. |
| 5. Host LSP trust | Host language servers are explicitly a trusted-code integration. A provider object must contain `trustedProject: true`; arrays or omitted/false flags retain static fallback without spawning a provider. Operator-specified server binaries can load project configuration/plugins, so this opt-in is unsuitable for untrusted roots. The model cannot choose server commands through tool arguments. |
| 6. Ledger append/open cost | Append verifies the externally anchored tail and unanchored suffix rather than replaying all history. Full `verify-log`/`rebuild-check` remain explicit and still detect older corruption. Current native/portable stores reopen without schema write transactions or FTS rebuilds. Local append means were 7.30 ms after 100 events and 7.06 ms after 800; a full 823-event verification took 14.19 ms. These are local measurements, not latency promises. |
| 7. XML entity bypass | Decode UTF-8 strictly, reject NULs, reject incompatible encoding declarations and reject DOCTYPE/entity declarations before ElementTree sees text. Tests cover UTF-8/16/32 attacks and valid UTF-8 reports. No extra XML dependency is needed. |
| 8. Archive symlinks | Symlink archive members are skipped without following targets. Bisect/evaluation evidence lists skipped paths. Other special files and unsafe archive paths still fail closed. Checks depending on an omitted symlink may need environment preparation; omission is not execution equivalence. |
| 9. Added/deleted files omitted | Workspace comparison emits complete unified additions/deletions, `/dev/null` headers and Git mode headers, including empty files. `patch` can create regular text files and `remove` explicitly deletes them. The proposal gate accepts these changes while retaining binary/symlink/mode-change rejection and existing test/build protections. Git apply checks the emitted patch. Mutation generation still requires edits to existing source files. |
| 10. Shared MCP attribution | Fresh connection/session suffixes are now the default, including anonymous clients. Two stdio connections declaring identical client name/version get different actors. Explicit false or a fixed suffix retains operator-selected stable attribution. This is provenance metadata, not authentication. |

Append verification intentionally has a narrower scope than full history verification: it cannot discover arbitrary corruption strictly before the anchored tail. Run full verification at review/handoff and before trusting historical claims. Local administrative access remains outside the integrity boundary.

## Additional hardening

- The POSIX bounded-process runner observes exit through waitid or macOS kqueue without reaping the group leader, then signals its private group and reaps it. Darwin zombie-only group EPERM is handled separately. LSP close avoids signaling a PID already reaped by a caller. Descendant cleanup is a POSIX feature, not a Windows process-tree isolation claim.
- LSP queue overflow has an explicit error checked by requests, with short queue waits for other reader failures. A flood regression does not wait out the 45-second analysis timeout.
- Stack parsing skips lines above 1 KiB, caps input/frame counts, anchors generic path tokens at boundaries and removes the erroneous verbose-regex mode for JS frames. Long blobs and repeated `at` inputs are timed; named and anonymous JS frames retain their format.
- Canonical roots and platform file-URI conversion handle macOS path aliases and Windows drive/short-name paths in LSP mapping and property checks.
- Private holdout mount arguments use CSV escaping, including commas in operator paths.
- The optional grammar provider uses the bundled 0.13 language pack and Tree-sitter 0.25.2, with the current QueryCursor API. This replaces `tree-sitter-languages` and has ABI3 wheels for Python 3.13/3.14. The language-pack 1.x loader downloads parsers on demand; it stays excluded until an offline-loader review. Installed parser/query tests preserve static fallback for unavailable grammars.
- The former 512 KiB `fsize` limit applied to every output file during a trace. All executions now use a documented 64 MiB per-file limit. A traced program writing a 2 MB file is tested. This is not an aggregate writable-workspace quota; use filesystem quotas for that requirement.
- CI expands parser installation to Python 3.12/3.13/3.14 and adds Windows/macOS portable-host tests. Linux Docker tests remain the execution/deadline validation environment.

## Remaining architectural proposals

A persistent worker, mandatory digest-only image allowlists, automatic npm publication with provenance, CodeQL/type-checker/pip-audit rollout, broad lint expansion and aggregate workspace filesystem quotas are not silently enabled by this patch. They change lifecycle, deployment or contributor policy and need independent implementation/validation. Image tags remain accepted for operator compatibility, but each run resolves and records an immutable image ID; prefer `name@sha256:...` in allowlists. No package publication or third-party messages were sent.

A deadline helper cannot remove a container while the engine daemon or host is unavailable. Recovery runs before the next execution when the engine is reachable. This approach is portable across approved images and does not require injecting a particular language interpreter or a `timeout` binary into the container. It is an independent host deadline, not a claim that arbitrary images contain a built-in self-termination mechanism.

## Primary references

- [Bundled language-pack 0.13 distribution files](https://pypi.org/project/tree-sitter-language-pack/0.13.0/).
- [Current language-pack installation and parser downloads](https://docs.tree-sitter-language-pack.xberg.io/getting-started/installation/).
- [Docker run limits, labels and mounts](https://docs.docker.com/reference/cli/docker/container/run/).

## Validation recorded before PR publication

- Full Linux Docker/property suite: 267 tests, three expected optional-provider skips; subsequent integrity/process/patch regressions also pass. CI will rerun the complete final suite.
- Python 3.9 checkout suite: 49 passed. Node/MCP/package suite: six passed, including identical-client-name attribution.
- Docker-backed overall coverage: 88%. Proposal gate/storage/test protection 100%; language server 96%, workspace 87%, mutation 96%. Follow-up tests bring the process runner to 94% and append integrity to 100%.
- Production complexity gate: 527 functions/methods, nine existing non-growing exceptions. Curated Ruff and whitespace checks pass.
- A clean dependency-free 1.0.0 wheel installs and exercises diagnosis fallback, redaction and new-file comparison. Parser wheels for Python 3.14 were downloaded successfully; hosted parser and platform jobs will verify their installations.

These local counts refer to the recorded commands, not a claim that skipped deployment providers were exercised.
