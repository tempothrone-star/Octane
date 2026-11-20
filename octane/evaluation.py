"""Replay local SWE-bench-style cases without model calls or networked checks."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import time

from .source_archive import export_revision, resolve_revision, git
from .analysis import analyzer


def apply_patch(root: Path, patch_text: str):
    if not isinstance(patch_text, str) or len(patch_text.encode()) > 2_000_000:
        raise ValueError("evaluation patches must be text below 2 MB")
    if "GIT binary patch" in patch_text or re.search(r"(?:new file mode|old mode|new mode) 120000", patch_text):
        raise ValueError("evaluation patches must not create binary files or symlinks")
    with tempfile.TemporaryDirectory(prefix="octane-eval-patch-") as directory:
        patch = Path(directory) / "candidate.diff"
        patch.write_text(patch_text)
        for options in (["--check"], []):
            try:
                git(root, "apply", *options, str(patch))
            except ValueError as exc:
                raise ValueError("evaluation patch does not apply to the selected source") from exc
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("evaluation patch created a symlink")


def _command(case, explicit, standard):
    if explicit in case:
        command = case[explicit]
    else:
        tests = case.get(standard, [])
        tests = json.loads(tests) if isinstance(tests, str) else tests
        command = ["python", "-B", "-m", "pytest", "-q", *tests] if tests else []
    if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("each evaluation case needs focused and preservation commands")
    return command


def evaluate_case(manager, project_id, case, image):
    started = time.monotonic()
    project = manager.intelligence.get_project(project_id)
    root = Path(project["root"])
    revision = resolve_revision(root, case["base_commit"])
    focused = _command(case, "test_command", "FAIL_TO_PASS")
    preservation = _command(case, "preservation_command", "PASS_TO_PASS")
    workspaces = []
    with tempfile.TemporaryDirectory(prefix="octane-evaluation-") as directory:
        checkout = export_revision(root, revision, Path(directory) / "project")
        if case.get("test_patch"):
            apply_patch(checkout, case["test_patch"])
        original = manager.create_workspace(project_id, "experimentation", source_root=checkout, source_revision=revision)
        workspaces.append(original)
        try:
            baseline = manager.run_experiment(original["id"], focused, image, 60)
            immutable = baseline["image_id"]
            before = manager.run_experiment(original["id"], preservation, immutable, 60)
            source, _ = analyzer()
            trace = baseline["stderr"].replace("/workspace/", str(checkout) + "/")
            ranking = source.locate_suspects(checkout, trace, query=case.get("problem_statement", ""))
            hypothesis_seconds = time.monotonic() - started
            ranked = [item["path"] for item in ranking["suspects"]]
            expected = set(case.get("expected_files", []))
            rank = next((index for index, name in enumerate(ranked, 1) if name in expected), None)
            candidate_tree = Path(directory) / "candidate"
            export_revision(root, revision, candidate_tree)
            if case.get("test_patch"):
                apply_patch(candidate_tree, case["test_patch"])
            candidate = manager.create_workspace(project_id, "experimentation", source_root=candidate_tree, source_revision=revision)
            workspaces.append(candidate)
            apply_patch(Path(candidate["path"]), case["patch"])
            after = manager.run_experiment(candidate["id"], focused, immutable, 60)
            preserved = manager.run_experiment(candidate["id"], preservation, immutable, 60)
            resolved = (baseline["returncode"] == 1 and before["returncode"] == 0 and after["returncode"] == 0
                        and preserved["returncode"] == 0 and not any(
                            run["timed_out"] or run["cleanup_warning"] or run["before_hash"] != run["after_hash"]
                            for run in (baseline, before, after, preserved))
                        and all(run["test_integrity"] == baseline["test_integrity"] for run in (before, after, preserved)))
            return {"instance_id": case["instance_id"], "base_commit": revision, "resolved": resolved,
                    "localization_rank": rank, "first_hypothesis_seconds": hypothesis_seconds, "ranked_files": ranked[:20], "image_id": immutable,
                    "baseline_run_id": baseline["run_id"], "candidate_run_id": after["run_id"],
                    "preservation_run_ids": [before["run_id"], preserved["run_id"]],
                    "evidence_ids": [run["evidence_id"] for run in (baseline, before, after, preserved)]}
        finally:
            for workspace in workspaces:
                manager.destroy_workspace(workspace["id"])


def evaluate_dataset(manager, project_id, relative_path, image, agent_id, limit=5):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("caseLimit must be an integer from 1 to 100")
    project = manager.intelligence.get_project(project_id)
    root = Path(project["root"])
    path = root / relative_path
    if Path(relative_path).is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("evaluation dataset must stay within the selected project")
    with path.open("rb") as stream:
        raw = stream.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("evaluation dataset exceeds 4 MB")
    cases = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not cases:
        raise ValueError("evaluation dataset must contain at least one case")
    if len({case["instance_id"] for case in cases}) != len(cases):
        raise ValueError("evaluation case IDs must be unique")
    outcomes = []
    for case in cases[:limit]:
        try:
            outcomes.append(evaluate_case(manager, project_id, case, image))
        except (ValueError, KeyError, OSError) as exc:
            outcomes.append({"instance_id": case.get("instance_id"), "resolved": False, "error": str(exc)})
    payload = _evaluation_summary(cases, outcomes, limit)
    evidence = manager.intelligence.submit_evidence(project_id, "evaluation_result", payload,
        "Recorded container-backed repair evaluation", agent_id, project["revision"], verification_status="observed")
    return {"evidence_id": evidence["id"], **payload}


def _evaluation_summary(cases, outcomes, limit):
    # Execution errors count as misses instead of silently shrinking the labeled denominator.
    ranks = [outcome.get("localization_rank") for case, outcome in zip(cases, outcomes) if case.get("expected_files")]
    tested = len(outcomes)
    return {"schema": "octane.evaluation.v2", "cases": outcomes, "case_count": tested,
            "resolved": sum(item["resolved"] for item in outcomes),
            "resolved_rate": sum(item["resolved"] for item in outcomes) / tested if tested else None,
            "localization_mrr": sum(1 / rank if rank else 0 for rank in ranks) / len(ranks) if ranks else None,
            "localization_top1": sum(rank == 1 for rank in ranks) / len(ranks) if ranks else None,
            "localization_top5": sum(rank is not None and rank <= 5 for rank in ranks) / len(ranks) if ranks else None,
            "localization_labeled_cases": len(ranks), "error_count": sum("error" in row for row in outcomes),
            "truncated": len(cases) > limit,
            "basis": "User-supplied local cases and patches; file-level labels, not autonomous fix discovery. Errors count as misses"}
