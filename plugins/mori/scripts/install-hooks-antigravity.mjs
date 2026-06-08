/**
 * install-hooks-antigravity.mjs — Install mori hooks into ~/.gemini/config/hooks.json (Node ESM)
 *
 * Writes absolute paths to the mori Antigravity hook scripts into the standalone
 * Antigravity hooks config at ~/.gemini/config/hooks.json using the named-hook schema.
 *
 * Named-hook schema:
 *   {
 *     "<name>": {
 *       "<Event>": [
 *         { "matcher": "...", "hooks": [ { "type": "command", "command": "...", "timeout": N } ] }
 *       ]
 *     }
 *   }
 *
 * Mori uses the named hook "mori". Existing non-mori named hooks are preserved.
 *
 * Behaviour:
 *   - Reads ~/.gemini/config/hooks.json (creates if absent)
 *   - Writes/replaces the "mori" named hook with entries for PostToolUse, Stop, PreInvocation
 *   - All other named hooks are left untouched
 *   - Prints a summary (or the full JSON in --dry-run mode)
 *
 * Usage:
 *   node install-hooks-antigravity.mjs --url <server> --api-key <key> [--dry-run]
 *   MORI_SERVER_URL=<server> MORI_API_KEY=<key> node install-hooks-antigravity.mjs
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
  const args = { url: '', apiKey: '', target: '', dryRun: false };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--url':     args.url    = argv[++i] ?? ''; break;
      case '--api-key': args.apiKey = argv[++i] ?? ''; break;
      case '--target':  args.target = argv[++i] ?? ''; break;
      case '--dry-run': args.dryRun = true; break;
    }
  }
  args.url    = args.url    || process.env.MORI_SERVER_URL || '';
  args.apiKey = args.apiKey || process.env.MORI_API_KEY    || '';
  return args;
}

// ---- Helpers -------------------------------------------------------------------

/** Read existing hooks.json or return an empty object. */
function readHooks(path) {
  if (!existsSync(path)) return {};
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    console.error(`Warning: ${path} exists but is not valid JSON — starting fresh.`);
    return {};
  }
}

/** Build a single Antigravity hook array entry. */
function hookEntry(command, timeout = 15) {
  return {
    matcher: '*',
    hooks: [{ type: 'command', command, timeout }],
  };
}

// ---- Main ----------------------------------------------------------------------

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (!args.url) {
    console.error('Error: --url <server> is required (or set MORI_SERVER_URL).');
    process.exit(1);
  }

  // Resolve absolute paths to hook scripts
  const contextHook     = resolve(__dirname, 'mori-context-hook-antigravity.mjs');
  const shipEvent       = resolve(__dirname, 'mori-ship-event-antigravity.mjs');
  const postCompactHook = resolve(__dirname, 'mori-post-compact-hook-antigravity.mjs');

  const apiKeyFlag = args.apiKey ? ` --api-key "${args.apiKey}"` : '';
  const baseShip   = `node "${shipEvent}" --url "${args.url}"${apiKeyFlag}`;

  // Build the "mori" named-hook block
  const moriHook = {
    PreInvocation: [hookEntry(`node "${contextHook}" --url "${args.url}"`, 10)],
    PostToolUse:   [hookEntry(`${baseShip} --event PostToolUse`, 15)],
    Stop:          [hookEntry(`${baseShip} --event Stop`, 20)],
    PostCompact:   [hookEntry(`node "${postCompactHook}"`, 10)],
  };

  // Determine target paths
  const targets = [];
  const geminiBase = join(homedir(), '.gemini');
  if (args.target === 'cli') {
    targets.push({ label: 'Antigravity CLI hooks.json', path: join(geminiBase, 'antigravity', 'hooks.json') });
  } else if (args.target === 'ide') {
    targets.push({ label: 'Antigravity IDE hooks.json', path: join(geminiBase, 'antigravity-ide', 'hooks.json') });
  } else if (args.target === 'both') {
    targets.push({ label: 'Antigravity CLI hooks.json', path: join(geminiBase, 'antigravity', 'hooks.json') });
    targets.push({ label: 'Antigravity IDE hooks.json', path: join(geminiBase, 'antigravity-ide', 'hooks.json') });
  } else {
    // Default: write to the active configuration profile (via ~/.gemini/config/hooks.json symlink)
    targets.push({ label: 'Active profile hooks.json', path: join(geminiBase, 'config', 'hooks.json') });
  }

  for (const t of targets) {
    const config = readHooks(t.path);
    config.mori = moriHook;
    const output = JSON.stringify(config, null, 2);

    if (args.dryRun) {
      console.log(`[dry-run] Would write to: ${t.path}`);
      console.log(output);
    } else {
      mkdirSync(dirname(t.path), { recursive: true });
      writeFileSync(t.path, output, 'utf8');
      console.log(`Wrote mori hook entries to: ${t.path}`);
    }
  }

  if (!args.dryRun) {
    console.log(`  mori.PreInvocation → ${contextHook}`);
    console.log(`  mori.PostToolUse   → ${shipEvent}`);
    console.log(`  mori.Stop          → ${shipEvent}`);
    console.log(`  mori.PostCompact   → ${postCompactHook}`);
    console.log('Restart Antigravity for hooks to take effect.');
  }
}

main();
