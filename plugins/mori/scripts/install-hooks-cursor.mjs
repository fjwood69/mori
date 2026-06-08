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
 * Usage:
 *   node install-hooks-cursor.mjs --url <server> --api-key <key> [--parity] [--dry-run]
 *
 * Options:
 *   --url <server>   Base URL of mori server (or env MORI_SERVER_URL)
 *   --api-key <key>  API key (or env MORI_API_KEY)
 *   --parity         Wire extended native events for legacy hook parity
 *   --dry-run        Print the resulting JSON without writing it
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';
import { join, dirname, resolve } from 'path';
import { homedir } from 'os';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

const MINIMAL_EVENTS = ['sessionStart', 'postToolUse', 'stop'];
const PARITY_EXTRA_EVENTS = ['beforeSubmitPrompt', 'postToolUseFailure'];

function parseArgs(argv) {
  const args = { url: '', apiKey: '', dryRun: false, parity: false };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--url':     args.url    = argv[++i] ?? ''; break;
      case '--api-key': args.apiKey = argv[++i] ?? ''; break;
      case '--dry-run': args.dryRun = true; break;
      case '--parity':  args.parity = true; break;
    }
  }
  args.url    = args.url    || process.env.MORI_SERVER_URL || '';
  args.apiKey = args.apiKey || process.env.MORI_API_KEY    || '';
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

/** @param {boolean} parity */
export function buildHookConfig(parity, scriptsDir, url, apiKey) {
  const contextHook = resolve(scriptsDir, 'mori-context-hook-cursor.mjs');
  const shipEvent   = resolve(scriptsDir, 'mori-ship-event-cursor.mjs');
  const apiKeyFlag = apiKey ? ` --api-key "${apiKey}"` : '';
  const baseShip   = `node "${shipEvent}" --url "${url}"${apiKeyFlag}`;

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

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (!args.url) {
    console.error('Error: --url <server> is required (or set MORI_SERVER_URL).');
    process.exit(1);
  }

  const { config: moriConfig, contextHook, shipEvent } = buildHookConfig(
    args.parity,
    __dirname,
    args.url,
    args.apiKey,
  );

  const hooksPath = join(homedir(), '.cursor', 'hooks.json');
  const existing = readHooks(hooksPath);
  const merged = mergeHooksFile(existing, moriConfig);
  const output = JSON.stringify(merged, null, 2);

  const events = args.parity
    ? [...MINIMAL_EVENTS, ...PARITY_EXTRA_EVENTS]
    : MINIMAL_EVENTS;

  if (args.dryRun) {
    console.log(`[dry-run] Would write to: ${hooksPath}`);
    console.log(output);
    return;
  }

  mkdirSync(join(homedir(), '.cursor'), { recursive: true });
  writeFileSync(hooksPath, output, 'utf8');

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

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main();
}
