"""Deterministic Python source discovery and budgeted exploration scheduling."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import ProjectIntelligence


@dataclass
class ExplorationProfile:
    objective: str
    starting_nodes: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=lambda: ["calls", "imports"])
    depth_limit: int = 2
    node_budget: int = 50
    time_budget_seconds: float = 10.0
    priority: float = 1.0


class PythonAdapter:
    """AST-based discovery; call resolution is intentionally conservative."""

    capabilities = ("parse", "symbols", "imports", "static_calls")

    def scan(self, intelligence: ProjectIntelligence, project_id: str, root: str | Path) -> dict[str, Any]:
        root = Path(root).resolve()
        project = intelligence.get_project(project_id)
        if not project:
            raise KeyError(project_id)
        if root != Path(project["root"]).resolve():
            raise ValueError("scan root must match the registered project root")
        scanner = intelligence.get_or_register_agent(project_id, "python-ast-adapter-v2", "language adapter",
                                                     ["python-ast", "symbols", "imports", "static-calls"], ["write_evidence", "update_graph"])
        files = sorted(p for p in root.rglob("*.py") if not self._excluded(p, root) and not p.is_symlink())
        content = {path: path.read_bytes() for path in files}
        hashes = {path.relative_to(root).as_posix(): hashlib.sha256(data).hexdigest() for path, data in content.items()}
        mtimes = {path.relative_to(root).as_posix(): path.stat().st_mtime_ns for path in files}
        revision = _revision(root, hashes)
        previous = {row["path"]: row["sha256"] for row in _files(intelligence, project_id) if not row["stale"]}
        changed = sorted(path for path, digest in hashes.items() if previous.get(path) != digest)
        removed = sorted(set(previous) - set(hashes))
        changed_set = set(changed) | set(removed)
        if changed_set:
            intelligence.invalidate_stale_knowledge(project_id, changed_set)

        module_nodes: dict[Path, str] = {}
        function_nodes: list[tuple[str, str, str, int, str]] = []
        imports: list[tuple[Path, str, int]] = []
        calls: list[tuple[str, str, int]] = []
        edge_count = 0
        for path in files:
            rel = path.relative_to(root).as_posix()
            file_id = intelligence.add_node(project_id, f"file:{rel}", "file", path.name, rel, language="python", revision=revision)
            module_nodes[path] = file_id
            try:
                tree = ast.parse(content[path], filename=rel)
            except (SyntaxError, UnicodeDecodeError) as exc:
                intelligence.submit_evidence(project_id, "parse_error", {"error": str(exc)}, "Python source could not be parsed", agent_id=scanner["id"], revision=revision, source_path=rel, verification_status="observed")
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    parent = _qualified_name(tree, node)
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    key = f"symbol:{rel}:{parent}"
                    symbol_id = intelligence.add_node(project_id, key, kind, parent, rel, node.lineno, "python", revision,
                                                     {"async": isinstance(node, ast.AsyncFunctionDef), "end_line": getattr(node, "end_lineno", None)})
                    intelligence.add_relationship(project_id, file_id, symbol_id, "defines", revision,
                                                  confidence=1.0, agent_id=scanner["id"])
                    edge_count += 1
                    function_nodes.append((parent, rel, symbol_id, node.lineno, kind))
                    if kind == "function":
                        for sub in ast.walk(node):
                            if isinstance(sub, ast.Call):
                                name = _call_name(sub.func)
                                if name:
                                    calls.append((f"{rel}:{parent}", name, sub.lineno))
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    imports.extend((path, name, node.lineno) for name in names)
            intelligence.submit_evidence(project_id, "file_scan", {"path": rel, "sha256": hashes[rel], "symbols": sum(1 for f in function_nodes if f[1] == rel)},
                                         "Python file hashed and inspected using AST", agent_id=scanner["id"], revision=revision, source_path=rel, verification_status="observed")

        by_module: dict[str, Path] = {}
        for path in files:
            rel = path.relative_to(root)
            module = ".".join(rel.with_suffix("").parts)
            if module.endswith(".__init__"):
                module = module[:-9]
            by_module[module] = path
        for source, imported, line in imports:
            target = by_module.get(imported)
            if target:
                intelligence.add_relationship(project_id, module_nodes[source], module_nodes[target], "imports", revision,
                                              {"line": line, "module": imported}, confidence=1.0, agent_id=scanner["id"])
                edge_count += 1
        by_simple_name: dict[str, list[tuple[str, str]]] = {}
        for qualified, rel, node_id, _, kind in function_nodes:
            if kind == "function":
                by_simple_name.setdefault(qualified.split(".")[-1], []).append((f"{rel}:{qualified}", node_id))
        for caller, callee_name, line in calls:
            caller_node = next((item[2] for item in function_nodes if f"{item[1]}:{item[0]}" == caller), None)
            candidates = by_simple_name.get(callee_name.split(".")[-1], [])
            if caller_node and len(candidates) == 1:
                intelligence.add_relationship(project_id, caller_node, candidates[0][1], "calls", revision,
                                              {"line": line, "resolution": "unique-name"}, confidence=0.8,
                                              agent_id=scanner["id"])
                edge_count += 1

        with intelligence._transaction() as db:
            for rel, digest in hashes.items():
                db.execute("INSERT INTO files(project_id,path,sha256,revision,stale,updated_at,mtime_ns) VALUES(?,?,?,?,0,datetime('now'),?) ON CONFLICT(project_id,path) DO UPDATE SET sha256=excluded.sha256,revision=excluded.revision,stale=0,updated_at=excluded.updated_at,mtime_ns=excluded.mtime_ns",
                           (project_id, rel, digest, revision, mtimes[rel]))
            for rel in removed:
                db.execute("UPDATE files SET stale=1 WHERE project_id=? AND path=?", (project_id, rel))
                db.execute("UPDATE graph_nodes SET stale=1 WHERE project_id=? AND path=?", (project_id, rel))
            known_languages = set(project["languages"])
            if files:
                known_languages.add("python")
            db.execute("UPDATE projects SET revision=?,languages_json=?,last_indexed_at=?,updated_at=datetime('now') WHERE id=?",
                       (revision, json.dumps(sorted(known_languages)), _utc_now(), project_id))
            change_details = {"changed": changed, "removed": removed,
                              "files": {relative: {"before_sha256": previous.get(relative),
                                                   "after_sha256": hashes.get(relative),
                                                   "mtime_ns": mtimes.get(relative)}
                                        for relative in sorted(changed_set)}}
            db.execute("INSERT OR IGNORE INTO project_revisions VALUES(?,?,?,?,?)",
                       (f"REV-{hashlib.sha256((project_id+revision).encode()).hexdigest()[:24]}", project_id, revision,
                        _utc_now(), json.dumps(change_details, sort_keys=True)))
            intelligence._audit(db, project_id, scanner["id"], "project.index", "project", project_id,
                                {"revision": revision, "files_scanned": len(files), "changed": changed, "removed": removed})
        return {"project_id": project_id, "revision": revision, "files_scanned": len(files), "symbols_discovered": len(function_nodes),
                "relationships_discovered": edge_count, "changed_files": changed, "removed_files": removed,
                "unsupported": sorted(set(p.suffix for p in root.rglob("*") if p.is_file() and not self._excluded(p, root) and not p.is_symlink() and p.suffix and p.suffix != ".py"))}

    @staticmethod
    def _excluded(path: Path, root: Path) -> bool:
        return any(part in {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"} for part in path.relative_to(root).parts)


class ExplorationScheduler:
    """Configurable scoring of graph nodes, with deduplication and hard budgets."""

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = {"relevance": 1.0, "uncertainty": 1.0, "connectivity": 0.5, "information_gain": 0.5, "cost": 0.5}
        if weights:
            unknown = set(weights) - set(self.weights)
            if unknown:
                raise ValueError(f"unknown scheduler weights: {sorted(unknown)}")
            if any(not math.isfinite(float(value)) or float(value) < 0 for value in weights.values()):
                raise ValueError("scheduler weights must be finite and non-negative")
            self.weights.update({key: float(value) for key, value in weights.items()})

    def score(self, relevance: float, uncertainty: float, connectivity: float, information_gain: float, cost: float) -> float:
        values = (relevance, uncertainty, connectivity, information_gain, cost)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("scheduler factors must be finite and non-negative")
        w = self.weights
        return w["relevance"] * relevance + w["uncertainty"] * uncertainty + w["connectivity"] * connectivity + w["information_gain"] * information_gain - w["cost"] * cost

    def prioritize(self, candidates: list[dict[str, Any]], explored: set[str] | None = None,
                   node_budget: int = 50, time_budget_seconds: float = 10.0) -> list[dict[str, Any]]:
        if node_budget < 0 or not math.isfinite(time_budget_seconds) or time_budget_seconds < 0:
            raise ValueError("budgets must be finite and non-negative")
        seen = explored or set()
        scored = []
        for candidate in candidates:
            key = candidate["node_key"]
            if key in seen:
                continue
            item = dict(candidate)
            item["score"] = self.score(*(float(candidate.get(k, 0.0)) for k in ("relevance", "uncertainty", "connectivity", "information_gain", "cost")))
            scored.append(item)
        scored.sort(key=lambda item: (-item["score"], item["node_key"]))
        result = []
        spent = 0.0
        for item in scored:
            cost = float(item.get("estimated_seconds", 0.0))
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("candidate cost must be finite and non-negative")
            if len(result) >= node_budget or spent + cost > time_budget_seconds:
                continue
            spent += cost
            result.append(item)
        return result


class Tentacle:
    """Lightweight graph worker that records bounded relationship discoveries."""

    _CAPABILITY_EDGES = {"calls": "calls", "imports": "imports"}

    def __init__(self, scheduler: ExplorationScheduler | None = None):
        self.scheduler = scheduler or ExplorationScheduler()

    def explore(self, intelligence: ProjectIntelligence, project_id: str, profile: ExplorationProfile,
                agent_id: str | None = None) -> dict[str, Any]:
        unsupported = set(profile.capabilities) - set(self._CAPABILITY_EDGES)
        if unsupported:
            raise ValueError(f"unsupported Python exploration capabilities: {sorted(unsupported)}")
        if (profile.depth_limit < 0 or profile.node_budget < 0
                or not math.isfinite(profile.time_budget_seconds) or profile.time_budget_seconds < 0
                or not math.isfinite(profile.priority) or profile.priority < 0):
            raise ValueError("exploration limits and priority must be finite and non-negative")
        started = time.monotonic()
        project = intelligence.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if agent_id is None:
            agent = intelligence.get_or_register_agent(project_id, "deterministic-tentacle-v2", "explorer",
                                                       ["calls", "imports"], ["write_evidence", "read_evidence", "read_graph"])
            agent_id = agent["id"]
        graph = intelligence.query_graph(project_id, requesting_agent_id=agent_id)
        nodes = {node["id"]: node for node in graph["nodes"]}
        by_key = {node["node_key"]: node for node in nodes.values()}
        if profile.starting_nodes:
            missing = set(profile.starting_nodes) - set(by_key)
            if missing:
                raise ValueError(f"unknown starting graph nodes: {sorted(missing)}")
            roots = [by_key[key] for key in profile.starting_nodes]
        else:
            roots = list(nodes.values())
        edge_kinds = {self._CAPABILITY_EDGES[capability] for capability in profile.capabilities}
        outgoing: dict[str, list[dict[str, Any]]] = {}
        for edge in graph["edges"]:
            if edge["kind"] in edge_kinds:
                outgoing.setdefault(edge["source_id"], []).append(edge)
        seen = set()
        for record in intelligence.search_evidence(project_id, evidence_type="tentacle_exploration", requesting_agent_id=agent_id):
            payload = record["payload"]
            if payload.get("capabilities") == sorted(profile.capabilities):
                seen.add(payload.get("node_key"))
        objective_terms = set(profile.objective.casefold().split())
        candidates = []
        for node in roots:
            if node["node_key"] in seen:
                continue
            connections = len(outgoing.get(node["id"], []))
            relevance = profile.priority * (1 + len(objective_terms & set(node["name"].casefold().split())))
            candidates.append({"node_key": node["node_key"], "relevance": relevance,
                               "uncertainty": 1.0, "connectivity": float(connections),
                               "information_gain": float(connections > 0), "cost": 1.0,
                               "estimated_seconds": 0.01})
        starts = self.scheduler.prioritize(candidates, node_budget=profile.node_budget,
                                           time_budget_seconds=profile.time_budget_seconds)
        queue = [(by_key[item["node_key"]], 0) for item in starts]
        if profile.node_budget == 0:
            exhausted = "node_budget"
        elif profile.time_budget_seconds == 0:
            exhausted = "time_budget"
        else:
            exhausted = None
        visited: set[str] = set()
        discoveries = []
        while queue:
            if len(visited) >= profile.node_budget:
                exhausted = "node_budget"
                break
            if time.monotonic() - started >= profile.time_budget_seconds:
                exhausted = "time_budget"
                break
            node, depth = queue.pop(0)
            if node["node_key"] in visited or node["node_key"] in seen:
                continue
            visited.add(node["node_key"])
            relationships = []
            for edge in outgoing.get(node["id"], []):
                target = nodes.get(edge["target_id"])
                if target:
                    relationships.append({"type": edge["kind"], "target": target["node_key"], "line": json.loads(edge["metadata_json"]).get("line")})
                    if depth < profile.depth_limit and target["node_key"] not in visited and target["node_key"] not in seen:
                        queue.append((target, depth + 1))
            payload = {"node_key": node["node_key"], "kind": node["kind"], "capabilities": sorted(profile.capabilities),
                       "depth": depth, "relationships": relationships, "revision": project["revision"]}
            evidence = intelligence.submit_evidence(project_id, "tentacle_exploration", payload,
                                                    f"Bounded {', '.join(sorted(profile.capabilities))} graph exploration",
                                                    agent_id=agent_id, revision=project["revision"], source_path=node["path"],
                                                    source_line=node["line"], verification_status="observed")
            discoveries.append({"node_key": node["node_key"], "evidence_id": evidence["id"], "relationships": len(relationships)})
        if queue and exhausted is None:
            exhausted = "depth_limit_or_no_more_nodes"
        elif not discoveries and exhausted is None:
            exhausted = "no_candidates"
        elif exhausted is None:
            exhausted = "completed"
        return {"investigation_objective": profile.objective, "explored": discoveries,
                "nodes_examined": len(visited), "remaining_queue": len(queue), "stop_reason": exhausted}


def _revision(root: Path, hashes: dict[str, str]) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=True)
        head = result.stdout.strip()
        status = subprocess.run(["git", "-C", str(root), "-c", "core.fsmonitor=false", "status", "--porcelain"],
                                capture_output=True, text=True, timeout=2, check=True).stdout
        tree = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:12]
        working = hashlib.sha256(status.encode()).hexdigest()[:12] if status else "clean"
        return f"{head}+{working}+{tree}"
    except (OSError, subprocess.SubprocessError):
        return "tree-" + hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:20]


def _files(intelligence: ProjectIntelligence, project_id: str) -> list[dict[str, Any]]:
    with intelligence._session() as db:
        return [dict(row) for row in db.execute("SELECT * FROM files WHERE project_id=?", (project_id,))]


def _call_name(node: ast.expr) -> str | None:
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr); node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
        return ".".join(reversed(names))
    return None


def _qualified_name(tree: ast.AST, target: ast.AST) -> str:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    parts = []
    current = target
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(current.name)
    parts.reverse()
    parts.append(getattr(target, "name", "<unknown>"))
    return ".".join(parts)


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
