# Reporting security issues

Report vulnerabilities privately through [GitHub Security Advisories](https://github.com/tempothrone-star/Octane/security/advisories/new). Include the affected revision, a minimal reproduction, the expected boundary, and the observed behavior. Use synthetic credentials and remove personal data from logs.

The current development branch receives fixes. Run released updates with the operator's project, image, and tool policies configured. Container engines, selected images, language server binaries, and host access remain part of the deployment's trust boundary.

Snapshot secret-file filtering and evidence redaction reduce accidental inclusion. Review source trees and reports before submitting them.

## Host analysis and storage limits

Language-server integrations execute operator-selected host programs and may load project configuration/plugins. They require explicit `trustedProject: true` and are unsuitable for untrusted checkouts; static analysis remains available without host providers. Container commands remain isolated from the host.

Each container has an owner label, deadline label and recorded run identity. An independent helper removes it at the crash deadline (execution timeout plus ten seconds); SIGTERM also unwinds immediate cleanup. Expired owned containers and interrupted run rows are recovered before the next container execution. Helpers require the local container daemon to remain available; daemon/host failure requires recovery when execution resumes. Unrelated owners and unexpired concurrent jobs are preserved.

Container memory, processes and output are bounded. The 64 MiB `fsize` limit applies to each regular file written by the process, not only tracing; it does not bound the aggregate size of a host bind mount. Use a quota-limited workspace filesystem when running untrusted writable builds. `/tmp` remains a 64 MiB tmpfs. Octane does not claim that Docker `--storage-opt` would quota bind-mounted workspaces.

Scrubbing covers common assignments, quoted JSON values, Basic/Bearer authorization, GitHub/OpenAI/AWS/Slack/Google key forms and private keys. It cannot recognize every possible secret encoding or neutral field name; never expose credentials to project code or include them in evidence. Full ledger history integrity is checked with `verify-log` and `rebuild-check`; writes check the anchor/tail and any unanchored suffix. Local administrator access can alter SQLite and its anchor.
