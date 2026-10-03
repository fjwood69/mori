/**
 * lib/config.mjs — one place every mori hook resolves its server URL and API key (Node ESM)
 *
 * #88: the Cursor hooks could only take the key as `--api-key <secret>` in the hook command,
 * which put the secret in a config file and in every hook process's argv (readable by other
 * local users) — so the NUC install had no key at all and every Cursor event got 401. The
 * Cursor installer now writes the key to a file and references the FILE in the command.
 * Used by the Claude Code and Cursor shippers.
 *
 * URL:  --url  →  MORI_SERVER_URL
 * Key:  --api-key-file  →  MORI_API_KEY_FILE  →  MORI_API_KEY  →  --api-key (deprecated)
 *
 * The key file must be a regular file, not a symlink, owned by this user, with no group or
 * world permission bits (0600 or stricter), checked on the descriptor actually read
 * (open O_NOFOLLOW → fstat → read), so a path swapped between check and read is not
 * trusted. Windows has no POSIX modes: the mode/owner check is skipped (warned once a day)
 * and the file must live under the user's home directory instead. An empty key file is a
 * misconfiguration, never "no key".
 */

import { closeSync, constants, fstatSync, openSync, readFileSync } from 'fs';
import { homedir } from 'os';
import { join, resolve, sep } from 'path';
import { logLine, warnOnce } from './state.mjs';

const NOFOLLOW = typeof constants.O_NOFOLLOW === 'number' ? constants.O_NOFOLLOW : 0;
const DAY_MS = 86_400_000;

/** Parse the flags every mori hook understands. Unknown flags are ignored. */
export function parseCommonArgs(argv) {
  const out = { url: '', apiKey: '', apiKeyFile: '', client: '', mode: '', event: '' };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--url':          out.url        = argv[++i] ?? ''; break;
      case '--api-key':      out.apiKey     = argv[++i] ?? ''; break;
      case '--api-key-file': out.apiKeyFile = argv[++i] ?? ''; break;
      case '--client':       out.client     = argv[++i] ?? ''; break;
      case '--mode':         out.mode       = argv[++i] ?? ''; break;
      case '--event':        out.event      = argv[++i] ?? ''; break;
    }
  }
  return out;
}

export function expandHome(p, home = homedir()) {
  if (p === '~') return home;
  if (p.startsWith('~/') || p.startsWith('~\\')) return join(home, p.slice(2));
  return p;
}

/**
 * Read a key file safely.
 * @returns {{key: string} | {problem: string}}
 */
export function readKeyFile(path, { platform = process.platform, home = homedir() } = {}) {
  const abs = resolve(expandHome(path, home));
  if (platform === 'win32') {
    const h = resolve(home).toLowerCase() + sep;
    if (!abs.toLowerCase().startsWith(h)) {
      return { problem: `key file ${abs} is outside the home directory` };
    }
  }
  let fd;
  try {
    fd = openSync(abs, constants.O_RDONLY | (platform === 'win32' ? 0 : NOFOLLOW));
  } catch (err) {
    if (err && err.code === 'ELOOP') return { problem: `key file ${abs} is a symlink; use a regular file` };
    if (err && err.code === 'ENOENT') return { problem: `key file ${abs} does not exist` };
    return { problem: `key file ${abs} cannot be opened (${err && err.code ? err.code : 'error'})` };
  }
  try {
    const st = fstatSync(fd);
    if (!st.isFile()) return { problem: `key file ${abs} is not a regular file` };
    if (platform === 'win32') {
      warnOnce('key-file-win-perms', `permission check skipped on Windows for ${abs}; keep it in your profile directory.`, DAY_MS);
    } else {
      if (typeof process.getuid === 'function' && st.uid !== process.getuid()) {
        return { problem: `key file ${abs} is owned by another user` };
      }
      const perms = st.mode & 0o777;
      if (perms & 0o077) {
        return { problem: `key file ${abs} has mode ${perms.toString(8).padStart(4, '0')}; expected 0600` };
      }
    }
    const first = readFileSync(fd, 'utf8').split(/\r?\n/, 1)[0].trim();
    if (!first) return { problem: `key file ${abs} is empty` };
    return { key: first };
  } catch (err) {
    return { problem: `key file ${abs} could not be read (${err && err.code ? err.code : 'error'})` };
  } finally {
    try { closeSync(fd); } catch { /* noop */ }
  }
}

/**
 * Resolve URL + key for a hook.
 * @returns {{url: string, apiKey: string, keySource: string, problem: string|null}}
 *   keySource: 'file' | 'env-file' | 'env' | 'flag' | 'none'
 *   problem:   a configuration error to surface (the hook must not ship when set)
 */
export function resolveConfig(args, env = process.env, opts = {}) {
  const url = (args.url || env.MORI_SERVER_URL || '').trim();
  const fileFromFlag = args.apiKeyFile || '';
  const fileFromEnv = env.MORI_API_KEY_FILE || '';
  for (const [path, source] of [[fileFromFlag, 'file'], [fileFromEnv, 'env-file']]) {
    if (!path) continue;
    const r = readKeyFile(path, opts);
    if (r.problem) return { url, apiKey: '', keySource: source, problem: r.problem };
    return { url, apiKey: r.key, keySource: source, problem: null };
  }
  if (env.MORI_API_KEY) return { url, apiKey: env.MORI_API_KEY.trim(), keySource: 'env', problem: null };
  if (args.apiKey) {
    warnOnce(
      'api-key-flag-deprecated',
      '--api-key on the hook command line is deprecated: it exposes the key in the process list. ' +
        'Re-run the mori installer to switch to --api-key-file.',
      DAY_MS,
    );
    logLine('config', 'deprecated --api-key flag in use');
    return { url, apiKey: args.apiKey.trim(), keySource: 'flag', problem: null };
  }
  return { url, apiKey: '', keySource: 'none', problem: null };
}
