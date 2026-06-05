/**
 * mori-ship-event-cursor.mjs — Mori event shipper for Cursor hooks (Node ESM)
 *
 * Reads a Cursor hook event from stdin, normalises it to the canonical mori
 * event schema, optionally enriches Stop events with a transcript tail, then
 * POSTs to the mori server's /api/events/raw endpoint.
 *
 * Always exits 0 (fail-open). Any error → logged to $TMPDIR/mori-hook.log, exit 0.
 *
 * Usage (wired by install-hooks-cursor.mjs into ~/.cursor/hooks.json):
 *   node /abs/path/mori-ship-event-cursor.mjs --url <base> --api-key <key> [--event <name>]
 *
 * Options:
 *   --url <base>      Base URL of the mori server (required)
 *   --api-key <key>   API key sent as X-Api-Key header (optional)
 *   --event <name>    Override event name (optional; falls back to stdin hook_event_name)
 *
 * Cursor input fields (snake_case):
 *   hook_event_name, conversation_id, transcript_path, workspace_roots, tool_name,
 *   tool_input, tool_response, source, cwd
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

// ---- Arg parsing ---------------------------------------------------------------

function parseArgs(argv) {
  const args = { url: '', apiKey: '', event: '' };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--url':     args.url    = argv[++i] ?? ''; break;
      case '--api-key': args.apiKey = argv[++i] ?? ''; break;
      case '--event':   args.event  = argv[++i] ?? ''; break;
    }
  }
  return args;
}

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
  const args = parseArgs(process.argv.slice(2));

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

  // Build endpoint URL
  const base = args.url.replace(/\/$/, '');
  const client = process.env.MORI_CLIENT_ID || hostname();
  const url = `${base}/api/events/raw?client=${encodeURIComponent(client)}`;

  await postEvent({ url, apiKey: args.apiKey, body: enriched });
  process.exit(0);
}

runFailOpen(main);
