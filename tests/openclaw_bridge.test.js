import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { bridgeTimeoutFor, callBridge } from "../openclaw/bridge.js";

test("workspace execution timeout includes the requested runtime and cleanup", () => {
  assert.equal(bridgeTimeoutFor("workspace_run", { timeoutSeconds: 300 }), 310_000);
  assert.equal(bridgeTimeoutFor("workspace_run", { timeoutSeconds: 30 }), 40_000);
  assert.equal(bridgeTimeoutFor("workspace_run", { timeoutSeconds: 999 }), 310_000);
  assert.equal(bridgeTimeoutFor("project_list", {}), 120_000);
  assert.equal(bridgeTimeoutFor("workspace_mutate", {mutationLimit: 20}), 810_000);
  assert.equal(bridgeTimeoutFor("workspace_bisect", {bisectBudget: 2}), 150_000);
  assert.equal(bridgeTimeoutFor("workspace_evaluate", {caseLimit: 1}), 290_000);
  assert.equal(bridgeTimeoutFor("workspace_run", {}), 120_000);
});

test("JavaScript bridge round trips through the real Python JSON protocol", async (t) => {
  const base = mkdtempSync(join(tmpdir(), "octane-js-test-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const root = join(base, "project");
  mkdirSync(root);
  const config = {
    allowedProjectRoots: [root],
    databasePath: join(base, "pil.sqlite3"),
    workspaceRoot: join(base, "workspaces"),
    pythonExecutable: process.env.PYTHON || "python3",
  };
  assert.deepEqual(await callBridge(config, "project_list", {}), []);
  const created = await callBridge(config, "project_create", { root, name: "fixture" });
  const projects = await callBridge(config, "project_list", {});
  assert.equal(projects[0].id, created.id);
  const checkout = join(root, "checkout");
  mkdirSync(checkout);
  writeFileSync(join(checkout, "package.json"), '{"packageManager":"pnpm@10.0.0","scripts":{"test":"node --test"}}');
  const triage = await callBridge(config, "repair_triage", {}, { workspaceDir: checkout });
  assert.equal(triage.root, checkout);
  assert.equal(triage.project_id, null);
  assert.deepEqual(triage.baseline_candidates, [{ command: ["pnpm", "run", "test"], source: "package.json" }]);
  const sandboxTriage = await callBridge(config, "repair_triage", { root: "/workspace" }, { workspaceDir: checkout, sandboxed: true });
  assert.equal(sandboxTriage.root, checkout);
  await assert.rejects(callBridge(config, "not_an_operation", {}), /operation/i);
  assert.throws(() => callBridge({...config, holdoutPolicy: "relative.json"}, "project_list", {}), /absolute operator-owned/);
  assert.throws(() => callBridge({}, "project_list", {}), /allowedProjectRoots/);
});

test("npm package contains both runtimes, skill migrations, and linked documentation", () => {
  const output = execFileSync("npm", ["pack", "--dry-run", "--json", "--ignore-scripts"], {
    cwd: fileURLToPath(new URL("..", import.meta.url)), encoding: "utf8",
  });
  const files = new Set(JSON.parse(output)[0].files.map((file) => file.path));
  for (const path of ["docs/accuracy-workflow.md", "octane/localization.py", "octane/holdout.py", "octane/diagnostics.py", "CHANGELOG.md", "docs/polish-review.md", "docs/follow-up-review.md", "octane/host_environment.py", "octane/build_config.py", "octane/mutation_support.py", "octane/sigstore.py", "octane/sarif.py", "mcp/server.js", "examples/blinker-pr149.jsonl", "docs/integrations.md", "docs/evaluation.md", "openclaw/tools.js", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "openclaw/index.js", "openclaw/bridge.js", "openclaw.plugin.json",
    "octane/openclaw_bridge.py", "octane/python_calls.py", "octane/repair.py", "octane/trace_runner.py",
    "octane/native_trace.py", "octane/native_trace_runner.py",
    "octane/syntax_calls.py", "octane/runtime_profiles.py", "octane/call_plot.py", "octane/focus.py", "octane/node_trace_runner.cjs",
    "octane/ruby_trace_runner.rb",
    "SKILL.md", "scripts/octane.py", "octane/__main__.py",
    "octane/ledger/core.py", "octane/ledger/migrations/001_initial.sql", "references/investigate.md",
    "docs/architecture.md", "docs/implementation-and-limitations.md"]) {
    assert.ok(files.has(path), `Missing package file: ${path}`);
  }
  assert.ok([...files].every((path) => !path.includes("__pycache__") && !path.endsWith(".pyc")));
  assert.deepEqual([...files].filter((path) => path.endsWith("SKILL.md")), ["SKILL.md"]);
  assert.ok([...files].every((path) => !path.startsWith("octance/")));
});
