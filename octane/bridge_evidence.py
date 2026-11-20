"""Evidence and memory handlers sharing the owning bridge's policy and actors."""
from __future__ import annotations
from typing import Any
from .bridge_support import BridgeError, _required
from .reports import import_report
from .sarif import export_sarif
from .provenance import statement

_ALLOWED_EVIDENCE_STATUS = {"observed", "inferred", "unverified", "contradicted"}


class EvidenceOperations:
    def _op_evidence_sarif(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        self._actor(project_id, "read_evidence")
        return export_sarif(self.intelligence, project_id, p.get("evidenceIds"), p.get("limit",1000))

    def _op_evidence_search(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        agent = self._actor(project_id, "read_evidence")
        return self.intelligence.search_evidence(
            project_id, query=p.get("query", ""), evidence_type=p.get("evidenceType"),
            source_path=p.get("sourcePath"), include_stale=p.get("includeStale", False),
            requesting_agent_id=agent, revision=p.get("revision"),
            verification_status=p.get("verificationStatus"),
        )

    def _op_evidence_import(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return import_report(self.intelligence, project_id, self._actor(project_id, "write_evidence"),
                             _required(p, "reportPath", str), _required(p, "reportFormat", str))

    def _op_evidence_attest(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        actor = self._actor(project_id, "read_evidence")
        self.intelligence.retrieve_evidence(_required(p, "evidenceId", str), requesting_agent_id=actor)
        return statement(self.intelligence, project_id, p["evidenceId"])

    def _op_evidence_read(self, p: dict[str, Any]) -> dict[str, Any] | None:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.retrieve_evidence(
            _required(p, "evidenceId", str), self._actor(project_id, "read_evidence")
        )

    def _op_evidence_add(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        status = p.get("verificationStatus", "unverified")
        if status not in _ALLOWED_EVIDENCE_STATUS:
            raise BridgeError("evidence verificationStatus must be observed, inferred, unverified, or contradicted")
        return self.intelligence.submit_evidence(
            project_id, _required(p, "evidenceType", str), _required(p, "payload", dict),
            _required(p, "description", str), self._actor(project_id, "write_evidence"),
            revision=p.get("revision"), source_path=p.get("sourcePath"), source_line=p.get("sourceLine"),
            verification_status=status,
        )

    def _op_evidence_link(self, p: dict[str, Any]) -> dict[str, bool]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        if p.get("relationship") == "verifies":
            raise BridgeError("use the verify action to attach recorded execution evidence")
        self.intelligence.link_evidence(
            _required(p, "evidenceId", str), _required(p, "relatedEvidenceId", str),
            p.get("relationship", "supports"), self._actor(project_id, "write_evidence"),
        )
        return {"linked": True}

    def _op_evidence_verify(self, p: dict[str, Any]) -> dict[str, bool]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        self._scan(project_id, only_if_changed=True)
        self.intelligence.mark_evidence_verified(
            _required(p, "evidenceId", str), _required(p, "verificationEvidenceId", str),
            self._actor(project_id, "verify_evidence"), require_recorded_run=True,
        )
        return {"verified": True}

    def _op_memory_search(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.search_memories(
            project_id, query=p.get("query", ""), category=p.get("category"),
            include_stale=p.get("includeStale", False), revision=p.get("revision"),
            source_path=p.get("sourcePath"), requesting_agent_id=self._actor(project_id, "read_memory"),
        )

    def _op_memory_read(self, p: dict[str, Any]) -> dict[str, Any] | None:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.retrieve_memory(
            _required(p, "memoryId", str), self._actor(project_id, "read_memory")
        )

    def _op_memory_add(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.add_memory(
            project_id, _required(p, "category", str), _required(p, "content", str),
            self._actor(project_id, "write_memory"), revision=p.get("revision"),
            supporting_evidence=p.get("supportingEvidenceIds", []),
        )

    def _op_memory_status(self, p: dict[str, Any]) -> dict[str, bool]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        self.intelligence.update_memory_status(
            _required(p, "memoryId", str), _required(p, "status", str), self._actor(project_id, "write_memory")
        )
        return {"updated": True}

