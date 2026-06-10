/**
 * Mori plugin for OpenCode
 *
 * Connects OpenCode sessions to a self-hosted Mori server:
 *   - Ships lifecycle events to the dream pipeline
 *   - Injects grounding context at session start and after compaction
 *   - Runs dream pipeline before context compaction (experimental hook)
 *
 * Required env vars:
 *   MORI_SERVER_URL  — base URL of your Mori server (e.g. http://10.0.0.10:8968)
 *   MORI_API_KEY     — bare secret from MORI_API_KEYS (not "name:secret")
 *
 * Optional:
 *   MORI_CLIENT      — override reported client hostname (defaults to $HOSTNAME)
 *   MORI_POST_COMPACT_BRIEF — set to "false" to suppress post-compact /brief nudge
 */

// Plugin context type — kept inline so no npm dependencies are required at runtime
type PluginContext = {
  project: string
  client?: string
  directory: string
  worktree?: string
  $?: unknown
}

// ── Helpers ─────────────────────────────────────────────────────────────────

const MORI_SERVER_URL = process.env.MORI_SERVER_URL ?? ""
const MORI_API_KEY = process.env.MORI_API_KEY ?? ""
const MORI_CLIENT = process.env.MORI_CLIENT
  ?? process.env.HOSTNAME
  ?? process.env.COMPUTERNAME
  ?? "opencode"

async function moriPost(path: string, body: Record<string, unknown>): Promise<unknown> {
  if (!MORI_SERVER_URL || !MORI_API_KEY) return null
  try {
    const res = await fetch(`${MORI_SERVER_URL}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": MORI_API_KEY,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    })
    return res.ok ? res.json() : null
  } catch {
    return null
  }
}

// ── Plugin ───────────────────────────────────────────────────────────────────

async function plugin({ directory }: PluginContext) {
  const sessionFired = new Set<string>()

  return {

    // ── Generic event router ────────────────────────────────────────────────
    // Catches session.created, session.compacted, session.idle, and any other
    // event not handled by a dedicated hook below.
    event: async ({ event }: { event: { type: string; properties?: Record<string, unknown> } }) => {
      const sid = (event.properties?.id as string) ?? "unknown"

      // Session start — ship event, trigger /brief nudge
      if (event.type === "session.created") {
        if (sessionFired.has(sid)) return
        sessionFired.add(sid)

        await moriPost("/api/events/raw", {
          type: "SessionStart",
          session_id: sid,
          client: MORI_CLIENT,
          cwd: directory,
          source: "session_created",
          ts: new Date().toISOString(),
        })
      }

      // Post-compaction re-grounding — nudge the agent to run /brief --post-compact
      if (event.type === "session.compacted") {
        await moriPost("/api/events/raw", {
          type: "PostCompact",
          session_id: sid,
          client: MORI_CLIENT,
          cwd: directory,
          source: "compact",
          ts: new Date().toISOString(),
        })
      }

      // Session idle / end — ship Stop event for dream ingestion
      if (event.type === "session.idle" || event.type === "session.removed") {
        await moriPost("/api/events/raw", {
          type: "Stop",
          session_id: sid,
          client: MORI_CLIENT,
          cwd: directory,
          ts: new Date().toISOString(),
        })
      }
    },

    // ── Tool capture → dream pipeline ───────────────────────────────────────
    "tool.execute.after": async (
      input: { sessionId?: string; tool?: string; args?: unknown },
      output: unknown
    ) => {
      const sid = input.sessionId ?? "unknown"
      const outputStr = typeof output === "string"
        ? output.slice(0, 2000)
        : JSON.stringify(output ?? "").slice(0, 2000)

      // Fire and forget — must never block tool execution
      moriPost("/api/events/raw", {
        type: "PostToolUse",
        session_id: sid,
        client: MORI_CLIENT,
        cwd: directory,
        tool: input.tool,
        input: input.args,
        output: outputStr,
        ts: new Date().toISOString(),
      }).catch(() => {})
    },

    // ── Pre-compaction (experimental) ───────────────────────────────────────
    // experimental.session.compacting fires before the LLM generates the
    // compaction summary. output.context.push() injects text directly into
    // what the LLM sees — the most powerful grounding point available.
    //
    // This hook is experimental in OpenCode and may not fire on all versions.
    // The plugin degrades gracefully if it is unavailable.
    "experimental.session.compacting": async (
      input: { sessionId?: string },
      output: { context?: { push: (s: string) => void }; prompt?: string }
    ) => {
      const sid = input.sessionId ?? "unknown"

      // 1. Trigger dream pipeline before compression captures the session state
      await moriPost("/api/dream/run", {
        session_id: sid,
        client: MORI_CLIENT,
        trigger: "precompact",
      })

      // 2. Inject re-grounding instruction into compaction context
      const suppressBrief = process.env.MORI_POST_COMPACT_BRIEF === "false"
      if (!suppressBrief && output?.context && typeof output.context.push === "function") {
        output.context.push(
          "\n## Mori shared memory — carry forward\n\n" +
          "Your shared memory store may have changed while this session was active.\n" +
          "After this compaction is complete, run `/brief --post-compact` immediately\n" +
          "to re-ground yourself on any memories added, superseded, or evicted since\n" +
          "your last brief. This is a lightweight delta check — not a full reload.\n"
        )
      }
    },

    // ── Shell env injection (experimental) ──────────────────────────────────
    // Injects MORI_SERVER_URL, MORI_API_KEY, and MORI_CLIENT into all shell
    // sessions spawned by OpenCode. Useful for sub-agents and shell tools.
    "shell.env": async (
      _input: unknown,
      output: { env?: Record<string, string> }
    ) => {
      if (!output?.env) return
      if (MORI_SERVER_URL) output.env.MORI_SERVER_URL = MORI_SERVER_URL
      if (MORI_API_KEY) output.env.MORI_API_KEY = MORI_API_KEY
      output.env.MORI_CLIENT = MORI_CLIENT
    },
  }
}

export default plugin
