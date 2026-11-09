"""Command-line interface for the Octance skill."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import CLAIM_KINDS, CLAIM_STATUSES, EVIDENCE_TYPES, Ledger, OctanceError, Project, canonical, diagnose_project, discover_root, init_project, resolve_project_root

EXIT_PERMISSION = 3
EXIT_CONFLICT = 4
EXIT_STALE = 5
EXIT_EXECUTION_DENIED = 6


class OctanceArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise OctanceError(message, 2, "invalid_input")


def _common(parser: argparse.ArgumentParser, include_agent: bool = True) -> None:
    if include_agent:
        parser.add_argument("--agent", help="Octance agent name or AG-<name>; defaults to OCTANCE_AGENT or owner")
    parser.add_argument("--human", action="store_true", help="print a readable response instead of JSON")
    parser.add_argument("--format", choices=("json", "compact"), default="json", help="JSON formatting")


def build_parser() -> argparse.ArgumentParser:
    parser = OctanceArgumentParser(prog="python3 scripts/octance.py", description="Persistent, evidence-backed project intelligence for OpenClaw agents")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=OctanceArgumentParser)

    init = commands.add_parser("init", help="initialize this project-local ledger")
    init.add_argument("--project-root", help="project root; defaults to Git top-level or current directory")
    init.add_argument("--update-gitignore", action="store_true", help="explicitly add .octance/ to .gitignore")
    init.add_argument("--trust-tier", choices=("low", "standard"), default="standard")
    _common(init, include_agent=False)

    for name, help_text in (("status", "show compact project health"), ("doctor", "check local setup and integrity"),
                            ("verify-log", "verify the append-only event hash chain")):
        command = commands.add_parser(name, help=help_text)
        _common(command)
    rebuild = commands.add_parser("rebuild", help="compare materialized tables with event-log replay")
    rebuild.add_argument("--check", action="store_true", required=True)
    _common(rebuild)

    agent = commands.add_parser("agent", help="manage project-local agent identities")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True, parser_class=OctanceArgumentParser)
    register = agent_commands.add_parser("register", help="register an agent with a fixed least-privilege role")
    register.add_argument("--name", required=True)
    register.add_argument("--role", required=True, choices=("explorer", "investigator", "tester", "documenter"))
    register.add_argument("--trust-tier", choices=("low", "standard"))
    _common(register)
    show = agent_commands.add_parser("show", help="show a project-local agent")
    show.add_argument("name")
    _common(show)
    whoami = agent_commands.add_parser("whoami", help="show the current Octance identity")
    _common(whoami)

    evidence = commands.add_parser("evidence", help="record and retrieve immutable observations")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True, parser_class=OctanceArgumentParser)
    add = evidence_commands.add_parser("add", help="append an immutable evidence record")
    add.add_argument("--type", required=True, choices=sorted(EVIDENCE_TYPES))
    add.add_argument("--description", required=True)
    add.add_argument("--payload", default="{}", help="JSON object containing structured observation data")
    add.add_argument("--file")
    add.add_argument("--symbol")
    add.add_argument("--line-range")
    add.add_argument("--method", default="agent_reported")
    add.add_argument("--related", action="append", default=[])
    add.add_argument("--idempotency-key")
    _common(add)
    get = evidence_commands.add_parser("get", help="retrieve an evidence record")
    get.add_argument("evidence_id")
    _common(get)
    search = evidence_commands.add_parser("search", help="search project evidence")
    search.add_argument("--query", default="")
    search.add_argument("--type", choices=sorted(EVIDENCE_TYPES))
    search.add_argument("--limit", type=int, default=50)
    _common(search)
    link = evidence_commands.add_parser("link", help="append an evidence relationship")
    link.add_argument("evidence_id")
    link.add_argument("related_id")
    link.add_argument("--relation", choices=("supports", "contradicts", "context", "supersedes", "verifies", "related_to"), default="related_to")
    link.add_argument("--idempotency-key")
    _common(link)

    claim = commands.add_parser("claim", help="record beliefs separately from observations")
    claim_commands = claim.add_subparsers(dest="claim_command", required=True, parser_class=OctanceArgumentParser)
    add_claim = claim_commands.add_parser("add", help="append an evidence-linked claim")
    add_claim.add_argument("--kind", required=True, choices=sorted(CLAIM_KINDS))
    add_claim.add_argument("--text", required=True)
    add_claim.add_argument("--evidence", action="append", default=[], help="supporting evidence ID; repeat as needed")
    add_claim.add_argument("--contradicting-evidence", action="append", default=[])
    add_claim.add_argument("--depends-on", action="append", default=[])
    add_claim.add_argument("--idempotency-key")
    _common(add_claim)
    get_claim = claim_commands.add_parser("get", help="retrieve a claim")
    get_claim.add_argument("claim_id")
    _common(get_claim)
    search_claim = claim_commands.add_parser("search", help="search claims by text or status")
    search_claim.add_argument("--query", default="")
    search_claim.add_argument("--status", choices=sorted(CLAIM_STATUSES))
    search_claim.add_argument("--limit", type=int, default=100)
    _common(search_claim)
    link_claim = claim_commands.add_parser("link", help="append a claim relationship")
    link_claim.add_argument("claim_id")
    link_claim.add_argument("related_id")
    link_claim.add_argument("--relation", choices=("supports", "contradicts", "depends_on", "conflicts_with"), required=True)
    link_claim.add_argument("--idempotency-key")
    _common(link_claim)

    brief = commands.add_parser("brief", help="retrieve compact, ranked project context")
    brief.add_argument("--task", required=True)
    brief.add_argument("--files", default="")
    brief.add_argument("--symbols", default="")
    brief.add_argument("--budget-tokens", type=int, default=2000)
    _common(brief)
    return parser


def _response(data: Any = None, warnings: Optional[List[str]] = None, next_steps: Optional[List[str]] = None,
              error: Optional[str] = None, message: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": error is None, "data": data, "warnings": warnings or [], "next": next_steps or []}
    if error:
        result["error"] = error
        result["message"] = message or error
    return result


def _human(value: Any) -> str:
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(str(key) + ":\n" + _human(item).replace("\n", "\n  "))
            else:
                lines.append(str(key) + ": " + str(item))
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join("- " + _human(item).replace("\n", "\n  ") for item in value) if value else "(none)"
    return str(value)


def _emit(result: Dict[str, Any], args: argparse.Namespace) -> None:
    if getattr(args, "human", False):
        if result["ok"]:
            print(_human(result["data"]))
            for warning in result["warnings"]:
                print("Warning: " + warning, file=sys.stderr)
        else:
            print("Error (%s): %s" % (result.get("error"), result.get("message")), file=sys.stderr)
            for step in result["next"]:
                print("Next: " + step, file=sys.stderr)
    else:
        compact = getattr(args, "format", "json") == "compact"
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":") if compact else None,
                         indent=None if compact else 2))


def _load_payload(text: str) -> Dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as error:
        raise OctanceError("--payload must be valid JSON: " + str(error), 2, "invalid_input")
    if not isinstance(value, dict):
        raise OctanceError("--payload must decode to a JSON object", 2, "invalid_input")
    return value


def _run(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "init":
        root = resolve_project_root(explicit=args.project_root)
        result = init_project(root, args.update_gitignore, args.trust_tier)
        return _response(result, result.pop("warnings", []), ["Run `python3 scripts/octance.py status` to inspect the new ledger"])
    root = discover_root()
    if args.command == "doctor":
        result = diagnose_project(root)
        return _response(result, result.get("warnings", []))
    project = Project(root)
    agent_id = project.agent_id(getattr(args, "agent", None))
    actor = project.ledger.get_agent(agent_id, project.project_id)
    if actor is None:
        raise OctanceError("agent identity is not registered", 3, "permission_denied")
    connection = project.ledger.connect()
    try:
        if args.command in {"status", "verify-log", "rebuild"}:
            project.ledger._require(connection, actor, "status")
    finally:
        connection.close()

    if args.command == "status":
        return _response(project.status())
    if args.command == "verify-log":
        result = project.ledger.verify_log(project.project_id)
        if not result["valid"]:
            raise OctanceError("event log verification failed at %s: %s" % (result.get("first_bad_seq"), result["reason"]), 1, "integrity_error")
        return _response(result)
    if args.command == "rebuild":
        result = project.ledger.rebuild_check(project.project_id)
        if not result["matches"]:
            raise OctanceError("materialized tables do not match event-log replay: " + ", ".join(result.get("mismatches", [])), 1, "rebuild_mismatch")
        return _response(result)
    if args.command == "agent":
        if args.agent_command == "register":
            return _response(project.register_agent(args.name, args.role, args.trust_tier, agent_id))
        if args.agent_command == "whoami":
            return _response(actor)
        target = args.name if args.name.startswith("AG-") else "AG-" + args.name
        result = project.ledger.get_agent(target, project.project_id)
        if result is None:
            raise OctanceError("unknown agent: " + target, 2, "not_found")
        connection = project.ledger.connect()
        try:
            project.ledger._require(connection, actor, "status")
        finally:
            connection.close()
        return _response(result)
    if args.command == "evidence":
        if args.evidence_command == "add":
            return _response(project.add_evidence(agent_id, args.type, args.description, _load_payload(args.payload),
                                                  args.file, args.symbol, args.line_range, args.method,
                                                  args.related, args.idempotency_key),
                             next_steps=["Link the observation to a relevant claim only after classifying what it supports"])
        if args.evidence_command == "get":
            connection = project.ledger.connect()
            try:
                project.ledger._require(connection, actor, "evidence.read")
            finally:
                connection.close()
            result = project.get_evidence(args.evidence_id)
            if result is None:
                raise OctanceError("evidence not found in this project", 2, "not_found")
            return _response(result)
        if args.evidence_command == "search":
            connection = project.ledger.connect()
            try:
                project.ledger._require(connection, actor, "evidence.search")
            finally:
                connection.close()
            return _response(project.search_evidence(args.query, args.type, args.limit))
        result = project.link_evidence(agent_id, args.evidence_id, args.related_id, args.relation, args.idempotency_key)
        return _response(result)
    if args.command == "claim":
        if args.claim_command == "add":
            return _response(project.add_claim(agent_id, args.kind, args.text, args.evidence,
                                               args.contradicting_evidence, args.depends_on, args.idempotency_key),
                             next_steps=["Use only supported evidence; a claim is not a verified result"])
        if args.claim_command == "get":
            connection = project.ledger.connect()
            try:
                project.ledger._require(connection, actor, "claim.read")
            finally:
                connection.close()
            result = project.get_claim(args.claim_id)
            if result is None:
                raise OctanceError("claim not found in this project", 2, "not_found")
            return _response(result)
        if args.claim_command == "search":
            connection = project.ledger.connect()
            try:
                project.ledger._require(connection, actor, "claim.search")
            finally:
                connection.close()
            return _response(project.search_claims(args.query, args.status, args.limit))
        return _response(project.link_claim(agent_id, args.claim_id, args.related_id, args.relation, args.idempotency_key))
    if args.command == "brief":
        connection = project.ledger.connect()
        try:
            project.ledger._require(connection, actor, "brief")
        finally:
            connection.close()
        data = project.brief(args.task, [x for x in args.files.split(",") if x],
                             [x for x in args.symbols.split(",") if x], args.budget_tokens)
        return _response(data, next_steps=["Record new observations as evidence before adding claims"])
    raise OctanceError("unsupported command", 2, "invalid_input")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        result = _run(args)
        _emit(result, args)
        return 0
    except OctanceError as error:
        args = locals().get("args", argparse.Namespace(human=False, format="json"))
        result = _response(error=str(error.kind), message=str(error), next_steps=[])
        _emit(result, args)
        return error.code
    except sqlite3.OperationalError as error:
        message = str(error)
        kind = "database_locked" if "locked" in message.lower() or "busy" in message.lower() else "database_error"
        code = 1
        args = locals().get("args", argparse.Namespace(human=False, format="json"))
        _emit(_response(error=kind, message=message, next_steps=[]), args)
        return code
    except (sqlite3.DatabaseError, OSError, ValueError) as error:
        args = locals().get("args", argparse.Namespace(human=False, format="json"))
        _emit(_response(error="runtime_error", message=str(error), next_steps=[]), args)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
