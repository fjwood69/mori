/**
 * mori-ship-event.mjs — Mori event shipper for Claude Code hooks (Node ESM)
 *
 * Node port of mori-ship-event.sh. Uses Node built-ins + global fetch (Node 18+).
 * Reads hook event JSON from stdin, enriches Stop events with a transcript tail,
 * then POSTs to the Mori server. Always exits 0 (fail-soft).
 *
 * Usage:
 *   node mori-ship-event.mjs [--url <base>] [--client <name>] [--api-key-file <path>] [--mode raw|precompact]
 *
 * Config (lib/config.mjs): URL from --url or MORI_SERVER_URL; key from --api-key-file,
 * MORI_API_KEY_FILE, MORI_API_KEY, or the deprecated --api-key. Fail-soft never means
 * fail-silent: a missing URL, an unreadable key file and a rejected key (401/403) are
 * surfaced on stderr (rate-limited) and recorded for the next session start; transient
 * failures go to the per-user hook log and counters (lib/state.mjs).
 *
 * #88: Cursor also runs this plugin's hooks, with its own event names and without Claude
 * Code's settings env. Events this plugin is not wired for are ignored (lib/claude-events.mjs)
 * before any config check — Cursor's own mori plugin ships them.
 *
 * Options:
 *   --url <base>          Base URL of the Mori server (or MORI_SERVER_URL)
 *   --client <name>       ?client= query param (default: os.hostname())
 *   --api-key-file <path> File holding the API key (0600; or MORI_API_KEY_FILE / MORI_API_KEY)
 *   --api-key <key>       Deprecated: exposes the key in the process list
 *   --mode raw|precompact raw (default): POST /api/events/raw
 *                         precompact: POST /api/precompact (blocks until the dream completes)
 */

import { hostname } from 'os';
import { isForeignEvent } from './lib/claude-events.mjs';
import { enrichStopEvent } from './lib/enrich.mjs';
import { parseCommonArgs, resolveConfig } from './lib/config.mjs';
import { postEvent } from './lib/post.mjs';
import { count, logLine, warnOnce } from './lib/state.mjs';

// Raw events stay well inside the host's hook timeout; PreCompact waits for the server's
// dream, so it gets nearly all of Claude Code's default 60 s instead of being killed silently.
const RAW_TIMEOUT_MS = 10_000;
const PRECOMPACT_TIMEOUT_MS = 55_000;

async function main() {
  const args = parseCommonArgs(process.argv.slice(2));
  const mode = args.mode === 'precompact' ? 'precompact' : 'raw';
  const client = args.client || hostname();

  let raw = '';
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    raw = Buffer.concat(chunks).toString('utf8').trim();
  } catch {
    process.exit(0);
  }
  if (!raw) process.exit(0);

  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // Malformed JSON — shipped as-is below.
  }

  // Another host running this plugin's hooks (Cursor): not ours to ship. Checked BEFORE the
  // config so a host without our env doesn't log a config error per event.
  if (isForeignEvent(parsed)) {
    count('foreign-event');
    process.exit(0);
  }

  const cfg = resolveConfig(args);
  const base = cfg.url.replace(/\/$/, '');
  if (!base || !/^https?:\/\//i.test(base)) {
    warnOnce(
      'no-url',
      `MORI_SERVER_URL is unset or invalid ("${cfg.url}") — capture events are NOT being shipped. Configure the plugin's server URL.`,
    );
    logLine('mori-ship', `${mode}: invalid base URL (MORI_SERVER_URL unset/invalid)`);
    count('no-url');
    process.exit(0);
  }
  if (cfg.problem) {
    warnOnce('key-config', `${cfg.problem} — events are NOT being shipped.`);
    logLine('mori-ship', `${mode}: key configuration problem`);
    count('key-config');
    process.exit(0);
  }

  let body = raw;
  if (parsed) {
    const enriched = enrichStopEvent(parsed, mode);
    if (enriched) body = JSON.stringify(enriched);
  }

  const endpoint = mode === 'precompact' ? 'precompact' : 'events/raw';
  const uri = `${base}/api/${endpoint}?client=${encodeURIComponent(client)}`;
  await postEvent({
    url: uri,
    apiKey: cfg.apiKey,
    body,
    timeoutMs: mode === 'precompact' ? PRECOMPACT_TIMEOUT_MS : RAW_TIMEOUT_MS,
    tag: 'mori-ship',
  });
  process.exit(0);
}

// Runs unconditionally: an "is this the main module?" guard can misfire when the plugin is
// reached through a symlink (Node resolves the real path), and a hook that silently never runs
// is the failure #88 is about. Testable logic lives in lib/ instead.
main().catch((err) => {
  try { logLine('mori-ship', `unexpected: ${err && err.name ? err.name : 'error'}`); } catch { /* noop */ }
  process.exit(0);
});
