/**
 * mori-ship-event-cursor.mjs — Mori event shipper for Cursor hooks (Node ESM)
 *
 * Reads a Cursor hook event from stdin, normalises it to the canonical mori
 * event schema, optionally enriches Stop events with a transcript tail, then
 * POSTs to the mori server's /api/events/raw endpoint.
 *
 * Always exits 0 (fail-open). Any error is logged under the per-user state directory
 * (`<tmpdir>/mori-<uid>/hook.log`), then the process exits 0.
 *
 * Usage (wired by install-hooks-cursor.mjs into ~/.cursor/hooks.json):
 *   node /abs/path/mori-ship-event-cursor.mjs --url <base> --api-key-file <path> [--event <name>]
 *
 * Options:
 *   --url <base>          Base URL of the mori server (or MORI_SERVER_URL)
 *   --api-key-file <path> File holding the API key, mode 0600 (or MORI_API_KEY_FILE / MORI_API_KEY)
 *   --api-key <key>       Deprecated: puts the key in hooks.json and the process list
 *   --event <name>        Override event name (optional; falls back to stdin hook_event_name)
 *
 * #88: this script used to accept the key only as --api-key, so the NUC install carried no
 * key and every Cursor event was rejected (401) with nothing visible. Key resolution is now
 * lib/config.mjs, and a missing URL or an unusable key file is surfaced, not dropped.
 *
 * Cursor input fields (snake_case, per cursor.com/docs/hooks):
 *   hook_event_name, conversation_id, transcript_path, workspace_roots, tool_name,
 *   tool_input, tool_output, tool_use_id, duration, cwd
 *
 * Stop enrichment: mirrors mori-ship-event.mjs — if hook_event_name is Stop and
 * transcript_path is readable, adds transcript_tail_b64 (last 64 KB, base64).
 *
 * Node 18+ required (global fetch).
 */

import { readFileSync, existsSync } from 'fs';
import { hostname } from 'os';
import { runFailOpen } from './lib/fail-open.mjs';
import { postEvent } from './lib/post.mjs';
import { toCanonical } from './lib/canonical.mjs';
import { parseCommonArgs, resolveConfig } from './lib/config.mjs';
import { count, logLine, warnOnce } from './lib/state.mjs';

// ---- Stop enrichment -----------------------------------------------------------

function enrichStop(canonical) {
  if (canonical.hook_event_name !== 'Stop') return canonical;
  const tpath = canonical.transcript_path;
  if (!tpath || typeof tpath !== 'string') return canonical;
  try {
    if (!existsSync(tpath)) return canonical;
    const buf = readFileSync(tpath);
    const tail = buf.length > 65536 ? buf.slice(buf.length - 65536) : buf;
    return { ...canonical, transcript_tail_b64: tail.toString('base64') };
  } catch {
    return canonical;
  }
}

// ---- Main ----------------------------------------------------------------------

async function main() {
  const args = parseCommonArgs(process.argv.slice(2));

  // Read stdin
  let raw = '';
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    raw = Buffer.concat(chunks).toString('utf8').trim();
  } catch {
    process.exit(0);
  }
  if (!raw) process.exit(0);

  // Parse and normalise
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // Ship raw string as-is wrapped in a minimal envelope
    parsed = {};
  }

  // Resolve event name: CLI flag > stdin field
  const eventName = args.event || parsed.hook_event_name || '';

  const canonical = toCanonical(parsed, { client: 'cursor', eventName });
  const enriched = enrichStop(canonical);

  const cfg = resolveConfig(args);
  const base = cfg.url.replace(/\/$/, '');
  if (!base || !/^https?:\/\//i.test(base)) {
    warnOnce('cursor-no-url', `mori (Cursor): server URL unset or invalid ("${cfg.url}") — events are NOT being shipped.`);
    logLine('mori-cursor', 'invalid base URL');
    count('no-url');
    process.exit(0);
  }
  if (cfg.problem) {
    warnOnce('cursor-key-config', `mori (Cursor): ${cfg.problem} — events are NOT being shipped.`);
    logLine('mori-cursor', 'key configuration problem');
    count('key-config');
    process.exit(0);
  }

  // Build endpoint URL
  const client = process.env.MORI_CLIENT_ID || hostname();
  const url = `${base}/api/events/raw?client=${encodeURIComponent(client)}`;

  await postEvent({ url, apiKey: cfg.apiKey, body: enriched, tag: 'mori-cursor' });
  process.exit(0);
}

runFailOpen(main);
