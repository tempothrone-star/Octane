"""Operator-owned final validation. Neither oracle code nor its output is returned to tools."""
from __future__ import annotations

import hashlib
import json
import itertools
import stat
import shutil
import tempfile
from pathlib import Path

from .workspace import _tree_hash


def _policy(path, project_root, forbidden_roots):
    policy_path = Path(path).expanduser().resolve(strict=True)
    if any(policy_path.is_relative_to(root) for root in forbidden_roots):
        raise ValueError('holdout policy must be outside agent-accessible project and workspace roots')
    with policy_path.open('rb') as stream:
        raw = stream.read(64001)
    if len(raw) > 64000:
        raise ValueError('holdout policy exceeds 64 KB')
    data = json.loads(raw)
    if Path(data['projectRoot']).resolve() != project_root:
        raise ValueError('holdout policy does not select this project')
    suite = Path(data['suiteRoot']).expanduser().resolve(strict=True)
    if not suite.is_dir() or any(suite.is_relative_to(root) or root.is_relative_to(suite) for root in forbidden_roots):
        raise ValueError('holdout suite must be separate from agent-accessible roots')
    command = data['command']
    if (not isinstance(command, list) or not command or len(command) > 64
            or any(not isinstance(part, str) or not part or len(part) > 2000 for part in command)):
        raise ValueError('holdout policy needs an explicit bounded command array')
    total = 0
    for number, item in enumerate(suite.rglob('*'), 1):
        if number > 1000:
            raise ValueError('holdout suite exceeds 1000 filesystem entries')
        if item.is_symlink():
            raise ValueError('holdout suites cannot contain symlinks')
        if item.is_file():
            total += item.stat().st_size
        if total > 2000000:
            raise ValueError('holdout suite exceeds 2 MB')
    return suite, command, hashlib.sha256(raw).hexdigest()


def _suite_hash(root):
    # Unlike project snapshots, oracle hashes include dotfiles and all suite data.
    entries = sorted(itertools.islice(root.rglob('*'), 1001))
    if len(entries) > 1000:
        raise ValueError('holdout suite exceeds 1000 filesystem entries')
    digest, total = hashlib.sha256(), 0
    for item in entries:
        mode = item.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError('holdout suite entries must be regular files or directories')
        content = b''
        if stat.S_ISREG(mode):
            with item.open('rb') as stream:
                content = stream.read(2000001)
            total += len(content)
            if total > 2000000:
                raise ValueError('holdout suite exceeds 2 MB')
        digest.update(json.dumps([item.relative_to(root).as_posix(), mode, hashlib.sha256(content).hexdigest()]).encode())
    return digest.hexdigest()


def validate_holdout(manager, workspace_id, candidate_run_id, policy_path, forbidden_roots):
    """Run only the frozen policy against the current, previously tested candidate."""
    workspace = manager._get(workspace_id)
    project = manager.intelligence.get_project(workspace['project_id'])
    with manager.intelligence._session() as db:
        row = db.execute('SELECT * FROM execution_runs WHERE id=? AND workspace_id=?',
                         (candidate_run_id, workspace_id)).fetchone()
    candidate_hash = _tree_hash(Path(workspace['path']))
    if (not row or row['status'] != 'passed' or row['after_hash'] != candidate_hash
            or row['before_hash'] != candidate_hash or not row['image_id']):
        raise ValueError('holdout validation needs a passing unchanged current candidate run')
    suite, command, policy_hash = _policy(policy_path, Path(project['root']).resolve(),
                                         [Path(p).resolve() for p in forbidden_roots])
    # A fresh frozen copy prevents the executing program from changing the oracle.
    with tempfile.TemporaryDirectory(prefix='.octane-holdout-') as temp:
        frozen = Path(temp) / 'suite'
        before = _suite_hash(suite)
        shutil.copytree(suite, frozen)
        if _suite_hash(frozen) != before or _suite_hash(suite) != before:
            raise ValueError('holdout suite changed during freezing')
        result = manager._docker_run(workspace, command, row['image_id'], 60, writable=False,
                                     investigation_id=row['investigation_id'], python_trace=False,
                                     private_suite=frozen)
    passed = (result['returncode'] == 0 and not result['timed_out'] and not result['cleanup_warning']
              and result['before_hash'] == result['after_hash'] == candidate_hash)
    actor = manager.intelligence.get_or_register_agent(project['id'], 'operator-holdout', 'private validation',
                                                       ['operator-owned-oracle'], ['write_evidence'])
    payload = {'schema': 'octane.holdout.v1', 'passed': passed, 'candidate_run_id': candidate_run_id,
               'candidate_hash': candidate_hash, 'image_id': row['image_id'], 'policy_sha256': policy_hash,
               'suite_sha256': before, 'run_id': result['run_id'], 'execution_evidence_id': result['evidence_id'],
               'basis': 'Operator-selected withheld checks; not a proof against malicious runtime code or all unseen inputs'}
    evidence = manager.intelligence.submit_evidence(project['id'], 'holdout_result', payload,
        'Private final validation; oracle contents and command output withheld', actor['id'], project['revision'])
    if not passed:
        raise ValueError('operator holdout rejected the candidate; evidence ' + evidence['id'])
    return {'evidence_id': evidence['id'], **payload}
