"""SQLite-backed Project Intelligence Layer."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit


_INITIALIZE_LOCK = threading.RLock()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_repository_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    if "@" in value and ":" in value.split("@", 1)[1]:
        return value.split("@", 1)[1]
    return value




def _scrub_sensitive_text(value: str) -> str:
    """Redact common bearer/basic-auth and assignment secrets before persistence."""
    import re
    value = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", value, flags=re.S)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)(://)[^/@:\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    value = re.sub(r"(?i)((?:[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|credential|private[_-]?key)[A-Za-z0-9_-]*)\s*[:=]\s*)([^\s,;]+)", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b", "[REDACTED TOKEN]", value)
    return value

def _git_remote_url(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return _sanitize_repository_url(value) if value else None


def _project_relative_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts or ".." in path.parts
            or (len(path.parts[0]) == 2 and path.parts[0][1] == ":")):
        raise ValueError("source paths must be project-relative")
    return path.as_posix()


def _normalize_languages(languages) -> list[str]:
    if isinstance(languages, str):
        languages = [languages]
    try:
        values = list(languages)
    except TypeError as exc:
        raise TypeError("languages must be a string or iterable of strings") from exc
    if any(not isinstance(language, str) or not language.strip() for language in values):
        raise ValueError("language names must be non-empty strings")
    return sorted({language.strip() for language in values})


def _scrub_sensitive_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_sensitive_text(value)
    if isinstance(value, dict):
        return {key: _scrub_sensitive_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_sensitive_value(item) for item in value]
    return value


def _reject_secret_keys(value: Any, parent: str = "") -> None:
    sensitive = ("password", "secret", "token", "api_key", "apikey", "credential", "private_key")
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if any(term in normalized for term in sensitive):
                raise ValueError(f"project configuration key {parent + str(key)!r} resembles a secret; secrets are not stored")
            _reject_secret_keys(nested, parent + str(key) + ".")
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secret_keys(nested, parent)


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


class ProjectIntelligence:
    """Small, transaction-safe SQLite API shared by deterministic agents."""

    def __init__(self, database: str | Path):
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("ProjectIntelligence requires a file-backed SQLite database")
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        # SQLite's journal-mode change and schema migrations must not race when
        # multiple API instances initialize database files concurrently.
        with _INITIALIZE_LOCK:
            self._initialize_database()

    def _initialize_database(self) -> None:
        with self._session() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", (_now(),))
            current = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if current > 4:
                raise RuntimeError(f"database schema version {current} is newer than this Octane supports")
            for version, statements in ((2, _MIGRATION_2), (3, _MIGRATION_3), (4, _MIGRATION_4)):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                    if current >= version:
                        connection.commit()
                        continue
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations VALUES(?,?)", (version, _now()))
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_project(self, name: str, root: str | Path, revision: str = "unscanned", *,
                       languages=(), repository_url: str | None = None, configuration: dict[str, Any] | None = None) -> dict[str, Any]:
        root = Path(root).resolve()
        if not name.strip():
            raise ValueError("project name must not be empty")
        if not root.is_dir():
            raise NotADirectoryError(root)
        configuration = {} if configuration is None else configuration
        if not isinstance(configuration, dict):
            raise TypeError("project configuration must be a dictionary")
        _reject_secret_keys(configuration)
        languages = _normalize_languages(languages)
        repository_url = _sanitize_repository_url(repository_url) if repository_url else _git_remote_url(root)
        project_id, now = _id("PRJ"), _now()
        with self._transaction() as db:
            db.execute("INSERT INTO projects(id,name,root,revision,created_at,updated_at,languages_json,repository_url,config_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (project_id, name, str(root), revision, now, now, json.dumps(languages), repository_url,
                        json.dumps(configuration, sort_keys=True)))
            revision_id = f"REV-{hashlib.sha256((project_id + revision).encode()).hexdigest()[:24]}"
            db.execute("INSERT INTO project_revisions VALUES(?,?,?,?,?)",
                       (revision_id, project_id, revision, now, json.dumps({"initial": True})))
            self._audit(db, project_id, None, "project.create", "project", project_id, {"name": name, "revision": revision})
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._project_dict(row)

    @staticmethod
    def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
        project = dict(row)
        project["languages"] = json.loads(project.pop("languages_json", "[]"))
        project["configuration"] = json.loads(project.pop("config_json", "{}"))
        return project

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
        agent = dict(row)
        agent["capabilities"] = json.loads(agent.pop("capabilities_json"))
        agent["permissions"] = json.loads(agent.pop("permissions_json"))
        return agent

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._session() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return self._project_dict(row) if row else None

    def list_projects(self) -> list[dict[str, Any]]:
        with self._session() as db:
            return [self._project_dict(row) for row in db.execute("SELECT * FROM projects ORDER BY created_at")]

    def update_project(self, project_id: str, *, name: str | None = None, languages=None,
                       repository_url: str | None = None, configuration: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._transaction() as db:
            project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            updated_name = name if name is not None else project["name"]
            if not updated_name.strip():
                raise ValueError("project name must not be empty")
            updated_languages = json.loads(project["languages_json"]) if languages is None else _normalize_languages(languages)
            updated_config = json.loads(project["config_json"]) if configuration is None else configuration
            if not isinstance(updated_config, dict):
                raise TypeError("project configuration must be a dictionary")
            _reject_secret_keys(updated_config)
            updated_repo = project["repository_url"] if repository_url is None else _sanitize_repository_url(repository_url)
            db.execute("UPDATE projects SET name=?,languages_json=?,repository_url=?,config_json=?,updated_at=? WHERE id=?",
                       (updated_name, json.dumps(updated_languages), updated_repo, json.dumps(updated_config, sort_keys=True), _now(), project_id))
            self._audit(db, project_id, None, "project.update", "project", project_id, {"name": updated_name})
            return self._project_dict(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def get_project_revision(self, project_id: str) -> str | None:
        with self._session() as db:
            row = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            return row["revision"] if row else None

    def set_revision(self, project_id: str, revision: str) -> None:
        with self._transaction() as db:
            cursor = db.execute("UPDATE projects SET revision=?,updated_at=? WHERE id=?", (revision, _now(), project_id))
            if cursor.rowcount != 1:
                raise KeyError(project_id)
            self._audit(db, project_id, None, "project.revision", "project", project_id, {"revision": revision})

    def get_project_changes(self, project_id: str, since_revision: str | None = None) -> dict[str, Any]:
        with self._session() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            rows = db.execute("SELECT revision,detected_at,changes_json FROM project_revisions WHERE project_id=? ORDER BY detected_at,rowid",
                              (project_id,)).fetchall()
            changes = [dict(row, changes=json.loads(row["changes_json"])) for row in rows]
            if since_revision is not None:
                revisions = [row["revision"] for row in rows]
                if since_revision not in revisions and since_revision != "unscanned":
                    raise ValueError(f"revision {since_revision!r} is not in this project's history")
                if since_revision != "unscanned":
                    changes = changes[revisions.index(since_revision) + 1:]
            return {"project_id": project_id, "current_revision": project["revision"], "changes": changes}

    def invalidate_stale_knowledge(self, project_id: str, changed_paths) -> dict[str, int]:
        paths = sorted({_project_relative_path(relative) for relative in changed_paths})
        counts = {"nodes": 0, "edges": 0, "evidence": 0, "memories": 0}
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise KeyError(project_id)
            for relative in paths:
                counts["nodes"] += db.execute("UPDATE graph_nodes SET stale=1 WHERE project_id=? AND path=? AND stale=0", (project_id, relative)).rowcount
                counts["edges"] += db.execute("UPDATE graph_edges SET stale=1 WHERE project_id=? AND (source_id IN (SELECT id FROM graph_nodes WHERE project_id=? AND path=?) OR target_id IN (SELECT id FROM graph_nodes WHERE project_id=? AND path=?)) AND stale=0",
                                               (project_id, project_id, relative, project_id, relative)).rowcount
                counts["evidence"] += db.execute("UPDATE evidence SET stale=1 WHERE project_id=? AND source_path=? AND stale=0", (project_id, relative)).rowcount
                counts["memories"] += db.execute("UPDATE memories SET status='stale',updated_at=? WHERE project_id=? AND status='active' AND id IN (SELECT me.memory_id FROM memory_evidence me JOIN evidence e ON e.id=me.evidence_id WHERE e.project_id=? AND e.stale=1 AND e.source_path=?)",
                                                  (_now(), project_id, project_id, relative)).rowcount
            if paths:
                self._audit(db, project_id, None, "knowledge.invalidate", "project", project_id, {"paths": paths, "counts": counts})
        return counts

    def register_agent(self, project_id: str, name: str, role: str, capabilities=(), permissions=()) -> dict[str, Any]:
        agent_id, now = _id("AGT"), _now()
        with self._transaction() as db:
            db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,NULL,?)", (agent_id, project_id, name, role, json.dumps(list(capabilities)), json.dumps(list(permissions)), now))
            self._audit(db, project_id, agent_id, "agent.register", "agent", agent_id, {"role": role})
            return self._agent_dict(db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())

    def get_or_register_agent(self, project_id: str, name: str, role: str, capabilities=(), permissions=()) -> dict[str, Any]:
        agent_id, now = _id("AGT"), _now()
        with self._transaction() as db:
            cursor = db.execute("INSERT OR IGNORE INTO agents VALUES(?,?,?,?,?,?,NULL,?)",
                                (agent_id, project_id, name, role, json.dumps(list(capabilities)), json.dumps(list(permissions)), now))
            agent = db.execute("SELECT * FROM agents WHERE project_id=? AND name=?", (project_id, name)).fetchone()
            if agent is None:
                raise KeyError(f"unknown project: {project_id}")
            if cursor.rowcount:
                self._audit(db, project_id, agent_id, "agent.register", "agent", agent_id, {"role": role, "internal": True})
            return self._agent_dict(agent)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._session() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            return self._agent_dict(row) if row else None

    def check_permission(self, agent_id: str, permission: str) -> bool:
        agent = self.get_agent(agent_id)
        if agent is None:
            return False
        return permission in agent["permissions"]

    def search_audit_logs(self, project_id: str, agent_id: str | None = None,
                          action: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("audit search limit must be between 1 and 1000")
        sql, args = "SELECT * FROM audit_logs WHERE project_id=?", [project_id]
        if agent_id:
            sql += " AND agent_id=?"; args.append(agent_id)
        if action:
            sql += " AND action=?"; args.append(action)
        sql += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
        with self._session() as db:
            rows = [dict(row) for row in db.execute(sql, args)]
        for row in rows:
            row["details"] = json.loads(row.pop("details_json"))
        return rows

    def submit_evidence(self, project_id: str, evidence_type: str, payload: dict[str, Any], description: str,
                        agent_id: str | None = None, revision: str | None = None, source_path: str | None = None,
                        source_line: int | None = None, verification_status: str = "observed") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("evidence payload must be a dictionary")
        _reject_secret_keys(payload)
        description = _scrub_sensitive_text(description)
        payload = _scrub_sensitive_value(payload)
        source_path = _project_relative_path(source_path)
        if source_line is not None and source_line < 1:
            raise ValueError("source_line must be a positive one-based line number")
        evidence_id, now = _id("EV"), _now()
        if verification_status not in {"observed", "inferred", "unverified", "contradicted"}:
            raise ValueError("use mark_evidence_verified() to attach verification evidence")
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(f"unknown project: {project_id}")
            if agent_id:
                self._require_permission(db, project_id, agent_id, "write_evidence")
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,0)", (
                evidence_id, project_id, agent_id, evidence_type, json.dumps(payload, sort_keys=True), description,
                now, revision or project["revision"], source_path, source_line, verification_status))
            self._audit(db, project_id, agent_id, "evidence.submit", "evidence", evidence_id, {"type": evidence_type})
            return dict(db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone())

    def retrieve_evidence(self, evidence_id: str, requesting_agent_id: str | None = None) -> dict[str, Any] | None:
        with self._session() as db:
            row = db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            result = self._dict(row)
            if result:
                if requesting_agent_id:
                    self._require_permission(db, result["project_id"], requesting_agent_id, "read_evidence")
                result["payload"] = json.loads(result.pop("payload_json"))
                result["relationships"] = [dict(item) for item in db.execute(
                    "SELECT evidence_id,related_evidence_id,relationship,'outgoing' AS direction FROM evidence_relationships WHERE evidence_id=? UNION ALL SELECT evidence_id,related_evidence_id,relationship,'incoming' AS direction FROM evidence_relationships WHERE related_evidence_id=?",
                    (evidence_id, evidence_id))]
            return result

    def search_evidence(self, project_id: str, query: str = "", evidence_type: str | None = None,
                        source_path: str | None = None, include_stale: bool = False,
                        requesting_agent_id: str | None = None, revision: str | None = None,
                        verification_status: str | None = None, source_agent_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM evidence WHERE project_id=?"
        args: list[Any] = [project_id]
        if not include_stale:
            sql += " AND stale=0"
        if evidence_type:
            sql += " AND evidence_type=?"; args.append(evidence_type)
        if source_path:
            sql += " AND source_path=?"; args.append(source_path)
        if revision:
            sql += " AND revision=?"; args.append(revision)
        if verification_status:
            sql += " AND verification_status=?"; args.append(verification_status)
        if source_agent_id:
            sql += " AND agent_id=?"; args.append(source_agent_id)
        if query:
            sql += " AND (description LIKE ? OR payload_json LIKE ?)"; args.extend([f"%{query}%"] * 2)
        sql += " ORDER BY collected_at"
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_evidence")
            rows = [dict(row) for row in db.execute(sql, args)]
            for row in rows:
                row["relationships"] = [dict(item) for item in db.execute(
                    "SELECT evidence_id,related_evidence_id,relationship,'outgoing' AS direction FROM evidence_relationships WHERE evidence_id=? UNION ALL SELECT evidence_id,related_evidence_id,relationship,'incoming' AS direction FROM evidence_relationships WHERE related_evidence_id=?",
                    (row["id"], row["id"]))]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def mark_evidence_verified(self, evidence_id: str, verification_evidence_id: str, agent_id: str) -> None:
        if evidence_id == verification_evidence_id:
            raise ValueError("an evidence record cannot verify itself")
        with self._transaction() as db:
            evidence = db.execute("SELECT project_id,stale FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            verification = db.execute("SELECT project_id,evidence_type,verification_status,stale FROM evidence WHERE id=?", (verification_evidence_id,)).fetchone()
            if evidence is None or verification is None or evidence["project_id"] != verification["project_id"]:
                raise ValueError("verification evidence must exist in the same project")
            if evidence["stale"] or verification["stale"]:
                raise ValueError("stale evidence cannot be used for verification")
            if verification["evidence_type"] not in {"test_result", "runtime_observation", "verification"} or verification["verification_status"] != "observed":
                raise ValueError("verification requires an observed test or runtime record")
            project_id = evidence["project_id"]
            self._require_permission(db, project_id, agent_id, "verify_evidence")
            db.execute("UPDATE evidence SET verification_status='verified' WHERE id=?", (evidence_id,))
            db.execute("INSERT OR IGNORE INTO evidence_relationships VALUES(?,?,?)", (verification_evidence_id, evidence_id, "verifies"))
            self._audit(db, project_id, agent_id, "evidence.verify", "evidence", evidence_id, {"verification_evidence_id": verification_evidence_id})

    def link_evidence(self, evidence_id: str, related_evidence_id: str, relationship: str = "supports",
                      agent_id: str | None = None) -> None:
        if evidence_id == related_evidence_id:
            raise ValueError("evidence cannot be linked to itself")
        if relationship not in {"supports", "contradicts", "context", "supersedes", "verifies"}:
            raise ValueError("invalid evidence relationship")
        with self._transaction() as db:
            rows = db.execute("SELECT id,project_id FROM evidence WHERE id IN (?,?)", (evidence_id, related_evidence_id)).fetchall()
            if len(rows) != 2 or rows[0]["project_id"] != rows[1]["project_id"]:
                raise ValueError("related evidence must exist in the same project")
            if agent_id:
                self._require_permission(db, rows[0]["project_id"], agent_id, "write_evidence")
            db.execute("INSERT OR IGNORE INTO evidence_relationships VALUES(?,?,?)", (evidence_id, related_evidence_id, relationship))
            self._audit(db, rows[0]["project_id"], agent_id, "evidence.link", "evidence", evidence_id,
                        {"related_evidence_id": related_evidence_id, "relationship": relationship})

    def invalidate_evidence(self, evidence_id: str) -> None:
        with self._transaction() as db:
            evidence = db.execute("SELECT project_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if evidence is None:
                raise KeyError(evidence_id)
            db.execute("UPDATE evidence SET stale=1 WHERE id=?", (evidence_id,))
            self._audit(db, evidence["project_id"], None, "evidence.invalidate", "evidence", evidence_id, {})

    def add_memory(self, project_id: str, category: str, content: str, agent_id: str | None = None,
                   revision: str | None = None, supporting_evidence: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
        content = _scrub_sensitive_text(content)
        memory_id, now = _id("MEM"), _now()
        with self._transaction() as db:
            row = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError(project_id)
            if agent_id:
                self._require_permission(db, project_id, agent_id, "write_memory")
            for evidence_id in supporting_evidence:
                evidence = db.execute("SELECT project_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
                if evidence is None or evidence["project_id"] != project_id:
                    raise ValueError("supporting evidence must belong to the same project")
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)", (memory_id, project_id, agent_id, category, content, "active", revision or row["revision"], now, now))
            for evidence_id in supporting_evidence:
                db.execute("INSERT INTO memory_evidence VALUES(?,?)", (memory_id, evidence_id))
            self._audit(db, project_id, agent_id, "memory.add", "memory", memory_id, {"category": category, "supporting_evidence": list(supporting_evidence)})
            return dict(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    def retrieve_memory(self, memory_id: str, requesting_agent_id: str | None = None) -> dict[str, Any] | None:
        with self._session() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                return None
            if requesting_agent_id:
                self._require_permission(db, row["project_id"], requesting_agent_id, "read_memory")
            memory = dict(row)
            memory["supporting_evidence_ids"] = [item[0] for item in db.execute("SELECT evidence_id FROM memory_evidence WHERE memory_id=? ORDER BY evidence_id", (memory_id,))]
            return memory

    def search_memories(self, project_id: str, query: str = "", category: str | None = None,
                        include_stale: bool = False, agent_id: str | None = None,
                        requesting_agent_id: str | None = None, revision: str | None = None,
                        source_path: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM memories WHERE project_id=?", [project_id]
        if not include_stale:
            sql += " AND status='active'"
        if category:
            sql += " AND category=?"; args.append(category)
        if agent_id:
            sql += " AND agent_id=?"; args.append(agent_id)
        if revision:
            sql += " AND revision=?"; args.append(revision)
        if source_path:
            sql += " AND id IN (SELECT me.memory_id FROM memory_evidence me JOIN evidence e ON e.id=me.evidence_id WHERE e.source_path=?)"; args.append(source_path)
        if query:
            sql += " AND content LIKE ?"; args.append(f"%{query}%")
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_memory")
            rows = [dict(row) for row in db.execute(sql + " ORDER BY created_at", args)]
            for memory in rows:
                memory["supporting_evidence_ids"] = [item[0] for item in db.execute("SELECT evidence_id FROM memory_evidence WHERE memory_id=? ORDER BY evidence_id", (memory["id"],))]
            return rows

    def update_memory_status(self, memory_id: str, status: str, agent_id: str | None = None) -> None:
        if status not in {"active", "stale", "superseded", "disputed"}:
            raise ValueError("invalid memory status")
        with self._transaction() as db:
            memory = db.execute("SELECT project_id FROM memories WHERE id=?", (memory_id,)).fetchone()
            if memory is None:
                raise KeyError(memory_id)
            if agent_id:
                self._require_permission(db, memory["project_id"], agent_id, "write_memory")
            db.execute("UPDATE memories SET status=?,updated_at=? WHERE id=?", (status, _now(), memory_id))
            self._audit(db, memory["project_id"], agent_id, "memory.status", "memory", memory_id, {"status": status})

    def add_node(self, project_id: str, node_key: str, kind: str, name: str, path: str | None = None,
                 line: int | None = None, language: str | None = None, revision: str | None = None,
                 metadata: dict[str, Any] | None = None) -> str:
        path = _project_relative_path(path)
        if line is not None and line < 1:
            raise ValueError("line must be a positive one-based line number")
        node_id = _id("NOD")
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            revision = revision or project["revision"]
            existing = db.execute("SELECT id FROM graph_nodes WHERE project_id=? AND node_key=?", (project_id, node_key)).fetchone()
            if existing:
                node_id = existing["id"]
                db.execute("UPDATE graph_nodes SET kind=?,name=?,path=?,line=?,language=?,revision=?,stale=0,metadata_json=? WHERE id=?",
                           (kind, name, path, line, language, revision, json.dumps(metadata or {}, sort_keys=True), node_id))
            else:
                db.execute("INSERT INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,0,?)", (node_id, project_id, node_key, kind, name, path, line, language, revision, json.dumps(metadata or {}, sort_keys=True)))
            return node_id

    def add_relationship(self, project_id: str, source_id: str, target_id: str, kind: str,
                         revision: str | None = None, metadata: dict[str, Any] | None = None,
                         confidence: float = 1.0, agent_id: str | None = None,
                         evidence_id: str | None = None) -> str:
        if not 0 <= confidence <= 1:
            raise ValueError("relationship confidence must be between 0 and 1")
        edge_id = _id("EDG")
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            nodes = db.execute("SELECT id,project_id FROM graph_nodes WHERE id IN (?,?)", (source_id, target_id)).fetchall()
            expected_endpoints = 1 if source_id == target_id else 2
            if len(nodes) != expected_endpoints or any(node["project_id"] != project_id for node in nodes):
                raise ValueError("relationships must connect existing nodes in the same project")
            if agent_id:
                self._require_permission(db, project_id, agent_id, "update_graph")
            if evidence_id:
                evidence = db.execute("SELECT project_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
                if evidence is None or evidence["project_id"] != project_id:
                    raise ValueError("relationship evidence must belong to the same project")
            revision = revision or project["revision"]
            old = db.execute("SELECT * FROM graph_edges WHERE project_id=? AND source_id=? AND target_id=? AND kind=?", (project_id, source_id, target_id, kind)).fetchone()
            if old:
                edge_id = old["id"]
                next_metadata = json.dumps(metadata or {}, sort_keys=True)
                next_agent = agent_id or old["provenance_agent_id"]
                next_evidence = evidence_id or old["evidence_id"]
                if (old["revision"], old["stale"], old["metadata_json"], old["confidence"], old["provenance_agent_id"], old["evidence_id"]) != (revision, 0, next_metadata, confidence, next_agent, next_evidence):
                    self._archive_graph_edge(db, old)
                db.execute("UPDATE graph_edges SET revision=?,stale=0,metadata_json=?,confidence=?,provenance_agent_id=?,evidence_id=? WHERE id=?",
                           (revision, next_metadata, confidence, next_agent, next_evidence, edge_id))
            else:
                db.execute("INSERT INTO graph_edges(id,project_id,source_id,target_id,kind,revision,stale,metadata_json,confidence,provenance_agent_id,evidence_id) VALUES(?,?,?,?,?,?,0,?,?,?,?)",
                           (edge_id, project_id, source_id, target_id, kind, revision, json.dumps(metadata or {}, sort_keys=True), confidence, agent_id, evidence_id))
            self._audit(db, project_id, agent_id, "graph.relationship", "edge", edge_id,
                        {"kind": kind, "source": source_id, "target": target_id, "confidence": confidence, "evidence_id": evidence_id})
            return edge_id

    def update_graph(self, project_id: str, nodes=(), relationships=(), agent_id: str | None = None) -> dict[str, list[str]]:
        """Atomically upsert project nodes and typed, evidence-linked relationships."""
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            if agent_id:
                self._require_permission(db, project_id, agent_id, "update_graph")
            node_ids: dict[str, str] = {}
            for item in nodes:
                required = {"node_key", "kind", "name"}
                if not required <= set(item):
                    raise ValueError("each graph node requires node_key, kind, and name")
                node_key = item["node_key"]
                node_path = _project_relative_path(item.get("path"))
                node_line = item.get("line")
                if node_line is not None and node_line < 1:
                    raise ValueError("line must be a positive one-based line number")
                existing = db.execute("SELECT id FROM graph_nodes WHERE project_id=? AND node_key=?", (project_id, node_key)).fetchone()
                node_id = existing["id"] if existing else _id("NOD")
                values = (item["kind"], item["name"], node_path, node_line, item.get("language"),
                          item.get("revision", project["revision"]), json.dumps(item.get("metadata", {}), sort_keys=True))
                if existing:
                    db.execute("UPDATE graph_nodes SET kind=?,name=?,path=?,line=?,language=?,revision=?,stale=0,metadata_json=? WHERE id=?", (*values, node_id))
                else:
                    db.execute("INSERT INTO graph_nodes(id,project_id,node_key,kind,name,path,line,language,revision,stale,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                               (node_id, project_id, node_key, *values))
                node_ids[node_key] = node_id
            edge_ids = []
            for item in relationships:
                if not {"source", "target", "kind"} <= set(item):
                    raise ValueError("each relationship requires source, target, and kind")
                source_id = node_ids.get(item["source"])
                target_id = node_ids.get(item["target"])
                if source_id is None:
                    source = db.execute("SELECT id FROM graph_nodes WHERE project_id=? AND node_key=?", (project_id, item["source"])).fetchone()
                    source_id = source["id"] if source else item["source"]
                if target_id is None:
                    target = db.execute("SELECT id FROM graph_nodes WHERE project_id=? AND node_key=?", (project_id, item["target"])).fetchone()
                    target_id = target["id"] if target else item["target"]
                endpoints = db.execute("SELECT id,project_id FROM graph_nodes WHERE id IN (?,?)", (source_id, target_id)).fetchall()
                expected_endpoints = 1 if source_id == target_id else 2
                if len(endpoints) != expected_endpoints or any(endpoint["project_id"] != project_id for endpoint in endpoints):
                    raise ValueError("relationships must connect existing nodes in the same project")
                confidence = float(item.get("confidence", 1.0))
                if not 0 <= confidence <= 1:
                    raise ValueError("relationship confidence must be between 0 and 1")
                evidence_id = item.get("evidence_id")
                if evidence_id and db.execute("SELECT 1 FROM evidence WHERE id=? AND project_id=?", (evidence_id, project_id)).fetchone() is None:
                    raise ValueError("relationship evidence must belong to the same project")
                revision = item.get("revision", project["revision"])
                metadata = json.dumps(item.get("metadata", {}), sort_keys=True)
                old = db.execute("SELECT * FROM graph_edges WHERE project_id=? AND source_id=? AND target_id=? AND kind=?",
                                 (project_id, source_id, target_id, item["kind"])).fetchone()
                edge_id = old["id"] if old else _id("EDG")
                if old:
                    if (old["revision"], old["stale"], old["metadata_json"], old["confidence"], old["provenance_agent_id"], old["evidence_id"]) != (revision, 0, metadata, confidence, agent_id or old["provenance_agent_id"], evidence_id or old["evidence_id"]):
                        self._archive_graph_edge(db, old)
                    db.execute("UPDATE graph_edges SET revision=?,stale=0,metadata_json=?,confidence=?,provenance_agent_id=?,evidence_id=? WHERE id=?",
                               (revision, metadata, confidence, agent_id or old["provenance_agent_id"], evidence_id or old["evidence_id"], edge_id))
                else:
                    db.execute("INSERT INTO graph_edges(id,project_id,source_id,target_id,kind,revision,stale,metadata_json,confidence,provenance_agent_id,evidence_id) VALUES(?,?,?,?,?,?,0,?,?,?,?)",
                               (edge_id, project_id, source_id, target_id, item["kind"], revision, metadata, confidence, agent_id, evidence_id))
                edge_ids.append(edge_id)
            self._audit(db, project_id, agent_id, "graph.update", "project", project_id,
                        {"node_count": len(node_ids), "relationship_count": len(edge_ids)})
            return {"nodes": list(node_ids.values()), "relationships": edge_ids}

    def query_graph(self, project_id: str, node_key: str | None = None, kind: str | None = None,
                    include_stale: bool = False, requesting_agent_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            sql, args = "SELECT * FROM graph_nodes WHERE project_id=?", [project_id]
            if node_key:
                sql += " AND node_key=?"; args.append(node_key)
            if kind:
                sql += " AND kind=?"; args.append(kind)
            if not include_stale:
                sql += " AND stale=0"
            nodes = [dict(row) for row in db.execute(sql, args)]
            edge_sql, edge_args = "SELECT * FROM graph_edges WHERE project_id=?", [project_id]
            if node_key or kind:
                node_filter = "project_id=?"
                node_args: list[Any] = [project_id]
                if node_key:
                    node_filter += " AND node_key=?"; node_args.append(node_key)
                if kind:
                    node_filter += " AND kind=?"; node_args.append(kind)
                subquery = f"SELECT id FROM graph_nodes WHERE {node_filter}"
                edge_sql += f" AND (source_id IN ({subquery}) OR target_id IN ({subquery}))"
                edge_args.extend(node_args * 2)
            if not include_stale:
                edge_sql += " AND stale=0"
            edges = [dict(row) for row in db.execute(edge_sql, edge_args)]
            for node in nodes:
                node["metadata"] = json.loads(node["metadata_json"])
            for edge in edges:
                edge["metadata"] = json.loads(edge["metadata_json"])
            return {"nodes": nodes, "edges": edges}

    def get_dependencies(self, project_id: str, node_key: str, relationship: str = "imports",
                         requesting_agent_id: str | None = None) -> list[dict[str, Any]]:
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            rows = db.execute("SELECT target.* FROM graph_edges edge JOIN graph_nodes source ON source.id=edge.source_id JOIN graph_nodes target ON target.id=edge.target_id WHERE edge.project_id=? AND source.node_key=? AND edge.kind=? AND edge.stale=0 AND target.stale=0 ORDER BY target.node_key",
                              (project_id, node_key, relationship)).fetchall()
            return [dict(row) for row in rows]

    def get_relationship_history(self, project_id: str, node_key: str | None = None,
                                 requesting_agent_id: str | None = None) -> list[dict[str, Any]]:
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            sql = "SELECT history.*,source.node_key AS source_key,target.node_key AS target_key FROM graph_edge_history history LEFT JOIN graph_nodes source ON source.id=history.source_id LEFT JOIN graph_nodes target ON target.id=history.target_id WHERE history.project_id=?"
            args: list[Any] = [project_id]
            if node_key:
                sql += " AND (source.node_key=? OR target.node_key=?)"; args.extend([node_key, node_key])
            sql += " ORDER BY history.recorded_at"
            rows = [dict(row) for row in db.execute(sql, args)]
            for row in rows:
                row["metadata"] = json.loads(row.pop("metadata_json"))
            return rows

    def create_investigation(self, project_id: str, objective: str, agent_id: str | None = None,
                             context: dict[str, Any] | None = None) -> dict[str, Any]:
        investigation_id, now = _id("INV"), _now()
        if not objective.strip():
            raise ValueError("investigation objective must not be empty")
        context = {} if context is None else context
        if not isinstance(context, dict):
            raise TypeError("investigation context must be a dictionary")
        _reject_secret_keys(context)
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            if agent_id:
                self._require_permission(db, project_id, agent_id, "investigate")
            db.execute("INSERT INTO investigations(id,project_id,objective,status,revision,created_by,created_at,updated_at,context_json,assigned_agent_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (investigation_id, project_id, objective, "created", project["revision"], agent_id, now, now,
                        json.dumps(context, sort_keys=True), agent_id))
            self._audit(db, project_id, agent_id, "investigation.create", "investigation", investigation_id, {"objective": objective})
            result = dict(db.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone())
            result["context"] = json.loads(result.pop("context_json"))
            return result

    def checkpoint_investigation(self, investigation_id: str, status: str, payload: dict[str, Any], agent_id: str | None = None) -> dict[str, Any]:
        if status not in {"created", "active", "paused", "blocked", "completed", "abandoned"}:
            raise ValueError("invalid investigation status")
        if not isinstance(payload, dict):
            raise TypeError("checkpoint payload must be a dictionary")
        _reject_secret_keys(payload)
        checkpoint_id, now = _id("CHK"), _now()
        with self._transaction() as db:
            investigation = db.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone()
            if investigation is None:
                raise KeyError(investigation_id)
            if agent_id:
                self._require_permission(db, investigation["project_id"], agent_id, "checkpoint_investigation")
            transitions = {"created": {"active", "paused", "blocked", "completed", "abandoned"},
                           "active": {"paused", "blocked", "completed", "abandoned"},
                           "paused": {"active", "blocked", "completed", "abandoned"},
                           "blocked": {"active", "completed", "abandoned"}}
            current_status = investigation["status"]
            if current_status not in transitions or (status != current_status and status not in transitions[current_status]):
                raise ValueError(f"invalid investigation transition: {current_status!r} -> {status!r}")
            db.execute("UPDATE investigations SET status=?,context_json=?,assigned_agent_id=COALESCE(?,assigned_agent_id),updated_at=? WHERE id=?",
                       (status, json.dumps(payload, sort_keys=True), agent_id, now, investigation_id))
            db.execute("INSERT INTO investigation_checkpoints VALUES(?,?,?,?,?,?)", (checkpoint_id, investigation_id, agent_id, status, json.dumps(payload, sort_keys=True), now))
            if agent_id:
                db.execute("UPDATE agents SET current_investigation_id=?,last_activity=? WHERE id=?", (investigation_id, now, agent_id))
            if status in {"completed", "abandoned"}:
                db.execute("UPDATE agents SET current_investigation_id=NULL WHERE current_investigation_id=?", (investigation_id,))
            self._audit(db, investigation["project_id"], agent_id, "investigation.checkpoint", "investigation", investigation_id,
                        {"status": status, "checkpoint_id": checkpoint_id})
            return {"id": checkpoint_id, "investigation_id": investigation_id, "status": status, "payload": payload, "created_at": now,
                    "revision": investigation["revision"], "current_revision": db.execute("SELECT revision FROM projects WHERE id=?", (investigation["project_id"],)).fetchone()["revision"]}

    def search_investigations(self, project_id: str, status: str | None = None,
                              requesting_agent_id: str | None = None, query: str = "") -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM investigations WHERE project_id=?", [project_id]
        if status:
            if status not in {"created", "active", "paused", "blocked", "completed", "abandoned"}:
                raise ValueError("invalid investigation status")
            sql += " AND status=?"; args.append(status)
        if query:
            sql += " AND (objective LIKE ? OR context_json LIKE ?)"; args.extend([f"%{query}%"] * 2)
        sql += " ORDER BY updated_at DESC"
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_investigation")
            result = []
            for row in db.execute(sql, args):
                investigation = dict(row)
                investigation["context"] = json.loads(investigation.pop("context_json"))
                investigation["revision_compatible"] = investigation["revision"] == db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()["revision"]
                result.append(investigation)
            return result

    def retrieve_investigation(self, investigation_id: str, requesting_agent_id: str | None = None) -> dict[str, Any] | None:
        with self._session() as db:
            investigation = db.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone()
            if investigation is None:
                return None
            if requesting_agent_id:
                self._require_permission(db, investigation["project_id"], requesting_agent_id, "read_investigation")
            result = dict(investigation)
            result["context"] = json.loads(result.pop("context_json"))
            checkpoints = db.execute("SELECT * FROM investigation_checkpoints WHERE investigation_id=? ORDER BY created_at,rowid", (investigation_id,)).fetchall()
            result["checkpoints"] = [dict(row, payload=json.loads(row["payload_json"])) for row in checkpoints]
            result["last_checkpoint"] = result["checkpoints"][-1] if result["checkpoints"] else None
            result["revision_compatible"] = result["revision"] == db.execute("SELECT revision FROM projects WHERE id=?", (result["project_id"],)).fetchone()["revision"]
            return result

    def update_investigation(self, investigation_id: str, *, objective: str | None = None,
                              context: dict[str, Any] | None = None, agent_id: str | None = None) -> dict[str, Any]:
        with self._transaction() as db:
            investigation = db.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone()
            if investigation is None:
                raise KeyError(investigation_id)
            if agent_id:
                self._require_permission(db, investigation["project_id"], agent_id, "investigate")
            new_objective = objective if objective is not None else investigation["objective"]
            if not new_objective.strip():
                raise ValueError("investigation objective must not be empty")
            new_context = json.loads(investigation["context_json"]) if context is None else context
            if not isinstance(new_context, dict):
                raise TypeError("investigation context must be a dictionary")
            _reject_secret_keys(new_context)
            now = _now()
            db.execute("UPDATE investigations SET objective=?,context_json=?,updated_at=? WHERE id=?",
                       (new_objective, json.dumps(new_context, sort_keys=True), now, investigation_id))
            self._audit(db, investigation["project_id"], agent_id, "investigation.update", "investigation", investigation_id, {"objective": new_objective})
            result = dict(db.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone())
            result["context"] = json.loads(result.pop("context_json"))
            return result

    def pause_investigation(self, investigation_id: str, agent_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        investigation = self.retrieve_investigation(investigation_id)
        if investigation is None:
            raise KeyError(investigation_id)
        return self.checkpoint_investigation(investigation_id, "paused", payload if payload is not None else investigation["context"], agent_id)

    def resume_investigation(self, investigation_id: str, agent_id: str) -> dict[str, Any]:
        checkpoint_id, now = _id("CHK"), _now()
        with self._transaction() as db:
            investigation = db.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone()
            if investigation is None:
                raise KeyError(investigation_id)
            self._require_permission(db, investigation["project_id"], agent_id, "checkpoint_investigation")
            current_revision = db.execute("SELECT revision FROM projects WHERE id=?", (investigation["project_id"],)).fetchone()["revision"]
            compatible = current_revision == investigation["revision"]
            if not compatible:
                return {"resumed": False, "revision_compatible": False, "investigation_id": investigation_id,
                        "investigation_revision": investigation["revision"], "current_revision": current_revision,
                        "status": investigation["status"], "reason": "project revision changed; revalidate previous findings first"}
            if investigation["status"] not in {"created", "paused", "blocked", "active"}:
                raise ValueError(f"cannot resume an investigation in {investigation['status']!r} state")
            stored_context = json.loads(investigation["context_json"])
            latest_checkpoint = db.execute("SELECT payload_json FROM investigation_checkpoints WHERE investigation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                                           (investigation_id,)).fetchone()
            context = (stored_context if investigation["status"] == "active" else
                       json.loads(latest_checkpoint["payload_json"]) if latest_checkpoint else stored_context)
            if investigation["status"] == "active":
                return {"resumed": True, "already_active": True, "revision_compatible": True,
                        "investigation_id": investigation_id, "status": "active", "context": context,
                        "revision": investigation["revision"], "current_revision": current_revision}
            db.execute("UPDATE investigations SET status='active',context_json=?,assigned_agent_id=?,updated_at=? WHERE id=?",
                       (json.dumps(context, sort_keys=True), agent_id, now, investigation_id))
            db.execute("INSERT INTO investigation_checkpoints VALUES(?,?,?,?,?,?)",
                       (checkpoint_id, investigation_id, agent_id, "active", json.dumps(context, sort_keys=True), now))
            db.execute("UPDATE agents SET current_investigation_id=?,last_activity=? WHERE id=?", (investigation_id, now, agent_id))
            self._audit(db, investigation["project_id"], agent_id, "investigation.resume", "investigation", investigation_id,
                        {"revision": current_revision, "from_status": investigation["status"]})
            return {"resumed": True, "revision_compatible": True, "investigation_id": investigation_id,
                    "status": "active", "context": context, "checkpoint_id": checkpoint_id,
                    "revision": investigation["revision"], "current_revision": current_revision}

    def complete_investigation(self, investigation_id: str, agent_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        investigation = self.retrieve_investigation(investigation_id)
        if investigation is None:
            raise KeyError(investigation_id)
        return self.checkpoint_investigation(investigation_id, "completed", payload if payload is not None else investigation["context"], agent_id)

    def abandon_investigation(self, investigation_id: str, agent_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        investigation = self.retrieve_investigation(investigation_id)
        if investigation is None:
            raise KeyError(investigation_id)
        return self.checkpoint_investigation(investigation_id, "abandoned", payload if payload is not None else investigation["context"], agent_id)

    def create_hypothesis(self, project_id: str, statement: str, investigation_id: str | None = None,
                          agent_id: str | None = None, status: str = "proposed", confidence: float = 0.5) -> dict[str, Any]:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if status not in {"proposed", "under_investigation", "supported", "contradicted", "inconclusive", "resolved"}:
            raise ValueError("invalid hypothesis status")
        statement = _scrub_sensitive_text(statement)
        hypothesis_id, now = _id("HYP"), _now()
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            if agent_id:
                self._require_permission(db, project_id, agent_id, "investigate")
            if investigation_id:
                investigation = db.execute("SELECT project_id FROM investigations WHERE id=?", (investigation_id,)).fetchone()
                if investigation is None or investigation["project_id"] != project_id:
                    raise ValueError("investigation must belong to the same project")
            db.execute("INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?,?)", (hypothesis_id, project_id, investigation_id, agent_id, statement, status, confidence, project["revision"], now, now))
            self._audit(db, project_id, agent_id, "hypothesis.create", "hypothesis", hypothesis_id,
                        {"status": status, "confidence": confidence})
            return dict(db.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone())

    def update_hypothesis(self, hypothesis_id: str, *, status: str | None = None, confidence: float | None = None,
                           agent_id: str | None = None, expected_updated_at: str | None = None) -> dict[str, Any]:
        allowed = {"proposed", "under_investigation", "supported", "contradicted", "inconclusive", "resolved"}
        if status is not None and status not in allowed:
            raise ValueError("invalid hypothesis status")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        with self._transaction() as db:
            hypothesis = db.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
            if hypothesis is None:
                raise KeyError(hypothesis_id)
            if agent_id:
                self._require_permission(db, hypothesis["project_id"], agent_id, "investigate")
            if expected_updated_at is not None and expected_updated_at != hypothesis["updated_at"]:
                raise RuntimeError("hypothesis changed since it was retrieved; reload before updating")
            new_status = status if status is not None else hypothesis["status"]
            new_confidence = confidence if confidence is not None else hypothesis["confidence"]
            now = _now()
            db.execute("UPDATE hypotheses SET status=?,confidence=?,updated_at=? WHERE id=?",
                       (new_status, new_confidence, now, hypothesis_id))
            self._audit(db, hypothesis["project_id"], agent_id, "hypothesis.update", "hypothesis", hypothesis_id,
                        {"old_status": hypothesis["status"], "status": new_status,
                         "old_confidence": hypothesis["confidence"], "confidence": new_confidence})
            result = dict(db.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone())
            result["evidence"] = [dict(row) for row in db.execute("SELECT evidence_id,relation FROM hypothesis_evidence WHERE hypothesis_id=? ORDER BY evidence_id", (hypothesis_id,))]
            return result

    def retrieve_hypotheses(self, project_id: str, investigation_id: str | None = None,
                            status: str | None = None, requesting_agent_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM hypotheses WHERE project_id=?", [project_id]
        if investigation_id:
            sql += " AND investigation_id=?"; args.append(investigation_id)
        if status:
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY created_at"
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_investigation")
            result = []
            for row in db.execute(sql, args):
                hypothesis = dict(row)
                hypothesis["evidence"] = [dict(item) for item in db.execute("SELECT evidence_id,relation FROM hypothesis_evidence WHERE hypothesis_id=? ORDER BY evidence_id", (row["id"],))]
                result.append(hypothesis)
            return result

    def attach_evidence(self, hypothesis_id: str, evidence_id: str, relation: str = "supports", agent_id: str | None = None) -> None:
        if relation not in {"supports", "contradicts", "context"}:
            raise ValueError("invalid evidence relation")
        with self._transaction() as db:
            rows = db.execute("SELECT id,project_id FROM hypotheses WHERE id=? UNION ALL SELECT id,project_id FROM evidence WHERE id=?", (hypothesis_id, evidence_id)).fetchall()
            if len(rows) != 2 or rows[0]["project_id"] != rows[1]["project_id"]:
                raise ValueError("hypothesis and evidence must exist in the same project")
            if agent_id:
                self._require_permission(db, rows[0]["project_id"], agent_id, "investigate")
            db.execute("INSERT OR IGNORE INTO hypothesis_evidence VALUES(?,?,?)", (hypothesis_id, evidence_id, relation))
            self._audit(db, rows[0]["project_id"], agent_id, "hypothesis.evidence", "hypothesis", hypothesis_id,
                        {"evidence_id": evidence_id, "relation": relation})

    @staticmethod
    def _archive_graph_edge(db: sqlite3.Connection, edge: sqlite3.Row) -> None:
        db.execute("INSERT INTO graph_edge_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (_id("EDH"), edge["id"], edge["project_id"], edge["source_id"], edge["target_id"], edge["kind"],
                    edge["revision"], edge["stale"], edge["metadata_json"], edge["confidence"],
                    edge["provenance_agent_id"], edge["evidence_id"], _now()))

    @staticmethod
    def _require_permission(db: sqlite3.Connection, project_id: str, agent_id: str, permission: str) -> None:
        agent = db.execute("SELECT project_id,permissions_json FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent is None or agent["project_id"] != project_id:
            raise PermissionError("agent is not registered for this project")
        if permission not in json.loads(agent["permissions_json"]):
            raise PermissionError(f"agent lacks {permission!r} permission")

    def _audit(self, db: sqlite3.Connection, project_id: str, agent_id: str | None,
               action: str, object_type: str, object_id: str | None, details: dict[str, Any]) -> None:
        details_json, now = json.dumps(details, sort_keys=True), _now()
        db.execute("INSERT INTO audit_logs VALUES(?,?,?,?,?,?,?,?)", (_id("AUD"), project_id, agent_id, action, object_type, object_id, details_json, now))
        db.execute("INSERT INTO agent_activity VALUES(?,?,?,?,?,?)", (_id("ACT"), project_id, agent_id, action, details_json, now))
        if agent_id:
            db.execute("UPDATE agents SET last_activity=? WHERE id=?", (now, agent_id))
