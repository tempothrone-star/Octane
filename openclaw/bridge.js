import { spawn } from "node:child_process";
import { realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const PLUGIN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const MAX_REQUEST_BYTES = 1_000_000;
const MAX_RESPONSE_BYTES = 8_000_000;
const DEFAULT_BRIDGE_TIMEOUT_MS = 120_000;

export function bridgeTimeoutFor(operation, params) {
  const bounded = (value, fallback, max) => Number.isInteger(value) ? Math.min(max, Math.max(1, value)) : fallback;
  if (operation === "workspace_mutate") return bounded(params?.mutationLimit, 5, 20) * 40_000 + 10_000;
  if (operation === "workspace_bisect") return bounded(params?.bisectBudget, 20, 100) * 70_000 + 10_000;
  if (operation === "workspace_evaluate") return bounded(params?.caseLimit, 5, 100) * 280_000 + 10_000;
  return (operation === "workspace_run" || operation === "workspace_property") && Number.isInteger(params?.timeoutSeconds)
    ? Math.min(300, Math.max(1, params.timeoutSeconds)) * 1000 + 10_000
    : DEFAULT_BRIDGE_TIMEOUT_MS;
}

function resolveConfig(config) {
  const roots = config.allowedProjectRoots;
  if (!Array.isArray(roots) || roots.length === 0 || roots.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error("Octane is deny-by-default: configure at least one allowedProjectRoots path.");
  }
  const allowedRoots = [...new Set(roots.map((item) => {
    if (!isAbsolute(item)) throw new Error(`Allowed project roots must be absolute paths: ${item}`);
    const root = realpathSync(resolve(item));
    if (!statSync(root).isDirectory()) throw new Error(`Allowed project root is not a directory: ${root}`);
    return root;
  }))];
  const stateDir = process.env.OPENCLAW_STATE_DIR || join(homedir(), ".openclaw");
  const databasePath = resolve(config.databasePath || join(stateDir, "octane", "pil.sqlite3"));
  const workspaceRoot = resolve(config.workspaceRoot || join(stateDir, "octane", "workspaces"));
  const allowedImages = config.allowedDockerImages ?? [];
  if (!Array.isArray(allowedImages) || allowedImages.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error("allowedDockerImages must be an array of explicit Docker image references.");
  }
  if (config.allowAgentProvisioning !== undefined && typeof config.allowAgentProvisioning !== "boolean") {
    throw new Error("allowAgentProvisioning must be a boolean.");
  }
  if (config.allowDefaultContainerUser !== undefined && typeof config.allowDefaultContainerUser !== "boolean") {
    throw new Error("allowDefaultContainerUser must be a boolean.");
  }
  const python = typeof config.pythonExecutable === "string" && config.pythonExecutable.trim()
    ? config.pythonExecutable
    : process.platform === "win32" ? "python" : "python3";
  return { allowedRoots, databasePath, workspaceRoot, allowedImages: [...new Set(allowedImages)],
    allowAgentProvisioning: config.allowAgentProvisioning === true,
    allowDefaultContainerUser: config.allowDefaultContainerUser === true,
    languageServers: config.languageServers || {},
    containerEngine: config.containerEngine || "docker", containerRuntime: config.containerRuntime, python };
}

export function callBridge(config, operation, params, context = {}) {
  const resolved = resolveConfig(config);
  const usesCheckout = operation === "repair_triage" || operation === "repair_focus";
  const request = Buffer.from(JSON.stringify({ operation, params, context: {
    agentId: context.agentId,
    workspaceDir: usesCheckout ? context.workspaceDir : undefined,
    sandboxed: usesCheckout ? context.sandboxed : undefined,
  } }), "utf8");
  if (request.byteLength > MAX_REQUEST_BYTES) throw new Error("Octane request exceeds the 1 MB limit.");
  const args = ["-m", "octane.openclaw_bridge", "--database", resolved.databasePath,
    "--workspace-root", resolved.workspaceRoot];
  for (const root of resolved.allowedRoots) args.push("--allowed-root", root);
  for (const image of resolved.allowedImages) args.push("--allowed-image", image);
  if (resolved.allowAgentProvisioning) args.push("--allow-agent-provisioning");
  if (resolved.allowDefaultContainerUser) args.push("--allow-default-container-user");
  args.push("--language-servers", JSON.stringify(resolved.languageServers));
  args.push("--container-engine", resolved.containerEngine);
  if (resolved.containerRuntime) args.push("--container-runtime", resolved.containerRuntime);
  const priorPath = process.env.PYTHONPATH;
  const pythonPath = [PLUGIN_ROOT, priorPath].filter(Boolean).join(delimiter);

  const bridgeTimeoutMs = bridgeTimeoutFor(operation, params);
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(resolved.python, args, {
      cwd: PLUGIN_ROOT,
      env: { ...process.env, PYTHONPATH: pythonPath, PYTHONDONTWRITEBYTECODE: "1" },
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let overflow = false;
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      const forceKill = setTimeout(() => child.kill("SIGKILL"), 1000);
      forceKill.unref?.();
    }, bridgeTimeoutMs);
    timeout.unref?.();
    child.stdout.on("data", (chunk) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > MAX_RESPONSE_BYTES) {
        overflow = true;
        child.kill();
      } else stdout.push(chunk);
    });
    child.stderr.on("data", (chunk) => {
      if (stderrBytes < 16_384) {
        const accepted = chunk.subarray(0, 16_384 - stderrBytes);
        stderr.push(accepted);
        stderrBytes += accepted.length;
      }
    });
    child.once("error", (error) => {
      clearTimeout(timeout);
      rejectPromise(new Error(`Could not start Octane's Python bridge: ${error.message}`));
    });
    child.once("close", (code) => {
      clearTimeout(timeout);
      if (timedOut) return rejectPromise(new Error(`Octane bridge timed out after ${Math.ceil(bridgeTimeoutMs / 1000)} seconds.`));
      if (overflow) return rejectPromise(new Error("Octane response exceeded the 8 MB limit."));
      const out = Buffer.concat(stdout).toString("utf8");
      if (code !== 0 && !out.trim()) {
        const detail = Buffer.concat(stderr).toString("utf8").trim();
        return rejectPromise(new Error(`Octane bridge exited with code ${code}${detail ? `: ${detail}` : ""}`));
      }
      let response;
      try { response = JSON.parse(out); }
      catch { return rejectPromise(new Error("Octane bridge returned invalid JSON.")); }
      if (!response.ok) {
        const error = response.error || {};
        return rejectPromise(new Error(`${error.type || "OctaneError"}: ${error.message || "operation failed"}`));
      }
      resolvePromise(response.result);
    });
    child.stdin.on("error", () => {});
    child.stdin.end(request);
  });
}
