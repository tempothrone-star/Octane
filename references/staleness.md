# Portable Octane revisions and staleness

Every portable evidence record and claim stores a SHA-256 revision label over project-relative file paths and file contents. The hash excludes `.git`, `.octane`, symlinks, and common generated or cache directories. It works with or without Git. Hashing does not copy source contents into SQLite.

Use the label to identify the recorded source state. Reopen the referenced source and repeat appropriate checks before relying on older findings. Claim status changes in the portable ledger are explicit. Use `claim status CL-0001 --status needs_review` when a review is needed.
