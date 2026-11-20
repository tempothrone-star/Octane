"""Graph persistence and bounded graph queries using the shared transaction API."""
from __future__ import annotations
import json
from typing import Any
from .call_plot import call_plot
from .db_support import _id, _project_relative_path


class GraphStore:
    def add_node(self, project_id: str, node_key: str, kind: str, name: str, path: str | None = None,
                 line: int | None = None, language: str | None = None, revision: str | None = None,
                 metadata: dict[str, Any] | None = None) -> str:
        path = _project_relative_path(path)
        if line is not None and line < 1:
            raise ValueError("line must be a positive one-based line number")
        node_id = _id("NOD")
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            revision = revision or project["revision"]
            existing = db.execute("SELECT id FROM graph_nodes WHERE project_id=? AND node_key=?", (project_id, node_key)).fetchone()
            if existing:
                node_id = existing["id"]
                db.execute("UPDATE graph_nodes SET kind=?,name=?,path=?,line=?,language=?,revision=?,stale=0,metadata_json=? WHERE id=?",
                           (kind, name, path, line, language, revision, json.dumps(metadata or {}, sort_keys=True), node_id))
            else:
                db.execute("INSERT INTO graph_nodes VALUES(?,?,?,?,?,?,?,?,?,0,?)", (node_id, project_id, node_key, kind, name, path, line, language, revision, json.dumps(metadata or {}, sort_keys=True)))
            return node_id

    def add_relationship(self, project_id: str, source_id: str, target_id: str, kind: str,
                         revision: str | None = None, metadata: dict[str, Any] | None = None,
                         confidence: float = 1.0, agent_id: str | None = None,
                         evidence_id: str | None = None) -> str:
        if not 0 <= confidence <= 1:
            raise ValueError("relationship confidence must be between 0 and 1")
        edge_id = _id("EDG")
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            nodes = db.execute("SELECT id,project_id FROM graph_nodes WHERE id IN (?,?)", (source_id, target_id)).fetchall()
            expected_endpoints = 1 if source_id == target_id else 2
            if len(nodes) != expected_endpoints or any(node["project_id"] != project_id for node in nodes):
                raise ValueError("relationships must connect existing nodes in the same project")
            if agent_id:
                self._require_permission(db, project_id, agent_id, "update_graph")
            if evidence_id:
                evidence = db.execute("SELECT project_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
                if evidence is None or evidence["project_id"] != project_id:
                    raise ValueError("relationship evidence must belong to the same project")
            revision = revision or project["revision"]
            old = db.execute("SELECT * FROM graph_edges WHERE project_id=? AND source_id=? AND target_id=? AND kind=?", (project_id, source_id, target_id, kind)).fetchone()
            if old:
                edge_id = old["id"]
                next_metadata = json.dumps(metadata or {}, sort_keys=True)
                next_agent = agent_id or old["provenance_agent_id"]
                next_evidence = evidence_id or old["evidence_id"]
                if (old["revision"], old["stale"], old["metadata_json"], old["confidence"], old["provenance_agent_id"], old["evidence_id"]) != (revision, 0, next_metadata, confidence, next_agent, next_evidence):
                    self._archive_graph_edge(db, old)
                db.execute("UPDATE graph_edges SET revision=?,stale=0,metadata_json=?,confidence=?,provenance_agent_id=?,evidence_id=? WHERE id=?",
                           (revision, next_metadata, confidence, next_agent, next_evidence, edge_id))
            else:
                db.execute("INSERT INTO graph_edges(id,project_id,source_id,target_id,kind,revision,stale,metadata_json,confidence,provenance_agent_id,evidence_id) VALUES(?,?,?,?,?,?,0,?,?,?,?)",
                           (edge_id, project_id, source_id, target_id, kind, revision, json.dumps(metadata or {}, sort_keys=True), confidence, agent_id, evidence_id))
            self._audit(db, project_id, agent_id, "graph.relationship", "edge", edge_id,
                        {"kind": kind, "source": source_id, "target": target_id, "confidence": confidence, "evidence_id": evidence_id})
            return edge_id

    def update_graph(self, project_id: str, nodes=(), relationships=(), agent_id: str | None = None) -> dict[str, list[str]]:
        """Atomically upsert project nodes and typed, evidence-linked relationships."""
        with self._transaction() as db:
            project = db.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(project_id)
            if agent_id:
                self._require_permission(db, project_id, agent_id, "update_graph")
            node_ids: dict[str, str] = {}
            for item in nodes:
                required = {"node_key", "kind", "name"}
                if not required <= set(item):
                    raise ValueError("each graph node requires node_key, kind, and name")
                node_key = item["node_key"]
                node_path = _project_relative_path(item.get("path"))
                node_line = item.get("line")
                if node_line is not None and node_line < 1:
                    raise ValueError("line must be a positive one-based line number")
                existing = db.execute("SELECT id FROM graph_nodes WHERE project_id=? AND node_key=?", (project_id, node_key)).fetchone()
                node_id = existing["id"] if existing else _id("NOD")
                values = (item["kind"], item["name"], node_path, node_line, item.get("language"),
                          item.get("revision", project["revision"]), json.dumps(item.get("metadata", {}), sort_keys=True))
                if existing:
                    db.execute("UPDATE graph_nodes SET kind=?,name=?,path=?,line=?,language=?,revision=?,stale=0,metadata_json=? WHERE id=?", (*values, node_id))
                else:
                    db.execute("INSERT INTO graph_nodes(id,project_id,node_key,kind,name,path,line,language,revision,stale,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                               (node_id, project_id, node_key, *values))
                node_ids[node_key] = node_id
            edge_ids = []
            for item in relationships:
                if not {"source", "target", "kind"} <= set(item):
                    raise ValueError("each relationship requires source, target, and kind")
                source_id = node_ids.get(item["source"])
                target_id = node_ids.get(item["target"])
                if source_id is None:
                    source = db.execute("SELECT id FROM graph_nodes WHERE project_id=? AND node_key=?", (project_id, item["source"])).fetchone()
                    source_id = source["id"] if source else item["source"]
                if target_id is None:
                    target = db.execute("SELECT id FROM graph_nodes WHERE project_id=? AND node_key=?", (project_id, item["target"])).fetchone()
                    target_id = target["id"] if target else item["target"]
                endpoints = db.execute("SELECT id,project_id FROM graph_nodes WHERE id IN (?,?)", (source_id, target_id)).fetchall()
                expected_endpoints = 1 if source_id == target_id else 2
                if len(endpoints) != expected_endpoints or any(endpoint["project_id"] != project_id for endpoint in endpoints):
                    raise ValueError("relationships must connect existing nodes in the same project")
                confidence = float(item.get("confidence", 1.0))
                if not 0 <= confidence <= 1:
                    raise ValueError("relationship confidence must be between 0 and 1")
                evidence_id = item.get("evidence_id")
                if evidence_id and db.execute("SELECT 1 FROM evidence WHERE id=? AND project_id=?", (evidence_id, project_id)).fetchone() is None:
                    raise ValueError("relationship evidence must belong to the same project")
                revision = item.get("revision", project["revision"])
                metadata = json.dumps(item.get("metadata", {}), sort_keys=True)
                old = db.execute("SELECT * FROM graph_edges WHERE project_id=? AND source_id=? AND target_id=? AND kind=?",
                                 (project_id, source_id, target_id, item["kind"])).fetchone()
                edge_id = old["id"] if old else _id("EDG")
                if old:
                    if (old["revision"], old["stale"], old["metadata_json"], old["confidence"], old["provenance_agent_id"], old["evidence_id"]) != (revision, 0, metadata, confidence, agent_id or old["provenance_agent_id"], evidence_id or old["evidence_id"]):
                        self._archive_graph_edge(db, old)
                    db.execute("UPDATE graph_edges SET revision=?,stale=0,metadata_json=?,confidence=?,provenance_agent_id=?,evidence_id=? WHERE id=?",
                               (revision, metadata, confidence, agent_id or old["provenance_agent_id"], evidence_id or old["evidence_id"], edge_id))
                else:
                    db.execute("INSERT INTO graph_edges(id,project_id,source_id,target_id,kind,revision,stale,metadata_json,confidence,provenance_agent_id,evidence_id) VALUES(?,?,?,?,?,?,0,?,?,?,?)",
                               (edge_id, project_id, source_id, target_id, item["kind"], revision, metadata, confidence, agent_id, evidence_id))
                edge_ids.append(edge_id)
            self._audit(db, project_id, agent_id, "graph.update", "project", project_id,
                        {"node_count": len(node_ids), "relationship_count": len(edge_ids)})
            return {"nodes": list(node_ids.values()), "relationships": edge_ids}

    def find_symbols(self, project_id: str, query: str, *, path: str | None = None,
                     language: str | None = None, limit: int = 20, requesting_agent_id: str | None = None):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 160 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("symbol search requires a name and a limit of 1 to 100")
        name = query.strip()
        pattern = "%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        filters = ["project_id=?", "stale=0", "kind IN ('function','class')", "name LIKE ? ESCAPE '\\'"]
        args = [project_id, pattern]
        for field, value in (("path", path), ("language", language)):
            if value is not None:
                filters.append(f"{field}=?")
                args.append(value)
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            rows = db.execute(f"""
                SELECT node_key,kind,name,path,line,language,revision FROM graph_nodes WHERE {' AND '.join(filters)}
                ORDER BY name=? COLLATE NOCASE DESC,length(name),name,path,line,node_key LIMIT ?
            """, [*args, name, limit + 1]).fetchall()
        return {"symbols": [dict(row) for row in rows[:limit]], "truncated": len(rows) > limit}

    def graph_connections(self, project_id: str, node_key: str, *, relationship: str = "calls",
                          direction: str = "both", limit: int = 30, requesting_agent_id: str | None = None):
        if direction not in {"incoming", "outgoing", "both"} or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("connections needs incoming, outgoing, or both and a limit of 1 to 100")
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            node = db.execute("SELECT id,node_key,path,line,name,revision FROM graph_nodes WHERE project_id=? AND node_key=? AND stale=0",
                              (project_id, node_key)).fetchone()
            if node is None:
                raise KeyError(f"unknown current graph node: {node_key}")
            queries, parameters = [], []
            for side in (("incoming", "outgoing") if direction == "both" else (direction,)):
                source, target = ("target_id", "source_id") if side == "incoming" else ("source_id", "target_id")
                queries.append(f"""
                    SELECT ? AS direction, n.node_key,n.path,n.line,n.name,n.revision,
                           e.id AS edge_id,e.kind,e.metadata_json,e.confidence,e.evidence_id
                    FROM graph_edges e JOIN graph_nodes n ON e.{target}=n.id
                    WHERE e.project_id=? AND e.{source}=? AND e.kind=? AND e.stale=0 AND n.stale=0
                """)
                parameters.extend([side, project_id, node["id"], relationship])
            rows = db.execute(" UNION ALL ".join(queries) + " ORDER BY direction,node_key,edge_id LIMIT ?",
                              [*parameters, limit + 1]).fetchall()
        connections = []
        for row in rows[:limit]:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            connections.append(item)
        result = {"node": dict(node), "relationship": relationship, "connections": connections,
                  "truncated": len(rows) > limit, "basis": "Indexed graph candidates. Use a runtime trace to observe execution."}
        if relationship == "calls":
            nodes = {node_key: result["node"], **{item["node_key"]: item for item in connections}}
            edges = []
            for item in connections:
                other = item["node_key"]
                caller, callee = (other, node_key) if item["direction"] == "incoming" else (node_key, other)
                edges.append((caller, callee, item["metadata"].get("line")))
            result["mermaid"] = call_plot(nodes, edges)
        return result

    def graph_summary(self, project_id: str, limit: int, *, include_stale: bool = False,
                      requesting_agent_id: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("graph summary limit must be an integer from 1 to 20")
        condition = "project_id=?" if include_stale else "project_id=? AND stale=0"
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            nodes = db.execute(f"SELECT node_key,kind,name,path,stale FROM graph_nodes WHERE {condition} ORDER BY node_key LIMIT ?",
                               (project_id, limit)).fetchall()
            counts = db.execute(f"SELECT (SELECT count(*) FROM graph_nodes WHERE {condition}) AS node_count, "
                                f"(SELECT count(*) FROM graph_edges WHERE {condition}) AS edge_count", (project_id, project_id)).fetchone()
        return {**dict(counts), "nodes": [dict(node) for node in nodes]}

    def query_graph(self, project_id: str, node_key: str | None = None, kind: str | None = None,
                    include_stale: bool = False, requesting_agent_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            sql, args = "SELECT * FROM graph_nodes WHERE project_id=?", [project_id]
            if node_key:
                sql += " AND node_key=?"; args.append(node_key)
            if kind:
                sql += " AND kind=?"; args.append(kind)
            if not include_stale:
                sql += " AND stale=0"
            nodes = [dict(row) for row in db.execute(sql, args)]
            edge_sql, edge_args = "SELECT * FROM graph_edges WHERE project_id=?", [project_id]
            if node_key or kind:
                node_filter = "project_id=?"
                node_args: list[Any] = [project_id]
                if node_key:
                    node_filter += " AND node_key=?"; node_args.append(node_key)
                if kind:
                    node_filter += " AND kind=?"; node_args.append(kind)
                subquery = f"SELECT id FROM graph_nodes WHERE {node_filter}"
                edge_sql += f" AND (source_id IN ({subquery}) OR target_id IN ({subquery}))"
                edge_args.extend(node_args * 2)
            if not include_stale:
                edge_sql += " AND stale=0"
            edges = [dict(row) for row in db.execute(edge_sql, edge_args)]
            for node in nodes:
                node["metadata"] = json.loads(node["metadata_json"])
            for edge in edges:
                edge["metadata"] = json.loads(edge["metadata_json"])
            return {"nodes": nodes, "edges": edges}

    def get_dependencies(self, project_id: str, node_key: str, relationship: str = "imports",
                         requesting_agent_id: str | None = None) -> list[dict[str, Any]]:
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            rows = db.execute("SELECT target.* FROM graph_edges edge JOIN graph_nodes source ON source.id=edge.source_id JOIN graph_nodes target ON target.id=edge.target_id WHERE edge.project_id=? AND source.node_key=? AND edge.kind=? AND edge.stale=0 AND target.stale=0 ORDER BY target.node_key",
                              (project_id, node_key, relationship)).fetchall()
            return [dict(row) for row in rows]

    def get_relationship_history(self, project_id: str, node_key: str | None = None,
                                 requesting_agent_id: str | None = None) -> list[dict[str, Any]]:
        with self._session() as db:
            if requesting_agent_id:
                self._require_permission(db, project_id, requesting_agent_id, "read_graph")
            sql = "SELECT history.*,source.node_key AS source_key,target.node_key AS target_key FROM graph_edge_history history LEFT JOIN graph_nodes source ON source.id=history.source_id LEFT JOIN graph_nodes target ON target.id=history.target_id WHERE history.project_id=?"
            args: list[Any] = [project_id]
            if node_key:
                sql += " AND (source.node_key=? OR target.node_key=?)"; args.extend([node_key, node_key])
            sql += " ORDER BY history.recorded_at"
            rows = [dict(row) for row in db.execute(sql, args)]
            for row in rows:
                row["metadata"] = json.loads(row.pop("metadata_json"))
            return rows

