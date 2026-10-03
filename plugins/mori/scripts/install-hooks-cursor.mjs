/**
 * install-hooks-cursor.mjs — Install mori hooks into ~/.cursor/hooks.json (Node ESM)
 *
 * Writes absolute paths to the mori Cursor hook scripts into the standalone Cursor
 * hooks config at ~/.cursor/hooks.json. This approach is used because Cursor plugin
 * hook bundling (using a plugin-relative path) is undocumented — the standalone
 * hooks.json with absolute paths is the only confirmed, stable mechanism.
 *
 * Behaviour:
 *   - Reads ~/.cursor/hooks.json (creates if absent, default: { version: 1, hooks: {} })
 *   - MERGES mori entries; leaves other hooks intact
 *   - Existing mori entries are replaced (identified by command containing "mori-")
 *   - --parity adds beforeSubmitPrompt + postToolUseFailure (legacy telemetry parity)
 *   - PreCompact/PostCompact are wired via ~/.claude/settings.json (install-mori-cursor-plugin.sh --parity)
 *
 * #88 — the API key is NEVER written into hooks.json. It used to be (`--api-key "<secret>"` in
 * every hook command: a secret in a config file and in every hook process's argv, and a
 * shell-quoting hazard), and installing without it left every Cursor event rejected (401).
 * Now the key goes into a 0600 file (default ~/.config/mori/api-key, dir 0700, written
 * atomically) and the hooks reference the FILE (`--api-key-file "<abs path>"`).
 *   - An existing install's `--api-key "<secret>"` is harvested into the key file (migration).
 *   - If an old entry carried a key that cannot be recovered and none is supplied, the
 *     installer aborts instead of "migrating" to an unauthenticated install.
 *   - hooks.json is written 0600, atomically, preserving a symlinked hooks.json's target.
 *   - --dry-run prints the commands, which hold only the key file's path.
 *
 * Usage:
 *   MORI_API_KEY=<key> node install-hooks-cursor.mjs --url <server> [--api-key-file <path>] [--parity] [--dry-run]
 *
 * Options:
 *   --url <server>         Base URL of mori server (or env MORI_SERVER_URL)
 *   --api-key-file <path>  Where the key lives (default ~/.config/mori/api-key)
 *   --api-key <key>        Key to store in the key file (prefer env MORI_API_KEY: argv is visible)
 *   --parity               Wire extended native events for legacy hook parity
 *   --dry-run              Print the resulting JSON without writing anything
 */

import {
  chmodSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync,
  realpathSync, renameSync, unlinkSync, writeSync,
} from 'fs';
import { join, dirname, resolve } from 'path';
import { homedir } from 'os';
import { randomBytes } from 'crypto';
import { fileURLToPath } from 'url';
import { expandHome, readKeyFile } from './lib/config.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

const MINIMAL_EVENTS = ['sessionStart', 'postToolUse', 'stop'];
const PARITY_EXTRA_EVENTS = ['beforeSubmitPrompt', 'postToolUseFailure'];

function parseArgs(argv) {
  const args = { url: '', apiKey: '', apiKeyFile: '', dryRun: false, parity: false };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--url':          args.url        = argv[++i] ?? ''; break;
      case '--api-key':      args.apiKey     = argv[++i] ?? ''; break;
      case '--api-key-file': args.apiKeyFile = argv[++i] ?? ''; break;
      case '--dry-run':      args.dryRun = true; break;
      case '--parity':       args.parity = true; break;
    }
  }
  args.url = args.url || process.env.MORI_SERVER_URL || '';
  // An explicit --api-key wins over an exported MORI_API_KEY (board ruling K2): otherwise a
  // stale key left in the shell silently replaces the one just typed. MORI_API_KEY is still
  // the recommended way to pass the key (--api-key puts it in this process's argv); the
  // shell wrapper passes it only through the environment.
  const envKey = (process.env.MORI_API_KEY || '').trim();
  const flagKey = args.apiKey.trim();
  if (flagKey && envKey && flagKey !== envKey) {
    console.error('Warning: --api-key and MORI_API_KEY are both set and differ; using --api-key.');
  }
  args.apiKey = flagKey || envKey;
  return args;
}

function readHooks(path) {
  if (!existsSync(path)) return { version: 1, hooks: {} };
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    console.error(`Warning: ${path} exists but is not valid JSON — starting fresh.`);
    return { version: 1, hooks: {} };
  }
}

function isMoriEntry(entry) {
  return typeof entry.command === 'string' && entry.command.includes('mori-');
}

function mergeHook(existing, newEntry) {
  const filtered = (existing || []).filter((e) => !isMoriEntry(e));
  return [...filtered, newEntry];
}

/**
 * Recover a key an older install embedded in its mori hook commands.
 * @returns {{found: boolean, key: string}} found = some mori entry carried --api-key;
 *   key = '' when it did but the value could not be parsed.
 */
export function harvestLegacyKey(hooksObj) {
  const re = /--api-key\s+(?:"([^"]*)"|'([^']*)'|([^\s"']+))/;
  let found = false;
  for (const entries of Object.values((hooksObj && hooksObj.hooks) || {})) {
    for (const e of entries || []) {
      if (!isMoriEntry(e) || !/--api-key(\s|$)/.test(e.command)) continue;
      found = true;
      const m = e.command.match(re);
      const key = m ? (m[1] ?? m[2] ?? m[3] ?? '').trim() : '';
      if (key) return { found, key };
    }
  }
  return { found, key: '' };
}

/** A value interpolated into a double-quoted hook command must not be able to break out. */
export function assertCommandSafe(value, label) {
  if (/["`$\\\n\r]/.test(value)) {
    throw new Error(`${label} contains a character that is unsafe in a hook command: ${JSON.stringify(value)}`);
  }
}

/**
 * Write `text` to `path` atomically with `mode`: a sibling temp file (O_EXCL), fsync, rename.
 * If `path` is a symlink, the link's target is replaced and the link is kept.
 */
export function atomicWrite(path, text, mode = 0o600) {
  const real = existsSync(path) ? realpathSync(path) : path;
  const dir = dirname(real);
  const tmp = join(dir, `.${randomBytes(6).toString('hex')}.mori-tmp`);
  const fd = openSync(tmp, 'wx', mode);
  try {
    writeSync(fd, text);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  try {
    renameSync(tmp, real);
  } catch (err) {
    try { unlinkSync(tmp); } catch { /* noop */ }
    throw err;
  }
  chmodSync(real, mode); // the mode on open() applies only at creation
}

/** Store the key in a 0600 file inside a 0700 directory. */
export function writeKeyFile(path, key) {
  const dir = dirname(path);
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  atomicWrite(path, `${key.trim()}\n`, 0o600);
}

/** @param {boolean} parity  @param {string} apiKeyFile  absolute path, or '' for no key */
export function buildHookConfig(parity, scriptsDir, url, apiKeyFile) {
  const contextHook = resolve(scriptsDir, 'mori-context-hook-cursor.mjs');
  const shipEvent   = resolve(scriptsDir, 'mori-ship-event-cursor.mjs');
  const keyFlag     = apiKeyFile ? ` --api-key-file "${apiKeyFile}"` : '';
  const baseShip    = `node "${shipEvent}" --url "${url}"${keyFlag}`;

  const contextEntry = {
    command: `node "${contextHook}" --url "${url}"`,
    matcher: '*',
    timeout: 10,
  };
  const shipEntry = (extraFlags = '') => ({
    command: `${baseShip}${extraFlags}`,
    matcher: '*',
    timeout: 15,
  });

  const config = { version: 1, hooks: {} };
  config.hooks.sessionStart = [contextEntry];
  config.hooks.postToolUse  = [shipEntry(' --event postToolUse')];
  config.hooks.stop         = [shipEntry(' --event stop')];

  if (parity) {
    config.hooks.beforeSubmitPrompt = [shipEntry(' --event beforeSubmitPrompt')];
    config.hooks.postToolUseFailure = [shipEntry(' --event postToolUseFailure')];
  }

  return { config, contextHook, shipEvent };
}

/** Merge mori hook config into existing hooks.json content. */
export function mergeHooksFile(existing, moriConfig) {
  const out = { ...existing, version: existing.version ?? 1, hooks: { ...existing.hooks } };
  for (const [event, entries] of Object.entries(moriConfig.hooks)) {
    out.hooks[event] = mergeHook(out.hooks[event], entries[0]);
  }
  return out;
}

/**
 * Decide the key file and whether a key must be written. Pure except for reading an existing
 * key file. Throws on a configuration that would lose a key or cannot be made safe.
 * @returns {{keyFile: string, keyToWrite: string, unauthenticated: boolean}}
 */
export function planKey({ apiKey, apiKeyFile, existingHooks, home = homedir() }) {
  // Default from `home`, not the path captured when this module was imported.
  const keyFile = resolve(expandHome(apiKeyFile || join(home, '.config', 'mori', 'api-key'), home));
  assertCommandSafe(keyFile, 'key file path');
  const legacy = harvestLegacyKey(existingHooks);
  const supplied = (apiKey || '').trim() || legacy.key;
  if (legacy.found && !legacy.key && !(apiKey || '').trim()) {
    throw new Error(
      'an existing mori hook in hooks.json carries --api-key but its value could not be read; ' +
        're-run with MORI_API_KEY=<key> so the key is not lost.',
    );
  }
  if (supplied) return { keyFile, keyToWrite: supplied, unauthenticated: false };
  if (existsSync(keyFile)) {
    const r = readKeyFile(keyFile, { home });
    if (r.problem) throw new Error(r.problem);
    return { keyFile, keyToWrite: '', unauthenticated: false };
  }
  return { keyFile: '', keyToWrite: '', unauthenticated: true };
}

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (!args.url) {
    console.error('Error: --url <server> is required (or set MORI_SERVER_URL).');
    process.exit(1);
  }

  const hooksPath = join(homedir(), '.cursor', 'hooks.json');
  const existing = readHooks(hooksPath);

  let plan;
  try {
    assertCommandSafe(args.url, 'server URL');
    plan = planKey({ apiKey: args.apiKey, apiKeyFile: args.apiKeyFile, existingHooks: existing });
  } catch (err) {
    console.error(`Error: ${err.message}`);
    process.exit(1);
  }

  const { config: moriConfig, contextHook, shipEvent } = buildHookConfig(
    args.parity,
    __dirname,
    args.url,
    plan.keyFile,
  );
  const merged = mergeHooksFile(existing, moriConfig);
  const output = JSON.stringify(merged, null, 2);

  const events = args.parity
    ? [...MINIMAL_EVENTS, ...PARITY_EXTRA_EVENTS]
    : MINIMAL_EVENTS;

  if (args.dryRun) {
    if (plan.keyToWrite) console.log(`[dry-run] Would write the API key to: ${plan.keyFile} (mode 0600)`);
    console.log(`[dry-run] Would write to: ${hooksPath}`);
    console.log(output);
    return;
  }

  if (plan.keyToWrite) {
    writeKeyFile(plan.keyFile, plan.keyToWrite);
    console.log(`Wrote the API key to: ${plan.keyFile} (mode 0600)`);
  }
  if (plan.unauthenticated) {
    console.warn(
      'Warning: no API key given (MORI_API_KEY or --api-key) and no key file found — hooks will ship ' +
        'unauthenticated, which only works for a server without MORI_API_KEYS.',
    );
  }
  mkdirSync(join(homedir(), '.cursor'), { recursive: true });
  atomicWrite(hooksPath, `${output}\n`, 0o600);

  console.log(`Wrote mori hook entries to: ${hooksPath}`);
  for (const ev of events) {
    console.log(`  ${ev}`);
  }
  console.log(`  context → ${contextHook}`);
  console.log(`  shipper → ${shipEvent}`);
  if (args.parity) {
    console.log('Compat layer (PreCompact/PostCompact): run install-mori-cursor-plugin.sh --parity');
  }
  console.log('Reload Cursor (Ctrl+Shift+P → Reload Window) for hooks to take effect.');
}

// Compare REAL paths: Node resolves the main module through symlinks, so a plain path compare
// makes an installer run through a symlink silently do nothing.
function isMainModule() {
  try {
    return Boolean(process.argv[1]) && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}
if (isMainModule()) {
  main();
}
