/**
 * mori-ship-event-antigravity.mjs — Mori event shipper for Antigravity (Node ESM)
 *
 * Reads an Antigravity hook event from stdin, normalises it to the canonical mori
 * event schema, optionally enriches Stop events with a transcript tail, then POSTs
 * to the mori server's /api/events/raw endpoint.
 *
 * The event name comes from the --event CLI flag (Antigravity stdin has no
 * hook_event_name field; the event is determined by which hook config key fired).
 *
 * Always exits 0 (fail-open). Any error → logged to $TMPDIR/mori-hook.log, exit 0.
 *
 * Usage (wired by install-hooks-antigravity.mjs into ~/.gemini/config/hooks.json):
 *   node /abs/path/mori-ship-event-antigravity.mjs --url <base> --api-key <key> --event <Event>
 *
 * Options:
 *   --url <base>      Base URL of the mori server (required)
 *   --api-key <key>   API key sent as X-Api-Key header (optional)
 *   --event <Event>   Event name — required; e.g. PostToolUse, Stop, PreInvocation
 *
 * Antigravity input fields (camelCase):
 *   conversationId, transcriptPath, stepIdx, error
 *   (PostToolUse may include tool name/input/response — placed in _clientMeta)
 *
 * Stop enrichment: if hook_event_name is Stop and transcriptPath is readable,
 * adds transcript_tail_b64 (last 64 KB, base64). Mirrors mori-ship-event.mjs.
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

  // Parse input
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = {};
  }

  // Normalise to canonical schema
  const canonical = toCanonical(parsed, { client: 'antigravity', eventName: args.event });
  const enriched = enrichStop(canonical);

  // Build endpoint URL
  const base = args.url.replace(/\/$/, '');
  const client = process.env.MORI_CLIENT_ID || hostname();
  const url = `${base}/api/events/raw?client=${encodeURIComponent(client)}`;

  await postEvent({ url, apiKey: args.apiKey, body: enriched });
  process.exit(0);
}

runFailOpen(main);
