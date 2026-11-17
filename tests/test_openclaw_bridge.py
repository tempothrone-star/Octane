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
from octane.native_trace import load_native_trace, native_trace_command
from octane.runtime_profiles import load_profile_trace
from octane.analysis import analyzer
from octane.experiments import _same_output


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

    def test_native_trace_plots_only_adjacent_project_frames(self):
        callee = {"function": "compare", "path": "ops.c", "line": 3, "inline": False}
        caller = {"function": "dispatch", "path": "entry.c", "line": 15, "inline": True}
        detached = {"function": "main", "path": "main.c", "line": 24, "inline": False}
        capture = self.base / "trace.json"
        raw = {"stacks": [[callee, caller, None, detached]], "hit_symbols": ["compare"],
               "unavailable_symbols": [], "truncated": False}
        capture.write_text(json.dumps(raw))
        trace = load_native_trace(capture, ["compare"])
        self.assertTrue(trace["available"])
        self.assertEqual(trace["calls"], [{"caller": caller, "callee": callee}])
        self.assertIn("n1 -->|line 15| n0", trace["mermaid"])
        self.assertEqual(trace["mermaid"].count("-->"), 1)
        callee["path"] = "../outside.c"
        capture.write_text(json.dumps(raw))
        self.assertIsNone(load_native_trace(capture, ["compare"]))

    def test_native_trace_accepts_literal_arguments_and_rejects_debugger_expressions(self):
        command = ["./sapi/cli/php", "-r", 'echo "$a";']
        self.assertEqual(native_trace_command(command, ["zend_compare"])[-4:], ["--args", *command])
        for targets in (["zend_compare\nshell id"], ["main", "main"], []):
            with self.assertRaises(ValueError):
                native_trace_command(command, targets)
        with self.assertRaises(ValueError):
            native_trace_command(["/bin/sh"], ["main"])

    def test_multilingual_symbols_are_searchable_and_ambiguous_overloads_stay_unlinked(self):
        if not analyzer()[1].ast_backend.available():
            self.skipTest("the optional Tree-sitter backend is required")
        (self.allowed / "main.ts").write_text("function helper(x: number) { return x; }\nconst run = (x: number) => helper(x);\n")
        (self.allowed / "Main.java").write_text(
            "class Main { static int helper(int x) { return x; } static String helper(String x) { return x; } "
            "static int run(int x) { return helper(x); } }"
        )
        project = self.call("project_create", root=str(self.allowed), name="multilingual")["id"]
        scan = self.call("scan", projectId=project)
        self.assertEqual(scan["capabilities"]["tree_sitter_call_candidates"], ["java", "typescript"])
        found = self.call("graph_find", projectId=project, query="run", language="typescript")["symbols"]
        self.assertEqual([symbol["path"] for symbol in found], ["main.ts"])
        graph = self.call("graph_connections", projectId=project, nodeKey=found[0]["node_key"])
        self.assertEqual([(row["name"], row["metadata"]["static"]) for row in graph["connections"]], [("helper", True)])
        self.assertIn("line 2", graph["mermaid"])
        overloaded = self.call("graph_find", projectId=project, query="helper", path="Main.java")["symbols"]
        self.assertEqual(len({item["node_key"] for item in overloaded}), 2)
        self.assertFalse(self.call("graph_connections", projectId=project, nodeKey="symbol:Main.java:Main.run")["connections"])

    def test_focus_on_current_checkout_needs_no_registration_and_honors_scope(self):
        result = self.bridge.dispatch({"operation": "repair_focus", "params": {"path": "app.py", "query": "entry"},
                                       "context": {"workspaceDir": str(self.allowed)}})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["files_examined"], 1)
        self.assertEqual([(edge["caller"]["name"], edge["callee"]["name"]) for edge in result["connections"]],
                         [("entry", "helper")])
        self.assertEqual(self.call("project_list"), [])
        outside = self.outside / "other.py"
        outside.write_text("def secret(): pass")
        (self.allowed / "linked.py").symlink_to(outside)
        with self.assertRaises(PermissionError):
            self.call("repair_focus", path="linked.py")

    def test_focus_recognizes_commonjs_methods_and_sibling_receiver_calls(self):
        if not analyzer()[1].ast_backend.available():
            self.skipTest("the optional Tree-sitter backend is required")
        (self.allowed / "response.js").write_text(
            "const res = module.exports = {};\nres.send = function send(src) { return src; };\n"
            "res.jsonp = function jsonp(src) { return this.send(src); };\n"
        )
        response = self.call("repair_focus", query="jsonp")
        self.assertEqual([node["name"] for node in response["symbols"]], ["res.jsonp"])
        self.assertEqual([(edge["caller"]["name"], edge["callee"]["name"]) for edge in response["connections"]],
                         [("res.jsonp", "res.send")])

    def test_triage_lists_mocha_compact_command_and_mixed_case_contracts(self):
        (self.allowed / "Readme.md").touch()
        (self.allowed / "History.md").touch()
        package = self.allowed / "package.json"
        package.write_text('{"scripts":{"test":"mocha --reporter spec test/"}}')
        triage = self.call("repair_triage")
        self.assertEqual(triage["contract_locations"], ["Readme.md", "History.md"])
        self.assertEqual(triage["baseline_candidates"][0]["quiet_command"], ["npm", "test", "--", "--reporter", "dot"])
        package.write_text('{"scripts":{"test":"mocha && npm run integration"}}')
        self.assertNotIn("quiet_command", self.call("repair_triage")["baseline_candidates"][0])

    def test_workspace_comparison_marks_reduced_security_header_references(self):
        filename = "headers.js"
        (self.allowed / filename).write_text("res.set('X-Content-Type-Options', 'nosniff');\nres.send(body);\n")
        project = self.call("project_create", root=str(self.allowed), name="headers")["id"]
        workspace = self.call("workspace_create", projectId=project, mode="experimentation")["id"]
        self.addCleanup(self.call, "workspace_destroy", workspaceId=workspace)
        self.call("workspace_patch", workspaceId=workspace, relativePath=filename, content="res.send(body);\n")
        notes = self.call("workspace_compare", workspaceId=workspace)["review_notes"]
        self.assertEqual([(note["path"], note["header"], note["removed_occurrences"]) for note in notes],
                         [(filename, "x-content-type-options", 1)])

    def test_preservation_output_marks_redacted_and_truncated_values_incomparable(self):
        ordinary = {"stdout": "stable output\n", "stderr": ""}
        self.assertTrue(_same_output(ordinary, ordinary))
        self.assertFalse(_same_output(ordinary, {**ordinary, "stderr": "changed\n"}))
        for recorded in ({**ordinary, "stdout": "Bearer [REDACTED]"},
                         {**ordinary, "output_truncated": {"stdout": True}}):
            self.assertIsNone(_same_output(recorded, recorded))

    def test_speedscope_profile_parses_evented_stacks_without_joining_external_frames(self):
        profile_file = self.base / "capture.json"
        trace = {"$schema": "https://www.speedscope.app/file-format-schema.json", "shared": {"frames": [
            {"name": "main", "file": "file:///workspace/app.py", "line": 1},
            {"name": "bridge", "file": "/usr/lib/framework.py", "line": 120},
            {"name": "helper", "file": "app.py", "line": 4},
        ]}, "profiles": [{"type": "evented", "events": [
            {"type": kind, "frame": index, "at": time}
            for time, (kind, index) in enumerate((("O", 0), ("O", 1), ("O", 2), ("C", 2), ("C", 1), ("O", 2), ("C", 2), ("C", 0)))
        ]}]}
        profile_file.write_text(json.dumps(trace))
        result = load_profile_trace(profile_file, self.allowed, "speedscope")
        self.assertTrue(result["available"])
        self.assertEqual([(item["caller"]["function"], item["callee"]["function"], item["samples"])
                          for item in result["calls"]], [("main", "helper", 1)])
        self.assertIn("-->", result["mermaid"])
        trace["profiles"][0]["events"].pop()
        profile_file.write_text(json.dumps(trace))
        self.assertIsNone(load_profile_trace(profile_file, self.allowed, "speedscope"))

    def test_repair_triage_selects_additional_project_test_runners(self):
        (self.allowed / "build.gradle.kts").touch()
        (self.allowed / "gradlew").touch(mode=0o755)
        (self.allowed / "composer.json").write_text('{"scripts":{"test":["phpunit"]}}')
        (self.allowed / "app.sln").touch()
        baselines = self.call("repair_triage")["baseline_candidates"]
        self.assertIn({"source": "build.gradle.kts", "command": ["./gradlew", "test"]}, baselines)
        self.assertIn({"source": "composer.json", "command": ["composer", "run-script", "test"]}, baselines)
        self.assertIn({"source": "app.sln", "command": ["dotnet", "test", "./app.sln"]}, baselines)

    def test_allowlist_scopes_project_registration_and_existing_project_access(self):
        with self.assertRaises(PermissionError):
            self.call("project_create", root=str(self.outside), name="outside")
        project = self.call("project_create", root=str(self.allowed), name="allowed")
        self.assertEqual(project["root"], str(self.allowed))
        self.assertEqual([item["id"] for item in self.call("project_list")], [project["id"]])
        with self.assertRaises(KeyError):
            self.call("project_show", projectId="missing")

    def test_repair_triage_starts_from_active_checkout_without_registration(self):
        (self.allowed / ".git").write_text("gitdir: /unused", encoding="utf-8")
        (self.allowed / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
        tests = self.allowed / "tests"
        tests.mkdir()
        (tests / "test_app.py").touch()
        result = self.bridge.dispatch({
            "operation": "repair_triage", "params": {"path": "app.py"},
            "context": {"workspaceDir": str(tests)},
        })
        self.assertEqual(result["root"], str(self.allowed))
        self.assertEqual(result["target"], "app.py")
        self.assertEqual(result["related_test_candidates"], ["tests/test_app.py"])
        self.assertEqual(result["baseline_candidates"], [
            {"command": ["python", "-m", "pytest", "-q"], "source": "pyproject.toml"},
        ])
        self.assertIsNone(result["project_id"])
        self.assertEqual(self.call("project_list"), [])
        project = self.call("project_create", root=str(self.allowed), name="repair")
        self.assertEqual(self.call("repair_triage")["project_id"], project["id"])
        self.assertEqual(self.call("repair_triage", projectId=project["id"])["root"], str(self.allowed))

    def test_repair_triage_does_not_guess_an_outside_or_ambiguous_checkout(self):
        outside_file = self.outside / "package.json"
        outside_file.write_text('{"scripts":{"test":"run-this"}}', encoding="utf-8")
        (self.allowed / "package.json").symlink_to(outside_file)
        result = self.bridge.dispatch({
            "operation": "repair_triage", "context": {"workspaceDir": str(self.outside)},
        })
        self.assertEqual(result["status"], "checkout_required")
        self.assertEqual(self.call("repair_triage")["baseline_candidates"], [])
        for path in ("../outside/package.json", "package.json"):
            with self.assertRaises(PermissionError):
                self.call("repair_triage", path=path)
        self.bridge.allowed_roots.append(self.outside)
        self.assertEqual(self.call("repair_triage")["status"], "checkout_required")
        self.assertEqual(self.call("repair_triage", root=str(self.outside))["baseline_candidates"], [
            {"command": ["npm", "test"], "source": "package.json"},
        ])

    def test_repair_triage_maps_docker_mount_only_to_an_allowed_runtime_workspace(self):
        request = {"operation": "repair_triage", "params": {"root": "/workspace"}}
        result = self.bridge.dispatch({**request, "context": {"sandboxed": True, "workspaceDir": str(self.allowed)}})
        self.assertEqual(result["root"], str(self.allowed))
        for context in ({"sandboxed": True}, {"sandboxed": True, "workspaceDir": str(self.outside)}):
            self.assertEqual(self.bridge.dispatch({**request, "context": context})["status"], "checkout_required")
        with self.assertRaises((FileNotFoundError, PermissionError)):
            self.bridge.dispatch({**request, "context": {"workspaceDir": str(self.allowed)}})

    def test_repair_triage_selects_a_php_source_build_and_local_phpt_scope(self):
        for filename in ("buildconf", "configure.ac", "main/php.h", "run-tests.php",
                         "tests/lang/general.phpt", "ext/example/example.c", "ext/example/tests/specific.phpt"):
            path = self.allowed / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        first = self.call("repair_triage")["baseline_candidates"][0]
        self.assertEqual(first["scope"], "tests/lang")
        self.assertEqual(first["setup_commands"], [
            ["./buildconf"],
            ["./configure", "--disable-all", "--enable-cli", "--disable-cgi", "--disable-phpdbg"],
            ["make", "-j2", "sapi/cli/php"],
        ])
        (self.allowed / "Makefile").touch()
        extension = self.call("repair_triage", path="ext/example/example.c")["baseline_candidates"][0]
        self.assertEqual(extension["scope"], "ext/example/tests")
        self.assertEqual(extension["setup_commands"], [["make", "-j2", "sapi/cli/php"]])
        cli = self.allowed / "sapi/cli/php"
        cli.parent.mkdir(parents=True)
        cli.touch(mode=0o755)
        targeted = self.call("repair_triage", path="ext/example/tests/specific.phpt")["baseline_candidates"][0]
        self.assertEqual(targeted["setup_commands"], [])
        self.assertEqual(targeted["command"], ["./sapi/cli/php", "-n", "run-tests.php", "-P", "-q", "-j2",
                                               "ext/example/tests/specific.phpt"])

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
        self.assertEqual(manifest["skills"], ["./openclaw/skills/octane", "./octance"])
        self.assertEqual(set(tools), set(manifest["toolMetadata"]))
        self.assertTrue(all(manifest["toolMetadata"][name]["optional"] for name in tools))
        self.assertTrue(all(f'name: "{name}"' in entry for name in tools))
        for operation in ("project_update", "context", "audit_search", "agents_list", "agents_show",
                          "agents_check", "agents_create", "investigation_update", "graph_update", "schedule"):
            self.assertIn(f"def _op_{operation}(", (root / "octane" / "openclaw_bridge.py").read_text(encoding="utf-8"))
        self.assertFalse(manifest["configSchema"]["properties"]["allowAgentProvisioning"].get("default", True))
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn("openclaw plugins install --link .", readme)
        self.assertIn("openclaw skills install git:tempothrone-star/Octane", readme)
        self.assertIn("(openclaw/README.md)", readme)
        self.assertIn("(octance/SKILL.md)", readme)
        for skill_root in manifest["skills"]:
            self.assertTrue((root / skill_root / "SKILL.md").is_file())
        native_skill = (root / "openclaw/skills/octane/SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(native_skill.startswith("---\nname: octane\n"))
        current_skill = (root / "octance" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(current_skill.startswith("---\nname: octance\n"))
        root_skill = (root / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(root_skill.startswith("---\nname: octance\n"))
        self.assertEqual(root_skill, current_skill.replace("{baseDir}/scripts/octance.py", "{baseDir}/octance/scripts/octance.py").replace("(references/", "(octance/references/"))
        self.assertTrue((root / "tests" / "__init__.py").is_file())
        self.assertIn("does not grant filesystem or shell access", current_skill)
        self.assertIn("`unverified`", current_skill)
        self.assertIn("(references/investigate.md)", current_skill)
        triage_reference = (root / "octance/references/investigate.md").read_text(encoding="utf-8")
        self.assertIn("--language-config .octance/languages.json", triage_reference)
        self.assertIn("quoted_import_keywords", triage_reference)
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "octane-openclaw")
        self.assertFalse(package["private"])
        self.assertIn("typebox", package["dependencies"])
        self.assertTrue(any(path.startswith("octance/") for path in package["files"]))
        self.assertIn("scripts/octance.py", package["files"])
        self.assertIn("SKILL.md", package["files"])


if __name__ == "__main__":
    unittest.main()
