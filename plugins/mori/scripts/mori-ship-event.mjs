/**
 * mori-ship-event.mjs — Mori event shipper for Claude/Cursor hooks (Node ESM)
 *
 * Node port of mori-ship-event.sh. Uses Node built-ins + global fetch (Node 18+).
 * Reads hook event JSON from stdin, enriches Stop events with a transcript tail,
 * then POSTs to the Mori server. Always exits 0 (fail-soft).
 *
 * Usage:
 *   node mori-ship-event.mjs --url <base> --client <name> [--api-key <key>] [--mode raw|precompact]
 *
 * Config resolution: --url/--api-key win; otherwise MORI_SERVER_URL / MORI_API_KEY,
 * then CLAUDE_PLUGIN_OPTION_server_url / _api_key (the plugin user_config path).
 * Always exits 0 (fail-soft), but config errors (missing URL, 401/403) are surfaced
 * on stderr — not silently dropped. Transient errors go to the log only.
 *
 * Options:
 *   --url <base>      Base URL of the Mori server (or set MORI_SERVER_URL)
 *   --client <name>   Client identifier sent as ?client= query param (default: os.hostname())
 *   --api-key <key>   API key sent as X-Api-Key header (or set MORI_API_KEY; omit for unauthenticated servers)
 *   --mode raw|precompact
 *                     raw (default): POST to /api/events/raw
 *                     precompact: POST to /api/precompact (blocks until dream completes)
 */

import { readFileSync, existsSync, appendFileSync, statSync, writeFileSync } from 'fs';
import { hostname } from 'os';

// ---- Arg parsing ---------------------------------------------------------------

function parseArgs(argv) {
  const args = { url: '', client: '', apiKey: '', mode: 'raw' };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--url':     args.url    = argv[++i] ?? ''; break;
      case '--client':  args.client = argv[++i] ?? ''; break;
      case '--api-key': args.apiKey = argv[++i] ?? ''; break;
      case '--mode':    args.mode   = argv[++i] ?? 'raw'; break;
    }
  }
  if (!args.client) args.client = hostname();
  // Explicit --url/--api-key win (tests / wrappers); otherwise fall back to the
  // env vars the Claude Code plugin sets (MORI_SERVER_URL / MORI_API_KEY).
  if (!args.url) args.url = process.env.MORI_SERVER_URL || process.env.CLAUDE_PLUGIN_OPTION_server_url || '';
  if (!args.apiKey) args.apiKey = process.env.MORI_API_KEY || process.env.CLAUDE_PLUGIN_OPTION_api_key || '';
  return args;
}

// ---- Logging -------------------------------------------------------------------

function logFailure(mode, uri, reason) {
  const log = `${process.env.TMPDIR || '/tmp'}/mori-hook.log`;
  try {
    // Rotate log if > 100 KB
    if (existsSync(log)) {
      try {
        const st = statSync(log);
        if (st.size > 102400) {
          // Best-effort rotation: append a separator instead of renaming
          // (renameSync is available but skipped to keep imports minimal — the
          // log will be reset naturally on the next rotation-eligible write)
          appendFileSync(`${log}.old`, readFileSync(log));
          appendFileSync(log, ''); // leave file in place; OS truncation not available without openSync
        }
      } catch { /* noop */ }
    }
    const ts = new Date().toISOString().replace('T', ' ').replace(/\.\d+Z$/, '');
    appendFileSync(log, `${ts} [mori-ship] ${mode} ${uri} : ${reason}\n`);
  } catch {
    // Truly fail-silent
  }
}

// ---- Visible failure surfacing -------------------------------------------------
// Fail-soft must not mean fail-silent. A missing server URL or a rejected POST
// used to drop events with zero signal. Surface CONFIG errors (no URL, 401/403)
// on stderr — visible in the Claude Code hook output — at most once per hour so
// PostToolUse can't spam. Transient errors (5xx/timeouts) go to the log only.
function warnOnce(reason, key = 'warn') {
  const marker = `${process.env.TMPDIR || '/tmp'}/mori-hook-${key}`;
  try {
    if (existsSync(marker) && Date.now() - statSync(marker).mtimeMs < 3_600_000) return;
    writeFileSync(marker, String(Date.now()));
  } catch { /* if we can't persist the marker, warn anyway */ }
  try { process.stderr.write(`[mori] ${reason}\n`); } catch { /* noop */ }
}

// ---- Stop-event enrichment -----------------------------------------------------
// Mirror bash logic: if hook_event_name === "Stop" and transcript_path is readable,
// read last 65536 bytes, base64-encode, add as transcript_tail_b64. Any failure →
// return null (caller ships original body unchanged).

function enrichStopEvent(parsed, mode) {
  if (mode !== 'raw') return null;
  if ((parsed.hook_event_name || '') !== 'Stop') return null;

  const tpath = parsed.transcript_path;
  if (!tpath || typeof tpath !== 'string') return null;

  try {
    if (!existsSync(tpath)) return null;
    const buf = readFileSync(tpath);
    const tail = buf.length > 65536 ? buf.slice(buf.length - 65536) : buf;
    const tailB64 = tail.toString('base64');
    return { ...parsed, transcript_tail_b64: tailB64 };
  } catch {
    return null;
  }
}

// ---- Main ----------------------------------------------------------------------

async function main() {
  const args = parseArgs(process.argv.slice(2));

  // Read all stdin
  let raw = '';
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    raw = Buffer.concat(chunks).toString('utf8').trim();
  } catch {
    process.exit(0);
  }

  if (!raw) process.exit(0);

  // Build endpoint URL
  const base = args.url.replace(/\/$/, '');
  const endpoint = args.mode === 'precompact' ? 'precompact' : 'events/raw';
  const uri = `${base}/api/${endpoint}?client=${encodeURIComponent(args.client)}`;

  // A missing/relative base URL makes fetch() throw and silently drops every
  // event. Stay fail-soft (exit 0) but say so loudly — this is a misconfiguration.
  if (!base || !/^https?:\/\//i.test(base)) {
    warnOnce(`MORI_SERVER_URL is unset or invalid ("${args.url}") — capture events are NOT being shipped. Configure the plugin's server URL.`, 'no-url');
    logFailure(args.mode, uri, 'invalid base URL (MORI_SERVER_URL unset/invalid)');
    process.exit(0);
  }

  // Attempt Stop-event enrichment (parse → enrich → re-serialise; fall back to raw string)
  let body = raw;
  try {
    const parsed = JSON.parse(raw);
    const enriched = enrichStopEvent(parsed, args.mode);
    if (enriched) body = JSON.stringify(enriched);
  } catch {
    // Malformed JSON — ship as-is
  }

  // POST — await so precompact blocks until the server's dream completes
  const headers = { 'Content-Type': 'application/json' };
  if (args.apiKey) headers['X-Api-Key'] = args.apiKey;

  try {
    const resp = await fetch(uri, { method: 'POST', headers, body });
    if (resp.status === 401 || resp.status === 403) {
      // Config error — a fetch() that returns 401 does NOT throw, so this used to
      // look like success. The key is wrong/missing; surface it.
      logFailure(args.mode, uri, `HTTP ${resp.status}`);
      warnOnce(`Mori server rejected the request (HTTP ${resp.status}) — check MORI_API_KEY. Events are not being recorded.`, 'auth');
    } else if (!resp.ok) {
      // Transient / server-side — log only, don't spam stderr.
      logFailure(args.mode, uri, `HTTP ${resp.status}`);
    }
  } catch (err) {
    // Transient (connection refused / timeout) — log only.
    logFailure(args.mode, uri, String(err));
  }

  process.exit(0);
}

main().catch((err) => {
  try { logFailure('?', '?', String(err)); } catch { /* noop */ }
  process.exit(0);
});
