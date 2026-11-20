import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { tools } from "../openclaw/tools.js";

test("MCP exposes the shared twelve tools and enforces bridge and tool policies", async (t) => {
  const base = mkdtempSync(join(tmpdir(), "octane-mcp-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const root = join(base, "project");
  mkdirSync(root);
  writeFileSync(join(root, "app.py"), "def entry():\n    return 1\n");
  const config = join(base, "config.json");
  writeFileSync(config, JSON.stringify({
    allowedProjectRoots: [root],
    databasePath: join(base, "pil.sqlite3"),
    workspaceRoot: join(base, "workspaces"),
    workspaceDir: root,
  }));
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [fileURLToPath(new URL("../mcp/server.js", import.meta.url)), "--config", config],
    stderr: "pipe",
  });
  const client = new Client({ name: "octane-test", version: "1.0" });
  t.after(() => client.close());
  await client.connect(transport);
  const listed = await client.listTools();
  assert.equal(listed.tools.length, 12);
  assert.deepEqual(listed.tools.map((item) => item.name), tools.map((item) => item.name));
  const triage = await client.callTool({ name: "octane_context", arguments: { action: "triage" } });
  assert.equal(triage.structuredContent.result.root, root);
  const outside = await client.callTool({
    name: "octane_project", arguments: { action: "create", root: base, name: "outside" },
  });
  assert.equal(outside.isError, true);
  assert.match(outside.content[0].text, /outside configured/);
  await assert.rejects(client.callTool({
    name: "octane_context", arguments: { action: "triage", agentId: "admin" },
  }), /Invalid Octane arguments/);
  const created = await client.callTool({
    name: "octane_project", arguments: { action: "create", root, name: "fixture" },
  });
  const workspace = await client.callTool({
    name: "octane_workspace", arguments: {
      action: "create", projectId: created.structuredContent.result.id, mode: "observation",
    },
  });
  const execution = await client.callTool({
    name: "octane_workspace", arguments: {
      action: "run", workspaceId: workspace.structuredContent.result.id, mode: "observation",
      image: "unapproved:image", command: ["python", "app.py"],
    },
  });
  assert.equal(execution.isError, true);
  assert.match(execution.content[0].text, /allowedDockerImages/);
});

test("MCP allowedTools policy limits discovery and invocation", async (t) => {
  const base = mkdtempSync(join(tmpdir(), "octane-mcp-policy-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const config = join(base, "config.json");
  writeFileSync(config, JSON.stringify({
    allowedProjectRoots: [base], allowedTools: ["octane_context"],
    databasePath: join(base, "state/pil.sqlite3"), workspaceRoot: join(base, "workspaces"),
  }));
  const client = new Client({ name: "policy-test", version: "1.0" });
  t.after(() => client.close());
  await client.connect(new StdioClientTransport({
    command: process.execPath,
    args: [fileURLToPath(new URL("../mcp/server.js", import.meta.url)), "--config", config],
    stderr: "pipe",
  }));
  assert.deepEqual((await client.listTools()).tools.map((tool) => tool.name), ["octane_context"]);
  await assert.rejects(client.callTool({
    name: "octane_project", arguments: { action: "list" },
  }), /unavailable/);
});
