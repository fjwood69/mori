/**
 * mori-context-hook-cursor.mjs — Mori context hook for Cursor (Node ESM)
 *
 * Fires on the Cursor `sessionStart` event. If MORI_SESSION_CONTEXT_FILE is set
 * to a readable file, emits { "additional_context": "<file contents>" } on stdout
 * so Cursor injects it as context at session start.
 *
 * Cursor has no compaction-inject point (no `source` field equivalent), so this
 * hook only handles the startup / new-session branch — no post-compact nudge.
 *
 * Health sentinel:
 *   On a confirmed sessionStart event, checks whether the Mori server at --url is
 *   reachable before injecting context. If not, injects a setup guide instead.
 *   Result is cached per conversation_id for 5 minutes to avoid re-pinging.
 *   - "down"         → { "additional_context": SETUP_MESSAGE }
 *   - "unconfigured" → { "additional_context": UNCONFIGURED_MESSAGE }
 *   - "up"           → falls through to normal context injection
 *
 * Always exits 0 (fail-open). Any error → no output, exit 0.
 *
 * Usage (wired by install-hooks-cursor.mjs into ~/.cursor/hooks.json):
 *   node /absolute/path/to/mori-context-hook-cursor.mjs --url <server_url>
 *
 * Environment:
 *   MORI_SESSION_CONTEXT_FILE    — path to a file whose contents are injected.
 *                                  Unset by default; nothing is injected on a stock install.
 *   MORI_SKIP_HEALTH_CHECK=1     — bypass the network check (treat server as "up").
 *                                  For tests and VPN-gated deployments.
 *
 * Output (Cursor sessionStart additional_context contract):
 *   { "additional_context": "..." }  — when context is available (ctx file or setup msg)
 *   (empty / no output)              — otherwise
 */

import { readFileSync, existsSync } from 'fs';
import { runFailOpen } from './lib/fail-open.mjs';
import { checkServer, getCached, setCached } from './lib/health-gate.mjs';
import { SETUP_MESSAGE, UNCONFIGURED_MESSAGE } from './lib/setup-message.mjs';

/** Parse --url <value> from argv. Returns '' if not found. */
function parseUrl(argv) {
  const idx = argv.indexOf('--url');
  if (idx !== -1 && idx + 1 < argv.length) return argv[idx + 1];
  return '';
}

async function main() {
  const serverUrl = parseUrl(process.argv.slice(2));

  // Read and parse stdin (required for well-behaved hooks; we only use it to
  // verify we're on a sessionStart event — Cursor passes the event JSON on stdin).
  let raw = '';
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    raw = Buffer.concat(chunks).toString('utf8').trim();
  } catch {
    process.exit(0);
  }

  // If no stdin, nothing to do — exit cleanly (preserve legacy behaviour).
  if (!raw) process.exit(0);

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    process.exit(0);
  }

  // Cursor delivers hook_event_name in snake_case.
  // Only act on sessionStart; exit silently for all other events.
  const eventName = (payload.hook_event_name || '').toLowerCase();
  if (eventName && eventName !== 'sessionstart') {
    process.exit(0);
  }

  // ── Health sentinel (confirmed sessionStart) ──────────────────────────────
  //
  // Check the server before injecting context. A cache hit (same conversation_id,
  // within 5 min) skips the fetch so repeated fires are cheap.
  //
  // MORI_SKIP_HEALTH_CHECK=1 bypasses the network check (treats server as "up").
  // For tests and private deployments where the VPN path should not gate startup.

  const sessionId = payload.conversation_id || payload.session_id || '';

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
    process.stdout.write(JSON.stringify({ additional_context: SETUP_MESSAGE }) + '\n');
    process.exit(0);
  }

  if (healthState === 'unconfigured') {
    process.stdout.write(JSON.stringify({ additional_context: UNCONFIGURED_MESSAGE }) + '\n');
    process.exit(0);
  }

  // ── Server is up — inject session context file if configured ──────────────

  const ctxFile = process.env.MORI_SESSION_CONTEXT_FILE;
  if (ctxFile) {
    try {
      if (existsSync(ctxFile)) {
        const body = readFileSync(ctxFile, 'utf8').trim();
        if (body) {
          process.stdout.write(JSON.stringify({ additional_context: body }) + '\n');
        }
      }
    } catch {
      process.exit(0);
    }
  }

  process.exit(0);
}

runFailOpen(main);
