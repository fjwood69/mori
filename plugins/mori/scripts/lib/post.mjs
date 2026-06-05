/**
 * lib/post.mjs — Fail-soft HTTP POST helper for mori hooks (Node ESM)
 *
 * Export: postEvent({ url, apiKey, body })
 *
 * POSTs `body` (string or object) as JSON to the given URL.
 * If `apiKey` is provided, it is sent as the X-Api-Key header.
 * On ANY network or parse error, appends a line to $TMPDIR/mori-hook.log
 * and resolves (never throws). The caller can safely await without try/catch.
 *
 * Requirements: Node 18+ (global fetch).
 */

import { appendFileSync, existsSync, statSync, readFileSync } from 'fs';

const LOG_MAX_BYTES = 102400; // 100 KB

/**
 * Append a failure line to $TMPDIR/mori-hook.log.
 * Silently ignores its own errors (truly fail-silent).
 *
 * @param {string} uri
 * @param {string} reason
 */
function logFailure(uri, reason) {
  const log = `${process.env.TMPDIR || '/tmp'}/mori-hook.log`;
  try {
    // Best-effort rotation: archive to .old when the log exceeds 100 KB
    if (existsSync(log)) {
      try {
        const st = statSync(log);
        if (st.size > LOG_MAX_BYTES) {
          appendFileSync(`${log}.old`, readFileSync(log));
          // Leave original in place; a full truncation would need openSync
        }
      } catch { /* noop */ }
    }
    const ts = new Date().toISOString().replace('T', ' ').replace(/\.\d+Z$/, '');
    appendFileSync(log, `${ts} [mori-post] ${uri} : ${reason}\n`);
  } catch {
    // Truly fail-silent
  }
}

/**
 * POST a JSON body to url, optionally authenticated with X-Api-Key.
 * Resolves (does not throw) on any error.
 *
 * @param {{ url: string, apiKey?: string, body: string | object }} opts
 * @returns {Promise<void>}
 */
export async function postEvent({ url, apiKey, body }) {
  const payload = typeof body === 'string' ? body : JSON.stringify(body);
  const headers = { 'Content-Type': 'application/json' };
  if (apiKey) headers['X-Api-Key'] = apiKey;
  try {
    await fetch(url, { method: 'POST', headers, body: payload });
  } catch (err) {
    logFailure(url, String(err));
  }
}
