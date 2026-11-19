# Security and trust boundaries

- The ledger is local SQLite data. Hash chaining and immutable-table triggers are tamper-evident/cooperative checks, not protection against a user with raw shell or database access; the project JSON anchor can also be altered by that user.
- The default portable CLI runs locally with the Python standard library. Tree-sitter is optional. The host agent supplies reasoning and treats stored text as untrusted data.
- Common secret-like keys and token/password patterns are redacted on ingest. This is best-effort filtering, not a guarantee; never provide credentials.
- The portable CLI reads project source for hashing and static triage. Run project tests through the host's authorized tools. The native Octane plugin has a separate Docker workspace runner governed by its own project and image allowlists.
- OpenClaw tool policy and sandbox configuration control shell access, filesystem visibility, and where commands run. Check the deployment's policy before using `exec`.
- SQLite WAL is intended for local filesystems. `doctor` warns for known network mounts and when filesystem type cannot be identified; treat an uncertain/network filesystem as unsupported.
