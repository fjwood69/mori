/**
 * test_tidy_up.mjs — Hermetic tests for plugins/mori/scripts/legacy/tidy-up.mjs
 *
 * Operates entirely on temporary directories — never touches real ~/.claude, ~/.cursor,
 * or ~/.gemini config. Temp dirs are cleaned up on exit.
 *
 * Run: node plugins/mori/tests/test_tidy_up.mjs
 */

import { spawnSync } from 'child_process';
import {
  mkdtempSync, writeFileSync, readFileSync, mkdirSync,
  existsSync, rmSync, readdirSync, statSync,
} from 'fs';
import { tmpdir } from 'os';
import { join, resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const TIDY_UP = resolve(__dirname, '../scripts/legacy/tidy-up.mjs');

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;
const errors = [];

function assert(condition, name, detail = '') {
  if (condition) {
    console.log(`  PASS  ${name}`);
    passed++;
  } else {
    const msg = `  FAIL  ${name}${detail ? ': ' + detail : ''}`;
    console.error(msg);
    errors.push(msg);
    failed++;
  }
}

/**
 * Run tidy-up.mjs with a custom HOME pointing at a temp dir.
 * Returns { status, stdout, stderr }.
 */
function run(fakeHome, extraArgs = []) {
  const result = spawnSync(
    process.execPath,
    [TIDY_UP, ...extraArgs],
    {
      env: {
        ...process.env,
        HOME: fakeHome,
        USERPROFILE: fakeHome,
        APPDATA: join(fakeHome, 'AppData', 'Roaming'),
        CLAUDE_CONFIG_DIR: join(fakeHome, '.claude'),
      },
      encoding: 'utf8',
      timeout: 15000,
    }
  );
  return {
    status: result.status ?? -1,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
  };
}

/**
 * Write a JSON file, creating parent dirs as needed.
 */
function writeJson(filePath, data) {
  mkdirSync(dirname(filePath), { recursive: true });
  writeFileSync(filePath, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

/**
 * Read JSON from a file.
 */
function readJson(filePath) {
  return JSON.parse(readFileSync(filePath, 'utf8'));
}

/**
 * Check whether a backup file exists next to filePath.
 * Returns true if any file matching <filePath>.mori-backup-* exists.
 */
function hasBackup(filePath) {
  const dir = dirname(filePath);
  const base = filePath.split('/').pop();
  if (!existsSync(dir)) return false;
  return readdirSync(dir).some((f) => f.startsWith(base + '.mori-backup-'));
}

// ---- Temp dir management ---------------------------------------------------

const ALL_TMPS = [];

function makeTmp() {
  const tmp = mkdtempSync(join(tmpdir(), 'mori-tidy-test-'));
  ALL_TMPS.push(tmp);
  return tmp;
}

function cleanupAll() {
  for (const tmp of ALL_TMPS) {
    try { rmSync(tmp, { recursive: true, force: true }); } catch { /* noop */ }
  }
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** A settings.json with both mori and non-mori content. */
function mixedSettings() {
  return {
    mcpServers: {
      mori: { type: 'http', url: 'http://localhost:8968/mcp' },
      other: { type: 'http', url: 'http://example.com/mcp' },
    },
    hooks: {
      PostToolUse: [
        {
          matcher: '*',
          hooks: [{ type: 'command', command: '"/home/user/.claude/mori-ship-event.sh" --url "http://localhost:8968" --mode raw' }],
        },
        {
          matcher: 'Read',
          hooks: [{ type: 'command', command: 'echo non-mori-hook' }],
        },
      ],
      Stop: [
        {
          hooks: [{ type: 'command', command: '"/home/user/.claude/mori-ship-event.sh" --mode raw' }],
        },
      ],
      PreCompact: [
        {
          hooks: [{ type: 'command', command: '"/home/user/.claude/mori-ship-event.sh" --mode precompact' }],
        },
      ],
      PostCompact: [
        {
          hooks: [{ type: 'command', command: '"/home/user/.claude/mori-post-compact-brief.sh"' }],
        },
      ],
    },
    permissions: {
      allow: [
        'mcp__mori__brief',
        'mcp__mori__memory_read',
        'Bash(*)',
        'mcp__other__tool',
      ],
    },
    someOtherKey: 'should be preserved',
  };
}

/** A cursor hooks.json with both mori and non-mori entries. */
function mixedCursorHooks() {
  return {
    version: 1,
    hooks: {
      sessionStart: [
        { command: 'node "/usr/local/lib/mori/scripts/mori-context-hook-cursor.mjs"', matcher: '*', timeout: 10 },
        { command: 'echo non-mori-session-hook', matcher: '*', timeout: 5 },
      ],
      postToolUse: [
        { command: 'node "/usr/local/lib/mori/scripts/mori-ship-event-cursor.mjs" --event postToolUse', matcher: '*', timeout: 15 },
      ],
      stop: [
        { command: 'node "/usr/local/lib/mori/scripts/mori-ship-event-cursor.mjs" --event stop', matcher: '*', timeout: 15 },
      ],
      customEvent: [
        { command: 'echo custom-non-mori', matcher: '*', timeout: 5 },
      ],
    },
  };
}

/** An Antigravity hooks.json with plugin-era mori block + other named hooks. */
function mixedAntigravityHooks() {
  return {
    mori: {
      PreInvocation: [
        { matcher: '*', hooks: [{ type: 'command', command: 'node "/path/mori-context-hook-antigravity.mjs"', timeout: 10 }] },
      ],
      PostToolUse: [
        { matcher: '*', hooks: [{ type: 'command', command: 'node "/path/mori-ship-event-antigravity.mjs" --event PostToolUse', timeout: 15 }] },
      ],
      Stop: [
        { matcher: '*', hooks: [{ type: 'command', command: 'node "/path/mori-ship-event-antigravity.mjs" --event Stop', timeout: 20 }] },
      ],
    },
    otherTool: {
      PostToolUse: [
        { matcher: '*', hooks: [{ type: 'command', command: 'echo other-tool-hook', timeout: 5 }] },
      ],
    },
  };
}

/** An Antigravity hooks.json with bespoke-era _mori_managed entries. */
function bespokeAntigravityHooks() {
  return {
    hooks: {
      PostToolUse: [
        { type: 'command', command: 'powershell -File "C:\\path\\mori-ship-event.ps1" -Mode raw', _mori_managed: true },
        { type: 'command', command: 'echo non-mori', _mori_managed: false },
      ],
      Stop: [
        { type: 'command', command: 'powershell -File "C:\\path\\mori-ship-event.ps1" -Mode raw', _mori_managed: true },
      ],
    },
  };
}

/** A settings.json with NO mori content. */
function cleanSettings() {
  return {
    someKey: 'someValue',
    mcpServers: { other: { type: 'http', url: 'http://example.com' } },
    permissions: { allow: ['Bash(*)'] },
  };
}

/** A malformed JSON file. */
function malformedJson() {
  return '{ this is not : valid JSON ';
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

console.log('\nmori tidy-up tests\n');

// ---------------------------------------------------------------------------
// Suite 1: Claude — dry-run does NOT write, reports correctly
// ---------------------------------------------------------------------------

{
  console.log('\n[1] Claude dry-run — reports removals, writes nothing');

  const tmp = makeTmp();
  const settingsPath = join(tmp, '.claude', 'settings.json');
  writeJson(settingsPath, mixedSettings());
  const originalMtime = statSync(settingsPath).mtimeMs;

  const r = run(tmp, ['--client', 'claude']);

  assert(r.status === 0, '1.1 exits 0');
  assert(r.stdout.includes('[found]'), '1.2 reports found entries');
  assert(r.stdout.includes('mcpServers.mori'), '1.3 reports mcpServers.mori');
  assert(r.stdout.includes('mcp__mori__'), '1.4 reports permissions.allow removals');
  assert(r.stdout.includes('dry-run'), '1.5 says dry-run');
  assert(!hasBackup(settingsPath), '1.6 no backup file created');

  const mtime = statSync(settingsPath).mtimeMs;
  assert(mtime === originalMtime, '1.7 file mtime unchanged (not written)');

  // Verify the file content is still the original
  const still = readJson(settingsPath);
  assert('mori' in still.mcpServers, '1.8 mcpServers.mori still present');
}

// ---------------------------------------------------------------------------
// Suite 2: Claude — --confirm removes ONLY mori entries, leaves non-mori intact
// ---------------------------------------------------------------------------

{
  console.log('\n[2] Claude --confirm — removes mori entries, leaves non-mori intact');

  const tmp = makeTmp();
  const settingsPath = join(tmp, '.claude', 'settings.json');
  writeJson(settingsPath, mixedSettings());

  const r = run(tmp, ['--client', 'claude', '--confirm']);

  assert(r.status === 0, '2.1 exits 0');
  assert(hasBackup(settingsPath), '2.2 backup file created');

  const result = readJson(settingsPath);

  // Mori MCP gone
  assert(!('mori' in (result.mcpServers ?? {})), '2.3 mcpServers.mori removed');
  // Non-mori MCP still present
  assert('other' in (result.mcpServers ?? {}), '2.4 mcpServers.other preserved');

  // Mori permissions gone, non-mori preserved
  const allow = result.permissions?.allow ?? [];
  assert(!allow.some((e) => e.startsWith('mcp__mori__')), '2.5 mcp__mori__* permissions removed');
  assert(allow.includes('Bash(*)'), '2.6 Bash(*) permission preserved');
  assert(allow.includes('mcp__other__tool'), '2.7 mcp__other__tool preserved');

  // Mori hooks gone — PostToolUse should only have the non-mori entry
  const ptu = result.hooks?.PostToolUse ?? [];
  assert(ptu.every((e) => !e.hooks?.some((h) => h.command?.includes('mori-ship-event'))), '2.8 mori PostToolUse hook removed');
  assert(ptu.some((e) => e.hooks?.some((h) => h.command?.includes('non-mori-hook'))), '2.9 non-mori PostToolUse hook preserved');

  // Stop and PreCompact hooks removed (were mori-only, so key should be gone or empty)
  const stop = result.hooks?.Stop;
  assert(!stop || stop.length === 0, '2.10 Stop hook empty after mori removal');

  // Non-mori top-level key preserved
  assert(result.someOtherKey === 'should be preserved', '2.11 non-mori top-level key preserved');

  // Output is still valid JSON (validated by JSON.parse above — if we got here, it is)
  assert(true, '2.12 output is valid JSON');
}

// ---------------------------------------------------------------------------
// Suite 3: Cursor — hooks.json with mixed entries → only mori removed
// ---------------------------------------------------------------------------

{
  console.log('\n[3] Cursor hooks.json — mixed entries → only mori removed');

  const tmp = makeTmp();
  const hooksPath = join(tmp, '.cursor', 'hooks.json');
  writeJson(hooksPath, mixedCursorHooks());

  const r = run(tmp, ['--client', 'cursor', '--confirm']);

  assert(r.status === 0, '3.1 exits 0');
  assert(hasBackup(hooksPath), '3.2 backup created');

  const result = readJson(hooksPath);

  // mori entries removed from sessionStart
  // Use the same path-segment heuristic: a mori hook command references a script
  // whose path segment starts with "mori-" (i.e. matches /[/"\\]mori-/ or is bare "mori-...")
  const ss = result.hooks?.sessionStart ?? [];
  const hasMoriCmd = (cmd) => typeof cmd === 'string' && (
    /[/"\\]mori-/i.test(cmd) || /^mori-/i.test(cmd.trimStart())
  );
  assert(!ss.some((e) => hasMoriCmd(e.command)), '3.3 mori sessionStart entries removed');
  assert(ss.some((e) => e.command?.includes('non-mori-session-hook')), '3.4 non-mori sessionStart entry preserved');

  // postToolUse and stop should be empty or gone (were mori-only)
  const ptu = result.hooks?.postToolUse ?? [];
  assert(ptu.length === 0, '3.5 postToolUse entries removed (was mori-only)');

  // customEvent preserved
  const custom = result.hooks?.customEvent ?? [];
  assert(custom.length === 1 && custom[0].command === 'echo custom-non-mori', '3.6 customEvent preserved');
}

// ---------------------------------------------------------------------------
// Suite 4: Antigravity — plugin-era hooks.json (named "mori" block) removed
// ---------------------------------------------------------------------------

{
  console.log('\n[4] Antigravity plugin hooks.json — "mori" block removed, others preserved');

  const tmp = makeTmp();
  const hooksPath = join(tmp, '.gemini', 'config', 'hooks.json');
  writeJson(hooksPath, mixedAntigravityHooks());

  const r = run(tmp, ['--client', 'antigravity', '--confirm']);

  assert(r.status === 0, '4.1 exits 0');
  assert(hasBackup(hooksPath), '4.2 backup created');

  const result = readJson(hooksPath);
  assert(!('mori' in result), '4.3 "mori" named block removed');
  assert('otherTool' in result, '4.4 "otherTool" block preserved');
}

// ---------------------------------------------------------------------------
// Suite 5: Antigravity — bespoke-era hooks.json with _mori_managed entries
// ---------------------------------------------------------------------------

{
  console.log('\n[5] Antigravity bespoke hooks.json — _mori_managed entries removed');

  const tmp = makeTmp();
  const hooksPath = join(tmp, '.gemini', 'antigravity', 'hooks.json');
  writeJson(hooksPath, bespokeAntigravityHooks());

  const r = run(tmp, ['--client', 'antigravity', '--confirm']);

  assert(r.status === 0, '5.1 exits 0');
  assert(hasBackup(hooksPath), '5.2 backup created');

  const result = readJson(hooksPath);
  const ptu = result.hooks?.PostToolUse ?? [];
  assert(!ptu.some((e) => e._mori_managed === true), '5.3 _mori_managed PostToolUse entries removed');
  assert(ptu.some((e) => e.command === 'echo non-mori'), '5.4 non-mori entry preserved');

  // Stop was mori-only — should be gone or empty
  const stop = result.hooks?.Stop;
  assert(!stop || stop.length === 0, '5.5 Stop hook empty/removed after mori removal');
}

// ---------------------------------------------------------------------------
// Suite 6: No mori entries → no-op, no backup, clear "nothing to do"
// ---------------------------------------------------------------------------

{
  console.log('\n[6] Clean config — no-op, no backup');

  const tmp = makeTmp();
  const settingsPath = join(tmp, '.claude', 'settings.json');
  writeJson(settingsPath, cleanSettings());

  const r = run(tmp, ['--client', 'claude', '--confirm']);

  assert(r.status === 0, '6.1 exits 0');
  assert(!hasBackup(settingsPath), '6.2 no backup (nothing to do)');
  assert(r.stdout.includes('[skip]') || r.stdout.includes('nothing to remove'), '6.3 reports nothing to remove');
}

// ---------------------------------------------------------------------------
// Suite 7: Malformed config → fail-gradual (one bad file, others still processed)
// ---------------------------------------------------------------------------

{
  console.log('\n[7] Malformed config — fail-gradual (other files still processed)');

  const tmp = makeTmp();

  // Write a malformed Claude settings
  mkdirSync(join(tmp, '.claude'), { recursive: true });
  writeFileSync(join(tmp, '.claude', 'settings.json'), malformedJson(), 'utf8');

  // Write a valid Cursor mcp.json that has mori
  const cursorMcpPath = join(tmp, '.cursor', 'mcp.json');
  writeJson(cursorMcpPath, {
    mcpServers: {
      mori: { type: 'http', url: 'http://localhost:8968/mcp' },
      other: { type: 'http', url: 'http://example.com' },
    },
  });

  const r = run(tmp, ['--client', 'all', '--confirm']);

  // Must not crash hard
  assert(r.status === 0, '7.1 exits 0 despite malformed Claude settings');
  // Claude settings failure reported
  assert(
    r.stdout.includes('[skip]') || r.stderr.includes('parse') || r.stdout.includes('parse'),
    '7.2 malformed file reported'
  );
  // Cursor still processed — mori removed
  const cursorMcp = readJson(cursorMcpPath);
  assert(!('mori' in (cursorMcp.mcpServers ?? {})), '7.3 Cursor mcp.json mori removed despite Claude failure');
  assert('other' in (cursorMcp.mcpServers ?? {}), '7.4 Cursor mcp.json other preserved');
}

// ---------------------------------------------------------------------------
// Suite 8: Validation gate — non-mori top-level key never removed
// ---------------------------------------------------------------------------

{
  console.log('\n[8] Validation gate — non-mori top-level key never removed');

  const tmp = makeTmp();
  const settingsPath = join(tmp, '.claude', 'settings.json');
  const data = {
    mcpServers: { mori: { type: 'http', url: 'http://localhost:8968/mcp' } },
    myImportantKey: { nested: 'value' },
    permissions: { allow: ['mcp__mori__brief', 'Bash(*)'] },
  };
  writeJson(settingsPath, data);

  const r = run(tmp, ['--client', 'claude', '--confirm']);
  assert(r.status === 0, '8.1 exits 0');

  const result = readJson(settingsPath);
  assert('myImportantKey' in result, '8.2 myImportantKey preserved');
  assert(!('mori' in (result.mcpServers ?? {})), '8.3 mori MCP removed');
  assert(result.myImportantKey?.nested === 'value', '8.4 nested value preserved');
}

// ---------------------------------------------------------------------------
// Suite 9: --include-skills removes known mori skill dirs only, backs up first
// ---------------------------------------------------------------------------

{
  console.log('\n[9] --include-skills — removes known mori skill dirs only, backs up');

  const tmp = makeTmp();
  const skillsDir = join(tmp, '.claude', 'skills');

  // Create a few mori skills and a non-mori skill
  mkdirSync(join(skillsDir, 'brief'), { recursive: true });
  writeFileSync(join(skillsDir, 'brief', 'SKILL.md'), '# brief skill\n', 'utf8');
  mkdirSync(join(skillsDir, 'mori-dream'), { recursive: true });
  writeFileSync(join(skillsDir, 'mori-dream', 'SKILL.md'), '# dream skill\n', 'utf8');
  mkdirSync(join(skillsDir, 'my-custom-skill'), { recursive: true });
  writeFileSync(join(skillsDir, 'my-custom-skill', 'SKILL.md'), '# custom\n', 'utf8');

  // No Claude settings needed — skills test only
  writeJson(join(tmp, '.claude', 'settings.json'), cleanSettings());

  const r = run(tmp, ['--client', 'claude', '--confirm', '--include-skills']);

  assert(r.status === 0, '9.1 exits 0');
  assert(!existsSync(join(skillsDir, 'brief')), '9.2 "brief" skill dir removed');
  assert(!existsSync(join(skillsDir, 'mori-dream')), '9.3 "mori-dream" skill dir removed');
  assert(existsSync(join(skillsDir, 'my-custom-skill')), '9.4 "my-custom-skill" preserved');

  // Backup dirs should exist
  const entries = readdirSync(skillsDir);
  const hasBriefBackup = entries.some((e) => e.startsWith('brief.mori-backup-'));
  const hasDreamBackup = entries.some((e) => e.startsWith('mori-dream.mori-backup-'));
  assert(hasBriefBackup, '9.5 backup for "brief" created');
  assert(hasDreamBackup, '9.6 backup for "mori-dream" created');
}

// ---------------------------------------------------------------------------
// Suite 10: Cursor dry-run — non-mori entry preserved, nothing written
// ---------------------------------------------------------------------------

{
  console.log('\n[10] Cursor dry-run — non-mori entries reported preserved, file not modified');

  const tmp = makeTmp();
  const hooksPath = join(tmp, '.cursor', 'hooks.json');
  writeJson(hooksPath, mixedCursorHooks());
  const originalMtime = statSync(hooksPath).mtimeMs;

  const r = run(tmp, ['--client', 'cursor']);

  assert(r.status === 0, '10.1 exits 0');
  assert(r.stdout.includes('dry-run') || r.stdout.includes('DRY-RUN'), '10.2 dry-run mentioned');
  assert(!hasBackup(hooksPath), '10.3 no backup created');
  const mtime = statSync(hooksPath).mtimeMs;
  assert(mtime === originalMtime, '10.4 file not modified');
}

// ---------------------------------------------------------------------------
// Suite 11: Antigravity dry-run on clean file — no backup, skip messages
// ---------------------------------------------------------------------------

{
  console.log('\n[11] Antigravity clean config — no-op, no backup');

  const tmp = makeTmp();
  const hooksPath = join(tmp, '.gemini', 'config', 'hooks.json');
  writeJson(hooksPath, { otherTool: { PostToolUse: [] } });

  const r = run(tmp, ['--client', 'antigravity', '--confirm']);

  assert(r.status === 0, '11.1 exits 0');
  assert(!hasBackup(hooksPath), '11.2 no backup (nothing to remove)');
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n${'─'.repeat(60)}`);
console.log(`  Results: ${passed} passed, ${failed} failed`);
if (errors.length > 0) {
  console.log('\nFailed tests:');
  for (const e of errors) console.log(e);
}
console.log('');

cleanupAll();

process.exit(failed > 0 ? 1 : 0);
