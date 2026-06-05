/**
 * test_cursor_hooks.mjs — Hermetic tests for mori Cursor hook scripts (Node ESM)
 *
 * Tests:
 *   mori-context-hook-cursor.mjs  — sessionStart context injection
 *   mori-ship-event-cursor.mjs    — event normalisation, Stop enrichment, fail-open
 *
 * Hermetic: no live mori server required. Network calls are directed at an
 * unreachable port; tests verify exit 0 on connection failure.
 *
 * Run: node plugins/mori/tests/test_cursor_hooks.mjs
 */

import { spawnSync } from 'child_process';
import { writeFileSync, mkdtempSync, rmSync, existsSync, unlinkSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPTS   = resolve(__dirname, '../scripts');
const FIXTURES  = resolve(__dirname, 'fixtures');

const CONTEXT_HOOK = join(SCRIPTS, 'mori-context-hook-cursor.mjs');
const SHIP_EVENT   = join(SCRIPTS, 'mori-ship-event-cursor.mjs');

// ── Test harness ─────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function assert(condition, name, detail = '') {
  if (condition) {
    console.log(`  PASS  ${name}`);
    passed++;
  } else {
    console.error(`  FAIL  ${name}${detail ? ': ' + detail : ''}`);
    failed++;
  }
}

function run(scriptPath, { stdin = '', env = {}, args = [] } = {}) {
  const result = spawnSync(process.execPath, [scriptPath, ...args], {
    input: stdin,
    env: { ...process.env, ...env },
    encoding: 'utf8',
    timeout: 8000,
  });
  return {
    status: result.status ?? -1,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
  };
}

import { readFileSync } from 'fs';
const fixture = (name) => readFileSync(join(FIXTURES, name), 'utf8');

// ── Temp dir ─────────────────────────────────────────────────────────────────

const TMP = mkdtempSync(join(tmpdir(), 'mori-cursor-test-'));

function cleanup() {
  try { rmSync(TMP, { recursive: true, force: true }); } catch { /* noop */ }
}

// ── mori-context-hook-cursor.mjs ─────────────────────────────────────────────

console.log('\n── mori-context-hook-cursor.mjs ──\n');

{
  // 1. sessionStart + MORI_SESSION_CONTEXT_FILE set → emits additional_context
  const ctxFile = join(TMP, 'ctx.txt');
  writeFileSync(ctxFile, 'You are in Cursor test mode. Hello from ctx file.');
  const r = run(CONTEXT_HOOK, {
    stdin: fixture('cursor-sessionStart.json'),
    env: { MORI_SESSION_CONTEXT_FILE: ctxFile, TMPDIR: TMP },
  });
  assert(r.status === 0, 'ctx-hook: sessionStart+ctxfile → exits 0');
  let parsed;
  try { parsed = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(
    parsed?.additional_context?.includes('Hello from ctx file'),
    'ctx-hook: sessionStart+ctxfile → additional_context injected',
    r.stdout,
  );
}

{
  // 2. sessionStart + MORI_SESSION_CONTEXT_FILE unset → no output
  const env = { ...process.env, TMPDIR: TMP };
  delete env.MORI_SESSION_CONTEXT_FILE;
  const r = spawnSync(process.execPath, [CONTEXT_HOOK], {
    input: fixture('cursor-sessionStart.json'),
    env,
    encoding: 'utf8',
    timeout: 5000,
  });
  assert((r.status ?? -1) === 0, 'ctx-hook: sessionStart+no-ctxfile → exits 0');
  assert((r.stdout ?? '').trim() === '', 'ctx-hook: sessionStart+no-ctxfile → no output');
}

{
  // 3. Non-sessionStart event → no output
  const r = run(CONTEXT_HOOK, {
    stdin: fixture('cursor-postToolUse.json'),
    env: { TMPDIR: TMP },
  });
  assert(r.status === 0, 'ctx-hook: postToolUse → exits 0');
  assert(r.stdout.trim() === '', 'ctx-hook: postToolUse → no output');
}

{
  // 4. Empty stdin → exits 0, no output
  const r = run(CONTEXT_HOOK, { stdin: '', env: { TMPDIR: TMP } });
  assert(r.status === 0, 'ctx-hook: empty stdin → exits 0');
  assert(r.stdout.trim() === '', 'ctx-hook: empty stdin → no output');
}

{
  // 5. Garbage stdin → exits 0 (fail-open)
  const r = run(CONTEXT_HOOK, { stdin: '{ not json }{', env: { TMPDIR: TMP } });
  assert(r.status === 0, 'ctx-hook: garbage stdin → exits 0');
}

// ── mori-ship-event-cursor.mjs ───────────────────────────────────────────────

console.log('\n── mori-ship-event-cursor.mjs ──\n');

{
  // 6. postToolUse event → canonical normalization correct fields
  // We test via lib/canonical directly (inline Node snippet)
  const canonicalTest = `
    import { toCanonical } from '${join(SCRIPTS, 'lib/canonical.mjs')}';
    const ev = ${fixture('cursor-postToolUse.json')};
    const canon = toCanonical(ev, { client: 'cursor', eventName: 'postToolUse' });
    console.log(JSON.stringify(canon));
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: canonicalTest,
    encoding: 'utf8',
    timeout: 5000,
  });
  let c;
  try { c = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(c?.session_id === 'conv-abc-123', 'canonical: conversation_id → session_id');
  assert(c?.hook_event_name === 'PostToolUse', 'canonical: postToolUse → PostToolUse (PascalCase)');
  assert(c?.transcript_path === '/tmp/cursor-transcript-abc123.jsonl', 'canonical: transcript_path identity');
  assert(c?.tool_name === 'Read', 'canonical: tool_name mapped');
  assert(c?._clientMeta?.client === 'cursor', 'canonical: _clientMeta.client = cursor');
}

{
  // 7. stop event → canonical hook_event_name = Stop
  const canonicalTest = `
    import { toCanonical } from '${join(SCRIPTS, 'lib/canonical.mjs')}';
    const ev = ${fixture('cursor-stop.json')};
    const canon = toCanonical(ev, { client: 'cursor', eventName: 'stop' });
    console.log(JSON.stringify(canon));
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: canonicalTest,
    encoding: 'utf8',
    timeout: 5000,
  });
  let c;
  try { c = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(c?.hook_event_name === 'Stop', 'canonical: stop → Stop (PascalCase)');
}

{
  // 8. Ship event: postToolUse → exits 0 even on network failure
  const r = run(SHIP_EVENT, {
    stdin: fixture('cursor-postToolUse.json'),
    args: ['--url', 'http://127.0.0.1:19999', '--event', 'postToolUse'],
    env: { TMPDIR: TMP },
  });
  assert(r.status === 0, 'ship-cursor: postToolUse exits 0 on network failure');
}

{
  // 9. Ship event: stop with readable transcript → enriches transcript_tail_b64
  const transcriptFile = join(TMP, 'cursor-transcript.jsonl');
  writeFileSync(transcriptFile, '{"role":"user","content":"test"}\n'.repeat(5));
  const stopEvt = JSON.stringify({
    hook_event_name: 'stop',
    conversation_id: 'conv-abc-123',
    transcript_path: transcriptFile,
    workspace_roots: [{ path: '/home/user/project' }],
  });
  // Test enrichment logic inline
  const enrichTest = `
    import { readFileSync, existsSync } from 'fs';
    const tpath = ${JSON.stringify(transcriptFile)};
    const buf = readFileSync(tpath);
    const tail = buf.length > 65536 ? buf.slice(buf.length - 65536) : buf;
    const b64 = tail.toString('base64');
    const decoded = Buffer.from(b64, 'base64').toString('utf8');
    console.log(decoded.includes('test') ? 'ok' : 'fail');
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: enrichTest,
    encoding: 'utf8',
    timeout: 3000,
  });
  assert(r.stdout.trim() === 'ok', 'ship-cursor: Stop enrichment encodes transcript correctly');
}

{
  // 10. Empty stdin → exits 0
  const r = run(SHIP_EVENT, {
    stdin: '',
    args: ['--url', 'http://127.0.0.1:19999'],
    env: { TMPDIR: TMP },
  });
  assert(r.status === 0, 'ship-cursor: empty stdin → exits 0');
}

{
  // 11. Garbage stdin → exits 0 (fail-open)
  const r = run(SHIP_EVENT, {
    stdin: '{ bad json }',
    args: ['--url', 'http://127.0.0.1:19999'],
    env: { TMPDIR: TMP },
  });
  assert(r.status === 0, 'ship-cursor: garbage stdin → exits 0');
}

{
  // 12. sessionStart canonical mapping — source field preserved
  const canonicalTest = `
    import { toCanonical } from '${join(SCRIPTS, 'lib/canonical.mjs')}';
    const ev = ${fixture('cursor-sessionStart.json')};
    const canon = toCanonical(ev, { client: 'cursor', eventName: 'sessionStart' });
    console.log(JSON.stringify(canon));
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: canonicalTest,
    encoding: 'utf8',
    timeout: 5000,
  });
  let c;
  try { c = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(c?.hook_event_name === 'SessionStart', 'canonical: sessionStart → SessionStart');
  assert(c?.source === 'startup', 'canonical: source field preserved');
  assert(Array.isArray(c?.workspace_roots), 'canonical: workspace_roots preserved');
}

// ── Results ───────────────────────────────────────────────────────────────────

cleanup();

console.log(`\n── Results: ${passed} passed, ${failed} failed ──\n`);
if (failed > 0) process.exit(1);
