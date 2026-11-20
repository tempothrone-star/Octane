"""Bounded mutation experiments using the same approved workspace runner."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

from .db import _id
from .experiments import _changed_lines
from .test_protection import baseline_tests
from .mutation_support import javascript_mutants, mutation_support
from .workspace import WorkspaceManager


def _python_mutants(text, lines, on_error=None):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        if on_error is not None:
            on_error(exc)
        return
    for index, node in enumerate(ast.walk(tree)):
        if getattr(node, "lineno", None) not in lines:
            continue
        mutated = copy.deepcopy(tree)
        target = list(ast.walk(mutated))[index]
        if isinstance(target, ast.Constant) and type(target.value) in {int, float, bool}:
            target.value = not target.value if type(target.value) is bool else target.value + 1
        elif isinstance(target, ast.Compare) and len(target.ops) == 1:
            replacements = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.LtE: ast.Gt,
                            ast.Gt: ast.LtE, ast.GtE: ast.Lt}
            replacement = replacements.get(type(target.ops[0]))
            if replacement is None:
                continue
            target.ops = [replacement()]
        elif isinstance(target, ast.BinOp) and isinstance(target.op, (ast.Add, ast.Sub)):
            target.op = ast.Sub() if isinstance(target.op, ast.Add) else ast.Add()
        else:
            continue
        yield "python:" + str(index), ast.unparse(mutated) + "\n"


def mutation_candidates(manager, workspace_id, limit):
    workspace = manager._get(workspace_id)
    comparison = manager.compare_workspace(workspace_id)
    if comparison["original_changed"] or workspace["mode"] != "experimentation":
        raise ValueError("mutation testing requires a current experimentation workspace")
    originals, candidates = Path(workspace["baseline_path"]), Path(workspace["path"])
    patches, mutants = {}, []
    for change in comparison["differences"]:
        if change["kind"] != "modified" or change.get("binary"):
            raise ValueError("mutation testing supports text edits to existing files")
        relative = change["path"]
        patches[relative] = (candidates / relative).read_text()
    support = mutation_support(comparison["differences"])
    available = {item["path"] for item in support["paths"] if item["available"]}
    for change in comparison["differences"]:
        relative = change["path"]
        if relative not in available:
            continue
        mutants.append({"id": _id("MUT"), "path": relative, "operator": "original-source",
                        "content": (originals / relative).read_text()})
        local = (_python_mutants(patches[relative], _changed_lines(change.get("diff", "")),
                                 lambda exc, path=relative: _record_parse_error(manager, workspace, path, exc)) if relative.casefold().endswith(".py")
                 else javascript_mutants(patches[relative], relative, _changed_lines(change.get("diff", ""))))
        for operator, content in local:
            mutants.append({"id": _id("MUT"), "path": relative, "operator": operator, "content": content})
            if len(mutants) >= limit:
                break
        if len(mutants) >= limit:
            break
    return workspace, comparison, patches, mutants[:limit]


def run_mutations(manager: WorkspaceManager, workspace_id, candidate_run_id, agent_id, allowed_images, limit=5):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("mutationLimit must be an integer from 1 to 20")
    workspace, comparison, patches, mutants = mutation_candidates(manager, workspace_id, limit)
    intelligence = manager.intelligence
    with intelligence._session() as db:
        row = db.execute("SELECT * FROM execution_runs WHERE id=? AND workspace_id=? AND status='passed'",
                         (candidate_run_id, workspace_id)).fetchone()
    if not row or row["after_hash"] != comparison["candidate_hash"] or not row["investigation_id"]:
        raise ValueError("mutation testing needs a passing run on the current candidate")
    if row["image"] not in allowed_images:
        raise PermissionError("mutation image is outside allowedDockerImages")
    result = json.loads(row["result_json"])
    original_tests, _ = baseline_tests(Path(workspace["baseline_path"]))
    if result.get("test_integrity") != original_tests:
        raise ValueError("mutation testing needs original baseline test protection")
    results = []
    trace_options = {"python_trace": "python_trace" in result,
                     "native_trace_symbols": result.get("native_trace", {}).get("symbols"),
                     "runtime_trace": result.get("runtime_trace", {}).get("format"),
                     "report_format": result.get("report_format"),
                     "allow_build_changes": result.get("build_changes", {}).get("allowed", False),
                     "build_changes_rationale": result.get("build_changes", {}).get("justification", "")}
    for mutant in mutants:
        snapshot = manager.create_workspace(workspace["project_id"], "experimentation")
        try:
            for relative, content in patches.items():
                manager.apply_candidate_patch(snapshot["id"], relative, content)
            manager.apply_candidate_patch(snapshot["id"], mutant["path"], mutant["content"])
            execution = manager.run_experiment(snapshot["id"], json.loads(row["command_json"]), row["image_id"], 30,
                                                investigation_id=row["investigation_id"], **trace_options)
            code = execution["returncode"]
            status = "inconclusive" if (execution["timed_out"] or execution["cleanup_warning"] or code in {None, 125, 126, 127}
                                        or code < 0) else ("survived" if code == 0 else "killed")
            results.append({key: mutant[key] for key in ("id", "path", "operator")} |
                           {"status": status, "run_id": execution["run_id"], "evidence_id": execution["evidence_id"]})
        finally:
            manager.destroy_workspace(snapshot["id"])
    killed = sum(item["status"] == "killed" for item in results)
    survived = sum(item["status"] == "survived" for item in results)
    tested = killed + survived
    reversions = [item for item in results if item["operator"] == "original-source"]
    support = mutation_support(comparison["differences"])
    status = ('unavailable' if support['status'] == 'unavailable' else
              ('inconclusive' if not tested or any(item['status'] == 'inconclusive' for item in results) else
               ('partial' if support['status'] == 'partial' else 'completed')))
    payload = {"status": status, "support": support, "reason": support['reason'], "candidate_hash": comparison["candidate_hash"], "candidate_run_id": candidate_run_id,
               "image_id": row["image_id"], "test_integrity": original_tests, "mutants": results,
               "killed": killed, "survived": survived, "score": killed / tested if tested and status not in {"unavailable", "inconclusive"} else None,
               "original_bug_detected": bool(reversions) and all(item["status"] == "killed" for item in reversions),
               "basis": "Bounded original-source reversions and local mutation operators"}
    evidence = intelligence.submit_evidence(workspace["project_id"], "mutation_result", payload,
                                             "Recorded mutation checks on the candidate", agent_id,
                                             workspace["base_revision"], verification_status="observed")
    for item in results:
        intelligence.link_evidence(evidence["id"], item["evidence_id"], "context", agent_id)
    return {"evidence_id": evidence["id"], **payload}


def _record_parse_error(manager, workspace, path, error):
    intelligence = manager.intelligence
    actor = intelligence.get_or_register_agent(workspace['project_id'], 'mutation-parser', 'syntax observer', [], ['write_evidence'])
    return intelligence.submit_evidence(workspace['project_id'], 'parse_error',
        {'path': path, 'error': type(error).__name__, 'stage': 'mutation_generation'},
        'Python mutation operators unavailable for this file; source reversion remains separate', actor['id'],
        workspace['base_revision'], path)
