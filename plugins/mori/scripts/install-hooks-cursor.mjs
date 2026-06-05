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
 *   - MERGES mori entries for sessionStart, postToolUse, stop; leaves other hooks intact
 *   - Existing non-mori hook entries for those events are preserved (mori entries appended)
 *   - Existing mori entries are replaced (identified by command containing "mori-")
 *   - Prints a summary of what was written (or would be written in --dry-run mode)
 *
 * Usage:
 *   node install-hooks-cursor.mjs --url <server> --api-key <key> [--dry-run]
 *   MORI_SERVER_URL=<server> MORI_API_KEY=<key> node install-hooks-cursor.mjs
 *
 * Options:
 *   --url <server>   Base URL of mori server (or env MORI_SERVER_URL)
 *   --api-key <key>  API key (or env MORI_API_KEY; may be omitted for unauthenticated servers)
 *   --dry-run        Print the resulting JSON without writing it
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';
import { join, dirname, resolve } from 'path';
import { homedir } from 'os';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---- Arg / env parsing ---------------------------------------------------------

function parseArgs(argv) {
  const args = { url: '', apiKey: '', dryRun: false };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--url':     args.url    = argv[++i] ?? ''; break;
      case '--api-key': args.apiKey = argv[++i] ?? ''; break;
      case '--dry-run': args.dryRun = true; break;
    }
  }
  args.url    = args.url    || process.env.MORI_SERVER_URL || '';
  args.apiKey = args.apiKey || process.env.MORI_API_KEY    || '';
  return args;
}

// ---- Helpers -------------------------------------------------------------------

/** Read existing hooks.json or return a fresh scaffold. */
function readHooks(path) {
  if (!existsSync(path)) return { version: 1, hooks: {} };
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    console.error(`Warning: ${path} exists but is not valid JSON — starting fresh.`);
    return { version: 1, hooks: {} };
  }
}

/** Return true if a hook command string was written by this installer. */
function isMoriEntry(entry) {
  return typeof entry.command === 'string' && entry.command.includes('mori-');
}

/**
 * Merge mori entries into an event's hook array.
 * Removes existing mori entries, then appends the new one.
 */
function mergeHook(existing, newEntry) {
  const filtered = (existing || []).filter((e) => !isMoriEntry(e));
  return [...filtered, newEntry];
}

// ---- Main ----------------------------------------------------------------------

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (!args.url) {
    console.error('Error: --url <server> is required (or set MORI_SERVER_URL).');
    process.exit(1);
  }

  // Resolve absolute paths to hook scripts (relative to this file's own location)
  const contextHook = resolve(__dirname, 'mori-context-hook-cursor.mjs');
  const shipEvent   = resolve(__dirname, 'mori-ship-event-cursor.mjs');

  // Build the base ship-event command
  const apiKeyFlag = args.apiKey ? ` --api-key "${args.apiKey}"` : '';
  const baseShip   = `node "${shipEvent}" --url "${args.url}"${apiKeyFlag}`;

  // New mori hook entries
  const contextEntry = {
    command: `node "${contextHook}" --url "${args.url}"`,
    matcher: '*',
    timeout: 10,
  };
  const shipEntry = (extraFlags = '') => ({
    command: `${baseShip}${extraFlags}`,
    matcher: '*',
    timeout: 15,
  });

  // Load and merge
  const hooksPath = join(homedir(), '.cursor', 'hooks.json');
  const config = readHooks(hooksPath);
  if (!config.hooks) config.hooks = {};

  config.hooks.sessionStart  = mergeHook(config.hooks.sessionStart, contextEntry);
  config.hooks.postToolUse   = mergeHook(config.hooks.postToolUse,  shipEntry(' --event postToolUse'));
  config.hooks.stop          = mergeHook(config.hooks.stop,         shipEntry(' --event stop'));

  const output = JSON.stringify(config, null, 2);

  if (args.dryRun) {
    console.log(`[dry-run] Would write to: ${hooksPath}`);
    console.log(output);
    return;
  }

  // Ensure ~/.cursor exists
  mkdirSync(join(homedir(), '.cursor'), { recursive: true });
  writeFileSync(hooksPath, output, 'utf8');

  console.log(`Wrote mori hook entries to: ${hooksPath}`);
  console.log(`  sessionStart  → ${contextHook}`);
  console.log(`  postToolUse   → ${shipEvent}`);
  console.log(`  stop          → ${shipEvent}`);
  console.log('Reload Cursor (Ctrl+Shift+P → Reload Window) for hooks to take effect.');
}

main();
