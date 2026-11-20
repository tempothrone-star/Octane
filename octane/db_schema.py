"""Versioned native PIL schema; data stays in the existing database."""
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL, revision TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
 role TEXT NOT NULL, capabilities_json TEXT NOT NULL, permissions_json TEXT NOT NULL,
 current_investigation_id TEXT, last_activity TEXT NOT NULL, UNIQUE(project_id,name));
CREATE TABLE IF NOT EXISTS files(
 project_id TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL, sha256 TEXT NOT NULL,
 revision TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
 PRIMARY KEY(project_id,path));
CREATE TABLE IF NOT EXISTS graph_nodes(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), node_key TEXT NOT NULL,
 kind TEXT NOT NULL, name TEXT NOT NULL, path TEXT, line INTEGER, language TEXT,
 revision TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL,
 UNIQUE(project_id,node_key));
CREATE TABLE IF NOT EXISTS graph_edges(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), source_id TEXT NOT NULL REFERENCES graph_nodes(id),
 target_id TEXT NOT NULL REFERENCES graph_nodes(id), kind TEXT NOT NULL, revision TEXT NOT NULL,
 stale INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL,
 UNIQUE(project_id,source_id,target_id,kind));
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), agent_id TEXT REFERENCES agents(id),
 evidence_type TEXT NOT NULL, payload_json TEXT NOT NULL, description TEXT NOT NULL,
 collected_at TEXT NOT NULL, revision TEXT NOT NULL, source_path TEXT, source_line INTEGER,
 verification_status TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS evidence_relationships(
 evidence_id TEXT NOT NULL REFERENCES evidence(id), related_evidence_id TEXT NOT NULL REFERENCES evidence(id),
 relationship TEXT NOT NULL, PRIMARY KEY(evidence_id,related_evidence_id,relationship));
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), agent_id TEXT REFERENCES agents(id),
 category TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, revision TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_evidence(
 memory_id TEXT NOT NULL REFERENCES memories(id), evidence_id TEXT NOT NULL REFERENCES evidence(id),
 PRIMARY KEY(memory_id,evidence_id));
CREATE TABLE IF NOT EXISTS investigations(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), objective TEXT NOT NULL,
 status TEXT NOT NULL, revision TEXT NOT NULL, created_by TEXT REFERENCES agents(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS investigation_checkpoints(
 id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL REFERENCES investigations(id),
 agent_id TEXT REFERENCES agents(id), status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hypotheses(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), investigation_id TEXT REFERENCES investigations(id),
 agent_id TEXT REFERENCES agents(id), statement TEXT NOT NULL, status TEXT NOT NULL,
 confidence REAL NOT NULL, revision TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hypothesis_evidence(
 hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id), evidence_id TEXT NOT NULL REFERENCES evidence(id),
 relation TEXT NOT NULL, PRIMARY KEY(hypothesis_id,evidence_id,relation));
CREATE TABLE IF NOT EXISTS project_revisions(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), revision TEXT NOT NULL,
 detected_at TEXT NOT NULL, changes_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL,
 mode TEXT NOT NULL, base_revision TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_activity(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), agent_id TEXT REFERENCES agents(id),
 action TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), agent_id TEXT REFERENCES agents(id),
 action TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_evidence_project_type ON evidence(project_id,evidence_type,revision);
CREATE INDEX IF NOT EXISTS idx_evidence_path ON evidence(project_id,source_path);
CREATE INDEX IF NOT EXISTS idx_nodes_project_kind ON graph_nodes(project_id,kind,stale);
CREATE INDEX IF NOT EXISTS idx_edges_source ON graph_edges(project_id,source_id,stale);
CREATE INDEX IF NOT EXISTS idx_memory_project_status ON memories(project_id,status,category);
CREATE INDEX IF NOT EXISTS idx_investigations_status ON investigations(project_id,status);
CREATE INDEX IF NOT EXISTS idx_checkpoints_investigation ON investigation_checkpoints(investigation_id,created_at);
"""

_MIGRATION_4 = (
    "ALTER TABLE investigations ADD COLUMN assigned_agent_id TEXT REFERENCES agents(id)",
)

_MIGRATION_5 = (
    "CREATE TABLE exploration_runs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), investigation_id TEXT NOT NULL REFERENCES investigations(id), revision TEXT NOT NULL, profile_json TEXT NOT NULL, state_json TEXT NOT NULL, status TEXT NOT NULL, created_by TEXT REFERENCES agents(id), updated_at TEXT NOT NULL)",
    "CREATE INDEX idx_exploration_investigation ON exploration_runs(investigation_id,updated_at)",
    "ALTER TABLE workspaces ADD COLUMN baseline_path TEXT",
    "ALTER TABLE workspaces ADD COLUMN snapshot_hash TEXT",
    "CREATE TABLE execution_runs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), workspace_id TEXT NOT NULL REFERENCES workspaces(id), investigation_id TEXT REFERENCES investigations(id), evidence_id TEXT REFERENCES evidence(id), command_json TEXT NOT NULL, image TEXT NOT NULL, image_id TEXT, before_hash TEXT NOT NULL, after_hash TEXT, snapshot_hash TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX idx_execution_investigation ON execution_runs(investigation_id,created_at)",
    "CREATE TABLE patch_proposals(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), investigation_id TEXT NOT NULL REFERENCES investigations(id), workspace_id TEXT NOT NULL REFERENCES workspaces(id), baseline_run_id TEXT NOT NULL REFERENCES execution_runs(id), candidate_run_id TEXT NOT NULL REFERENCES execution_runs(id), evidence_id TEXT NOT NULL REFERENCES evidence(id), created_at TEXT NOT NULL)",
)

_MIGRATION_6 = (
    "ALTER TABLE workspaces ADD COLUMN source_revision TEXT",
)

_MIGRATION_3 = (
    "CREATE TABLE graph_edge_history(id TEXT PRIMARY KEY, edge_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id), source_id TEXT NOT NULL, target_id TEXT NOT NULL, kind TEXT NOT NULL, revision TEXT NOT NULL, stale INTEGER NOT NULL, metadata_json TEXT NOT NULL, confidence REAL NOT NULL, provenance_agent_id TEXT, evidence_id TEXT, recorded_at TEXT NOT NULL)",
    "CREATE INDEX idx_graph_edge_history_project ON graph_edge_history(project_id,revision,recorded_at)",
)

_MIGRATION_2 = (
    "ALTER TABLE projects ADD COLUMN languages_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE projects ADD COLUMN repository_url TEXT",
    "ALTER TABLE projects ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE projects ADD COLUMN last_indexed_at TEXT",
    "ALTER TABLE files ADD COLUMN mtime_ns INTEGER",
    "ALTER TABLE investigations ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE graph_edges ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0",
    "ALTER TABLE graph_edges ADD COLUMN provenance_agent_id TEXT REFERENCES agents(id)",
    "ALTER TABLE graph_edges ADD COLUMN evidence_id TEXT REFERENCES evidence(id)",
    "UPDATE hypotheses SET status='proposed' WHERE status='open'",
    "UPDATE hypotheses SET status='inconclusive' WHERE status='unresolved'",
)
