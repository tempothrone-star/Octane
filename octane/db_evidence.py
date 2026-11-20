"""EvidenceStore operations using the shared native transaction boundary."""
from __future__ import annotations
import json
from typing import Any
from .db_support import _id, _now, _project_relative_path, _reject_secret_keys, _scrub_sensitive_text, _scrub_sensitive_value


class EvidenceStore:
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

    def mark_evidence_verified(self, evidence_id: str, verification_evidence_id: str, agent_id: str,
                               *, require_recorded_run: bool = False) -> None:
        if evidence_id == verification_evidence_id:
            raise ValueError("an evidence record cannot verify itself")
        with self._transaction() as db:
            evidence = db.execute("SELECT project_id,revision,stale FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            verification = db.execute("SELECT project_id,revision,evidence_type,verification_status,stale FROM evidence WHERE id=?", (verification_evidence_id,)).fetchone()
            if evidence is None or verification is None or evidence["project_id"] != verification["project_id"]:
                raise ValueError("verification evidence must exist in the same project")
            if evidence["stale"] or verification["stale"]:
                raise ValueError("stale evidence cannot be used for verification")
            if verification["evidence_type"] not in {"test_result", "runtime_observation", "verification"} or verification["verification_status"] != "observed":
                raise ValueError("verification requires an observed test or runtime record")
            project_id = evidence["project_id"]
            self._require_permission(db, project_id, agent_id, "verify_evidence")
            if require_recorded_run:
                current_revision = self.get_project_revision(project_id)
                run = db.execute("SELECT before_hash,after_hash,snapshot_hash,status FROM execution_runs WHERE project_id=? AND evidence_id=?",
                                 (project_id, verification_evidence_id)).fetchone()
                if (not run or run["status"] != "passed" or run["before_hash"] != run["snapshot_hash"]
                        or run["after_hash"] != run["snapshot_hash"] or evidence["revision"] != current_revision
                        or verification["revision"] != current_revision):
                    raise ValueError("verification needs a recorded passing run on the unchanged snapshot at the finding's current revision")
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

