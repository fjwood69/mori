/**
 * lib/state.mjs — per-user state for mori hooks: log, counters, markers (Node ESM)
 *
 * #88: hooks used to write fixed paths in a shared temp dir ($TMPDIR or /tmp:
 * mori-hook.log, mori-hook-<key>, mori-health-*, mori-conv-*). On a multi-user host another
 * user could pre-create one as a symlink (clobber), or create it first with their own
 * permissions so this user's writes fail silently. Everything now lives in ONE per-user
 * directory that this module creates 0700 and verifies (a real directory, owned by us, not
 * group/world-accessible) before use; files are opened without following symlinks.
 *
 *   stateDir()                  → absolute dir, or null if no safe dir is available
 *   logLine(tag, message)       → append "<ts> [tag] message" to hook.log (rotates at 100 KB)
 *   count(reason)               → append {"ts","reason"} to counters.jsonl (append-only)
 *   warnOnce(key, msg, periodMs)→ stderr "[mori] msg" at most once per period
 *   markAuthFailure(status, tag)     → record a rejected key for one tool (read by its context hook)
 *   clearAuthFailure(tag)            → that tool's key worked again
 *   recentAuthFailure(tag, maxAgeMs) → {ts, status} | null
 *
 * The auth marker is per TOOL (the shipper's tag: 'mori-ship' = Claude Code, 'mori-cursor' =
 * Cursor). Several tools share this directory on one machine, often with different keys: with
 * one shared marker, a working Claude Code key cleared Cursor's rejection on every turn before
 * Cursor's session start could show it, and a Cursor rejection was reported as Claude Code's.
 *   statePath(name)             → path of a file in the state dir (or null)
 *
 * Never throws; never logs a key or a request/response body.
 */

import {
  closeSync, constants, existsSync, fstatSync, lstatSync, mkdirSync,
  openSync, readFileSync, renameSync, statSync, writeFileSync, writeSync,
} from 'fs';
import { homedir, tmpdir, userInfo } from 'os';
import { join } from 'path';

const LOG_MAX_BYTES = 102400;
const IS_WIN = process.platform === 'win32';
// O_NOFOLLOW is POSIX-only; on Windows the constant is undefined and must not be passed.
const NOFOLLOW = typeof constants.O_NOFOLLOW === 'number' ? constants.O_NOFOLLOW : 0;

let _dir; // memoised: undefined = not resolved yet, null = no safe dir

function userTag() {
  if (typeof process.getuid === 'function') return String(process.getuid());
  try {
    return String(userInfo().username).replace(/[^A-Za-z0-9_-]/g, '_') || 'user';
  } catch {
    return 'user';
  }
}

/** A directory we may use: a real dir (not a symlink), ours, not group/world accessible. */
function isSafeDir(dir) {
  try {
    const st = lstatSync(dir);
    if (!st.isDirectory() || st.isSymbolicLink()) return false;
    if (IS_WIN) return true; // no POSIX ownership/mode bits on Windows
    if (typeof process.getuid === 'function' && st.uid !== process.getuid()) return false;
    return (st.mode & 0o077) === 0;
  } catch {
    return false;
  }
}

function ensureDir(dir) {
  try {
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true, mode: 0o700 });
  } catch {
    return false;
  }
  return isSafeDir(dir);
}

/** The per-user state directory, or null if none can be made safe. */
export function stateDir() {
  if (_dir !== undefined) return _dir;
  const candidates = [join(tmpdir(), `mori-${userTag()}`)];
  const xdgState = process.env.XDG_STATE_HOME || join(homedir(), '.local', 'state');
  candidates.push(join(xdgState, 'mori'));
  _dir = null;
  for (const dir of candidates) {
    if (ensureDir(dir)) {
      _dir = dir;
      break;
    }
  }
  return _dir;
}

/** Test hook: forget the memoised directory (tests point TMPDIR elsewhere). */
export function _resetStateDirForTests() {
  _dir = undefined;
}

export function statePath(name) {
  const dir = stateDir();
  return dir ? join(dir, name) : null;
}

/** Append to a file in the state dir without following a symlink planted at its path. */
function safeAppend(name, text) {
  const p = statePath(name);
  if (!p) return;
  try {
    if (existsSync(p) && statSync(p).size > LOG_MAX_BYTES) {
      renameSync(p, `${p}.old`); // keeps exactly one previous generation
    }
  } catch { /* rotation is best-effort */ }
  let fd;
  try {
    fd = openSync(p, constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | NOFOLLOW, 0o600);
    if (!IS_WIN && typeof process.getuid === 'function' && fstatSync(fd).uid !== process.getuid()) {
      return;
    }
    writeSync(fd, text);
  } catch {
    /* fail-silent: the stderr warning (warnOnce) is the user-visible channel */
  } finally {
    if (fd !== undefined) {
      try { closeSync(fd); } catch { /* noop */ }
    }
  }
}

function ts() {
  return new Date().toISOString().replace('T', ' ').replace(/\.\d+Z$/, '');
}

export function logLine(tag, message) {
  safeAppend('hook.log', `${ts()} [${tag}] ${message}\n`);
}

export function count(reason) {
  safeAppend('counters.jsonl', `${JSON.stringify({ ts: new Date().toISOString(), reason })}\n`);
}

/** Write a small marker file (create or replace) without following a symlink. */
function writeMarker(name, value) {
  const p = statePath(name);
  if (!p) return false;
  try {
    if (existsSync(p) && lstatSync(p).isSymbolicLink()) return false;
    writeFileSync(p, value, { mode: 0o600 });
    return true;
  } catch {
    return false;
  }
}

function markerAgeMs(name) {
  const p = statePath(name);
  if (!p) return null;
  try {
    const st = lstatSync(p);
    if (st.isSymbolicLink()) return null;
    return Date.now() - st.mtimeMs;
  } catch {
    return null;
  }
}

/** stderr "[mori] message" at most once per periodMs for this key (per user). */
export function warnOnce(key, message, periodMs = 3_600_000) {
  const name = `warn-${String(key).replace(/[^A-Za-z0-9_-]/g, '_')}`;
  const age = markerAgeMs(name);
  if (age !== null && age < periodMs) return false;
  writeMarker(name, String(Date.now()));
  try { process.stderr.write(`[mori] ${message}\n`); } catch { /* noop */ }
  return true;
}

function authMarkerName(tag) {
  return `auth-failure-${String(tag || 'unknown').replace(/[^A-Za-z0-9_-]/g, '_')}`;
}

export function markAuthFailure(status, tag) {
  writeMarker(authMarkerName(tag), JSON.stringify({ ts: Date.now(), status }));
}

export function clearAuthFailure(tag) {
  const p = statePath(authMarkerName(tag));
  if (!p) return;
  try {
    // Called after every successful post: write only when a failure is actually recorded.
    if (!existsSync(p)) return;
    const st = lstatSync(p);
    if (!st.isSymbolicLink() && st.size > 0) writeFileSync(p, '', { mode: 0o600 });
  } catch { /* noop */ }
}

/** This tool's most recent rejected-key event within maxAgeMs, or null. */
export function recentAuthFailure(tag, maxAgeMs = 86_400_000) {
  const p = statePath(authMarkerName(tag));
  if (!p) return null;
  try {
    if (lstatSync(p).isSymbolicLink()) return null;
    const raw = readFileSync(p, 'utf8').trim();
    if (!raw) return null;
    const obj = JSON.parse(raw);
    if (typeof obj.ts !== 'number' || Date.now() - obj.ts > maxAgeMs) return null;
    return { ts: obj.ts, status: obj.status };
  } catch {
    return null;
  }
}
