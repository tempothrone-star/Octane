"""Optional GitHub evidence and publication through the operator's GitHub CLI."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import tempfile

from .db import _scrub_sensitive_text
from .reports import parse_report
from .process import run_bounded


def _repository(value):
    if (not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+", value)
            or value.rsplit("/", 1)[-1] in {".", ".."}):
        raise ValueError("repository must be in owner/name form")
    return value


class GitHubIntegration:
    def __init__(self, intelligence, project_id, agent_id):
        self.intelligence = intelligence
        self.project = intelligence.get_project(project_id)
        if self.project is None:
            raise KeyError(project_id)
        self.agent = agent_id

    def _run(self, arguments):
        executable = shutil.which("gh")
        if executable is None:
            raise RuntimeError("Install and authenticate GitHub CLI before using the GitHub integration")
        result = run_bounded([executable, *arguments])
        if result.returncode != 0:
            raise RuntimeError("GitHub CLI failed: " + _scrub_sensitive_text(result.stderr.decode("utf-8", "replace")[:2000]))
        if len(result.stdout) > 4_000_000:
            raise ValueError("GitHub response exceeds the 4 MB evidence budget")
        return result.stdout

    def issue(self, repository, number):
        repository = _repository(repository)
        if type(number) is not int or number < 1:
            raise ValueError("issue number must be positive")
        raw = self._run(["api", f"repos/{repository}/issues/{number}"])
        report = parse_report(raw, "github", Path(self.project["root"]))
        return self.intelligence.submit_evidence(self.project["id"], "github_issue", report,
            f"GitHub issue {repository}#{number}", self.agent, self.project["revision"], verification_status="observed")

    def workflow(self, repository, run_id):
        repository = _repository(repository)
        if type(run_id) is not int or run_id < 1:
            raise ValueError("workflow run ID must be positive")
        metadata = json.loads(self._run(["api", f"repos/{repository}/actions/runs/{run_id}"]))
        logs = self._run(["run", "view", str(run_id), "--repo", repository, "--log-failed"]).decode("utf-8", "replace")
        payload = {"repository": repository, "run_id": run_id, "url": metadata.get("html_url"),
                   "head_sha": metadata.get("head_sha"), "conclusion": metadata.get("conclusion"), "log": logs}
        return self.intelligence.submit_evidence(self.project["id"], "github_workflow", payload,
            f"GitHub workflow {repository}/{run_id}", self.agent, self.project["revision"], verification_status="observed")

    def _summary(self, evidence_ids):
        records = []
        for identifier in evidence_ids:
            item = self.intelligence.retrieve_evidence(identifier)
            if item is None or item["project_id"] != self.project["id"]:
                raise ValueError("summary evidence must belong to the selected project")
            records.append(f"- {item['id']} · {item['description']} · {item['verification_status']}")
        if not records:
            raise ValueError("select evidence IDs before publishing a summary")
        return "Octane investigation evidence\n\n" + "\n".join(records)

    def publish(self, repository, evidence_ids, *, pull_request=None, head_sha=None, approved=False):
        if approved is not True:
            raise PermissionError("publishing requires an explicit --publish request")
        repository = _repository(repository)
        if (pull_request is None) == (head_sha is None):
            raise ValueError("select exactly one pull request or check-run commit")
        summary = self._summary(evidence_ids)
        with tempfile.TemporaryDirectory(prefix="octane-github-") as directory:
            path = Path(directory) / "body"
            if pull_request is not None:
                if type(pull_request) is not int or pull_request < 1:
                    raise ValueError("pull request number must be positive")
                path.write_text(summary)
                output = self._run(["pr", "comment", str(pull_request), "--repo", repository, "--body-file", str(path)])
            else:
                if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
                    raise ValueError("head_sha must be a full commit SHA")
                path.write_text(json.dumps({"name": "Octane evidence", "head_sha": head_sha, "status": "completed",
                    "conclusion": "neutral", "output": {"title": "Recorded Octane evidence", "summary": summary}}))
                output = self._run(["api", f"repos/{repository}/check-runs", "--method", "POST", "--input", str(path)])
        with self.intelligence._transaction() as db:
            self.intelligence._audit(db, self.project["id"], self.agent, "github.publish", "project", self.project["id"],
                                     {"repository": repository, "evidence_ids": evidence_ids,
                                      "pull_request": pull_request, "head_sha": head_sha})
        return {"published": True, "repository": repository, "response": output.decode("utf-8", "replace")[:2000]}
