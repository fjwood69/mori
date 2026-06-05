/**
 * mori-context-hook.mjs — Mori cross-platform Claude Code SessionStart hook (Node ESM)
 *
 * Re-grounds the agent at session boundaries using the SANCTIONED SessionStart
 * mechanism documented by Claude Code — NOT a PostCompact/sentinel workaround.
 * (PostCompact is observability-only and cannot inject context; SessionStart
 * re-fires after a compaction with source="compact" and supports additionalContext.)
 *
 * Wired to the SessionStart event. Branches on the `source` field:
 *
 *   source === "compact":
 *     Fires when the session resumes after a context compaction. Emits a nudge to
 *     run /brief --post-compact for a lightweight delta re-ground.
 *     Disable with MORI_POST_COMPACT_BRIEF=false.
 *
 *   source === "startup" | "resume" | "clear":
 *     New / resumed / cleared session. If MORI_SESSION_CONTEXT_FILE points at a
 *     readable file, injects its contents as additionalContext. Public default:
 *     the env var is unset, so nothing is injected. Private deployments point it
 *     at an operational-context file (kept out of this public repo).
 *
 * Health sentinel (runs before the above for startup/resume/clear):
 *   Checks whether the Mori server at --url is reachable. If not, injects a
 *   setup guide instead of the normal context and exits. This prevents a raw
 *   connection error from being the user's first experience.
 *   - "down"         → injects SETUP_MESSAGE
 *   - "unconfigured" → injects UNCONFIGURED_MESSAGE
 *   - "up"           → falls through to normal behaviour
 *   Result is cached per session_id for 5 minutes (temp file) to avoid re-pinging.
 *
 * Design:
 *   - SessionStart fires exactly once per boundary, so there is NO per-prompt
 *     wallpaper risk and no throttle is needed (unlike a UserPromptSubmit hook).
 *   - Output cap is 10,000 chars (harness limit); a context file over that is
 *     truncated by the harness — keep context files small.
 *   - Fail open: any error -> write nothing, exit 0. A crashing hook disables the
 *     entire event block, so robustness beats diagnostics here.
 *   - Node built-ins only (process, fs, fetch). No npm packages. ESM.
 *
 * Server URL resolution (in order):
 *   1. --url <value> argv (explicit; used by tests and the Cursor/Antigravity wrappers)
 *   2. MORI_SERVER_URL environment variable (the Claude Code plugin's config path)
 *   Empty → the health sentinel reports "unconfigured" and injects the setup guide.
 *
 * Invoked by the Claude Code harness as:
 *   node "${CLAUDE_PLUGIN_ROOT}/scripts/mori-context-hook.mjs"
 * with MORI_SERVER_URL exported in the environment.
 */

import { readFileSync, existsSync } from 'fs';
import { checkServer, getCached, setCached } from './lib/health-gate.mjs';
import { SETUP_MESSAGE, UNCONFIGURED_MESSAGE } from './lib/setup-message.mjs';

function emit(additionalContext) {
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'SessionStart',
        additionalContext,
      },
    }) + '\n',
  );
}

/** Parse --url <value> from argv. Returns '' if not found. */
function parseUrl(argv) {
  const idx = argv.indexOf('--url');
  if (idx !== -1 && idx + 1 < argv.length) return argv[idx + 1];
  return '';
}

async function main() {
  // Explicit --url wins (tests / wrappers); otherwise read the env var the plugin
  // sets. Empty string → health sentinel returns "unconfigured".
  const serverUrl = parseUrl(process.argv.slice(2)) || process.env.MORI_SERVER_URL || '';

  let raw = '';
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    raw = Buffer.concat(chunks).toString('utf8').trim();
  } catch {
    process.exit(0);
  }

  if (!raw) process.exit(0);

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    process.exit(0);
  }

  if ((payload.hook_event_name || '').trim() !== 'SessionStart') {
    process.exit(0);
  }

  const source = (payload.source || '').trim();

  if (source === 'compact') {
    if (process.env.MORI_POST_COMPACT_BRIEF === 'false') process.exit(0);
    emit(
      'Context was just compacted. Before doing anything else, run `/brief --post-compact` ' +
        'to re-ground — a lightweight delta of what changed in shared state since the last ' +
        'brief (new/superseded memories, pending mori-msg items, NATS traffic). Run it first, ' +
        'then continue.',
    );
    process.exit(0);
  }

  // ── Health sentinel (startup / resume / clear) ────────────────────────────
  //
  // Check the server before doing anything else. A cache hit (same session_id,
  // within 5 min) skips the fetch so repeated SessionStart events are cheap.
  //
  // MORI_SKIP_HEALTH_CHECK=1 bypasses the network check entirely (treats server
  // as "up"). Intended for tests and for private deployments where the server is
  // behind a VPN and the hook should not block on a flaky network check.

  const sessionId = payload.session_id || '';

  let healthState;
  if (process.env.MORI_SKIP_HEALTH_CHECK === '1') {
    healthState = 'up';
  } else {
    healthState = getCached(sessionId);
    if (!healthState) {
      healthState = await checkServer(serverUrl);
      setCached(sessionId, healthState);
    }
  }

  if (healthState === 'down') {
    emit(SETUP_MESSAGE);
    process.exit(0);
  }

  if (healthState === 'unconfigured') {
    emit(UNCONFIGURED_MESSAGE);
    process.exit(0);
  }

  // ── Server is up — normal behaviour ──────────────────────────────────────

  const ctxFile = process.env.MORI_SESSION_CONTEXT_FILE;
  if (ctxFile) {
    try {
      if (existsSync(ctxFile)) {
        const body = readFileSync(ctxFile, 'utf8').trim();
        if (body) emit(body);
      }
    } catch {
      process.exit(0);
    }
  }

  process.exit(0);
}

main().catch(() => process.exit(0));
