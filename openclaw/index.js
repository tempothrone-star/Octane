import { Type } from "typebox";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { callBridge } from "./bridge.js";
import { tools, repairGuidance } from "./tools.js";

const configSchema = Type.Object({
  allowedProjectRoots: Type.Array(Type.String({ minLength: 1 }), { minItems: 1 }),
  allowedDockerImages: Type.Optional(Type.Array(Type.String({ minLength: 1 }))),
  allowAgentProvisioning: Type.Optional(Type.Boolean()),
  allowDefaultContainerUser: Type.Optional(Type.Boolean()),
  containerEngine: Type.Optional(Type.Union([Type.Literal("docker"), Type.Literal("podman")])),
  containerRuntime: Type.Optional(Type.Literal("runsc")),
  languageServers: Type.Optional(Type.Record(Type.String(), Type.Union([
    Type.Array(Type.String()),
    Type.Object({ command: Type.Array(Type.String()), initializationOptions: Type.Optional(Type.Record(Type.String(), Type.Any())), environmentAllowlist: Type.Optional(Type.Array(Type.String())) }),
  ]))),
  databasePath: Type.Optional(Type.String({ minLength: 1 })),
  workspaceRoot: Type.Optional(Type.String({ minLength: 1 })),
  pythonExecutable: Type.Optional(Type.String({ minLength: 1 })),
}, { additionalProperties: false });

export default definePluginEntry({
  id: "octane",
  name: "Octane",
  description: "Persistent, provenance-aware project intelligence and investigation tools for OpenClaw.",
  configSchema,
  register(api) {
    for (const definition of tools) {
      api.registerTool((toolContext) => ({
        name: definition.name,
        ...(definition.catalogMode && { catalogMode: definition.catalogMode }),
        description: definition.description,
        parameters: definition.parameters,
        async execute(_toolCallId, params) {
          const operation = definition.operation(params);
          if (!operation) throw new Error(`Unsupported ${definition.name} action.`);
          const result = await callBridge(api.pluginConfig || {}, operation, params, toolContext);
          const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
          return { content: [{ type: "text", text }], details: { result } };
        },
      }), {
        name: definition.name,
        optional: true,
        promptGuidance: `## Octane\n${definition.guidance}`,
      });
    }
    const hookPolicy = api.config?.plugins?.entries?.octane?.hooks;
    if (hookPolicy?.allowConversationAccess === true && hookPolicy.allowPromptInjection !== false) {
      api.on("before_prompt_build", (_event, context) => {
        if (context.toolAuthority?.allows("octane_context")) {
          return { prependContext: `## Octane repair workflow\n${repairGuidance}` };
        }
      }, { requiresToolAuthority: true });
    }
    api.logger.info(`Registered ${tools.length} optional Octane tools`);
  },
});
