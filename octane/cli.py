"""Command-line entry point for local project discovery."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import ProjectIntelligence
from .exploration import ExplorationProfile, PythonAdapter, Tentacle


def main() -> None:
    parser = argparse.ArgumentParser(prog="octane", description="Evidence-driven project intelligence")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="register a project in a SQLite PIL database")
    init.add_argument("root", type=Path)
    init.add_argument("--database", type=Path, default=Path(".octane/pil.sqlite3"))
    init.add_argument("--name")
    scan = commands.add_parser("scan", help="scan Python files and update the project graph")
    scan.add_argument("project_id")
    scan.add_argument("--database", type=Path, default=Path(".octane/pil.sqlite3"))
    demo = commands.add_parser("demo", help="run a disposable database and source-analysis demonstration")
    demo.add_argument("--database", type=Path, help="retain demo database at this path")
    args = parser.parse_args()
    if args.command == "demo" and args.database is None:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="octane-pil-demo-") as temp:
            print(json.dumps(run_demo(ProjectIntelligence(Path(temp) / "pil.sqlite3")), indent=2))
        return
    intelligence = ProjectIntelligence(args.database if getattr(args, "database", None) else ".octane/pil.sqlite3")
    if args.command == "init":
        if not args.root.is_dir():
            parser.error(f"project root is not a directory: {args.root}")
        project = intelligence.create_project(args.name or args.root.name, args.root)
        print(json.dumps(project, indent=2))
    elif args.command == "scan":
        project = intelligence.get_project(args.project_id)
        if project is None:
            parser.error(f"unknown project: {args.project_id}")
        print(json.dumps(PythonAdapter().scan(intelligence, args.project_id, project["root"]), indent=2))
    else:
        print(json.dumps(run_demo(intelligence), indent=2))


def run_demo(intelligence: ProjectIntelligence) -> dict:
    """Exercise persisted three-agent handoff and source revision handling without host execution."""
    import tempfile
    from .workspace import WorkspaceManager

    with tempfile.TemporaryDirectory(prefix="octane-demo-") as temp:
        root = Path(__file__).resolve().parent.parent / "examples" / "sample_project"
        project = intelligence.create_project("sample-project", root, configuration={"analysis": "python-ast-demo"})
        explorer = intelligence.register_agent(project["id"], "explorer", "explorer", ["python-ast"],
                                              ["write_evidence", "read_evidence", "read_graph"])
        investigator = intelligence.register_agent(project["id"], "investigator", "investigator", ["investigate"],
                                                    ["investigate", "write_evidence", "checkpoint_investigation", "read_evidence", "read_graph", "read_investigation"])
        tester = intelligence.register_agent(project["id"], "tester", "tester", ["testing", "graph-verification"],
                                             ["write_evidence", "checkpoint_investigation", "verify_evidence", "investigate", "read_evidence", "read_graph", "read_investigation"])
        scan = PythonAdapter().scan(intelligence, project["id"], root)
        calls = Tentacle().explore(intelligence, project["id"], ExplorationProfile(
            "Trace rendered label calculation", ["symbol:pipeline.py:render_label"], ["calls"], depth_limit=2, node_budget=4), explorer["id"])
        imports = Tentacle().explore(intelligence, project["id"], ExplorationProfile(
            "Trace test module dependencies", ["file:example_expectations.py"], ["imports"], depth_limit=1, node_budget=2), explorer["id"])
        file_evidence = intelligence.search_evidence(project["id"], evidence_type="file_scan", requesting_agent_id=investigator["id"])
        tentacle_evidence = intelligence.search_evidence(project["id"], evidence_type="tentacle_exploration", requesting_agent_id=investigator["id"])
        investigation = intelligence.create_investigation(project["id"], "Inspect the example normalization and rendering path", investigator["id"],
                                                          {"relevant_files": ["pipeline.py"], "completed": [], "unresolved": []})
        hypothesis = intelligence.create_hypothesis(project["id"], "render_label calls normalize_label before formatting", investigation["id"], investigator["id"])
        render_finding = next((item for item in tentacle_evidence if item["payload"].get("node_key") == "symbol:pipeline.py:render_label"), None)
        if render_finding is None:
            raise RuntimeError("the call-tracing tentacle did not record the expected entry node")
        intelligence.attach_evidence(hypothesis["id"], render_finding["id"], "context", investigator["id"])
        checkpoint_payload = {"objective": investigation["objective"],
            "completed": ["Scanned Python symbols and traversed the static call/import graph"],
            "unresolved": ["The expected display-case behavior still needs isolated runtime verification"],
            "evidence_ids": [item["id"] for item in tentacle_evidence],
            "hypothesis_ids": [hypothesis["id"]], "next_action": "Check recorded call edges, then request isolated runtime tests",
            "project_revision": scan["revision"]}
        checkpoint = intelligence.pause_investigation(investigation["id"], investigator["id"], checkpoint_payload)
        resumed = intelligence.resume_investigation(investigation["id"], tester["id"])
        if not resumed["resumed"]:
            raise RuntimeError(f"demo investigation could not resume: {resumed['reason']}")
        checkpoint_evidence = [intelligence.retrieve_evidence(item_id, requesting_agent_id=tester["id"])
                              for item_id in resumed["context"]["evidence_ids"]]
        edges = intelligence.query_graph(project["id"], node_key="symbol:pipeline.py:render_label",
                                         requesting_agent_id=tester["id"])["edges"]
        symbols = {node["id"]: node["node_key"] for node in intelligence.query_graph(
            project["id"], requesting_agent_id=tester["id"])["nodes"]}
        verified_targets = {symbols[edge["target_id"]] for edge in edges if edge["kind"] == "calls" and edge["target_id"] in symbols}
        statically_verified = {"symbol:pipeline.py:normalize_label", "symbol:pipeline.py:calculate_length"} <= verified_targets
        if not statically_verified or not all(checkpoint_evidence):
            raise RuntimeError("Agent C could not verify the persisted call-graph evidence")
        verification = intelligence.submit_evidence(project["id"], "verification",
            {"check": "render_label outgoing calls exist in the persisted graph", "passed": statically_verified,
             "verified_targets": sorted(verified_targets), "method": "database graph assertion"},
            "Agent C verified the recorded static call relationships; this is not runtime behavior verification",
            tester["id"], scan["revision"], "pipeline.py", verification_status="observed")
        intelligence.mark_evidence_verified(render_finding["id"], verification["id"], tester["id"])
        intelligence.attach_evidence(hypothesis["id"], verification["id"], "supports", tester["id"])
        hypothesis = intelligence.update_hypothesis(hypothesis["id"], status="supported", agent_id=tester["id"])
        blocked_checkpoint = intelligence.checkpoint_investigation(investigation["id"], "blocked", {
            **checkpoint_payload, "completed": checkpoint_payload["completed"] + ["Agent C independently verified the recorded call graph"],
            "verification_evidence_ids": [verification["id"]],
            "unresolved": ["Whether lowercasing violates intended display behavior; project code tests need isolated runtime execution"],
            "next_action": "Run example expectations in an explicitly configured Docker image"}, tester["id"])
        resumed = intelligence.retrieve_investigation(investigation["id"], requesting_agent_id=tester["id"])
        workspace = WorkspaceManager(intelligence, Path(temp) / "workspaces").create_workspace(project["id"], "experimentation")
        revision_demo = _revision_change_demo(Path(temp), root)
        return {"project_id": project["id"], "agents": [explorer["id"], investigator["id"], tester["id"]],
                "scan": scan, "tentacles": {"calls": calls, "imports": imports}, "file_evidence_count": len(file_evidence),
                "tentacle_evidence_count": len(tentacle_evidence), "hypothesis": {"id": hypothesis["id"], "status": hypothesis["status"]},
                "checkpoint_id": checkpoint["id"], "final_checkpoint_id": blocked_checkpoint["id"],
                "resumed_by_agent": tester["id"], "resume_revision_compatible": resumed["revision_compatible"],
                "investigation_status": resumed["status"], "verification_evidence_id": verification["id"],
                "verified_claim": "static graph edges only; runtime behavior remains unresolved",
                "revision_change_demo": revision_demo, "workspace_mode": workspace["mode"],
                "runtime_execution": "not run: Docker image and explicit command are required; no host execution fallback"}


def _revision_change_demo(parent: Path, fixture: Path) -> dict:
    import shutil
    from .exploration import PythonAdapter

    project_root = parent / "revision-fixture"
    shutil.copytree(fixture, project_root)
    pil = ProjectIntelligence(parent / "revision-pil.sqlite3")
    project = pil.create_project("revision-change-fixture", project_root)
    adapter = PythonAdapter()
    before = adapter.scan(pil, project["id"], project_root)
    evidence = next(item for item in pil.search_evidence(project["id"], evidence_type="file_scan")
                    if item["source_path"] == "pipeline.py")
    memory = pil.add_memory(project["id"], "design-note", "The normalization behavior should be rechecked after source changes",
                            supporting_evidence=[evidence["id"]])
    source = project_root / "pipeline.py"
    original = source.read_text(encoding="utf-8")
    changed = original.replace("return value.strip().lower()", "return value.strip()")
    if changed == original:
        raise RuntimeError("revision demonstration could not find its expected source line")
    source.write_text(changed, encoding="utf-8")
    after = adapter.scan(pil, project["id"], project_root)
    history = pil.get_project_changes(project["id"], since_revision=before["revision"])
    return {"changed_files": after["changed_files"], "revision_changed": before["revision"] != after["revision"],
            "historical_evidence_preserved": pil.retrieve_evidence(evidence["id"]) is not None,
            "evidence_marked_stale": bool(pil.retrieve_evidence(evidence["id"])["stale"]),
            "supporting_memory_status": pil.retrieve_memory(memory["id"])["status"],
            "revision_history_entries_since_baseline": len(history["changes"])}


if __name__ == "__main__":
    main()
