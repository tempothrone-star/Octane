"""Git bisect bookkeeping on the host with all project checks in containers."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import tempfile

from .source_archive import export_revision, git, resolve_revision
from .test_protection import baseline_tests


def _first_bad(output):
    # Git versions differ on quoting the term; do not match commit-message prose.
    found = re.search(r"(?m)^([0-9a-f]{40}|[0-9a-f]{64}) is the first (?:bad|'bad') commit\r?$", output)
    return found.group(1) if found else None


def bisect_project(manager, project_id, good, bad, command, image, agent_id, budget=20):
    if type(budget) is not int or not 2 <= budget <= 100:
        raise ValueError("bisect budget must be an integer from 2 to 100")
    project = manager.intelligence.get_project(project_id)
    repository = Path(project["root"])
    good = resolve_revision(repository, good)
    bad = resolve_revision(repository, bad)
    git(repository, "merge-base", "--is-ancestor", good, bad)
    results, skipped_paths = [], []
    image_id = None
    with tempfile.TemporaryDirectory(prefix="octane-bisect-") as directory:
        base = Path(directory)
        history = base / "history"
        history.mkdir()
        git(history, "init", "--bare", "-q")
        git(history, "fetch", "--quiet", str(repository), good, bad)
        git(history, "update-ref", "HEAD", bad)
        tests = export_revision(repository, bad, base / "tests", skipped_paths=skipped_paths)
        _, overlays = baseline_tests(tests)

        def check(revision):
            nonlocal image_id
            checkout = export_revision(repository, revision, base / revision, skipped_paths=skipped_paths)
            for relative in overlays:
                original, target = tests / relative, checkout / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if original.is_dir():
                    shutil.copytree(original, target, dirs_exist_ok=True)
                else:
                    shutil.copyfile(original, target)
            workspace = manager.create_workspace(project_id, "experimentation", source_root=checkout, source_revision=revision)
            try:
                result = manager.run_experiment(workspace["id"], command, image_id or image, 60)
                image_id = result["image_id"]
            finally:
                manager.destroy_workspace(workspace["id"])
            code = result["returncode"]
            outcome = "good" if code == 0 else ("bad" if code == 1 else "skip")
            if result["timed_out"] or result["cleanup_warning"]:
                outcome = "skip"
            results.append({"revision": revision, "outcome": outcome, "run_id": result["run_id"],
                            "evidence_id": result["evidence_id"], "image_id": result["image_id"]})
            return outcome

        if check(good) != "good" or check(bad) != "bad":
            raise ValueError("bisect endpoints must reproduce a passing good revision and a failing bad revision")
        output = git(history, "bisect", "start", "--no-checkout", bad, good).decode()
        while _first_bad(output) is None and len(results) < budget:
            revision = git(history, "rev-parse", "BISECT_HEAD").decode().strip()
            outcome = check(revision)
            output = git(history, "bisect", outcome, revision).decode()
            if "could be any of" in output:
                break
        first_bad = _first_bad(output)
        completed = first_bad is not None
    payload = {"good": good, "bad": bad, "first_bad": first_bad, "completed": completed,
               "runs": results, "skipped_archive_paths": skipped_paths, "command": command, "image": image, "image_id": image_id,
               "basis": "Same baseline tests over archived revisions, executed in the configured container engine"}
    evidence = manager.intelligence.submit_evidence(project_id, "bisect_result", payload,
        "Container-backed Git bisect", agent_id, project["revision"], verification_status="observed")
    for result in results:
        manager.intelligence.link_evidence(evidence["id"], result["evidence_id"], "context", agent_id)
    return {"evidence_id": evidence["id"], **payload}
