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
 * Always exits 0 (fail-open). Any error → { "injectSteps": [] }, exit 0.
 *
 * Usage (wired by install-hooks-antigravity.mjs into ~/.gemini/config/hooks.json):
 *   node /abs/path/mori-context-hook-antigravity.mjs
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

/** Emit the required Antigravity PreInvocation response and exit. */
function respond(steps) {
  process.stdout.write(JSON.stringify({ injectSteps: steps }) + '\n');
  process.exit(0);
}

async function main() {
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

  // Inject only if: context file is configured AND this is the first invocation
  // for this conversation.
  if (ctxFile && conversationId && firedOnce(conversationId)) {
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
