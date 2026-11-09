"""JSON-lines bridge used by the native OpenClaw plugin.

The bridge deliberately exposes a fixed, audited operation set. It never runs
project commands on the host; workspace commands are delegated to the existing
Docker-only WorkspaceManager.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .db import ProjectIntelligence
from .exploration import ExplorationProfile, ExplorationScheduler, PythonAdapter, Tentacle
from .workspace import WorkspaceManager

_MAX_REQUEST_BYTES = 1_000_000
_ALLOWED_EVIDENCE_STATUS = {"observed", "inferred", "unverified", "contradicted"}
_PERMISSIONS = {
    "read_evidence": ("read_evidence",),
    "write_evidence": ("write_evidence",),
    "verify_evidence": ("verify_evidence",),
    "read_memory": ("read_memory",),
    "write_memory": ("write_memory",),
    "read_graph": ("read_graph",),
    "update_graph": ("update_graph",),
    "explore_graph": ("read_graph", "read_evidence", "write_evidence"),
    "read_investigation": ("read_investigation",),
    "investigate": ("investigate",),
    "checkpoint_investigation": ("checkpoint_investigation",),
}
_AGENT_ROLES = {
    "reader": {"capabilities": ["project-reader"], "permissions": ["read_evidence", "read_memory", "read_graph", "read_investigation"]},
    "explorer": {"capabilities": ["source-explorer"], "permissions": ["read_evidence", "write_evidence", "read_graph", "update_graph"]},
    "investigator": {"capabilities": ["investigation"], "permissions": ["read_evidence", "write_evidence", "read_memory", "write_memory", "read_graph", "read_investigation", "investigate", "checkpoint_investigation"]},
    "verifier": {"capabilities": ["evidence-verification"], "permissions": ["read_evidence", "read_graph", "read_investigation", "verify_evidence"]},
}


class BridgeError(ValueError):
    """A safe, user-facing bridge validation error."""


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class OctaneBridge:
    def __init__(self, database: str, allowed_roots: list[str], workspace_root: str,
                 allowed_images: list[str] | None = None, allow_agent_provisioning: bool = False):
        if not allowed_roots:
            raise BridgeError("Configure at least one allowedProjectRoots entry before using Octane")
        self.allowed_roots = []
        for value in allowed_roots:
            root = Path(value).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise BridgeError(f"allowed project root is not a directory: {root}")
            if root not in self.allowed_roots:
                self.allowed_roots.append(root)
        self.intelligence = ProjectIntelligence(Path(database).expanduser())
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.allowed_images = set(allowed_images or [])
        self.allow_agent_provisioning = bool(allow_agent_provisioning)

    def _check_root(self, value: str | Path) -> Path:
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise BridgeError("project root must be an existing directory")
        if not any(_is_within(root, allowed) for allowed in self.allowed_roots):
            raise PermissionError("project root is outside configured allowedProjectRoots")
        return root

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.intelligence.get_project(project_id)
        if project is None:
            raise KeyError(f"unknown project: {project_id}")
        root = self._check_root(project["root"])
        project["root"] = str(root)
        return project

    def _actor(self, project_id: str, capability: str) -> str:
        permissions = _PERMISSIONS[capability]
        name = "openclaw-" + capability.replace("_", "-")
        agent = self.intelligence.get_or_register_agent(
            project_id, name, "OpenClaw", ["openclaw-plugin", capability], permissions
        )
        if agent["role"] != "OpenClaw" or set(agent["permissions"]) != set(permissions):
            raise PermissionError(f"reserved Octane bridge agent {name!r} has unexpected permissions; rename or repair it")
        return agent["id"]

    def dispatch(self, request: dict[str, Any]) -> Any:
        if not isinstance(request, dict) or not isinstance(request.get("operation"), str):
            raise BridgeError("request must contain a string operation")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise BridgeError("params must be an object")
        operation = request["operation"]
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:
            raise BridgeError(f"unsupported Octane operation: {operation}")
        return handler(params)

    def _op_project_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for project in self.intelligence.list_projects():
            try:
                self._check_root(project["root"])
            except (PermissionError, FileNotFoundError):
                continue
            result.append(project)
        return result

    def _op_project_create(self, p: dict[str, Any]) -> dict[str, Any]:
        root = self._check_root(_required(p, "root", str))
        return self.intelligence.create_project(_required(p, "name", str), root)

    def _op_project_show(self, p: dict[str, Any]) -> dict[str, Any] | None:
        return self._project(_required(p, "projectId", str))

    def _op_project_update(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.update_project(
            project_id, name=p.get("name"), languages=p.get("languages"),
            repository_url=p.get("repositoryUrl"), configuration=p.get("configuration"),
        )

    def _op_project_changes(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.get_project_changes(project_id, p.get("sinceRevision"))

    def _op_context(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        project = self._project(project_id)
        limit = p.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise BridgeError("context limit must be an integer from 1 to 20")
        scan_summary = None
        if p.get("refreshScan", False):
            scan = PythonAdapter().scan(self.intelligence, project_id, project["root"])
            scan_summary = {key: scan[key] for key in ("revision", "files_scanned", "symbols_discovered", "changed_files", "removed_files")}
            project = self._project(project_id)
        query, include_stale = p.get("query", ""), p.get("includeStale", False)
        evidence = self.intelligence.search_evidence(
            project_id, query=query, include_stale=include_stale,
            requesting_agent_id=self._actor(project_id, "read_evidence"),
        )[-limit:]
        memories = self.intelligence.search_memories(
            project_id, query=query, include_stale=include_stale,
            requesting_agent_id=self._actor(project_id, "read_memory"),
        )[-limit:]
        investigations = self.intelligence.search_investigations(
            project_id, requesting_agent_id=self._actor(project_id, "read_investigation"), query=query,
        )[:limit]
        hypotheses = self.intelligence.retrieve_hypotheses(
            project_id, requesting_agent_id=self._actor(project_id, "read_investigation"),
        )[-limit:]
        graph = self.intelligence.query_graph(
            project_id, include_stale=include_stale,
            requesting_agent_id=self._actor(project_id, "read_graph"),
        )
        changes = self.intelligence.get_project_changes(project_id).get("changes", [])[-5:]
        return {
            "project": {key: project[key] for key in ("id", "name", "root", "revision", "languages", "repository_url", "last_indexed_at") if key in project},
            "scan": scan_summary,
            "recent_changes": changes,
            "evidence": [{key: item.get(key) for key in ("id", "evidence_type", "description", "revision", "source_path", "source_line", "verification_status", "stale")} for item in evidence],
            "memories": [{key: (item.get(key, "")[:1000] if key == "content" else item.get(key)) for key in ("id", "category", "content", "status", "revision", "supporting_evidence_ids")} for item in memories],
            "investigations": [{key: item.get(key) for key in ("id", "objective", "status", "revision", "assigned_agent_id", "revision_compatible")} for item in investigations],
            "hypotheses": [{key: item.get(key) for key in ("id", "statement", "status", "confidence", "revision", "evidence")} for item in hypotheses],
            "graph": {"node_count": len(graph["nodes"]), "edge_count": len(graph["edges"]),
                      "nodes": [{key: node.get(key) for key in ("node_key", "kind", "name", "path", "stale")} for node in graph["nodes"][:limit]]},
        }

    def _op_audit_search(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        limit = p.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise BridgeError("audit limit must be an integer from 1 to 1000")
        return self.intelligence.search_audit_logs(
            project_id, agent_id=p.get("agentId"), action=p.get("eventAction"), limit=limit,
        )

    def _op_agents_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        with self.intelligence._session() as db:
            rows = db.execute("SELECT * FROM agents WHERE project_id=? ORDER BY last_activity,name", (project_id,)).fetchall()
        return [self.intelligence._agent_dict(row) for row in rows]

    def _op_agents_show(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        agent = self.intelligence.get_agent(_required(p, "agentId", str))
        if agent is None or agent["project_id"] != project_id:
            raise KeyError("agent does not exist in this project")
        return agent

    def _op_agents_check(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        agent_id = _required(p, "agentId", str)
        permission = _required(p, "permission", str)
        if permission not in {item for permissions in _PERMISSIONS.values() for item in permissions}:
            raise BridgeError("unknown Octane permission")
        agent = self.intelligence.get_agent(agent_id)
        if agent is None or agent["project_id"] != project_id:
            raise KeyError("agent does not exist in this project")
        return {"agent_id": agent_id, "permission": permission,
                "allowed": self.intelligence.check_permission(agent_id, permission)}

    def _op_agents_create(self, p: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_agent_provisioning:
            raise PermissionError("agent creation is disabled; an operator must set allowAgentProvisioning=true")
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        role = _required(p, "role", str)
        if role not in _AGENT_ROLES:
            raise BridgeError(f"role must be one of: {', '.join(sorted(_AGENT_ROLES))}")
        preset = _AGENT_ROLES[role]
        return self.intelligence.register_agent(
            project_id, _required(p, "name", str), role,
            capabilities=preset["capabilities"], permissions=preset["permissions"],
        )

    def _op_scan(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        project = self._project(project_id)
        return PythonAdapter().scan(self.intelligence, project_id, project["root"])

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
        self.intelligence.link_evidence(
            _required(p, "evidenceId", str), _required(p, "relatedEvidenceId", str),
            p.get("relationship", "supports"), self._actor(project_id, "write_evidence"),
        )
        return {"linked": True}

    def _op_evidence_verify(self, p: dict[str, Any]) -> dict[str, bool]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        self.intelligence.mark_evidence_verified(
            _required(p, "evidenceId", str), _required(p, "verificationEvidenceId", str),
            self._actor(project_id, "verify_evidence"),
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

    def _op_investigation_create(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.create_investigation(
            project_id, _required(p, "objective", str), self._actor(project_id, "investigate"),
            p.get("context", {}),
        )

    def _op_investigation_search(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.search_investigations(
            project_id, status=p.get("status"), requesting_agent_id=self._actor(project_id, "read_investigation"),
            query=p.get("query", ""),
        )

    def _op_investigation_read(self, p: dict[str, Any]) -> dict[str, Any] | None:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.retrieve_investigation(
            _required(p, "investigationId", str), self._actor(project_id, "read_investigation")
        )

    def _op_investigation_update(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.update_investigation(
            _required(p, "investigationId", str), objective=p.get("objective"), context=p.get("context"),
            agent_id=self._actor(project_id, "investigate"),
        )

    def _op_investigation_checkpoint(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.checkpoint_investigation(
            _required(p, "investigationId", str), _required(p, "status", str),
            _required(p, "payload", dict), self._actor(project_id, "checkpoint_investigation"),
        )

    def _op_investigation_resume(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.resume_investigation(
            _required(p, "investigationId", str), self._actor(project_id, "checkpoint_investigation")
        )

    def _op_investigation_complete(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.complete_investigation(
            _required(p, "investigationId", str), self._actor(project_id, "checkpoint_investigation"), p.get("payload")
        )

    def _op_investigation_abandon(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.abandon_investigation(
            _required(p, "investigationId", str), self._actor(project_id, "checkpoint_investigation"), p.get("payload")
        )

    def _op_graph_update(self, p: dict[str, Any]) -> dict[str, list[str]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        nodes = p.get("nodes", [])
        if not isinstance(nodes, list):
            raise BridgeError("nodes must be an array")
        relationships = p.get("graphRelationships", [])
        if not isinstance(relationships, list):
            raise BridgeError("graphRelationships must be an array")
        normalized_nodes = []
        for node in nodes:
            if not isinstance(node, dict):
                raise BridgeError("every graph node must be an object")
            item = dict(node)
            if "nodeKey" in item:
                item["node_key"] = item.pop("nodeKey")
            normalized_nodes.append(item)
        normalized_edges = []
        for edge in relationships:
            if not isinstance(edge, dict):
                raise BridgeError("every graph relationship must be an object")
            item = dict(edge)
            if "evidenceId" in item:
                item["evidence_id"] = item.pop("evidenceId")
            normalized_edges.append(item)
        return self.intelligence.update_graph(
            project_id, nodes=normalized_nodes, relationships=normalized_edges,
            agent_id=self._actor(project_id, "update_graph"),
        )

    def _op_graph_query(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.query_graph(
            project_id, node_key=p.get("nodeKey"), kind=p.get("kind"),
            include_stale=p.get("includeStale", False), requesting_agent_id=self._actor(project_id, "read_graph"),
        )

    def _op_graph_dependencies(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.get_dependencies(
            project_id, _required(p, "nodeKey", str), p.get("relationship", "imports"),
            self._actor(project_id, "read_graph"),
        )

    def _op_graph_history(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.get_relationship_history(
            project_id, node_key=p.get("nodeKey"), requesting_agent_id=self._actor(project_id, "read_graph")
        )

    def _op_graph_explore(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        profile = ExplorationProfile(
            _required(p, "objective", str), _required(p, "startingNodes", list),
            p.get("relationships", ["calls", "imports"]), p.get("depthLimit", 2),
            p.get("nodeBudget", 20), p.get("timeBudgetSeconds", 10),
        )
        return Tentacle().explore(self.intelligence, project_id, profile, self._actor(project_id, "explore_graph"))

    def _op_schedule(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = _required(p, "candidates", list)
        normalized = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise BridgeError("every schedule candidate must be an object")
            item = dict(candidate)
            if "nodeKey" in item:
                item["node_key"] = item.pop("nodeKey")
            if "informationGain" in item:
                item["information_gain"] = item.pop("informationGain")
            if "estimatedSeconds" in item:
                item["estimated_seconds"] = item.pop("estimatedSeconds")
            normalized.append(item)
        scheduler = ExplorationScheduler(p.get("weights"))
        return scheduler.prioritize(
            normalized, explored=set(p.get("explored", [])),
            node_budget=p.get("nodeBudget", 20), time_budget_seconds=p.get("timeBudgetSeconds", 10),
        )

    def _op_hypothesis_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.retrieve_hypotheses(
            project_id, investigation_id=p.get("investigationId"), status=p.get("status"),
            requesting_agent_id=self._actor(project_id, "read_investigation"),
        )

    def _op_hypothesis_create(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.create_hypothesis(
            project_id, _required(p, "statement", str), p.get("investigationId"),
            self._actor(project_id, "investigate"), status=p.get("status", "proposed"),
            confidence=p.get("confidence", 0.5),
        )

    def _op_hypothesis_update(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return self.intelligence.update_hypothesis(
            _required(p, "hypothesisId", str), status=p.get("status"), confidence=p.get("confidence"),
            agent_id=self._actor(project_id, "investigate"), expected_updated_at=p.get("expectedUpdatedAt"),
        )

    def _op_hypothesis_attach(self, p: dict[str, Any]) -> dict[str, bool]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        self.intelligence.attach_evidence(
            _required(p, "hypothesisId", str), _required(p, "evidenceId", str), p.get("relation", "supports"),
            self._actor(project_id, "investigate"),
        )
        return {"attached": True}

    def _op_workspace_create(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return WorkspaceManager(self.intelligence, self.workspace_root).create_workspace(
            project_id, p.get("mode", "observation")
        )

    def _workspace(self, workspace_id: str) -> tuple[WorkspaceManager, str]:
        with self.intelligence._session() as db:
            row = db.execute("SELECT project_id FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown workspace: {workspace_id}")
        project_id = row["project_id"]
        self._project(project_id)
        return WorkspaceManager(self.intelligence, self.workspace_root), project_id

    def _op_workspace_patch(self, p: dict[str, Any]) -> dict[str, bool]:
        manager, _ = self._workspace(_required(p, "workspaceId", str))
        manager.apply_candidate_patch(
            p["workspaceId"], _required(p, "relativePath", str), _required(p, "content", str)
        )
        return {"patched": True}

    def _op_workspace_compare(self, p: dict[str, Any]) -> dict[str, Any]:
        manager, _ = self._workspace(_required(p, "workspaceId", str))
        return manager.compare_workspace(p["workspaceId"])

    def _op_workspace_run(self, p: dict[str, Any]) -> dict[str, Any]:
        manager, _ = self._workspace(_required(p, "workspaceId", str))
        mode = p.get("mode", "observation")
        command = _required(p, "command", list)
        image = _required(p, "image", str)
        if image not in self.allowed_images:
            raise PermissionError("Docker image is not in the operator-configured allowedDockerImages list")
        timeout = p.get("timeoutSeconds", 30)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise BridgeError("timeoutSeconds must be an integer from 1 to 300")
        if mode == "observation":
            return manager.run_observation(p["workspaceId"], command, image, timeout)
        if mode == "experimentation":
            return manager.run_experiment(p["workspaceId"], command, image, timeout)
        raise BridgeError("mode must be observation or experimentation")

    def _op_workspace_destroy(self, p: dict[str, Any]) -> dict[str, bool]:
        manager, _ = self._workspace(_required(p, "workspaceId", str))
        manager.destroy_workspace(p["workspaceId"])
        return {"destroyed": True}


def _required(params: dict[str, Any], key: str, expected: type) -> Any:
    value = params.get(key)
    if not isinstance(value, expected) or (expected is int and type(value) is not int):
        raise BridgeError(f"{key} must be {expected.__name__}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--allowed-root", action="append", default=[])
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--allowed-image", action="append", default=[])
    parser.add_argument("--allow-agent-provisioning", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        response = {"ok": False, "error": {"type": "BridgeError", "message": "request exceeds 1 MB"}}
    else:
        try:
            request = json.loads(raw.decode("utf-8"))
            result = OctaneBridge(args.database, args.allowed_root, args.workspace_root,
                                   args.allowed_image, args.allow_agent_provisioning).dispatch(request)
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
