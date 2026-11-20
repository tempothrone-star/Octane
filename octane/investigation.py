"""Persistent deterministic exploration and evidence-linked investigation reports."""
from __future__ import annotations

import json
import math
import time
from collections import deque

from .db import ProjectIntelligence, _id, _now, _scrub_sensitive_value
from .exploration import ExplorationScheduler


def context_evidence_ids(context):
    identifiers = context.get("evidence_ids", []) if isinstance(context, dict) else []
    return {item for item in identifiers if isinstance(item, str)} if isinstance(identifiers, list) else set()


def _validate_exploration(node_budget, depth_limit, time_budget_seconds, direction, relationships):
    if type(node_budget) is not int or not 1 <= node_budget <= 100:
        raise ValueError("nodeBudget must be an integer from 1 to 100")
    if type(depth_limit) is not int or not 0 <= depth_limit <= 10:
        raise ValueError("depthLimit must be an integer from 0 to 10")
    if isinstance(time_budget_seconds, bool) or not math.isfinite(time_budget_seconds) or not 0.1 <= time_budget_seconds <= 60:
        raise ValueError("timeBudgetSeconds must be finite and from 0.1 to 60")
    if direction not in {"incoming", "outgoing", "both"}:
        raise ValueError("direction must be incoming, outgoing, or both")
    relationships = relationships if relationships is not None else ["calls", "imports", "defines"]
    if not isinstance(relationships, list) or not relationships or set(relationships) - {"calls", "imports", "defines", "depends_on", "tests", "documents", "related_to"}:
        raise ValueError("unsupported exploration relationships")
    return relationships


def _adjacency(graph, nodes, by_id, relationships, direction):
    adjacency = {key: [] for key in nodes}
    for edge in graph["edges"]:
        if edge["kind"] not in relationships or edge["source_id"] not in by_id or edge["target_id"] not in by_id:
            continue
        source, target = by_id[edge["source_id"]]["node_key"], by_id[edge["target_id"]]["node_key"]
        if direction in {"outgoing", "both"}:
            adjacency[source].append({"node_key": target, "kind": edge["kind"], "direction": "outgoing", "edge_id": edge["id"]})
        if direction in {"incoming", "both"}:
            adjacency[target].append({"node_key": source, "kind": edge["kind"], "direction": "incoming", "edge_id": edge["id"]})
    return adjacency


class InvestigationWorkflow:
    def __init__(self, intelligence: ProjectIntelligence):
        self.intelligence = intelligence

    def explore(self, project_id, investigation_id, agent_id, *, exploration_id=None,
                starting_nodes=None, relationships=None, direction="outgoing", depth_limit=2,
                node_budget=20, time_budget_seconds=10, weights=None):
        relationships = _validate_exploration(node_budget, depth_limit, time_budget_seconds, direction, relationships)
        scheduler = ExplorationScheduler(weights)
        started = time.monotonic()
        with self.intelligence._transaction() as db:
            investigation = self.intelligence.retrieve_investigation(investigation_id)
            if not investigation or investigation["project_id"] != project_id:
                raise ValueError("investigation must belong to this project")
            for permission in ("read_graph", "write_evidence", "checkpoint_investigation"):
                self.intelligence._require_permission(db, project_id, agent_id, permission)
            if not investigation["revision_compatible"]:
                raise ValueError("project changed; revalidate the investigation before exploring")
            if investigation["status"] in {"completed", "abandoned"}:
                raise ValueError("cannot explore a terminal investigation")
            self.intelligence.resume_investigation(investigation_id, agent_id)
            revision = investigation["revision"]
            graph = self.intelligence.query_graph(project_id, requesting_agent_id=agent_id)
            nodes = {node["node_key"]: node for node in graph["nodes"]}
            by_id = {node["id"]: node for node in nodes.values()}
            if exploration_id:
                row = db.execute("SELECT * FROM exploration_runs WHERE id=? AND investigation_id=? AND project_id=?",
                                 (exploration_id, investigation_id, project_id)).fetchone()
                if not row:
                    raise KeyError("unknown exploration in this investigation")
                if row["revision"] != revision:
                    raise ValueError("saved exploration belongs to an old revision; start a new exploration")
                profile, state = json.loads(row["profile_json"]), json.loads(row["state_json"])
                relationships, direction, depth_limit = profile["relationships"], profile["direction"], profile["depth_limit"]
            else:
                exploration_id = _id("EXP")
                if starting_nodes is not None and (not isinstance(starting_nodes, list) or any(not isinstance(k, str) for k in starting_nodes)):
                    raise ValueError("startingNodes must be an array of node keys")
                missing = set(starting_nodes or []) - set(nodes)
                if missing:
                    raise ValueError("unknown starting nodes: " + ", ".join(sorted(missing)))
                terms = set(investigation["objective"].casefold().split())
                candidates = [{"node_key": key, "relevance": len(terms & set(node["name"].casefold().split())),
                               "uncertainty": 1, "information_gain": 1, "cost": 0}
                              for key, node in nodes.items() if not starting_nodes or key in starting_nodes]
                ranked = scheduler.prioritize(candidates, node_budget=len(candidates), time_budget_seconds=60)
                state = {"queue": [[item["node_key"], 0] for item in ranked], "visited": [], "evidence_ids": []}
                profile = {"relationships": relationships, "direction": direction, "depth_limit": depth_limit,
                           "objective": investigation["objective"], "weights": scheduler.weights}
                db.execute("INSERT INTO exploration_runs VALUES(?,?,?,?,?,?,?,?,?)",
                           (exploration_id, project_id, investigation_id, revision, json.dumps(profile), json.dumps(state), "active", agent_id, _now()))
            adjacency = _adjacency(graph, nodes, by_id, relationships, direction)
            queue, visited, discoveries = deque(state["queue"]), set(state["visited"]), []
            queued = {item[0] for item in queue}
            while queue and len(discoveries) < node_budget and time.monotonic() - started < time_budget_seconds:
                key, depth = queue.popleft()
                queued.discard(key)
                if key in visited:
                    continue
                node = nodes.get(key)
                if node is None:
                    raise ValueError("saved graph node is no longer current; start a new exploration")
                visited.add(key)
                related = sorted(adjacency[key], key=lambda item: (item["node_key"], item["kind"], item["direction"]))
                evidence = self.intelligence.submit_evidence(project_id, "tentacle_exploration",
                    {"investigation_id": investigation_id, "exploration_id": exploration_id, "node_key": key,
                     "relationships": related, "depth": depth, "basis": "stored static graph; not runtime verification"},
                    "Investigated recorded graph relationships", agent_id, revision, node["path"], node["line"])
                state["evidence_ids"].append(evidence["id"])
                discoveries.append({"node_key": key, "evidence_id": evidence["id"], "depth": depth})
                if depth < depth_limit:
                    for item in related:
                        neighbor = item["node_key"]
                        if neighbor not in visited and neighbor not in queued:
                            queue.append([neighbor, depth + 1])
                            queued.add(neighbor)
            state["queue"] = [item for item in queue if item[0] not in visited]
            state["visited"] = sorted(visited)
            status = "paused" if state["queue"] else "completed"
            stop = "completed" if status == "completed" else ("node_budget" if len(discoveries) >= node_budget else "time_budget")
            db.execute("UPDATE exploration_runs SET state_json=?,status=?,updated_at=? WHERE id=?",
                       (json.dumps(state), status, _now(), exploration_id))
            context = dict(investigation["context"])
            context.update({"exploration_id": exploration_id, "explored_nodes": state["visited"],
                            "evidence_ids": sorted(context_evidence_ids(context).union(state["evidence_ids"])),
                            "next_action": "Resume exploration " + exploration_id if state["queue"] else "Review the evidence and test explicit hypotheses"})
            self.intelligence.checkpoint_investigation(investigation_id, "paused" if state["queue"] else "active", context, agent_id)
            return {"exploration_id": exploration_id, "investigation_id": investigation_id, "revision": revision,
                    "explored": discoveries, "nodes_examined": len(discoveries), "total_nodes_examined": len(visited),
                    "remaining_queue": len(state["queue"]), "stop_reason": stop, "status": status}

    def revalidate(self, project_id, investigation_id, agent_id, evidence_ids, note):
        if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(item, str) for item in evidence_ids):
            raise ValueError("revalidation needs explicit current evidence IDs")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("revalidation needs a rationale explaining what was rechecked")
        with self.intelligence._transaction() as db:
            investigation = self.intelligence.retrieve_investigation(investigation_id)
            if not investigation or investigation["project_id"] != project_id:
                raise ValueError("investigation must belong to this project")
            self.intelligence._require_permission(db, project_id, agent_id, "checkpoint_investigation")
            if investigation["status"] in {"completed", "abandoned"}:
                raise ValueError("terminal investigations cannot be revalidated")
            revision = self.intelligence.get_project_revision(project_id)
            for identifier in evidence_ids:
                row = db.execute("SELECT revision,stale FROM evidence WHERE id=? AND project_id=?", (identifier, project_id)).fetchone()
                if not row or row["stale"] or row["revision"] != revision:
                    raise ValueError("revalidation evidence must be current and belong to this project")
            context = dict(investigation["context"])
            context["revalidation"] = _scrub_sensitive_value({"previous_revision": investigation["revision"], "revision": revision,
                                                            "evidence_ids": evidence_ids, "rationale": note})
            db.execute("UPDATE investigations SET revision=? WHERE id=?", (revision, investigation_id))
            result = self.intelligence.checkpoint_investigation(investigation_id, "blocked", context, agent_id)
            self.intelligence._audit(db, project_id, agent_id, "investigation.revalidate", "investigation", investigation_id, context["revalidation"])
            return {"revalidated": True, "revision": revision, "checkpoint": result,
                    "note": "Revalidation is an attributed review; historical evidence and old exploration runs remain unchanged."}

    def report(self, project_id, investigation_id, agent_id):
        investigation = self.intelligence.retrieve_investigation(investigation_id, agent_id)
        if not investigation or investigation["project_id"] != project_id:
            raise ValueError("investigation must belong to this project")
        hypotheses = self.intelligence.retrieve_hypotheses(project_id, investigation_id, requesting_agent_id=agent_id)
        with self.intelligence._session() as db:
            explorations = [dict(row) for row in db.execute("SELECT id,revision,status,updated_at FROM exploration_runs WHERE investigation_id=? ORDER BY updated_at", (investigation_id,))]
            executions = [dict(row) for row in db.execute("SELECT id,workspace_id,evidence_id,status,before_hash,after_hash,image,image_id FROM execution_runs WHERE investigation_id=? ORDER BY created_at", (investigation_id,))]
            proposals = [dict(row) for row in db.execute("SELECT * FROM patch_proposals WHERE investigation_id=? ORDER BY created_at", (investigation_id,))]
        evidence_ids = context_evidence_ids(investigation["context"])
        for checkpoint in investigation["checkpoints"]:
            evidence_ids.update(context_evidence_ids(checkpoint["payload"]))
        evidence_ids.update(context_evidence_ids(investigation["context"].get("revalidation")))
        evidence_ids.update(edge["evidence_id"] for hypothesis in hypotheses for edge in hypothesis.get("evidence", []))
        evidence_ids.update(row["evidence_id"] for row in executions + proposals if row["evidence_id"])
        evidence = [self.intelligence.retrieve_evidence(identifier) for identifier in sorted(evidence_ids)]
        evidence = [{key: item[key] for key in ("id", "evidence_type", "description", "agent_id", "revision", "source_path",
                                               "source_line", "verification_status", "stale")}
                    for item in evidence if item and item["project_id"] == project_id]
        return {"investigation": investigation, "hypotheses": hypotheses, "explorations": explorations,
                "executions": executions, "patch_proposals": proposals,
                "evidence": evidence, "next_action": investigation["context"].get("next_action", "Review evidence and unresolved questions"),
                "limitations": ["Static graph observations do not establish root cause.",
                                "A passing candidate check applies only to its recorded image, snapshot, and command.",
                                "Patch proposals require human review; Octane never applies them to the original project."]}
