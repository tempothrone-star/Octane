"""Compare recorded executions and preserve reviewable candidate patches."""
from __future__ import annotations

import json

from .db import _id, _now, _scrub_sensitive_text
from .investigation import context_evidence_ids
from .workspace import WorkspaceManager

_INTENT_TYPES = {"user_requirement", "documented_behavior", "design_decision", "intent_contract"}


def _trace(result):
    return result.get("runtime_trace", result.get("native_trace", result.get("python_trace")))


def _trace_available(result):
    trace = _trace(result)
    return trace is None or trace.get("available") is True


def _trace_settings(run):
    result = json.loads(run["result_json"])
    if "native_trace" in result:
        return "native", tuple(result["native_trace"]["symbols"])
    if "runtime_trace" in result:
        return "runtime", result["runtime_trace"]["format"]
    return ("python",) if "python_trace" in result else ()


def _same_output(baseline, candidate):
    for result in (baseline, candidate):
        truncated = result.get("output_truncated", {})
        if (any(truncated.values()) if isinstance(truncated, dict) else bool(truncated)):
            return None
        for key in ("stdout", "stderr"):
            output = result.get(key)
            if not isinstance(output, str) or "[REDACTED" in output or "\ufffd" in output:
                return None
    return all(baseline[key] == candidate[key] for key in ("stdout", "stderr"))


def propose_patch(manager: WorkspaceManager, project_id: str, investigation_id: str, workspace_id: str,
                  baseline_run_id: str, candidate_run_id: str, regression_run_ids: list[str],
                  rationale: str, supporting_evidence_ids: list[str], agent_id: str,
                  *, strict_preservation_output: bool = False):
    if type(strict_preservation_output) is not bool:
        raise ValueError("strictPreservationOutput must be a boolean")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("a patch proposal needs an intent-preserving rationale")
    if not isinstance(regression_run_ids, list) or not regression_run_ids:
        raise ValueError("a patch proposal needs at least one separate recorded regression run")
    if not isinstance(supporting_evidence_ids, list) or not supporting_evidence_ids:
        raise ValueError("a patch proposal needs evidence explaining expected behavior")
    ids = [baseline_run_id, candidate_run_id] + regression_run_ids
    if any(not isinstance(item, str) for item in ids + supporting_evidence_ids) or len(set(ids)) != len(ids):
        raise ValueError("baseline, candidate, and regression run IDs must be distinct strings")
    intelligence = manager.intelligence
    workspace = manager._get(workspace_id)
    if workspace["project_id"] != project_id or workspace["mode"] != "experimentation":
        raise ValueError("candidate must be an experimentation workspace in this project")
    comparison = manager.compare_workspace(workspace_id)
    if comparison["original_changed"]:
        raise ValueError("original project changed since snapshot; create a fresh experiment")
    if not comparison["patch"] or any(change.get("binary") or change["kind"] != "modified" for change in comparison["differences"]):
        raise ValueError("proposals currently require a non-empty text-only patch to existing files")
    if len(comparison["patch"].encode()) > 1_000_000:
        raise ValueError("patch exceeds the 1 MB proposal limit")
    if _scrub_sensitive_text(comparison["patch"]) != comparison["patch"]:
        raise ValueError("patch contains credential-like text that cannot be stored verbatim")
    with intelligence._transaction() as db:
        for permission in ("write_evidence", "read_investigation", "checkpoint_investigation"):
            intelligence._require_permission(db, project_id, agent_id, permission)
        investigation = intelligence.retrieve_investigation(investigation_id)
        if not investigation or investigation["project_id"] != project_id or not investigation["revision_compatible"]:
            raise ValueError("proposal needs a current investigation in this project")
        if investigation["status"] in {"completed", "abandoned"}:
            raise ValueError("cannot add a proposal to a terminal investigation")
        runs = []
        for identifier in ids:
            row = db.execute("SELECT r.*,w.mode,w.base_revision FROM execution_runs r JOIN workspaces w ON w.id=r.workspace_id WHERE r.id=? AND r.project_id=? AND r.investigation_id=?",
                             (identifier, project_id, investigation_id)).fetchone()
            if not row or not row["evidence_id"]:
                raise ValueError("every run must be recorded in this project and investigation")
            result = json.loads(row["result_json"])
            if row["before_hash"] != row["after_hash"] or result.get("timed_out") or result.get("cleanup_warning"):
                raise ValueError("runs must finish without changing the tested source or timing out")
            if not _trace_available(result):
                raise ValueError("a requested runtime trace is unavailable; rerun the check")
            if row["snapshot_hash"] != workspace["snapshot_hash"] or row["base_revision"] != investigation["revision"]:
                raise ValueError("runs must originate from the same baseline snapshot")
            runs.append(dict(row))
        baseline, candidate, *regressions = runs
        if baseline["status"] != "failed" or candidate["status"] != "passed":
            raise ValueError("the designated baseline must fail and the candidate must pass")
        if baseline["mode"] != "observation" or baseline["before_hash"] != workspace["snapshot_hash"]:
            raise ValueError("baseline must test the unchanged snapshot")
        if (baseline["command_json"] != candidate["command_json"] or baseline["image_id"] != candidate["image_id"]
                or _trace_settings(baseline) != _trace_settings(candidate)):
            raise ValueError("baseline and candidate must use the same command, immutable image, and trace setting")
        for run in [candidate] + regressions:
            if run["workspace_id"] != workspace_id or run["after_hash"] != comparison["candidate_hash"] or run["status"] != "passed":
                raise ValueError("candidate and regression runs must pass on the current candidate bytes")
            if run["image_id"] != candidate["image_id"]:
                raise ValueError("regressions must use the candidate image")
        if any(run["command_json"] == candidate["command_json"] for run in regressions):
            raise ValueError("regression command must be separate from the focused candidate check")
        preservation_pairs = []
        for regression in regressions:
            originals = db.execute("""
                SELECT r.id,r.evidence_id,r.result_json FROM execution_runs r
                JOIN workspaces w ON w.id=r.workspace_id
                WHERE r.project_id=? AND r.investigation_id=? AND r.command_json=? AND r.image_id=?
                  AND r.snapshot_hash=? AND r.before_hash=r.snapshot_hash AND r.after_hash=r.snapshot_hash
                  AND w.mode='observation' AND w.base_revision=? AND r.status='passed' AND r.evidence_id IS NOT NULL
                ORDER BY r.created_at DESC,r.id
                """, (project_id, investigation_id, regression["command_json"], regression["image_id"],
                      workspace["snapshot_hash"], investigation["revision"]))
            original = next((row for row in originals if _trace_settings(row) == _trace_settings(regression)
                             and _trace_available(json.loads(row["result_json"]))), None)
            if original is None:
                raise ValueError("run each preservation check successfully on the unchanged observation workspace with the same image and trace setting")
            output_match = _same_output(json.loads(original["result_json"]), json.loads(regression["result_json"]))
            if strict_preservation_output and output_match is not True:
                raise ValueError("preservation output differs or is incomplete; compare the paired stdout and stderr")
            preservation_pairs.append({"baseline_run_id": original["id"], "baseline_evidence_id": original["evidence_id"],
                                       "candidate_run_id": regression["id"], "candidate_evidence_id": regression["evidence_id"],
                                       "output_match": output_match})
        intent_ids = []
        for identifier in supporting_evidence_ids:
            row = db.execute("SELECT stale,revision,evidence_type,verification_status FROM evidence WHERE id=? AND project_id=?", (identifier, project_id)).fetchone()
            if not row or row["stale"] or row["revision"] != investigation["revision"]:
                raise ValueError("supporting evidence must be current and belong to this project")
            if row["evidence_type"] in _INTENT_TYPES and row["verification_status"] not in {"contradicted", "inferred"}:
                intent_ids.append(identifier)
        if not intent_ids:
            raise ValueError("cite current user_requirement, documented_behavior, design_decision, or intent_contract evidence")
        traces = [_trace(json.loads(run["result_json"])) or {} for run in (baseline, candidate)]
        observed_paths = set()
        for trace in traces:
            observed_paths.update(entry["path"] for entry in trace.get("entries", []))
            for edge in trace.get("calls", []):
                observed_paths.update((edge["caller"]["path"], edge["callee"]["path"]))
        scope = [{"path": change["path"], "seen_in_focused_trace": change["path"] in observed_paths if any(traces) else None}
                 for change in comparison["differences"]]
        proposal_id = _id("PATCH")
        payload = {"proposal_id": proposal_id, "investigation_id": investigation_id, "workspace_id": workspace_id,
                   "baseline_run_id": baseline_run_id, "candidate_run_id": candidate_run_id,
                   "regression_run_ids": regression_run_ids, "supporting_evidence_ids": supporting_evidence_ids,
                   "intent_evidence_ids": intent_ids, "preservation_pairs": preservation_pairs,
                   "changed_paths": scope, "review_notes": comparison["review_notes"],
                   "rationale": _scrub_sensitive_text(rationale), "patch": comparison["patch"], "snapshot_hash": workspace["snapshot_hash"],
                   "candidate_hash": comparison["candidate_hash"], "image_id": candidate["image_id"],
                   "conclusion": "Focused check failed before and passed after. Preservation checks passed on both snapshots.",
                   "original_modified_by_octane": False}
        evidence = intelligence.submit_evidence(project_id, "patch_proposal", payload,
                                                 "Candidate patch with reproducible execution evidence", agent_id,
                                                 investigation["revision"], verification_status="unverified")
        for run in runs:
            intelligence.link_evidence(evidence["id"], run["evidence_id"], "context", agent_id)
        for pair in preservation_pairs:
            intelligence.link_evidence(evidence["id"], pair["baseline_evidence_id"], "context", agent_id)
        for identifier in supporting_evidence_ids:
            intelligence.link_evidence(evidence["id"], identifier, "supports", agent_id)
        db.execute("INSERT INTO patch_proposals VALUES(?,?,?,?,?,?,?,?)",
                   (proposal_id, project_id, investigation_id, workspace_id, baseline_run_id, candidate_run_id, evidence["id"], _now()))
        context = dict(investigation["context"])
        context.update({"patch_proposal_id": proposal_id, "next_action": "Review the patch and execution evidence before applying any change to the original",
                        "evidence_ids": sorted(context_evidence_ids(context) | {evidence["id"]})})
        intelligence.checkpoint_investigation(investigation_id, "blocked", context, agent_id)
        return {"proposal_id": proposal_id, "evidence_id": evidence["id"], "review_required": True, **payload}
