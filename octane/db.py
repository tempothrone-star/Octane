"""SQLite-backed Project Intelligence Layer."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .db_support import (
    _id as _id,
    _now as _now,
    _sanitize_repository_url as _sanitize_repository_url,
    _scrub_sensitive_text as _scrub_sensitive_text,
    _git_remote_url as _git_remote_url,
    _project_relative_path as _project_relative_path,
    _normalize_languages as _normalize_languages,
    _scrub_sensitive_value as _scrub_sensitive_value,
    _reject_secret_keys as _reject_secret_keys,
)
from .db_schema import (
    _SCHEMA,
    _MIGRATION_4,
    _MIGRATION_5,
    _MIGRATION_6,
    _MIGRATION_3,
    _MIGRATION_2,
)

from .db_graph import GraphStore
from .db_evidence import EvidenceStore
from .db_investigations import InvestigationStore


_INITIALIZE_LOCK = threading.RLock()


class ProjectIntelligence(GraphStore, EvidenceStore, InvestigationStore):
    """Small, transaction-safe SQLite API shared by deterministic agents."""

    def __init__(self, database: str | Path):
        self._local = threading.local()
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("ProjectIntelligence requires a file-backed SQLite database")
        from .storage import prepare_database

        prepare_database(Path(self.database))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
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
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'schema_migrations' in tables:
                current = connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
                if current is not None and current > 6:
                    raise RuntimeError(f"database schema version {current} is newer than this Octane supports")
                if current == 6:
                    return
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", (_now(),))
            current = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if current > 6:
                raise RuntimeError(f"database schema version {current} is newer than this Octane supports")
            for version, statements in ((2, _MIGRATION_2), (3, _MIGRATION_3), (4, _MIGRATION_4), (5, _MIGRATION_5), (6, _MIGRATION_6)):
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
        active = getattr(self._local, "connection", None)
        if active is not None:
            savepoint = "nested_" + uuid.uuid4().hex
            active.execute("SAVEPOINT " + savepoint)
            try:
                yield active
                active.execute("RELEASE " + savepoint)
            except Exception:
                active.execute("ROLLBACK TO " + savepoint)
                active.execute("RELEASE " + savepoint)
                raise
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._local.connection = connection
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._local.connection = None
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
                    last_match = max(i for i, revision in enumerate(revisions) if revision == since_revision)
                    changes = changes[last_match + 1:]
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
