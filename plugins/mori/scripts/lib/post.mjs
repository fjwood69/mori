/**
 * lib/post.mjs — Fail-soft HTTP POST helper for mori hooks (Node ESM)
 *
 * Export: postEvent({ url, apiKey, body, timeoutMs, tag }) → Promise<{ok, status, reason}>
 * `tag` names the calling tool ('mori-ship' = Claude Code, 'mori-cursor' = Cursor).
 *
 * POSTs `body` (string or object) as JSON; `apiKey`, if given, goes in the X-Api-Key header.
 * Never throws — the caller can await without try/catch (signature unchanged for existing
 * callers, which ignore the result).
 *
 * #88: `fetch` resolves on a 401, so a rejected key used to look like success and a client
 * could ship nothing for weeks unnoticed. Now every non-2xx and every network failure is
 * classified and recorded (never the key, never a request or response body):
 *   auth (401/403) · rate (429) · client (other 4xx) · server (5xx) · timeout · network
 * - each failure: one line in the per-user hook log + a counter entry;
 * - auth: also a marker the tool's context hook surfaces at the next session start, and a
 *   once-an-hour stderr warning — both per `tag`, i.e. per tool (lib/state.mjs);
 * - a hung server is cut off after `timeoutMs` (default 10 s, below the hooks' 15 s timeout)
 *   instead of being killed by the host with nothing logged;
 * - sending a key over plain http to a non-loopback host is warned about once a day.
 */

import { clearAuthFailure, count, logLine, markAuthFailure, warnOnce } from './state.mjs';

const DAY_MS = 86_400_000;
const LOOPBACK = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

export function classifyStatus(status) {
  if (status >= 200 && status < 300) return null;
  if (status === 401 || status === 403) return 'auth';
  if (status === 429) return 'rate';
  if (status >= 400 && status < 500) return 'client';
  return 'server';
}

/** Log target without query string (keeps ?client=… out of nothing sensitive, and short). */
function target(url) {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`;
  } catch {
    return '<invalid url>';
  }
}

function warnPlainHttp(url, apiKey) {
  if (!apiKey) return;
  try {
    const u = new URL(url);
    if (u.protocol === 'http:' && !LOOPBACK.has(u.hostname)) {
      warnOnce(
        'plain-http-key',
        `sending the API key over plain http to ${u.host}; use https unless this network is private.`,
        DAY_MS,
      );
    }
  } catch { /* invalid URL is reported by the caller */ }
}

/**
 * @param {{ url: string, apiKey?: string, body: string | object, timeoutMs?: number, tag?: string }} opts
 * @returns {Promise<{ok: boolean, status: number, reason: string|null}>}
 */
export async function postEvent({ url, apiKey, body, timeoutMs = 10_000, tag = 'mori-post' }) {
  const payload = typeof body === 'string' ? body : JSON.stringify(body);
  const headers = { 'Content-Type': 'application/json' };
  if (apiKey) headers['X-Api-Key'] = apiKey;
  warnPlainHttp(url, apiKey);
  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers,
      body: payload,
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    const reason = err && (err.name === 'TimeoutError' || err.name === 'AbortError') ? 'timeout' : 'network';
    logLine(tag, `${target(url)} : ${reason} (${err && err.name ? err.name : 'error'})`);
    count(reason);
    return { ok: false, status: 0, reason };
  }
  // Never read the body: error pages can echo request headers. Release the socket.
  try { await res.body?.cancel(); } catch { /* noop */ }
  const reason = classifyStatus(res.status);
  if (!reason) {
    clearAuthFailure(tag);
    return { ok: true, status: res.status, reason: null };
  }
  logLine(tag, `${target(url)} : HTTP ${res.status} (${reason})`);
  count(reason);
  if (reason === 'auth') {
    markAuthFailure(res.status, tag);
    warnOnce(
      `auth-${tag}`,
      `Mori server rejected this client's API key (HTTP ${res.status}) — events are NOT being recorded. ` +
        'Check MORI_API_KEY or the key file.',
    );
  }
  return { ok: false, status: res.status, reason };
}
