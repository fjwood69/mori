/**
 * mori-context-hook-antigravity.mjs — Mori context hook for Antigravity (Node ESM)
 *
 * Fires on the Antigravity `PreInvocation` event (fires before every model call).
 * On the first call for a given conversationId, if MORI_SESSION_CONTEXT_FILE is
 * set to a readable file, returns:
 *   { "injectSteps": [ { "ephemeralMessage": "<file contents>" } ] }
 *
 * On all subsequent calls for the same conversationId (throttled via a temp flag),
 * or if the env var is not set, returns:
 *   { "injectSteps": [] }
 *
 * The throttle is per-conversation (using lib/throttle.mjs) so context is injected
 * exactly once per conversation, not on every model invocation.
 *
 * Health sentinel:
 *   On the first invocation for a conversationId, before injecting the session
 *   context file, checks whether the Mori server at --url is reachable. If not,
 *   injects a setup guide instead. The health result is cached per conversationId
 *   for 5 minutes so subsequent PreInvocation calls (after the throttle clears)
 *   do not re-ping.
 *   - "down"         → { "injectSteps": [{ "ephemeralMessage": SETUP_MESSAGE }] }
 *   - "unconfigured" → { "injectSteps": [{ "ephemeralMessage": UNCONFIGURED_MESSAGE }] }
 *   - "up"           → falls through to normal context injection
 *
 * Always exits 0 (fail-open). Any error → { "injectSteps": [] }, exit 0.
 *
 * Usage (wired by install-hooks-antigravity.mjs into ~/.gemini/config/hooks.json):
 *   node /abs/path/mori-context-hook-antigravity.mjs --url <server_url>
 *
 * Antigravity PreInvocation input (camelCase, from stdin):
 *   conversationId     string   Unique conversation identifier
 *   transcriptPath     string   Path to transcript (may be absent on PreInvocation)
 *   stepIdx            number   Model call step index within conversation
 *
 * Environment:
 *   MORI_SESSION_CONTEXT_FILE — path to file whose contents are injected once per
 *                               conversation. Unset by default.
 *
 * Output contract (Antigravity PreInvocation):
 *   { "injectSteps": [ { "ephemeralMessage": "..." } ] }  — first call + ctx file set
 *   { "injectSteps": [] }                                 — all other cases
 */

import { readFileSync, existsSync } from 'fs';
import { runFailOpen } from './lib/fail-open.mjs';
import { firedOnce } from './lib/throttle.mjs';
import { checkServer, getCached, setCached } from './lib/health-gate.mjs';
import { SETUP_MESSAGE, UNCONFIGURED_MESSAGE } from './lib/setup-message.mjs';

/** Emit the required Antigravity PreInvocation response and exit. */
function respond(steps) {
  process.stdout.write(JSON.stringify({ injectSteps: steps }) + '\n');
  process.exit(0);
}

/** Parse --url <value> from argv. Returns '' if not found. */
function parseUrl(argv) {
  const idx = argv.indexOf('--url');
  if (idx !== -1 && idx + 1 < argv.length) return argv[idx + 1];
  return '';
}

async function main() {
  const serverUrl = parseUrl(process.argv.slice(2));

  // Read stdin
  let raw = '';
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    raw = Buffer.concat(chunks).toString('utf8').trim();
  } catch {
    respond([]);
  }

  if (!raw) respond([]);

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    respond([]);
  }

  const conversationId = payload.conversationId || payload.conversation_id || '';
  const ctxFile = process.env.MORI_SESSION_CONTEXT_FILE;

  // Only act on the first invocation for this conversationId (throttle gate).
  // firedOnce returns true the first time and false for all subsequent calls.
  if (!conversationId || !firedOnce(conversationId)) {
    respond([]);
  }

  // ── Health sentinel (first invocation only) ───────────────────────────────
  //
  // MORI_SKIP_HEALTH_CHECK=1 bypasses the network check (treats server as "up").
  // For tests and private deployments where the VPN path should not gate startup.

  let healthState;
  if (process.env.MORI_SKIP_HEALTH_CHECK === '1') {
    healthState = 'up';
  } else {
    healthState = getCached(conversationId);
    if (!healthState) {
      healthState = await checkServer(serverUrl);
      setCached(conversationId, healthState);
    }
  }

  if (healthState === 'down') {
    respond([{ ephemeralMessage: SETUP_MESSAGE }]);
  }

  if (healthState === 'unconfigured') {
    respond([{ ephemeralMessage: UNCONFIGURED_MESSAGE }]);
  }

  // ── Server is up — inject context file if configured ─────────────────────

  if (ctxFile) {
    try {
      if (existsSync(ctxFile)) {
        const body = readFileSync(ctxFile, 'utf8').trim();
        if (body) {
          respond([{ ephemeralMessage: body }]);
        }
      }
    } catch {
      // Fall through to empty response
    }
  }

  respond([]);
}

runFailOpen(main);
