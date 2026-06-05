#!/usr/bin/env node
/**
 * tidy-up.mjs — Multi-client mori cleanup tool (Node ESM, cross-platform)
 *
 * Removes bespoke-installer mori entries from Claude Code, Cursor, and
 * Antigravity config files, preparing for a clean plugin install.
 *
 * DRY-RUN BY DEFAULT. Nothing is written unless --confirm is passed.
 *
 * Usage:
 *   node tidy-up.mjs [--confirm] [--client claude|cursor|antigravity|all]
 *                    [--include-skills]
 *
 * Options:
 *   --confirm         Write changes (default: dry-run, prints only)
 *   --client <name>   Limit to one client: claude, cursor, antigravity, or all (default: all)
 *   --include-skills  Also remove bespoke mori skill directories (brief, consult, dream,
 *                     ingest, msg, nats, pensieve, req, wrap) from the client skills dir.
 *                     Directories are backed up before removal. Default: OFF.
 *   --help            Show this help
 */

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync, cpSync, rmSync, statSync } from 'fs';
import { dirname, join } from 'path';
import { homedir, platform } from 'os';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// Signature constants — derived directly from the bespoke installers
// ---------------------------------------------------------------------------

/** Hook command substrings that identify a bespoke/plugin mori hook entry. */
const MORI_HOOK_SUBSTRINGS = [
  'mori-ship-event',
  'mori-post-compact-brief',
  'mori-context-hook',
  '/api/events/raw',
  '/api/precompact',
];

/**
 * Known mori skill directory names — as deployed by the bespoke installers.
 * The Linux/macOS installer (install-mori-claude.sh) deploys skills under their
 * plain name (e.g. "brief") while the Windows installer uses "mori-<name>".
 * We handle both variants.
 */
const MORI_SKILL_NAMES = [
  'brief', 'consult', 'dream', 'ingest', 'msg', 'nats', 'pensieve', 'req', 'wrap',
];

// All hook event keys written by either bespoke installer
const HOOK_EVENTS = [
  'SessionStart', 'PostToolUse', 'PostToolUseFailure',
  'UserPromptSubmit', 'Stop', 'PreCompact', 'PostCompact',
];

// Cursor's camelCase hook event keys (plugin-era install-hooks-cursor.mjs)
const CURSOR_HOOK_EVENTS = ['sessionStart', 'postToolUse', 'stop'];

// ---------------------------------------------------------------------------
// CLI argument parsing
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = { confirm: false, client: 'all', includeSkills: false, help: false };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--confirm':        args.confirm = true; break;
      case '--include-skills': args.includeSkills = true; break;
      case '--help': case '-h': args.help = true; break;
      case '--client':
        args.client = argv[++i] ?? 'all';
        break;
      default:
        // Ignore unknown flags silently to allow forward compatibility
    }
  }
  return args;
}

function showHelp() {
  console.log(`
tidy-up.mjs — Remove bespoke mori installer entries across Claude Code, Cursor, Antigravity

Usage:
  node tidy-up.mjs [--confirm] [--client claude|cursor|antigravity|all] [--include-skills]

Options:
  --confirm         Write changes. Without this flag the tool prints a dry-run report only.
  --client <name>   Limit to one client (default: all).
  --include-skills  Also remove bespoke mori skill dirs (brief, consult, dream, ingest,
                    msg, nats, pensieve, req, wrap) from the client skills directory.
                    A backup is made first. Default: OFF (higher-stakes — opt in explicitly).
  --help            Show this message.

Dry-run example:
  node plugins/mori/scripts/legacy/tidy-up.mjs

Apply changes:
  node plugins/mori/scripts/legacy/tidy-up.mjs --confirm
`);
}

// ---------------------------------------------------------------------------
// Safe JSON read — returns { ok, data, error }
// ---------------------------------------------------------------------------

function readJson(filePath) {
  if (!existsSync(filePath)) {
    return { ok: false, data: null, error: 'file not found' };
  }
  let raw;
  try {
    raw = readFileSync(filePath, 'utf8');
  } catch (err) {
    return { ok: false, data: null, error: `read error: ${err.message}` };
  }
  // Strip BOM if present
  if (raw.charCodeAt(0) === 0xFEFF) raw = raw.slice(1);
  try {
    const data = JSON.parse(raw);
    return { ok: true, data };
  } catch (err) {
    return { ok: false, data: null, error: `JSON parse error: ${err.message}` };
  }
}

// ---------------------------------------------------------------------------
// Atomic backup — <file>.mori-backup-<UTC-ISO>[.<counter>]
// Returns the backup path, or throws.
// ---------------------------------------------------------------------------

function makeBackup(filePath) {
  const base = filePath + '.mori-backup-' + new Date().toISOString().replace(/[:.]/g, '-');
  let dest = base;
  let counter = 0;
  while (existsSync(dest)) {
    counter++;
    dest = `${base}.${counter}`;
  }
  // Read raw bytes to preserve formatting exactly
  const raw = readFileSync(filePath);
  writeFileSync(dest, raw);
  return dest;
}

// ---------------------------------------------------------------------------
// Atomic write — write to <file>.mori-tmp then rename over original
// ---------------------------------------------------------------------------

function atomicWrite(filePath, content) {
  const tmpPath = filePath + '.mori-tmp';
  writeFileSync(tmpPath, content, 'utf8');
  renameSync(tmpPath, filePath);
}

// ---------------------------------------------------------------------------
// Validation gates
// (a) still parses as JSON
// (b) no NON-mori top-level keys removed
// (c) required container structures present
// ---------------------------------------------------------------------------

/**
 * Top-level keys that are owned by mori and may legitimately be removed.
 * "mori" — the Antigravity plugin-era named-hook block written by install-hooks-antigravity.mjs.
 */
const MORI_OWNED_TOP_LEVEL_KEYS = new Set(['mori']);

function validateConfig(originalData, newData) {
  // (a) re-serialise + re-parse to confirm valid JSON
  let reparsed;
  try {
    reparsed = JSON.parse(JSON.stringify(newData));
  } catch (err) {
    return { ok: false, reason: `re-serialisation failed: ${err.message}` };
  }

  // (b) check no NON-mori top-level keys were removed
  const origKeys = Object.keys(originalData);
  const newKeys = new Set(Object.keys(reparsed));
  for (const k of origKeys) {
    if (!newKeys.has(k) && !MORI_OWNED_TOP_LEVEL_KEYS.has(k)) {
      return { ok: false, reason: `top-level key "${k}" would be removed — aborting` };
    }
  }

  // (c) if mcpServers existed before, it must still exist (even if empty)
  if (originalData.mcpServers !== undefined && reparsed.mcpServers === undefined) {
    return { ok: false, reason: '"mcpServers" container disappeared — aborting' };
  }

  return { ok: true };
}

// ---------------------------------------------------------------------------
// Reporting helper
// ---------------------------------------------------------------------------

const SECTION = (label) => console.log(`\n${'─'.repeat(60)}\n  ${label}\n${'─'.repeat(60)}`);
const INFO    = (msg) => console.log(`  ${msg}`);
const FOUND   = (msg) => console.log(`  [found]   ${msg}`);
const REMOVE  = (msg) => console.log(`  [remove]  ${msg}`);
const SKIP    = (msg) => console.log(`  [skip]    ${msg}`);
const WARN    = (msg) => console.log(`  [warn]    ${msg}`);
const ERROR   = (msg) => console.error(`  [ERROR]   ${msg}`);
const BACKUP  = (msg) => console.log(`  [backup]  ${msg}`);
const DONE    = (msg) => console.log(`  [done]    ${msg}`);
const DRYRUN  = (msg) => console.log(`  [dry-run] ${msg}`);

// ---------------------------------------------------------------------------
// Core: detect and remove mori entries from a Claude Code/Cursor settings.json
// Returns { changed, removals: string[] }
// ---------------------------------------------------------------------------

/**
 * Returns true if a hook command string identifies a mori hook.
 *
 * Checks for:
 *   - Path/filename segments starting with "mori-" (e.g. node "/path/mori-ship-event.mjs")
 *   - Known endpoint substrings written by bespoke installers (/api/events/raw etc.)
 *
 * Deliberately does NOT simply match the substring "mori-" in the full command string
 * because user commands like "echo non-mori-message" would be false positives.
 */
function isMoriHookCommand(cmd) {
  if (typeof cmd !== 'string') return false;
  const lower = cmd.toLowerCase();
  // Endpoint substrings from bespoke installers (these are unambiguous)
  if (lower.includes('/api/events/raw') || lower.includes('/api/precompact')) return true;
  // Script-name match: a path segment (after /, \, or ") whose basename starts with "mori-"
  // Matches: node "/path/mori-ship-event.mjs", "/home/x/.claude/mori-ship-event.sh", etc.
  if (/[/"\\]mori-/i.test(cmd)) return true;
  // Bare invocation (no path prefix): "mori-ship-event.sh --url ..."
  if (/^mori-/i.test(cmd.trimStart())) return true;
  return false;
}

/**
 * Returns true if a hook group object (wrapped or flat) is a mori hook.
 * Handles:
 *   - wrapped: { hooks: [{ command, ... }], ... }
 *   - flat:    { command, type, ... }
 *   - plugin cursor marker: { command, _mori_managed: true }
 *   - flat cursor: { command: "node ... mori-..." }
 */
function isMoriHookEntry(entry) {
  if (!entry || typeof entry !== 'object') return false;
  // _mori_managed marker (Cursor/Antigravity bespoke installer)
  if (entry._mori_managed === true) return true;
  // Flat entry with direct command
  if (isMoriHookCommand(entry.command)) return true;
  // Wrapped entry: { hooks: [...] }
  if (Array.isArray(entry.hooks)) {
    return entry.hooks.some((h) => isMoriHookCommand(h?.command));
  }
  return false;
}

/**
 * Process settings.json (Claude Code or Cursor shared ~/.claude/settings.json).
 * Returns { changed, newData, removals } or throws on fatal error.
 */
function processSettingsJson(data) {
  const removals = [];
  // Deep clone to avoid mutating original (needed for validation comparison)
  let d = JSON.parse(JSON.stringify(data));

  // 1. Remove mcpServers.mori
  if (d.mcpServers && typeof d.mcpServers === 'object' && 'mori' in d.mcpServers) {
    FOUND('mcpServers.mori');
    delete d.mcpServers.mori;
    removals.push('mcpServers.mori');
  }

  // 2. Remove mori hook entries from each event
  if (d.hooks && typeof d.hooks === 'object') {
    for (const event of HOOK_EVENTS) {
      if (!Array.isArray(d.hooks[event])) continue;
      const before = d.hooks[event].length;
      const kept = d.hooks[event].filter((entry) => !isMoriHookEntry(entry));
      const removed = before - kept.length;
      if (removed > 0) {
        FOUND(`hooks.${event} — ${removed} mori entry/entries`);
        removals.push(`hooks.${event} (${removed} removed)`);
        if (kept.length === 0) {
          delete d.hooks[event];
        } else {
          d.hooks[event] = kept;
        }
      }
    }
  }

  // 3. Remove mori entries from permissions.allow
  if (d.permissions && Array.isArray(d.permissions.allow)) {
    const before = d.permissions.allow.length;
    const kept = d.permissions.allow.filter((e) => !String(e).startsWith('mcp__mori__'));
    const removed = before - kept.length;
    if (removed > 0) {
      FOUND(`permissions.allow — ${removed} mcp__mori__* entry/entries`);
      removals.push(`permissions.allow (${removed} removed)`);
      d.permissions.allow = kept;
    }
  }

  return { changed: removals.length > 0, newData: d, removals };
}

// ---------------------------------------------------------------------------
// Core: detect and remove mori entries from ~/.cursor/hooks.json
// Cursor hooks.json format: { version, hooks: { <event>: [ { command, matcher, timeout } ] } }
// ---------------------------------------------------------------------------

function processCursorHooksJson(data) {
  const removals = [];
  let d = JSON.parse(JSON.stringify(data));

  const allCursorEvents = new Set([...CURSOR_HOOK_EVENTS]);
  // Also handle any other event keys that might exist with mori entries
  if (d.hooks && typeof d.hooks === 'object') {
    for (const k of Object.keys(d.hooks)) allCursorEvents.add(k);
  }

  if (d.hooks && typeof d.hooks === 'object') {
    for (const event of allCursorEvents) {
      if (!Array.isArray(d.hooks[event])) continue;
      const before = d.hooks[event].length;
      const kept = d.hooks[event].filter((entry) => {
        // Cursor hook entries: { command: "node .../mori-...", matcher, timeout }
        // Use isMoriHookCommand which matches path-segment "mori-" not arbitrary substrings
        if (isMoriHookCommand(entry.command)) return false;
        // Also catch _mori_managed marker
        if (entry._mori_managed === true) return false;
        return true;
      });
      const removed = before - kept.length;
      if (removed > 0) {
        FOUND(`hooks.${event} — ${removed} mori entry/entries`);
        removals.push(`hooks.${event} (${removed} removed)`);
        if (kept.length === 0) {
          delete d.hooks[event];
        } else {
          d.hooks[event] = kept;
        }
      }
    }
  }

  return { changed: removals.length > 0, newData: d, removals };
}

// ---------------------------------------------------------------------------
// Core: detect and remove mori entries from Antigravity hooks.json
// Plugin-era format: { "mori": { ... }, "other": { ... } }
// Bespoke-era format: { hooks: { PostToolUse: [...], ... } } with _mori_managed entries
// We handle both.
// ---------------------------------------------------------------------------

function processAntigravityHooksJson(data) {
  const removals = [];
  let d = JSON.parse(JSON.stringify(data));

  // Plugin-era: named "mori" block at root
  if ('mori' in d) {
    FOUND('"mori" named-hook block');
    delete d.mori;
    removals.push('"mori" named-hook block');
  }

  // Bespoke-era: flat hook entries in .hooks.<event> with _mori_managed or mori commands
  if (d.hooks && typeof d.hooks === 'object') {
    for (const event of Object.keys(d.hooks)) {
      if (!Array.isArray(d.hooks[event])) continue;
      const before = d.hooks[event].length;
      const kept = d.hooks[event].filter((entry) => !isMoriHookEntry(entry));
      const removed = before - kept.length;
      if (removed > 0) {
        FOUND(`hooks.${event} — ${removed} mori entry/entries`);
        removals.push(`hooks.${event} (${removed} removed)`);
        if (kept.length === 0) {
          delete d.hooks[event];
        } else {
          d.hooks[event] = kept;
        }
      }
    }
  }

  return { changed: removals.length > 0, newData: d, removals };
}

// ---------------------------------------------------------------------------
// Core: detect and remove mcpServers.mori from an MCP config file
// ---------------------------------------------------------------------------

function processMcpJson(data) {
  const removals = [];
  let d = JSON.parse(JSON.stringify(data));

  if (d.mcpServers && typeof d.mcpServers === 'object' && 'mori' in d.mcpServers) {
    FOUND('mcpServers.mori');
    delete d.mcpServers.mori;
    removals.push('mcpServers.mori');
  }

  return { changed: removals.length > 0, newData: d, removals };
}

// ---------------------------------------------------------------------------
// Apply changes to a single JSON config file (with backup + validation + atomic write)
// ---------------------------------------------------------------------------

function applyJsonChange(filePath, originalData, newData, confirm, label) {
  const val = validateConfig(originalData, newData);
  if (!val.ok) {
    ERROR(`Validation failed for ${label}: ${val.reason}`);
    ERROR('No changes written.');
    return false;
  }

  const serialised = JSON.stringify(newData, null, 2) + '\n';

  if (!confirm) {
    DRYRUN(`Would write updated ${label} (backup first, then atomic write)`);
    return true;
  }

  // Backup
  let backupPath;
  try {
    backupPath = makeBackup(filePath);
    BACKUP(`${label} → ${backupPath}`);
  } catch (err) {
    ERROR(`Backup failed for ${label}: ${err.message} — skipping write`);
    return false;
  }

  // Atomic write
  try {
    atomicWrite(filePath, serialised);
    DONE(`${label} updated`);
    return true;
  } catch (err) {
    ERROR(`Write failed for ${label}: ${err.message}`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Skills cleanup — handles both "brief" and "mori-brief" variants
// ---------------------------------------------------------------------------

function processSkills(skillsDir, confirm) {
  if (!existsSync(skillsDir)) {
    SKIP(`Skills directory not found: ${skillsDir}`);
    return;
  }

  const variants = [];
  for (const name of MORI_SKILL_NAMES) {
    for (const dirName of [name, `mori-${name}`]) {
      const p = join(skillsDir, dirName);
      if (existsSync(p)) {
        try {
          const stat = statSync(p);
          if (stat.isDirectory()) variants.push({ dirName, p });
        } catch { /* skip */ }
      }
    }
  }

  if (variants.length === 0) {
    SKIP('No mori skill directories found');
    return;
  }

  for (const { dirName, p } of variants) {
    FOUND(`skill dir: ${p}`);
    if (!confirm) {
      DRYRUN(`Would backup and remove skill dir: ${dirName}`);
      continue;
    }

    // Backup
    const backupBase = p + '.mori-backup-' + new Date().toISOString().replace(/[:.]/g, '-');
    let backupDest = backupBase;
    let counter = 0;
    while (existsSync(backupDest)) {
      counter++;
      backupDest = `${backupBase}.${counter}`;
    }

    try {
      cpSync(p, backupDest, { recursive: true });
      BACKUP(`${dirName} → ${backupDest}`);
    } catch (err) {
      ERROR(`Backup failed for skill dir ${dirName}: ${err.message} — skipping`);
      continue;
    }

    try {
      rmSync(p, { recursive: true, force: true });
      DONE(`Removed skill dir: ${dirName}`);
    } catch (err) {
      ERROR(`Remove failed for skill dir ${dirName}: ${err.message}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Client: Claude Code
// Files: ~/.claude/settings.json  (or $CLAUDE_CONFIG_DIR/settings.json)
//        ~/.config/Code/User/settings.json (VS Code extension, Linux/macOS)
//        %APPDATA%\Code\User\settings.json  (VS Code extension, Windows)
// Skills: ~/.claude/skills/
// ---------------------------------------------------------------------------

function tidyClaude(confirm, includeSkills) {
  SECTION('Claude Code');

  const claudeDir = process.env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude');

  const targets = [
    { label: 'Claude Code CLI settings', path: join(claudeDir, 'settings.json'), type: 'claude-settings' },
  ];

  // VS Code settings (optional, may not exist)
  const isWindows = platform() === 'win32';
  const isDarwin  = platform() === 'darwin';
  let vscodeSettingsPath;
  if (isWindows) {
    vscodeSettingsPath = join(process.env.APPDATA ?? join(homedir(), 'AppData', 'Roaming'), 'Code', 'User', 'settings.json');
  } else if (isDarwin) {
    vscodeSettingsPath = join(homedir(), 'Library', 'Application Support', 'Code', 'User', 'settings.json');
  } else {
    vscodeSettingsPath = join(homedir(), '.config', 'Code', 'User', 'settings.json');
  }
  targets.push({ label: 'VS Code extension settings', path: vscodeSettingsPath, type: 'claude-settings' });

  for (const { label, path, type } of targets) {
    INFO(`\nFile: ${path}`);
    const { ok, data, error } = readJson(path);
    if (!ok) {
      SKIP(`${label}: ${error}`);
      continue;
    }

    const { changed, newData, removals } = processSettingsJson(data);
    if (!changed) {
      SKIP(`${label}: nothing to remove`);
      continue;
    }

    for (const r of removals) REMOVE(r);
    applyJsonChange(path, data, newData, confirm, label);
  }

  // Skills
  if (includeSkills) {
    INFO('\nSkills:');
    processSkills(join(claudeDir, 'skills'), confirm);
  }
}

// ---------------------------------------------------------------------------
// Client: Cursor
// MCP:    ~/.cursor/mcp.json
// Hooks:  ~/.cursor/hooks.json  (plugin-era)
// Shared hooks/settings from ~/.claude/settings.json already handled by Claude section
// Skills: ~/.claude/skills/ (Cursor uses same skills dir as Claude Code)
// ---------------------------------------------------------------------------

function tidyCursor(confirm, includeSkills) {
  SECTION('Cursor');

  const isWindows = platform() === 'win32';
  const cursorDir = isWindows
    ? join(process.env.APPDATA ?? join(homedir(), 'AppData', 'Roaming'), 'Cursor')
    : join(homedir(), '.cursor');

  // 1. Cursor MCP config
  const mcpPath = join(cursorDir, 'mcp.json');
  INFO(`\nFile: ${mcpPath}`);
  {
    const { ok, data, error } = readJson(mcpPath);
    if (!ok) {
      SKIP(`Cursor MCP config: ${error}`);
    } else {
      const { changed, newData, removals } = processMcpJson(data);
      if (!changed) {
        SKIP('Cursor MCP config: nothing to remove');
      } else {
        for (const r of removals) REMOVE(r);
        applyJsonChange(mcpPath, data, newData, confirm, 'Cursor MCP config');
      }
    }
  }

  // 2. Cursor hooks.json (plugin-era standalone hooks)
  const hooksPath = join(cursorDir, 'hooks.json');
  INFO(`\nFile: ${hooksPath}`);
  {
    const { ok, data, error } = readJson(hooksPath);
    if (!ok) {
      SKIP(`Cursor hooks.json: ${error}`);
    } else {
      const { changed, newData, removals } = processCursorHooksJson(data);
      if (!changed) {
        SKIP('Cursor hooks.json: nothing to remove');
      } else {
        for (const r of removals) REMOVE(r);
        applyJsonChange(hooksPath, data, newData, confirm, 'Cursor hooks.json');
      }
    }
  }

  // 3. ~/.claude/settings.json (bespoke Cursor installer wrote hooks here too)
  const claudeDir = process.env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude');
  const claudeSettingsPath = join(claudeDir, 'settings.json');
  INFO(`\nFile: ${claudeSettingsPath} (Cursor bespoke hook entries)`);
  {
    const { ok, data, error } = readJson(claudeSettingsPath);
    if (!ok) {
      SKIP(`Claude settings (for Cursor hooks): ${error}`);
    } else {
      const { changed, newData, removals } = processSettingsJson(data);
      if (!changed) {
        SKIP('Claude settings (Cursor hooks): nothing to remove');
      } else {
        for (const r of removals) REMOVE(r);
        applyJsonChange(claudeSettingsPath, data, newData, confirm, 'Claude settings (Cursor hooks)');
      }
    }
  }

  // Skills
  if (includeSkills) {
    INFO('\nSkills:');
    processSkills(join(claudeDir, 'skills'), confirm);
  }
}

// ---------------------------------------------------------------------------
// Client: Antigravity
// MCP:    ~/.gemini/antigravity/mcp_config.json
//         ~/.gemini/antigravity-ide/mcp_config.json
// Hooks:  ~/.gemini/config/hooks.json  (plugin-era)
//         ~/.gemini/antigravity/hooks.json   (bespoke-era)
//         ~/.gemini/antigravity-ide/hooks.json (bespoke-era)
// ---------------------------------------------------------------------------

function tidyAntigravity(confirm, includeSkills) {
  SECTION('Antigravity');

  const geminiBase = join(homedir(), '.gemini');

  // MCP configs (bespoke-era)
  const mcpTargets = [
    { label: 'Antigravity CLI mcp_config.json', path: join(geminiBase, 'antigravity', 'mcp_config.json') },
    { label: 'Antigravity IDE mcp_config.json', path: join(geminiBase, 'antigravity-ide', 'mcp_config.json') },
  ];

  for (const { label, path } of mcpTargets) {
    INFO(`\nFile: ${path}`);
    const { ok, data, error } = readJson(path);
    if (!ok) {
      SKIP(`${label}: ${error}`);
      continue;
    }
    const { changed, newData, removals } = processMcpJson(data);
    if (!changed) {
      SKIP(`${label}: nothing to remove`);
      continue;
    }
    for (const r of removals) REMOVE(r);
    applyJsonChange(path, data, newData, confirm, label);
  }

  // Hooks — plugin-era: ~/.gemini/config/hooks.json
  const pluginHooksPath = join(geminiBase, 'config', 'hooks.json');
  INFO(`\nFile: ${pluginHooksPath}`);
  {
    const { ok, data, error } = readJson(pluginHooksPath);
    if (!ok) {
      SKIP(`Antigravity plugin hooks.json: ${error}`);
    } else {
      const { changed, newData, removals } = processAntigravityHooksJson(data);
      if (!changed) {
        SKIP('Antigravity plugin hooks.json: nothing to remove');
      } else {
        for (const r of removals) REMOVE(r);
        applyJsonChange(pluginHooksPath, data, newData, confirm, 'Antigravity plugin hooks.json');
      }
    }
  }

  // Hooks — bespoke-era per-profile hooks.json
  const bespokeHookTargets = [
    { label: 'Antigravity CLI hooks.json', path: join(geminiBase, 'antigravity', 'hooks.json') },
    { label: 'Antigravity IDE hooks.json', path: join(geminiBase, 'antigravity-ide', 'hooks.json') },
  ];

  for (const { label, path } of bespokeHookTargets) {
    INFO(`\nFile: ${path}`);
    const { ok, data, error } = readJson(path);
    if (!ok) {
      SKIP(`${label}: ${error}`);
      continue;
    }
    const { changed, newData, removals } = processAntigravityHooksJson(data);
    if (!changed) {
      SKIP(`${label}: nothing to remove`);
      continue;
    }
    for (const r of removals) REMOVE(r);
    applyJsonChange(path, data, newData, confirm, label);
  }

  // Skills: Antigravity bespoke installer put skills in ~/.claude/skills/ (same as Claude)
  if (includeSkills) {
    const claudeDir = process.env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude');
    INFO('\nSkills:');
    processSkills(join(claudeDir, 'skills'), confirm);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) {
    showHelp();
    process.exit(0);
  }

  const confirm = args.confirm;
  const client  = args.client;
  const includeSkills = args.includeSkills;

  if (!['claude', 'cursor', 'antigravity', 'all'].includes(client)) {
    console.error(`Unknown --client value: "${client}". Use: claude, cursor, antigravity, or all.`);
    process.exit(1);
  }

  console.log('');
  console.log('mori tidy-up — remove bespoke installer entries');
  console.log('');
  if (!confirm) {
    console.log('  DRY-RUN MODE (default). No files will be modified.');
    console.log('  Run with --confirm to apply changes.');
  } else {
    console.log('  CONFIRM MODE — changes will be written.');
  }
  console.log(`  Client: ${client}`);
  if (includeSkills) console.log('  --include-skills: mori skill directories will also be removed.');

  if (client === 'claude' || client === 'all')       tidyClaude(confirm, includeSkills);
  if (client === 'cursor' || client === 'all')       tidyCursor(confirm, includeSkills);
  if (client === 'antigravity' || client === 'all')  tidyAntigravity(confirm, includeSkills);

  console.log('');
  console.log('─'.repeat(60));
  if (!confirm) {
    console.log('  Dry-run complete. Use --confirm to apply the changes above.');
  } else {
    console.log('  Done. Reload your IDE/editor to apply changes.');
  }
  console.log('');
}

main();
