/**
 * lib/health-gate.mjs — Mori server health check with session cache (Node ESM)
 *
 * Exported API:
 *   checkServer(url)              → Promise<"up" | "down" | "unconfigured">
 *   getCached(sessionId)          → "up" | "down" | "unconfigured" | null
 *   setCached(sessionId, state)   → void (best-effort; errors silently ignored)
 *
 * Design:
 *   - Returns "unconfigured" immediately (no fetch) when url is empty, the
 *     literal default placeholder, or not a parseable http(s) URL.
 *   - Fetches `<url>/health` with a 2 000 ms AbortController timeout. (Set at 2 s,
 *     not 600 ms, because Node ESM startup overhead in a hook child process takes
 *     ~600 ms before the first I/O; 2 s remains well within the 10 s hook timeout.)
 *   - Treats HTTP 2xx AND 404 as "up" (older servers may not have /health).
 *   - 5xx / network error / abort → "down".
 *   - NEVER fetches any URL other than the one passed in (privacy invariant).
 *   - Never throws — any unexpected error returns "down".
 *
 * Session cache:
 *   Stored in $TMPDIR/mori-health-<sanitised-sessionId>.json with a 5-minute TTL.
 *   Prevents repeated pings when the harness fires SessionStart multiple times
 *   (e.g. PostCompact). Errors reading/writing the cache are silently ignored
 *   (fail-open: treat as no cache).
 *
 * Node built-ins only. No npm packages. ESM.
 */

import { readFileSync, writeFileSync } from 'fs';

// Default placeholder that the Claude Code plugin UI ships. Treat as unconfigured.
const DEFAULT_PLACEHOLDER = 'http://localhost:8968';

/** 5-minute TTL for the session cache (ms). */
const CACHE_TTL_MS = 5 * 60 * 1000;

// ── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Sanitise a string for use as a filename component.
 * Replaces any char outside [a-zA-Z0-9_-] with '_', capped at 128 chars.
 */
function sanitise(s) {
  return String(s).replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 128);
}

/** Path to the cache file for a given sessionId. */
function cachePath(sessionId) {
  const tmp = process.env.TMPDIR || '/tmp';
  return `${tmp}/mori-health-${sanitise(sessionId)}.json`;
}

// ── Session cache ────────────────────────────────────────────────────────────

/**
 * Read a cached health state for sessionId.
 * Returns the state string if the cache exists and is within TTL, else null.
 * Never throws.
 *
 * @param {string} sessionId
 * @returns {"up"|"down"|"unconfigured"|null}
 */
export function getCached(sessionId) {
  if (!sessionId) return null;
  try {
    const raw = readFileSync(cachePath(sessionId), 'utf8');
    const obj = JSON.parse(raw);
    if (!obj || typeof obj.state !== 'string' || typeof obj.ts !== 'number') return null;
    if (Date.now() - obj.ts > CACHE_TTL_MS) return null;
    return obj.state;
  } catch {
    return null;
  }
}

/**
 * Write a health state to the session cache.
 * Fails silently (fail-open).
 *
 * @param {string} sessionId
 * @param {"up"|"down"|"unconfigured"} state
 */
export function setCached(sessionId, state) {
  if (!sessionId) return;
  try {
    writeFileSync(cachePath(sessionId), JSON.stringify({ state, ts: Date.now() }), 'utf8');
  } catch {
    // Silently ignore — cache is best-effort
  }
}

// ── Health check ─────────────────────────────────────────────────────────────

/**
 * Check whether the Mori server at `url` is reachable.
 *
 * @param {string} url  Base URL of the Mori server (from plugin config).
 * @returns {Promise<"up"|"down"|"unconfigured">}
 */
export async function checkServer(url) {
  // ── Guard: unconfigured ──────────────────────────────────────────────────

  if (!url || typeof url !== 'string') return 'unconfigured';

  const trimmed = url.trim();
  if (!trimmed) return 'unconfigured';

  // Literal default placeholder → not yet configured
  if (trimmed === DEFAULT_PLACEHOLDER || trimmed === DEFAULT_PLACEHOLDER + '/') {
    return 'unconfigured';
  }

  // Must be a parseable http(s) URL
  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch {
    return 'unconfigured';
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return 'unconfigured';
  }

  // ── Fetch /health ────────────────────────────────────────────────────────

  const healthUrl = trimmed.replace(/\/$/, '') + '/health';

  const controller = new AbortController();
  // 2 000 ms bounds the network wait and accounts for ESM module startup overhead
  // in child processes (Node startup + module loading can take ~600 ms on its own).
  // The hook-level timeout (10 s for context hooks) remains the hard outer limit.
  const timer = setTimeout(() => controller.abort(), 2000);

  try {
    const res = await fetch(healthUrl, { signal: controller.signal });
    clearTimeout(timer);
    // 2xx or 404 → server is alive (older servers may not have /health endpoint)
    if ((res.status >= 200 && res.status < 300) || res.status === 404) {
      return 'up';
    }
    // 5xx → server is reachable but unhealthy — treat as "down"
    return 'down';
  } catch {
    clearTimeout(timer);
    // Network error, DNS failure, or abort (timeout) → down
    return 'down';
  }
}
