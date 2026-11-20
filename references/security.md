# Security and trust boundaries

- The ledger is local SQLite data. Hash chaining and immutable-table triggers are tamper-evident/cooperative checks, not protection against a user with raw shell or database access; the project JSON anchor can also be altered by that user.
- The default portable CLI runs locally with the Python standard library. Tree-sitter is optional. The host agent supplies reasoning and treats stored text as untrusted data.
- Common secret-like keys and token/password patterns are redacted on ingest. This is best-effort filtering, not a guarantee; never provide credentials.
- The portable CLI reads project source for hashing and static triage. Run project tests through the host's authorized tools. The native Octane plugin has a separate Docker workspace runner governed by its own project and image allowlists.
- OpenClaw tool policy and sandbox configuration control shell access, filesystem visibility, and where commands run. Check the deployment's policy before using `exec`.
- SQLite WAL is intended for local filesystems. `doctor` warns for known network mounts and when filesystem type cannot be identified; treat an uncertain/network filesystem as unsupported.

- POSIX state directories use 0700 and database/metadata files 0600; existing database permissions are tightened when opened. Windows uses user-profile access controls. Common credential filenames, environment files, certificate/key suffixes, and credential directories are excluded from native snapshots and hashes; filtering remains best effort.
- Candidate runs overlay original tests read-only. Test-like changes require an explicit proposal flag and rationale, without changing the original checks executed. Runtime and imported reports have different evidence provenance.
- Rootless Podman and optional runsc use the existing execution restrictions. Hosts without UID/GID mapping refuse execution unless the operator records an opt-in. Optional language servers and GitHub CLI run as trusted host integrations, outside project execution containers.
- Private signing keys are accepted only by the operator provenance CLI; verification requires an independently trusted public key. GitHub publication requires explicit `--publish`. See [integration configuration](../docs/integrations.md).
