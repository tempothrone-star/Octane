"""Bounded SARIF export of recorded findings with Octane evidence attribution."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .reports import _source


def export_sarif(intelligence, project_id, evidence_ids=None, limit=1000):
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError('SARIF result limit must be from 1 to 10000')
    project = intelligence.get_project(project_id)
    if project is None:
        raise KeyError(project_id)
    root = Path(project['root']).resolve()
    if evidence_ids is None:
        with intelligence._session() as db:
            identifiers = [row['id'] for row in db.execute(
                "SELECT id FROM evidence WHERE project_id=? AND evidence_type='structured_report' ORDER BY collected_at,id LIMIT ?",
                (project_id, min(limit,1000)))]
    else:
        if not isinstance(evidence_ids, list) or len(evidence_ids) > 1000 or any(not isinstance(item,str) for item in evidence_ids):
            raise ValueError('SARIF evidence IDs must be a list of at most 1000 strings')
        identifiers = list(dict.fromkeys(evidence_ids))
    results, rules, truncated = [], {}, False
    for identifier in identifiers:
        evidence = intelligence.retrieve_evidence(identifier)
        if evidence is None or evidence['project_id'] != project_id:
            raise ValueError('SARIF evidence must belong to the selected project')
        for finding in evidence['payload'].get('findings', []):
            if len(results) >= limit:
                truncated = True
                break
            rule = str(finding.get('rule') or 'octane.imported-finding')[:200]
            rules.setdefault(rule, {'id':rule})
            level = finding.get('level','warning')
            result = {'ruleId': rule, 'level':level if level in {'none','note','warning','error'} else 'warning',
                      'message': {'text':str(finding.get('message','Recorded finding'))[:2000]},
                      'properties': {'octaneEvidenceId': identifier, 'octaneRevision': evidence['revision'],
                                     'octaneVerificationStatus': evidence['verification_status'], 'octaneStale': bool(evidence['stale'])}}
            path = _source(finding.get('path'), root)
            if path is not None:
                location = {'artifactLocation': {'uri':quote(path, safe='/'), 'uriBaseId':'%SRCROOT%'}}
                line = finding.get('line')
                if type(line) is int and line > 0:
                    location['region'] = {'startLine': line}
                result['locations'] = [{'physicalLocation':location}]
            results.append(result)
    return {'version':'2.1.0', '$schema':'https://json.schemastore.org/sarif-2.1.0.json',
            'runs':[{'tool':{'driver':{'name':'Octane', 'informationUri':'https://github.com/tempothrone-star/Octane',
                                     'rules':list(rules.values())}}, 'results':results,
                     'properties':{'octaneProjectId':project_id, 'truncated':truncated}}]}
