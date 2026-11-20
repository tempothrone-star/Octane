"""Keep optional diagnosis and operator validation CLI paths small and explicit."""
from __future__ import annotations

import json
from pathlib import Path
from .diagnostics import KINDS, diagnose
from .workspace import WorkspaceManager


def add_commands(commands):
    analysis = commands.add_parser('diagnose', help='record trace, spectrum, analyzer or pattern diagnosis')
    analysis.add_argument('project_id')
    analysis.add_argument('--kind', required=True, choices=KINDS)
    analysis.add_argument('--evidence', default='')
    analysis.add_argument('--options-json', default='{}')
    hidden = commands.add_parser('holdout', help='operator-only final check with a private policy')
    hidden.add_argument('project_id')
    hidden.add_argument('workspace_id')
    hidden.add_argument('candidate_run_id')
    hidden.add_argument('--policy', type=Path, required=True)
    performance = commands.add_parser('benchmark', help='compare independent paired recorded timings')
    performance.add_argument('project_id')
    performance.add_argument('--before-runs', required=True)
    performance.add_argument('--after-runs', required=True)
    performance.add_argument('--preservation-pairs-json', required=True)
    performance.add_argument('--minimum-improvement', type=float, default=0.05)
    for parser in (analysis, hidden, performance):
        parser.add_argument('--database', type=Path, required=True)
    for parser in (hidden, performance):
        parser.add_argument('--workspace-root', type=Path, required=True)
        parser.add_argument('--container-engine', choices=('docker', 'podman'), default='docker')
        parser.add_argument('--container-runtime', choices=('runsc',))


def run_command(intelligence, args):
    project = intelligence.get_project(args.project_id)
    if project is None:
        raise KeyError(args.project_id)
    actor = intelligence.get_or_register_agent(args.project_id, 'diagnostic-cli', 'analysis', [],
                                                ['write_evidence', 'read_evidence', 'investigate'])
    if args.command == 'diagnose':
        return diagnose(intelligence, args.project_id, actor['id'], args.kind,
                        args.evidence.split(',') if args.evidence else [], json.loads(args.options_json))
    manager = WorkspaceManager(intelligence, args.workspace_root, container_engine=args.container_engine,
                               container_runtime=args.container_runtime)
    if args.command == 'holdout':
        from .holdout import validate_holdout
        if manager._get(args.workspace_id)['project_id'] != args.project_id:
            raise ValueError('holdout workspace must belong to the selected project')
        return validate_holdout(manager, args.workspace_id, args.candidate_run_id, args.policy,
                                [Path(project['root']), args.workspace_root])
    from .performance import compare_recorded
    return compare_recorded(manager, args.project_id, args.before_runs.split(','), args.after_runs.split(','),
                            json.loads(args.preservation_pairs_json), actor['id'], args.minimum_improvement)
