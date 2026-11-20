"""Order already-gated proposals and flag suspicious edits without generating fixes."""
from __future__ import annotations

import re
from .workspace import _tree_hash
from pathlib import Path


def masking_notes(patch):
    added = [line[1:] for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++')]
    notes = []
    for line in added:
        if re.search(r'\bexcept\s*(?::|(?:BaseException|Exception)\s*[:)])|\bcatch\s*\(', line):
            notes.append('Added broad exception handling may mask the symptom; inspect propagation and intent')
        if re.search(r'\b(?:pytest\.skip|skipTest|unittest\.skip|xit|xdescribe)\s*\(', line):
            notes.append('Added test skipping requires independent review')
        if re.search(r'^\s*(?:return\s+(?:[-+]?\d+|["\']))', line):
            notes.append('Added constant return may overfit an example; run independent holdout inputs')
    return sorted(set(notes))


def rank_patches(intelligence, project_id, records):
    if not records:
        raise ValueError('rank-patches needs recorded patch proposal evidence')
    ranked = []
    with intelligence._session() as db:
        for record in records:
            row = db.execute('SELECT p.workspace_id,r.image_id FROM patch_proposals p JOIN execution_runs r ON r.id=p.candidate_run_id WHERE p.project_id=? AND p.evidence_id=?',
                             (project_id, record['id'])).fetchone()
            if row is None or record['evidence_type'] != 'patch_proposal':
                raise ValueError('only proposals accepted by the recorded patch gate can be ranked')
            workspace = db.execute('SELECT path,status FROM workspaces WHERE id=?', (row['workspace_id'],)).fetchone()
            data = record['payload']
            if workspace['status'] != 'active' or _tree_hash(Path(workspace['path'])) != data['candidate_hash']:
                raise ValueError('rerun the patch gate after candidate bytes change')
            patch = data['patch']
            size = sum(line.startswith(('+', '-')) and not line.startswith(('+++', '---')) for line in patch.splitlines())
            ranked.append({'evidence_id': record['id'], 'changed_lines': size,
                           'masking_notes': masking_notes(patch), 'candidate_hash': data['candidate_hash'],
                           'image_id': row['image_id'], 'review_required': True})
    if len({r['image_id'] for r in ranked}) != 1:
        raise ValueError('rank candidates validated in the same immutable image')
    ranked.sort(key=lambda r: (len(r['masking_notes']), r['changed_lines'], r['evidence_id']))
    return {'candidates': ranked, 'basis': 'Gate-qualified proposals; masking risk then minimal diff. No unmeasured performance or accuracy bonus'}
