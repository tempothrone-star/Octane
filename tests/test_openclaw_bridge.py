from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if sys.version_info < (3, 10):
    raise unittest.SkipTest("the native PIL plugin suite requires Python 3.10+")

from octane.openclaw_bridge import BridgeError, OctaneBridge


class OpenClawBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.allowed = self.base / "allowed"
        self.allowed.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.allowed / "app.py").write_text("def entry():\n    return helper()\n\ndef helper():\n    return 3\n", encoding="utf-8")
        self.bridge = OctaneBridge(
            str(self.base / "state" / "pil.sqlite3"), [str(self.allowed)],
            str(self.base / "state" / "workspaces"),
        )

    def call(self, operation, **params):
        return self.bridge.dispatch({"operation": operation, "params": params})

    def test_allowlist_scopes_project_registration_and_existing_project_access(self):
        with self.assertRaises(PermissionError):
            self.call("project_create", root=str(self.outside), name="outside")
        project = self.call("project_create", root=str(self.allowed), name="allowed")
        self.assertEqual(project["root"], str(self.allowed))
        self.assertEqual([item["id"] for item in self.call("project_list")], [project["id"]])
        with self.assertRaises(KeyError):
            self.call("project_show", projectId="missing")

    def test_scan_and_provenance_operations_use_existing_pil(self):
        project = self.call("project_create", root=str(self.allowed), name="fixture")
        scan = self.call("scan", projectId=project["id"])
        self.assertEqual(scan["symbols_discovered"], 2)
        evidence = self.call(
            "evidence_add", projectId=project["id"], evidenceType="review_note",
            description="The function calls helper", payload={"source": "review"},
            sourcePath="app.py", sourceLine=1,
        )
        self.assertEqual(evidence["verification_status"], "unverified")
        records = self.call("evidence_search", projectId=project["id"], query="helper")
        self.assertTrue(any(item["id"] == evidence["id"] for item in records))
        memory = self.call(
            "memory_add", projectId=project["id"], category="interpretation",
            content="entry delegates to helper", supportingEvidenceIds=[evidence["id"]],
        )
        self.assertEqual(memory["agent_id"], self.bridge.intelligence.get_agent(memory["agent_id"])["id"])
        self.assertEqual(self.call("memory_read", projectId=project["id"], memoryId=memory["id"])["supporting_evidence_ids"], [evidence["id"]])

    def test_checkpoint_handoff_and_revision_staleness_are_exposed(self):
        project = self.call("project_create", root=str(self.allowed), name="handoff")
        scan = self.call("scan", projectId=project["id"])
        investigation = self.call(
            "investigation_create", projectId=project["id"], objective="Inspect entry path",
            context={"next_action": "check helper", "evidence_ids": []},
        )
        self.call(
            "investigation_checkpoint", projectId=project["id"], investigationId=investigation["id"],
            status="paused", payload={"next_action": "check helper", "evidence_ids": []},
        )
        resumed = self.call("investigation_resume", projectId=project["id"], investigationId=investigation["id"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["context"]["next_action"], "check helper")
        self.assertEqual(self.call("investigation_read", projectId=project["id"], investigationId=investigation["id"])["status"], "active")
        self.assertEqual(self.call("project_changes", projectId=project["id"])["current_revision"], scan["revision"])
        source = self.allowed / "app.py"
        source.write_text(source.read_text(encoding="utf-8") + "\n# edited after checkpoint\n", encoding="utf-8")
        changed_scan = self.call("scan", projectId=project["id"])
        self.assertNotEqual(changed_scan["revision"], scan["revision"])
        stale_resume = self.call("investigation_resume", projectId=project["id"], investigationId=investigation["id"])
        self.assertFalse(stale_resume["resumed"])
        self.assertEqual(stale_resume["current_revision"], changed_scan["revision"])

    def test_workspace_execution_requires_operator_allowlisted_image(self):
        project = self.call("project_create", root=str(self.allowed), name="workspace")
        workspace = self.call("workspace_create", projectId=project["id"], mode="observation")
        with self.assertRaises(PermissionError):
            self.call("workspace_run", workspaceId=workspace["id"], mode="observation",
                      command=["python", "-V"], image="unreviewed:latest", timeoutSeconds=1)

    def test_reserved_agent_names_cannot_expand_bridge_permissions(self):
        project = self.call("project_create", root=str(self.allowed), name="permissions")
        self.bridge.intelligence.register_agent(
            project["id"], "openclaw-read-evidence", "operator", permissions=["read_evidence", "write_evidence"]
        )
        with self.assertRaises(PermissionError):
            self.call("evidence_search", projectId=project["id"])

    def test_bridge_rejects_unregistered_operations_and_secret_evidence(self):
        with self.assertRaises(BridgeError):
            self.bridge.dispatch({"operation": "run_shell", "params": {"command": "id"}})
        project = self.call("project_create", root=str(self.allowed), name="secrets")
        with self.assertRaises(ValueError):
            self.call(
                "evidence_add", projectId=project["id"], evidenceType="note", description="bad",
                payload={"api_token": "not-stored"},
            )

    def test_cli_json_protocol_invokes_only_the_named_operation(self):
        project_request = {"operation": "project_create", "params": {"root": str(self.allowed), "name": "cli-fixture"}}
        command = [sys.executable, "-m", "octane.openclaw_bridge", "--database",
                   str(self.base / "cli.sqlite3"), "--allowed-root", str(self.allowed),
                   "--workspace-root", str(self.base / "cli-workspaces")]
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                                input=json.dumps(project_request), text=True, capture_output=True, check=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        response = json.loads(result.stdout)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["name"], "cli-fixture")
        self.assertEqual(result.stderr, "")

    def test_project_metadata_context_and_audit_are_exposed(self):
        project = self.call("project_create", root=str(self.allowed), name="context")
        updated = self.call("project_update", projectId=project["id"], name="context-updated",
                            languages=["Python"], configuration={"mode": "analysis"})
        self.assertEqual(updated["name"], "context-updated")
        self.assertEqual(updated["languages"], ["Python"])
        evidence = self.call("evidence_add", projectId=project["id"], evidenceType="note",
                             description="Observed entry point", payload={"result": "present"})
        memory = self.call("memory_add", projectId=project["id"], category="design",
                           content="entry is an application boundary", supportingEvidenceIds=[evidence["id"]])
        context = self.call("context", projectId=project["id"], limit=2)
        self.assertEqual(context["project"]["name"], "context-updated")
        self.assertEqual(context["evidence"][0]["id"], evidence["id"])
        self.assertEqual(context["memories"][0]["id"], memory["id"])
        refreshed = self.call("context", projectId=project["id"], refreshScan=True, includeStale=True)
        self.assertEqual(refreshed["scan"]["symbols_discovered"], 2)
        self.assertTrue(refreshed["project"]["revision"])
        with self.assertRaises(BridgeError):
            self.call("context", projectId=project["id"], limit=21)
        audit = self.call("audit_search", projectId=project["id"], eventAction="project.update")
        self.assertTrue(any(row["action"] == "project.update" for row in audit))

    def test_agent_inspection_and_opt_in_role_provisioning(self):
        project = self.call("project_create", root=str(self.allowed), name="agents")
        with self.assertRaises(PermissionError):
            self.call("agents_create", projectId=project["id"], name="helper", role="reader")
        self.bridge.allow_agent_provisioning = True
        reader = self.call("agents_create", projectId=project["id"], name="helper", role="reader")
        self.assertIn(reader, self.call("agents_list", projectId=project["id"]))
        self.assertEqual(self.call("agents_show", projectId=project["id"], agentId=reader["id"])["id"], reader["id"])
        self.assertTrue(self.call("agents_check", projectId=project["id"], agentId=reader["id"],
                                  permission="read_evidence")["allowed"])
        self.assertFalse(self.call("agents_check", projectId=project["id"], agentId=reader["id"],
                                   permission="write_evidence")["allowed"])
        with self.assertRaises(BridgeError):
            self.call("agents_create", projectId=project["id"], name="root", role="operator")
        other = self.call("project_create", root=str(self.allowed), name="other")
        with self.assertRaises(KeyError):
            self.call("agents_show", projectId=other["id"], agentId=reader["id"])

    def test_graph_investigation_hypothesis_and_schedule_operations(self):
        project = self.call("project_create", root=str(self.allowed), name="graph")
        first, second = "symbol:app.py:entry", "symbol:app.py:helper"
        self.call("graph_update", projectId=project["id"], nodes=[
            {"nodeKey": first, "kind": "function", "name": "entry", "path": "app.py"},
            {"nodeKey": second, "kind": "function", "name": "helper", "path": "app.py"},
        ], graphRelationships=[{"source": first, "target": second, "kind": "calls", "confidence": 0.8}])
        graph = self.call("graph_query", projectId=project["id"], nodeKey=first)
        self.assertTrue(any(edge["kind"] == "calls" for edge in graph["edges"]))
        self.call("graph_dependencies", projectId=project["id"], nodeKey=first, relationship="calls")
        self.call("graph_update", projectId=project["id"], graphRelationships=[
            {"source": first, "target": second, "kind": "calls", "confidence": 0.6},
        ])
        self.assertTrue(self.call("graph_history", projectId=project["id"], nodeKey=first))
        explored = self.call("graph_explore", projectId=project["id"], objective="follow calls",
                             startingNodes=[first], relationships=["calls"], nodeBudget=2)
        self.assertGreaterEqual(explored["nodes_examined"], 1)

        investigation = self.call("investigation_create", projectId=project["id"], objective="Review entry")
        updated = self.call("investigation_update", projectId=project["id"],
                            investigationId=investigation["id"], objective="Review entry and helper",
                            context={"next_action": "inspect helper"})
        self.assertEqual(updated["context"]["next_action"], "inspect helper")
        hypothesis = self.call("hypothesis_create", projectId=project["id"], investigationId=investigation["id"],
                               statement="entry delegates to helper", confidence=0.7)
        self.call("hypothesis_attach", projectId=project["id"], hypothesisId=hypothesis["id"],
                  evidenceId=self.call("evidence_add", projectId=project["id"], evidenceType="review",
                                       description="Call found in source", payload={})["id"])
        self.assertEqual(self.call("hypothesis_list", projectId=project["id"], investigationId=investigation["id"])[0]["id"], hypothesis["id"])
        self.call("hypothesis_update", projectId=project["id"], hypothesisId=hypothesis["id"],
                  status="under_investigation", expectedUpdatedAt=hypothesis["updated_at"])
        scheduled = self.call("schedule", candidates=[
            {"nodeKey": first, "informationGain": 0.9, "estimatedSeconds": 1},
            {"nodeKey": second, "informationGain": 0.1, "estimatedSeconds": 1},
        ], nodeBudget=1, timeBudgetSeconds=3)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]["node_key"], first)

    def test_openclaw_manifest_and_skill_contract_match_tools(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "openclaw.plugin.json").read_text(encoding="utf-8"))
        entry = (root / "openclaw" / "index.js").read_text(encoding="utf-8")
        tools = manifest["contracts"]["tools"]
        self.assertEqual(len(tools), 12)
        self.assertEqual(manifest["skills"], ["./octance"])
        self.assertEqual(set(tools), set(manifest["toolMetadata"]))
        self.assertTrue(all(manifest["toolMetadata"][name]["optional"] for name in tools))
        self.assertTrue(all(f'name: "{name}"' in entry for name in tools))
        for operation in ("project_update", "context", "audit_search", "agents_list", "agents_show",
                          "agents_check", "agents_create", "investigation_update", "graph_update", "schedule"):
            self.assertIn(f"def _op_{operation}(", (root / "octane" / "openclaw_bridge.py").read_text(encoding="utf-8"))
        self.assertFalse(manifest["configSchema"]["properties"]["allowAgentProvisioning"].get("default", True))
        readme = (root / "README.md").read_text(encoding="utf-8")
        architecture = (root / "docs" / "architecture.md").read_text(encoding="utf-8")
        self.assertTrue(readme.startswith("# Octance\n"))
        self.assertIn("not an autonomous repair agent", readme)
        self.assertIn("The product is the portable OpenClaw skill in `octance/`", architecture)
        legacy_readme = (root / "openclaw" / "README.md").read_text(encoding="utf-8")
        self.assertIn("optional native extension", legacy_readme)
        self.assertIn("typebox", legacy_readme)
        current_skill = (root / "octance" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(current_skill.startswith("---\nname: octance\n"))
        root_skill = (root / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(root_skill.startswith("---\nname: octance\n"))
        self.assertEqual(root_skill, current_skill.replace("{baseDir}/scripts/octance.py", "{baseDir}/octance/scripts/octance.py").replace("(references/", "(octance/references/"))
        self.assertTrue((root / "tests" / "__init__.py").is_file())
        self.assertIn("does not grant filesystem or shell access", current_skill)
        self.assertIn("does not yet implement a predeclared replayable check runner", current_skill)
        self.assertIn("is heuristic, and is not root-cause localization", current_skill)
        self.assertIn("safe import keywords", current_skill)
        self.assertIn("--language-config .octance/languages.json", current_skill)
        self.assertIn("openclaw skills install git:https://github.com/tempothrone-star/Octane.git", readme)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "octane-openclaw")
        self.assertFalse(package["private"])
        self.assertIn("typebox", package["dependencies"])
        self.assertTrue(any(path.startswith("octance/") for path in package["files"]))
        self.assertIn("scripts/octance.py", package["files"])
        self.assertIn("SKILL.md", package["files"])


if __name__ == "__main__":
    unittest.main()
