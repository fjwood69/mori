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
 * Always exits 0 (fail-open). Any error → no output, exit 0.
 *
 * Usage (wired by install-hooks-cursor.mjs into ~/.cursor/hooks.json):
 *   node /absolute/path/to/mori-context-hook-cursor.mjs
 *
 * Environment:
 *   MORI_SESSION_CONTEXT_FILE — path to a file whose contents are injected.
 *                               Unset by default; nothing is injected on a stock install.
 *
 * Output (Cursor sessionStart additional_context contract):
 *   { "additional_context": "..." }  — when a context file is set and readable
 *   (empty / no output)              — otherwise
 */

import { readFileSync, existsSync } from 'fs';
import { runFailOpen } from './lib/fail-open.mjs';

async function main() {
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

  // If stdin is present, only act on sessionStart
  if (raw) {
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      process.exit(0);
    }
    // Cursor delivers hook_event_name in snake_case
    const eventName = (payload.hook_event_name || '').toLowerCase();
    if (eventName && eventName !== 'sessionstart') {
      process.exit(0);
    }
  }

  // Inject session context file if configured
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
