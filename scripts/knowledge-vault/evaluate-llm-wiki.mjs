#!/usr/bin/env node

import { spawn, execFileSync } from "node:child_process";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir, userInfo } from "node:os";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { performance } from "node:perf_hooks";

const DEFAULT_BASE_URL = "http://127.0.0.1:19828";
const DEFAULT_CASES = "evals/llm-wiki/cases.json";
const DEFAULT_MCP_COMMAND = resolve(
  process.env.CODEX_HOME || resolve(homedir(), ".codex"),
  "bin/llm-wiki-mcp",
);
const REQUEST_TIMEOUT_MS = 10_000;

function parseArgs(argv) {
  const options = {
    baseUrl: process.env.LLM_WIKI_API_BASE_URL || DEFAULT_BASE_URL,
    casesPath: DEFAULT_CASES,
    mcpCommand: DEFAULT_MCP_COMMAND,
    outputPath: null,
    transport: "both",
    legacyRemote: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = argv[index + 1];
    if (arg === "--base-url" && next) options.baseUrl = next, index += 1;
    else if (arg === "--cases" && next) options.casesPath = next, index += 1;
    else if (arg === "--mcp-command" && next) options.mcpCommand = next, index += 1;
    else if (arg === "--output" && next) options.outputPath = next, index += 1;
    else if (arg === "--transport" && next) options.transport = next, index += 1;
    else if (arg === "--legacy-remote") options.legacyRemote = true;
    else throw new Error(`Unknown or incomplete argument: ${arg}`);
  }

  if (!["api", "mcp", "both"].includes(options.transport)) {
    throw new Error("--transport must be api, mcp, or both");
  }
  return options;
}

function loadSuite(casesPath) {
  const suite = JSON.parse(readFileSync(resolve(casesPath), "utf8"));
  if (suite.schemaVersion !== 1 || !Array.isArray(suite.cases)) {
    throw new Error("Unsupported or invalid evaluation case schema");
  }
  return suite;
}

function resolveToken(keychainService) {
  if (process.env.LLM_WIKI_API_TOKEN?.trim()) {
    return { source: "env", value: process.env.LLM_WIKI_API_TOKEN.trim() };
  }
  if (process.platform !== "darwin") {
    throw new Error("Set LLM_WIKI_API_TOKEN before running API evaluation");
  }
  const value = execFileSync(
    "security",
    [
      "find-generic-password",
      "-a",
      userInfo().username,
      "-s",
      keychainService,
      "-w",
    ],
    { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
  ).trim();
  if (!value) throw new Error("LLM Wiki API token is missing from Keychain");
  return { source: "keychain", value };
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  const body = await response.json();
  if (!response.ok || body.ok === false) {
    throw new Error(`LLM Wiki API ${response.status}: ${body.error || response.statusText}`);
  }
  return body;
}

function percentile(values, ratio) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * ratio) - 1)];
}

function gradeCase(definition, paths) {
  if (definition.expectedNoResults === true) {
    return { passed: paths.length === 0, recallAtK: paths.length === 0 ? 1 : 0 };
  }
  const expected = definition.expectedPaths || [];
  const matched = expected.filter((expectedPath) => paths.includes(expectedPath));
  const forbidden = (definition.forbiddenPaths || []).filter((path) => paths.includes(path));
  const recallAtK = expected.length === 0 ? null : matched.length / expected.length;
  return {
    passed: expected.length > 0 && matched.length === expected.length && forbidden.length === 0,
    recallAtK,
    missingPaths: expected.filter((path) => !paths.includes(path)),
    forbiddenHits: forbidden,
  };
}

async function evaluateTransport(name, activeCases, search) {
  const results = [];
  for (const definition of activeCases) {
    const startedAt = performance.now();
    const response = await search(definition);
    const latencyMs = performance.now() - startedAt;
    const grade = gradeCase(definition, response.paths);
    results.push({
      id: definition.id,
      passed: grade.passed,
      recallAtK: grade.recallAtK,
      missingPaths: grade.missingPaths || [],
      forbiddenHits: grade.forbiddenHits || [],
      resultPaths: response.paths,
      latencyMs: Number(latencyMs.toFixed(2)),
      payloadChars: response.payloadChars,
      mode: response.mode || null,
    });
  }

  const latencies = results.map((result) => result.latencyMs);
  return {
    name,
    passed: results.every((result) => result.passed),
    passedCases: results.filter((result) => result.passed).length,
    failedCases: results.filter((result) => !result.passed).length,
    latencyMs: {
      p50: percentile(latencies, 0.5),
      p95: percentile(latencies, 0.95),
    },
    payloadChars: results.reduce((sum, result) => sum + result.payloadChars, 0),
    results,
  };
}

async function createApiTransport(options, suite) {
  const token = resolveToken(suite.keychainService || "com.llmwiki.api");
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  const headers = {
    Accept: "application/json",
    Authorization: `Bearer ${token.value}`,
    "Content-Type": "application/json",
  };
  const health = await fetchJson(`${baseUrl}/api/v1/health`);
  if (!health.authConfigured || !health.mcpEnabled) {
    throw new Error("LLM Wiki API token or MCP access is not enabled in the desktop app");
  }

  return {
    connection: {
      authSource: token.source,
      status: health.status,
      version: health.version,
      mcpEnabled: health.mcpEnabled,
      allowUnauthenticated: health.allowUnauthenticated,
    },
    search: async (definition) => {
      const body = await fetchJson(
        `${baseUrl}/api/v1/projects/${encodeURIComponent(suite.projectId || "current")}/search`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({
            query: definition.query,
            topK: definition.topK,
            includeContent: false,
          }),
        },
      );
      return {
        paths: (body.results || []).map((result) => result.path),
        payloadChars: JSON.stringify(body).length,
        mode: body.mode,
      };
    },
  };
}

class McpClient {
  constructor(command) {
    this.command = command;
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = "";
    this.stderr = "";
  }

  async connect() {
    this.child = spawn(this.command, [], { stdio: ["pipe", "pipe", "pipe"] });
    this.child.stdout.setEncoding("utf8");
    this.child.stderr.setEncoding("utf8");
    this.child.stdout.on("data", (chunk) => this.onData(chunk));
    this.child.stderr.on("data", (chunk) => {
      this.stderr = `${this.stderr}${chunk}`.slice(-2000);
    });
    this.child.on("exit", (code) => {
      for (const { reject } of this.pending.values()) {
        reject(new Error(`MCP process exited with code ${code}: ${this.stderr.trim()}`));
      }
      this.pending.clear();
    });
    await this.request("initialize", {
      protocolVersion: "2025-11-25",
      capabilities: {},
      clientInfo: { name: "woon-knowledge-evaluator", version: "1.0.0" },
    });
    this.notify("notifications/initialized", {});
  }

  onData(chunk) {
    this.buffer += chunk;
    let newlineIndex = this.buffer.indexOf("\n");
    while (newlineIndex >= 0) {
      const line = this.buffer.slice(0, newlineIndex).trim();
      this.buffer = this.buffer.slice(newlineIndex + 1);
      if (line) {
        const message = JSON.parse(line);
        const pending = this.pending.get(message.id);
        if (pending) {
          clearTimeout(pending.timeout);
          this.pending.delete(message.id);
          if (message.error) pending.reject(new Error(message.error.message));
          else pending.resolve(message.result);
        }
      }
      newlineIndex = this.buffer.indexOf("\n");
    }
  }

  request(method, params) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolveRequest, rejectRequest) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        rejectRequest(new Error(`MCP request timed out: ${method}`));
      }, REQUEST_TIMEOUT_MS);
      this.pending.set(id, { resolve: resolveRequest, reject: rejectRequest, timeout });
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    });
  }

  notify(method, params) {
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method, params })}\n`);
  }

  async close() {
    if (!this.child) return;
    this.child.stdin.end();
    await new Promise((resolveClose) => {
      const timeout = setTimeout(() => this.child.kill(), 1000);
      this.child.once("exit", () => {
        clearTimeout(timeout);
        resolveClose();
      });
    });
  }
}

function textContent(toolResult) {
  return (toolResult.content || [])
    .filter((content) => content.type === "text")
    .map((content) => content.text)
    .join("\n");
}

function searchPathsFromMcp(text) {
  return [...text.matchAll(/^Path: (.+)$/gm)].map((match) => match[1].trim());
}

async function createMcpTransport(options) {
  const client = new McpClient(options.mcpCommand);
  await client.connect();
  const listed = await client.request("tools/list", {});
  const statusResult = await client.request("tools/call", {
    name: "llm_wiki_status",
    arguments: {},
  });
  const status = JSON.parse(textContent(statusResult));
  if (!status.mcpEnabled || !status.authConfigured) {
    await client.close();
    throw new Error("MCP reached LLM Wiki, but authentication or MCP access is disabled");
  }

  return {
    client,
    connection: {
      toolCount: listed.tools?.length || 0,
      status: status.status,
      version: status.version,
      projectName: status.currentProject?.name || null,
    },
    search: async (definition) => {
      const result = await client.request("tools/call", {
        name: "llm_wiki_search",
        arguments: {
          project_id: "current",
          query: definition.query,
          top_k: definition.topK,
          include_content: false,
        },
      });
      const text = textContent(result);
      const modeMatch = text.match(/^Mode: ([^|\n]+)/m);
      return {
        paths: searchPathsFromMcp(text),
        payloadChars: text.length,
        mode: modeMatch?.[1]?.trim() || null,
      };
    },
  };
}

function compareTransports(apiReport, mcpReport) {
  if (!apiReport || !mcpReport) return null;
  const mcpById = new Map(mcpReport.results.map((result) => [result.id, result]));
  const cases = apiReport.results.map((apiResult) => {
    const mcpResult = mcpById.get(apiResult.id);
    const samePaths = JSON.stringify(apiResult.resultPaths) === JSON.stringify(mcpResult?.resultPaths);
    return { id: apiResult.id, samePaths };
  });
  return { passed: cases.every((item) => item.samePaths), cases };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.legacyRemote) {
    throw new Error(
      "This is a historical remote API/MCP evaluator. Run the current local gate with " +
        "`woon knowledge evaluate --vault . --cases evals/llm-wiki/current-retrieval-cases.json` " +
        "or pass --legacy-remote explicitly.",
    );
  }
  const suite = loadSuite(options.casesPath);
  const activeCases = suite.cases.filter((item) => item.status === "active");
  const pendingCases = suite.cases.filter((item) => item.status !== "active");
  const report = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    casesFile: options.casesPath,
    activeCases: activeCases.length,
    pendingCorpusCases: pendingCases.map((item) => item.id),
    connections: {},
    transports: {},
    parity: null,
    passed: false,
  };

  let mcpTransport;
  try {
    if (options.transport === "api" || options.transport === "both") {
      const apiTransport = await createApiTransport(options, suite);
      report.connections.api = apiTransport.connection;
      report.transports.api = await evaluateTransport("api", activeCases, apiTransport.search);
    }
    if (options.transport === "mcp" || options.transport === "both") {
      mcpTransport = await createMcpTransport(options);
      report.connections.mcp = mcpTransport.connection;
      report.transports.mcp = await evaluateTransport("mcp", activeCases, mcpTransport.search);
    }
    report.parity = compareTransports(report.transports.api, report.transports.mcp);
    const transportReports = Object.values(report.transports);
    report.passed =
      transportReports.length > 0 &&
      transportReports.every((transport) => transport.passed) &&
      (report.parity === null || report.parity.passed);
  } finally {
    await mcpTransport?.client.close();
  }

  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  if (options.outputPath) {
    const outputPath = resolve(options.outputPath);
    mkdirSync(dirname(outputPath), { recursive: true });
    writeFileSync(outputPath, serialized, "utf8");
  }
  process.stdout.write(serialized);
  process.exitCode = report.passed ? 0 : 1;
}

main().catch((error) => {
  console.error(JSON.stringify({ error: error.message }, null, 2));
  process.exitCode = 2;
});
