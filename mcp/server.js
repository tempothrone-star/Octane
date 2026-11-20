#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import Ajv from "ajv";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { tools } from "../openclaw/tools.js";
import { callBridge } from "../openclaw/bridge.js";

export function clientIdentity(info, suffix = "") {
  if (!info || typeof info.name !== "string" || !info.name.trim()) return "mcp-stdio";
  const name = info.name.slice(0, 1024);
  const version = typeof info.version === "string" ? info.version.slice(0, 128) : "";
  const slug = name.normalize("NFKC").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48) || "client";
  const digest = createHash("sha256").update(JSON.stringify([name, version])).digest("hex").slice(0, 12);
  return `mcp:${slug}:${digest}${suffix ? ":" + suffix : ""}`;
}

export function createServer(config) {
  if (!Array.isArray(config.allowedProjectRoots) || config.allowedProjectRoots.length === 0) {
    throw new Error("Configure allowedProjectRoots before starting Octane MCP.");
  }
  const setting = config.mcpSessionSuffix ?? false;
  if (typeof setting !== "boolean" && (typeof setting !== "string" || !/^[A-Za-z0-9._-]{1,64}$/.test(setting))) {
    throw new Error("mcpSessionSuffix must be a boolean or a short safe suffix.");
  }
  const sessionSuffix = setting === true ? randomUUID() : setting || "";
  const names = new Set(tools.map((tool) => tool.name));
  const allowed = config.allowedTools ?? [...names];
  if (!Array.isArray(allowed) || allowed.some((name) => !names.has(name))) {
    throw new Error("allowedTools must contain registered Octane tool names.");
  }
  const enabled = tools.filter((tool) => allowed.includes(tool.name));
  const ajv = new Ajv({ strict: false, allErrors: true });
  const validators = new Map(enabled.map((tool) => [tool.name, ajv.compile(tool.parameters)]));
  const server = new Server({ name: "octane", version: "0.2.0" }, { capabilities: { tools: {} } });
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: enabled.map((tool) => ({
      name: tool.name, description: tool.description, inputSchema: tool.parameters,
    })),
  }));
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const definition = enabled.find((tool) => tool.name === request.params.name);
    if (!definition) throw new Error("Tool is unavailable under the configured allowedTools policy.");
    const params = request.params.arguments ?? {};
    const validate = validators.get(definition.name);
    if (!validate(params)) throw new Error("Invalid Octane arguments: " + ajv.errorsText(validate.errors));
    const operation = definition.operation(params);
    if (!operation) throw new Error("Unsupported Octane action.");
    try {
      const result = await callBridge(config, operation, params, {
        agentId: clientIdentity(server.getClientVersion(), sessionSuffix),
        workspaceDir: config.workspaceDir,
      });
      return { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: { result } };
    } catch (error) {
      return { isError: true, content: [{ type: "text", text: error.message }] };
    }
  });
  return server;
}

async function main() {
  const { values } = parseArgs({ options: { config: { type: "string" } } });
  if (!values.config) throw new Error("Usage: octane-mcp --config /absolute/path/to/config.json");
  const config = JSON.parse(readFileSync(resolve(values.config), "utf8"));
  const server = createServer(config);
  await server.connect(new StdioServerTransport());
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  main().catch((error) => {
    process.stderr.write(error.message + "\n");
    process.exitCode = 1;
  });
}
