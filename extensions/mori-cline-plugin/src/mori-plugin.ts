import * as http from "http";
import * as https from "https";
import * as url from "url";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import * as crypto from "crypto";
import type {
  AgentRuntimePlugin,
  AgentRuntimePluginContext,
  AgentRuntimePluginSetup,
  AgentRunLifecycleContext,
  AgentBeforeModelContext,
  AgentAfterToolContext,
  AgentRunResult,
  AgentMessage,
} from "@cline/shared";

// ── Types ──────────────────────────────────────────────────────────────────

interface EventLogEntry {
  event_name: string;
  session_id: string;
  client: string;
  tool_name?: string;
  tool_input?: string;
  tool_output?: string;
  tool_error?: string;
  model?: string;
  cwd?: string;
  transcript_path?: string;
  prompt?: string;
  stop_reason?: string;
}

interface SpoolEntry {
  id: string;
  event: EventLogEntry;
  retries: number;
  firstFailedAt: string;
  lastRetryAt: string;
  lastError: string;
}

// ── Config ──────────────────────────────────────────────────────────────────

interface Config {
  apiUrl: string;
  apiKey: string;
  client: string;
}

function getConfig(): Config {
  return {
    apiUrl: process.env.MORI_API_URL ?? "http://localhost:8968",
    apiKey: process.env.MORI_API_KEY ?? "",
    client: process.env.MORI_CLIENT ?? os.hostname(),
  };
}

// ── HTTP helper ────────────────────────────────────────────────────────────

interface ShipResult {
  ok: boolean;
  statusCode?: number;
  error?: string;
}

function shipEvent(baseUrl: string, apiKey: string, entry: EventLogEntry): Promise<ShipResult> {
  return new Promise((resolve) => {
    const endpoint = `${baseUrl.replace(/\/+$/, "")}/api/events/raw`;
    const parsed = url.parse(endpoint);
    const isHttps = parsed.protocol === "https:";
    const transport = isHttps ? https : http;

    const queryClient = entry.client ? `?client=${encodeURIComponent(entry.client)}` : "";

    const body = JSON.stringify(entry);
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body).toString(),
    };
    if (apiKey) {
      headers["X-Api-Key"] = apiKey;
    }

    const options: http.RequestOptions = {
      hostname: parsed.hostname,
      port: parsed.port ? parseInt(parsed.port, 10) : isHttps ? 443 : 80,
      path: `${parsed.pathname}${queryClient}`,
      method: "POST",
      headers,
      timeout: 10000,
    };

    const req = transport.request(options, (res) => {
      let data = "";
      res.on("data", (chunk: Buffer) => (data += chunk.toString()));
      res.on("end", () => {
        if (res.statusCode === 202) {
          resolve({ ok: true, statusCode: res.statusCode });
        } else {
          resolve({ ok: false, statusCode: res.statusCode, error: data });
        }
      });
    });

    req.on("error", (err: Error) => {
      resolve({ ok: false, error: err.message });
    });

    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, error: "timeout" });
    });

    req.write(body);
    req.end();
  });
}

// ── Spooler ────────────────────────────────────────────────────────────────

const SPOOL_DIR = path.join(os.homedir(), ".mori", "queue");
const DEAD_DIR = path.join(os.homedir(), ".mori", "dead");
const MAX_RETRIES = 10;

const BACKOFFS = [10_000, 30_000, 60_000, 120_000, 300_000, 600_000];

function backoff(retries: number): number {
  if (retries >= BACKOFFS.length) return 600_000;
  return BACKOFFS[retries];
}

class Spooler {
  private timers = new Map<string, NodeJS.Timeout>();
  private onRetry: (entry: EventLogEntry) => Promise<boolean>;

  constructor(onRetry: (entry: EventLogEntry) => Promise<boolean>) {
    this.onRetry = onRetry;
    this.ensureDirs();
  }

  private ensureDirs(): void {
    fs.mkdirSync(SPOOL_DIR, { recursive: true });
    fs.mkdirSync(DEAD_DIR, { recursive: true });
  }

  /** Write a failed event to the spool queue and schedule retry. */
  enqueue(entry: EventLogEntry, error: string): void {
    const id = crypto.randomUUID();
    const spool: SpoolEntry = {
      id,
      event: entry,
      retries: 0,
      firstFailedAt: new Date().toISOString(),
      lastRetryAt: new Date().toISOString(),
      lastError: error,
    };
    fs.writeFileSync(path.join(SPOOL_DIR, `${id}.json`), JSON.stringify(spool, null, 2), "utf-8");
    this.scheduleRetry(id, 0);
  }

  /** Replay all pending spools (called on session start). */
  replayAll(): void {
    let files: string[];
    try {
      files = fs.readdirSync(SPOOL_DIR);
    } catch {
      return;
    }

    for (const file of files) {
      if (!file.endsWith(".json")) continue;
      const filePath = path.join(SPOOL_DIR, file);
      try {
        const raw = fs.readFileSync(filePath, "utf-8");
        const spool: SpoolEntry = JSON.parse(raw);
        const id = spool.id || file.replace(".json", "");
        this.scheduleRetry(id, spool.retries);
      } catch {
        try {
          fs.renameSync(filePath, path.join(DEAD_DIR, file));
        } catch {
          // best effort
        }
      }
    }
  }

  /** Clear all pending retry timers (called on session end). */
  dispose(): void {
    for (const [, timer] of this.timers) {
      clearTimeout(timer);
    }
    this.timers.clear();
  }

  private scheduleRetry(id: string, currentRetries: number): void {
    if (currentRetries >= MAX_RETRIES) {
      this.moveToDead(id);
      return;
    }

    const delay = backoff(currentRetries);
    const timer = setTimeout(async () => {
      this.timers.delete(id);
      await this.retryOne(id, currentRetries);
    }, delay);
    this.timers.set(id, timer);
  }

  private async retryOne(id: string, currentRetries: number): Promise<void> {
    const filePath = path.join(SPOOL_DIR, `${id}.json`);
    let spool: SpoolEntry;
    try {
      const raw = fs.readFileSync(filePath, "utf-8");
      spool = JSON.parse(raw);
    } catch {
      return;
    }

    spool.retries = currentRetries + 1;
    spool.lastRetryAt = new Date().toISOString();

    try {
      const ok = await this.onRetry(spool.event);
      if (ok) {
        fs.unlinkSync(filePath);
        return;
      }
    } catch (err) {
      spool.lastError = String(err);
    }

    fs.writeFileSync(filePath, JSON.stringify(spool, null, 2), "utf-8");
    this.scheduleRetry(id, spool.retries);
  }

  private moveToDead(id: string): void {
    const src = path.join(SPOOL_DIR, `${id}.json`);
    const dst = path.join(DEAD_DIR, `${id}.json`);
    try {
      fs.renameSync(src, dst);
    } catch {
      // best effort
    }
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

/** Extract the full text from a user message's content parts. */
function extractPrompt(message: AgentMessage): string {
  if (!Array.isArray(message.content)) return "";
  return message.content
    .filter((p): p is { type: "text"; text: string } => p.type === "text")
    .map((p) => p.text)
    .join("\n");
}

/** Find the last user message from a messages array. */
function lastUserMessage(messages: readonly AgentMessage[]): AgentMessage | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "user") return messages[i];
  }
  return undefined;
}

/** Fire-and-forget POST to trigger dream pipeline on the mori server. */
function triggerDream(baseUrl: string): void {
  const endpoint = `${baseUrl.replace(/\/+$/, "")}/api/dream/run`;
  const parsed = url.parse(endpoint);
  const isHttps = parsed.protocol === "https:";
  const transport = isHttps ? https : http;

  const options: http.RequestOptions = {
    hostname: parsed.hostname,
    port: parsed.port ? parseInt(parsed.port, 10) : isHttps ? 443 : 80,
    path: parsed.pathname,
    method: "POST",
    timeout: 5000,
  };

  const req = transport.request(options, () => {});
  req.on("error", () => {});
  req.on("timeout", () => req.destroy());
  req.end();
}

/** Ship an event fire-and-forget; spool on failure. */
function shipAndSpool(entry: EventLogEntry, spooler: Spooler): void {
  const cfg = getConfig();
  shipEvent(cfg.apiUrl, cfg.apiKey, entry)
    .then((result) => {
      if (!result.ok) spooler.enqueue(entry, result.error ?? "unknown");
    })
    .catch((err: unknown) => spooler.enqueue(entry, String(err)));
}

// ── Plugin Registration ────────────────────────────────────────────────────

const moriPlugin: AgentRuntimePlugin = {
  name: "mori",

  setup: (_ctx: AgentRuntimePluginContext): AgentRuntimePluginSetup => {
    const spooler = new Spooler(async (entry) => {
      const cfg = getConfig();
      const result = await shipEvent(cfg.apiUrl, cfg.apiKey, entry);
      return result.ok;
    });

    return {
      hooks: {
        // Session start — replay any events that failed to ship previously
        beforeRun: (_ctx: AgentRunLifecycleContext) => {
          spooler.replayAll();
          return undefined;
        },

        // Before model call — capture the user prompt from the pending request
        beforeModel: (ctx: AgentBeforeModelContext) => {
          const sessionId =
            ctx.snapshot?.conversationId ?? ctx.snapshot?.agentId ?? "";
          const messages = ctx.request?.messages;

          if (messages?.length) {
            const userMsg = lastUserMessage(messages);
            if (userMsg) {
              const prompt = extractPrompt(userMsg);
              if (prompt) {
                const entry: EventLogEntry = {
                  event_name: "UserPromptSubmit",
                  session_id: sessionId,
                  client: getConfig().client,
                  prompt,
                };
                shipAndSpool(entry, spooler);
              }
            }
          }

          return undefined;
        },

        // After tool execution — capture tool use event
        afterTool: (ctx: AgentAfterToolContext) => {
          const sessionId =
            ctx.snapshot?.conversationId ?? ctx.snapshot?.agentId ?? "";
          const toolOutput = ctx.result?.output;
          const entry: EventLogEntry = {
            event_name: "PostToolUse",
            session_id: sessionId,
            client: getConfig().client,
            tool_name: ctx.tool?.name ?? ctx.toolCall?.toolName,
            tool_input: JSON.stringify(ctx.input ?? {}),
            tool_output:
              toolOutput !== undefined
                ? typeof toolOutput === "string"
                  ? toolOutput
                  : JSON.stringify(toolOutput)
                : undefined,
            tool_error: ctx.result?.isError ? "Tool returned error" : undefined,
          };
          shipAndSpool(entry, spooler);
          return undefined;
        },

        // Session end — send Stop event and trigger dream flush
        afterRun: (ctx: AgentRunLifecycleContext & { result: AgentRunResult }) => {
          const sessionId =
            ctx.snapshot?.conversationId ?? ctx.snapshot?.agentId ?? "";
          const entry: EventLogEntry = {
            event_name: "Stop",
            session_id: sessionId,
            client: getConfig().client,
            stop_reason: ctx.result?.status ?? "completed",
          };
          shipAndSpool(entry, spooler);

          triggerDream(getConfig().apiUrl);
          spooler.dispose();
        },
      },
    };
  },
};

export default moriPlugin;