# Revisions and staleness

Every Phase 1 evidence record and claim stores a SHA-256 revision label over project-relative file paths and file contents. `.git`, `.octance`, symlinks, and common generated/cache directories are excluded. Git is optional; the source hash is used either way. Source contents are not copied into SQLite.

**Phase 1 does not compute symbol footprints, compare old and current revisions, mark claims stale, propagate `needs_review`, or offer revalidation.** A changed revision therefore does not automatically invalidate or confirm a previous claim. Reopen the referenced source and repeat appropriate checks before relying on older findings.

The future `changes`/footprint implementation must distinguish comment-only edits from changes to relevant syntax and preserve prior evidence. Until that gate passes, do not call revision labels “precise staleness.”
