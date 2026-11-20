"""InvestigationStore operations using the shared native transaction boundary."""
from __future__ import annotations
import json
from typing import Any
from .db_support import _id, _now, _reject_secret_keys, _scrub_sensitive_text


class InvestigationStore:
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
                db.execute("UPDATE agents SET current_investigation_id=NULL WHERE current_investigation_id=? AND id<>?",
                           (investigation_id, agent_id))
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
            db.execute("UPDATE agents SET current_investigation_id=NULL WHERE current_investigation_id=? AND id<>?",
                       (investigation_id, agent_id))
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

