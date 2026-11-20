"""Workspace operations; allowlists and identities come from the owning bridge."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from .bridge_support import BridgeError, _required
from .workspace import WorkspaceManager
from .experiments import propose_patch
from .mutation import run_mutations
from .property_checks import property_command
from .bisect import bisect_project
from .evaluation import evaluate_dataset


class WorkspaceOperations:
    def _op_workspace_create(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        self._scan(project_id, only_if_changed=True,
                              max_file_bytes=p.get("maxFileBytes", 5 * 1024 * 1024),
                              max_total_bytes=p.get("maxTotalBytes", 100 * 1024 * 1024))
        return self._workspace_manager().create_workspace(
            project_id, p.get("mode", "observation")
        )

    def _workspace(self, workspace_id: str) -> tuple[WorkspaceManager, str]:
        with self.intelligence._session() as db:
            row = db.execute("SELECT project_id FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown workspace: {workspace_id}")
        project_id = row["project_id"]
        self._project(project_id)
        return self._workspace_manager(), project_id

    def _workspace_manager(self):
        return WorkspaceManager(self.intelligence, self.workspace_root,
                                allow_default_container_user=self.allow_default_container_user,
                                container_engine=self.container_engine, container_runtime=self.container_runtime)

    def _op_workspace_patch(self, p: dict[str, Any]) -> dict[str, bool]:
        manager, _ = self._workspace(_required(p, "workspaceId", str))
        manager.apply_candidate_patch(
            p["workspaceId"], _required(p, "relativePath", str), _required(p, "content", str)
        )
        return {"patched": True}

    def _op_workspace_remove(self, p: dict[str, Any]) -> dict[str, bool]:
        manager, _ = self._workspace(_required(p, "workspaceId", str))
        manager.remove_candidate_file(p['workspaceId'], _required(p, 'relativePath', str))
        return {'removed': True}

    def _op_workspace_compare(self, p: dict[str, Any]) -> dict[str, Any]:
        manager, _ = self._workspace(_required(p, "workspaceId", str))
        return manager.compare_workspace(p["workspaceId"])

    def _op_workspace_run(self, p: dict[str, Any]) -> dict[str, Any]:
        manager, project_id = self._workspace(_required(p, "workspaceId", str))
        investigation_id = p.get("investigationId")
        if investigation_id is not None:
            investigation = self.intelligence.retrieve_investigation(investigation_id)
            if not investigation or investigation["project_id"] != project_id:
                raise BridgeError("investigation must belong to the workspace project")
        mode = p.get("mode", "observation")
        command = _required(p, "command", list)
        image = _required(p, "image", str)
        if image not in self.allowed_images:
            raise PermissionError("Docker image is not in the operator-configured allowedDockerImages list")
        timeout = p.get("timeoutSeconds", 30)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise BridgeError("timeoutSeconds must be an integer from 1 to 300")
        python_trace = p.get("pythonTrace", False)
        if type(python_trace) is not bool:
            raise BridgeError("pythonTrace must be a boolean")
        native_symbols = _required(p, "nativeTraceSymbols", list) if "nativeTraceSymbols" in p else None
        runtime_trace = _required(p, "runtimeTrace", str) if "runtimeTrace" in p else None
        report_format = _required(p, "reportFormat", str) if "reportFormat" in p else None
        if mode == "observation":
            return manager.run_observation(p["workspaceId"], command, image, timeout,
                                           investigation_id=investigation_id, python_trace=python_trace,
                                           native_trace_symbols=native_symbols, runtime_trace=runtime_trace, report_format=report_format,
                                           allow_build_changes=p.get("allowBuildChanges", False),
                                           build_changes_rationale=p.get("buildChangesRationale", ""))
        if mode == "experimentation":
            return manager.run_experiment(p["workspaceId"], command, image, timeout,
                                          investigation_id=investigation_id, python_trace=python_trace,
                                          native_trace_symbols=native_symbols, runtime_trace=runtime_trace, report_format=report_format,
                                           allow_build_changes=p.get("allowBuildChanges", False),
                                           build_changes_rationale=p.get("buildChangesRationale", ""))
        raise BridgeError("mode must be observation or experimentation")

    def _op_workspace_propose(self, p: dict[str, Any]) -> dict[str, Any]:
        manager, project_id = self._workspace(_required(p, "workspaceId", str))
        holdout = None
        if self.holdout_policy is not None:
            from .holdout import validate_holdout
            holdout = validate_holdout(manager, p["workspaceId"], _required(p, "candidateRunId", str),
                                      self.holdout_policy, [*self.allowed_roots, self.workspace_root])
        result = propose_patch(manager, project_id, _required(p, "investigationId", str), p["workspaceId"],
                             _required(p, "baselineRunId", str), _required(p, "candidateRunId", str),
                             _required(p, "regressionRunIds", list), _required(p, "rationale", str),
                             _required(p, "evidenceIds", list), self._actor(project_id, "workflow"),
                             strict_preservation_output=p.get("strictPreservationOutput", False),
                             allow_test_changes=p.get("allowTestChanges", False),
                             test_changes_rationale=p.get("testChangesRationale", ""),
                             allow_build_changes=p.get("allowBuildChanges", False),
                             build_changes_rationale=p.get("buildChangesRationale", ""), final_validation=holdout)
        if holdout is not None:
            result["holdout"] = holdout
            self.intelligence.link_evidence(result["evidence_id"], holdout["evidence_id"], "context",
                                            self._actor(project_id, "write_evidence"))
        return result

    def _op_workspace_mutate(self, p: dict[str, Any]) -> dict[str, Any]:
        manager, project_id = self._workspace(_required(p, "workspaceId", str))
        return run_mutations(manager, p["workspaceId"], _required(p, "candidateRunId", str),
                             self._actor(project_id, "workflow"), self.allowed_images, p.get("mutationLimit", 5))

    def _op_workspace_property(self, p: dict[str, Any]) -> dict[str, Any]:
        manager, project_id = self._workspace(_required(p, "workspaceId", str))
        workspace = manager._get(p["workspaceId"])
        spec = _required(p, "propertySpec", dict)
        command = property_command(Path(workspace["path"]), spec)
        result = self._op_workspace_run({**p, "command": command})
        evidence = self.intelligence.submit_evidence(project_id, "property_check", {"specification": spec,
            "run_id": result["run_id"], "execution_evidence_id": result["evidence_id"]},
            "Recorded property or differential check", self._actor(project_id, "write_evidence"),
            workspace["base_revision"], verification_status="observed")
        return {**result, "property_evidence_id": evidence["id"]}

    def _op_workspace_bisect(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        image = _required(p, "image", str)
        if image not in self.allowed_images:
            raise PermissionError("bisect image is outside allowedDockerImages")
        return bisect_project(self._workspace_manager(), project_id, _required(p, "goodRevision", str),
                              _required(p, "badRevision", str), _required(p, "command", list), image,
                              self._actor(project_id, "workflow"), p.get("bisectBudget", 20))

    def _op_workspace_evaluate(self, p: dict[str, Any]) -> dict[str, Any]:
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        image = _required(p, "image", str)
        if image not in self.allowed_images:
            raise PermissionError("evaluation image is outside allowedDockerImages")
        return evaluate_dataset(self._workspace_manager(), project_id, _required(p, "datasetPath", str), image,
                                 self._actor(project_id, "workflow"), p.get("caseLimit", 5))

    def _op_workspace_benchmark(self, p: dict[str, Any]) -> dict[str, Any]:
        from .performance import compare_recorded
        project_id = _required(p, "projectId", str)
        self._project(project_id)
        return compare_recorded(self._workspace_manager(), project_id, _required(p, "baselineRunIds", list),
                                _required(p, "candidateRunIds", list), _required(p, "preservationPairs", list),
                                self._actor(project_id, "workflow"), p.get("minimumImprovement", 0.05))

    def _op_workspace_destroy(self, p: dict[str, Any]) -> dict[str, bool]:
        manager, _ = self._workspace(_required(p, "workspaceId", str))
        manager.destroy_workspace(p["workspaceId"])
        return {"destroyed": True}


