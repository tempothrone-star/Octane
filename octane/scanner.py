"""Atomic, bounded multilingual indexing into the native PIL database."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .analysis import analyzer
from .db import ProjectIntelligence, _id, _now
from .python_calls import analyze_python
from .syntax_calls import analyze_syntax_calls


class ProjectScanner:
    def scan(self, intelligence: ProjectIntelligence, project_id: str, *, parser="auto",
             max_files=100000, max_file_bytes=5 * 1024 * 1024, max_total_bytes=100 * 1024 * 1024,
             only_if_changed=False):
        project = intelligence.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        root = Path(project["root"]).resolve(strict=True)
        source, polygraph = analyzer()
        inventory = source.source_inventory(root, max_files, max_file_bytes)
        if inventory["truncated"] or inventory["skipped"].get("over_file_size_limit") or inventory["skipped"].get("unreadable"):
            raise ValueError("scan is incomplete under the selected limits; no index was updated")
        if type(max_total_bytes) is not int or not 1 <= max_total_bytes <= 1024 ** 3:
            raise ValueError("maxTotalBytes must be an integer from 1 to 1073741824")
        if inventory["source_bytes"] > max_total_bytes:
            raise ValueError("scan exceeds maxTotalBytes; no index was updated")
        contents, hashes = {}, {}
        for item in inventory["files"]:
            path = root / item["path"]
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("source paths changed during indexing; retry the scan")
            with path.open("rb") as stream:
                content = stream.read(max_file_bytes + 1)
            if len(content) != item["bytes"] or len(content) > max_file_bytes:
                raise ValueError("source size changed during indexing; retry the scan")
            contents[item["path"]] = content
            hashes[item["path"]] = hashlib.sha256(content).hexdigest()
        # Go module metadata affects import resolution but has no source extension.
        go_mod = root / "go.mod"
        if not go_mod.is_symlink() and go_mod.is_file():
            with go_mod.open("rb") as stream:
                module_bytes = stream.read(min(max_file_bytes, 1024 ** 2) + 1)
            if len(module_bytes) > min(max_file_bytes, 1024 ** 2) or inventory["source_bytes"] + len(module_bytes) > max_total_bytes:
                raise ValueError("Go module metadata exceeds scan limits; no index was updated")
            hashes["go.mod"] = hashlib.sha256(module_bytes).hexdigest()
            contents["go.mod"] = module_bytes
        revision = "tree-" + hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        if only_if_changed and revision == project["revision"]:
            return {"project_id": project_id, "revision": revision, "changed_files": [], "removed_files": []}
        graph = polygraph.build_dependency_graph(root, max_files, max_file_bytes, max_total_bytes, parser,
                                                snapshot=(inventory, contents))
        if graph["truncated"] or graph["files_indexed"] != len(inventory["files"]):
            raise ValueError("dependency scan is incomplete; no index was updated")
        python_sources = {item["path"]: contents[item["path"]] for item in inventory["files"]
                          if item["language"] == "python" and item["path"].endswith((".py", ".pyi"))}
        symbols, calls, parse_errors = analyze_python(python_sources, revision)
        syntax_symbols, syntax_calls, syntax_coverage = analyze_syntax_calls(
            polygraph.ast_backend, inventory["files"], contents, revision, graph["edges"], enabled=parser != "heuristic",
        )
        symbols += syntax_symbols
        calls += syntax_calls
        # The parser and hasher must have examined the same source bytes.
        final_inventory = source.source_inventory(root, max_files, max_file_bytes)
        if (final_inventory["truncated"] or final_inventory["skipped"].get("unreadable")
                or final_inventory["skipped"].get("over_file_size_limit") or final_inventory["files"] != inventory["files"]):
            raise ValueError("source file inventory changed during indexing; retry the scan")
        if (not go_mod.is_symlink() and go_mod.is_file()) != ("go.mod" in hashes):
            raise ValueError("Go module metadata changed during indexing; retry the scan")
        for relative, digest in hashes.items():
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("source paths changed during indexing; retry the scan")
            with path.open("rb") as stream:
                data = stream.read(max_file_bytes + 1)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("source changed during indexing; retry the scan")
        # The PIL stores one symbol per key and one edge per typed pair. Merge
        # repeated discoveries before writing so counts and history match the index.
        symbols = list({symbol["node_key"]: symbol for symbol in symbols}.values())
        with intelligence._transaction() as db:
            current = intelligence.get_project(project_id)
            if current["revision"] != project["revision"]:
                raise RuntimeError("another scan changed the project; retry against the current revision")
            scanner = intelligence.get_or_register_agent(project_id, "native-source-scanner", "language adapter",
                                                         ["multilingual-imports", "source-symbols"], ["write_evidence", "update_graph"])
            previous = {row["path"]: row["sha256"] for row in db.execute(
                "SELECT path,sha256 FROM files WHERE project_id=? AND stale=0", (project_id,))}
            changed = sorted(path for path in hashes if previous.get(path) != hashes[path])
            removed = sorted(set(previous) - set(hashes))
            affected = changed + removed
            intelligence.invalidate_stale_knowledge(project_id, affected)
            if revision != current["revision"]:
                intelligence.set_revision(project_id, revision)
            evidence_by_path = {row["source_path"]: row["id"] for row in db.execute("""
                SELECT source_path,id FROM (
                    SELECT source_path,id,ROW_NUMBER() OVER(PARTITION BY source_path ORDER BY collected_at DESC,id) AS rank
                    FROM evidence WHERE project_id=? AND evidence_type='file_scan' AND stale=0
                ) WHERE rank=1
            """, (project_id,))}
            for rel in changed:
                ev = intelligence.submit_evidence(project_id, "file_scan", {"sha256": hashes[rel]},
                                                   "Source bytes hashed and statically indexed", scanner["id"], revision, rel)
                evidence_by_path[rel] = ev["id"]
            nodes = [{"node_key": "file:" + item["path"], "kind": "file", "name": Path(item["path"]).name,
                      "path": item["path"], "language": item["language"], "revision": revision,
                      "metadata": {"sha256": hashes[item["path"]], "owner": "native-scanner"}}
                     for item in inventory["files"]] + symbols
            edges = [{"source": "file:" + edge["source"], "target": "file:" + edge["target"], "kind": "imports",
                      "confidence": 0.7, "metadata": {"line": edge["line"], "basis": edge["basis"], "cross_language": edge["cross_language"]},
                      "evidence_id": evidence_by_path.get(edge["source"])} for edge in graph["edges"]]
            edges += [{"source": "file:" + node["path"], "target": node["node_key"], "kind": "defines",
                       "confidence": 1.0, "evidence_id": evidence_by_path.get(node["path"])} for node in symbols]
            symbol_paths = {node["node_key"]: node["path"] for node in symbols}
            for call in calls:
                call["evidence_id"] = evidence_by_path.get(symbol_paths[call["source"]])
            edges += calls
            edges_by_key = {}
            for edge in edges:
                key = (edge["source"], edge["target"], edge["kind"])
                previous_edge = edges_by_key.get(key)
                if previous_edge:
                    metadata = previous_edge["metadata"]
                    lines = metadata.get("lines", [metadata["line"]])
                    metadata["lines"] = sorted(set(lines + [edge["metadata"]["line"]]))
                else:
                    edges_by_key[key] = edge
            edges = list(edges_by_key.values())
            # Reconcile only obsolete scanner-owned structure; an unchanged scan
            # must not manufacture edge-history entries or delete agent findings.
            keys = {node["node_key"] for node in nodes}
            for row in db.execute("SELECT id,node_key FROM graph_nodes WHERE project_id=? AND metadata_json LIKE ?",
                                  (project_id, '%"owner": "native-scanner"%')).fetchall():
                if row["node_key"] not in keys:
                    db.execute("UPDATE graph_nodes SET stale=1 WHERE id=?", (row["id"],))
            for edge in db.execute("SELECT e.*,s.node_key AS source_key,t.node_key AS target_key FROM graph_edges e JOIN graph_nodes s ON s.id=e.source_id JOIN graph_nodes t ON t.id=e.target_id WHERE e.project_id=? AND e.provenance_agent_id=? AND e.stale=0",
                                   (project_id, scanner["id"])).fetchall():
                if (edge["source_key"], edge["target_key"], edge["kind"]) not in edges_by_key:
                    intelligence._archive_graph_edge(db, edge)
                    db.execute("UPDATE graph_edges SET stale=1 WHERE id=?", (edge["id"],))
            intelligence.update_graph(project_id, nodes, edges, scanner["id"])
            for error in parse_errors:
                if error["path"] in changed:
                    intelligence.submit_evidence(project_id, "parse_error", error, "Python AST parsing failed", scanner["id"], revision, error["path"])
            for rel, digest in hashes.items():
                db.execute("INSERT INTO files(project_id,path,sha256,revision,stale,updated_at) VALUES(?,?,?,?,0,?) ON CONFLICT(project_id,path) DO UPDATE SET sha256=excluded.sha256,revision=excluded.revision,stale=0,updated_at=excluded.updated_at",
                           (project_id, rel, digest, revision, _now()))
            for rel in removed:
                db.execute("UPDATE files SET stale=1 WHERE project_id=? AND path=?", (project_id, rel))
            db.execute("UPDATE projects SET languages_json=?,last_indexed_at=? WHERE id=?",
                       (json.dumps(sorted(inventory["languages"])), _now(), project_id))
            details = {"changed": changed, "removed": removed,
                       "files": {rel: {"before_sha256": previous.get(rel), "after_sha256": hashes.get(rel)} for rel in affected}}
            if revision != current["revision"]:
                db.execute("INSERT INTO project_revisions VALUES(?,?,?,?,?)",
                           (_id("REV"), project_id, revision, _now(), json.dumps(details)))
            intelligence._audit(db, project_id, scanner["id"], "project.index", "project", project_id,
                                {"revision": revision, "changed": changed, "removed": removed})
        return {"project_id": project_id, "revision": revision, "files_scanned": len(hashes),
                "symbols_discovered": len(symbols), "relationships_discovered": len(edges),
                "changed_files": changed, "removed_files": removed, "languages": inventory["languages"],
                "parser_coverage": {**graph["parser_coverage"], "syntax_calls": syntax_coverage}, "parse_errors": parse_errors,
                "unsupported": sorted(inventory["unsupported_extensions"]),
                "capabilities": {"python": ["symbols", "lexical_and_import_call_candidates", "imports"],
                                 "tree_sitter_call_candidates": sorted(language for language, counts in syntax_coverage.items() if counts.get("symbols")),
                                 "other_languages": ["file_hashes", "best_effort_imports"],
                                 "unsupported": ["semantic_types", "complete_data_flow", "runtime_reachability"]}}
