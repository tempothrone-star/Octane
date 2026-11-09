# Security and trust boundaries

- The ledger is local SQLite data. Hash chaining and immutable-table triggers are tamper-evident/cooperative checks, not protection against a user with raw shell or database access; the project JSON anchor can also be altered by that user.
- Octance uses no network services, telemetry, model SDK, or third-party Python packages. The host agent supplies reasoning and must treat stored text as untrusted data, not executable instructions.
- Common secret-like keys and token/password patterns are redacted on ingest. This is best-effort filtering, not a guarantee; never provide credentials.
- The CLI only reads project source to hash it. It does not run or patch source code. The future check runner will require explicit `--allow-exec`, a project allowlist, and a snapshot.
- A skill does not grant shell or filesystem access. OpenClaw tool policy and sandbox configuration control execution. OpenClaw documentation says sandboxing is off by default; `exec` may run on the Gateway host when no sandbox is active. A skill cannot guarantee network or filesystem isolation.
- SQLite WAL is intended for local filesystems. `doctor` warns for known network mounts and when filesystem type cannot be identified; treat an uncertain/network filesystem as unsupported.
