/**
 * test_antigravity_hooks.mjs — Hermetic tests for mori Antigravity hook scripts (Node ESM)
 *
 * Tests:
 *   mori-context-hook-antigravity.mjs  — PreInvocation once-per-conversation injection
 *   mori-ship-event-antigravity.mjs    — event normalisation, Stop enrichment, fail-open
 *
 * Hermetic: no live mori server required. Network calls go to an unreachable port;
 * tests verify exit 0 on connection failure. Throttle temp flags are cleaned up.
 *
 * Run: node plugins/mori/tests/test_antigravity_hooks.mjs
 */

import { spawnSync } from 'child_process';
import { writeFileSync, mkdtempSync, rmSync, existsSync, unlinkSync, readdirSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { readFileSync } from 'fs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPTS   = resolve(__dirname, '../scripts');
const FIXTURES  = resolve(__dirname, 'fixtures');

const CONTEXT_HOOK = join(SCRIPTS, 'mori-context-hook-antigravity.mjs');
const SHIP_EVENT   = join(SCRIPTS, 'mori-ship-event-antigravity.mjs');

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

const fixture = (name) => readFileSync(join(FIXTURES, name), 'utf8');

// ── Temp dir ─────────────────────────────────────────────────────────────────

const TMP = mkdtempSync(join(tmpdir(), 'mori-ag-test-'));

function cleanup() {
  try { rmSync(TMP, { recursive: true, force: true }); } catch { /* noop */ }
}

/** Remove throttle flag for a given conversationId (so tests are independent). */
function clearThrottleFlag(conversationId) {
  const safe = conversationId.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 200);
  const flag = join(TMP, `mori-conv-${safe}`);
  try { if (existsSync(flag)) unlinkSync(flag); } catch { /* noop */ }
}

// ── mori-context-hook-antigravity.mjs ────────────────────────────────────────

console.log('\n── mori-context-hook-antigravity.mjs ──\n');

const CONV_ID = 'ag-conv-xyz-789';

{
  // 1. PreInvocation — first call + ctx file set + skip-health → injectSteps with ephemeralMessage
  // MORI_SKIP_HEALTH_CHECK=1 bypasses the network gate (sandbox prevents child→parent TCP).
  clearThrottleFlag(CONV_ID);
  const ctxFile = join(TMP, 'ag-ctx.txt');
  writeFileSync(ctxFile, 'Antigravity session context: test mode active.');
  const r = run(CONTEXT_HOOK, {
    stdin: fixture('antigravity-PreInvocation.json'),
    env: { MORI_SESSION_CONTEXT_FILE: ctxFile, TMPDIR: TMP, MORI_SKIP_HEALTH_CHECK: '1' },
    args: ['--url', 'http://127.0.0.1:8968'],
  });
  assert(r.status === 0, 'ctx-ag: first PreInvocation+ctxfile → exits 0');
  let parsed;
  try { parsed = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(
    Array.isArray(parsed?.injectSteps) && parsed.injectSteps.length > 0,
    'ctx-ag: first PreInvocation → injectSteps non-empty',
    r.stdout,
  );
  assert(
    parsed?.injectSteps?.[0]?.ephemeralMessage?.includes('test mode active'),
    'ctx-ag: ephemeralMessage contains ctx file contents',
    r.stdout,
  );
}

{
  // 2. PreInvocation — second call (same conversationId) → injectSteps empty (throttled)
  // Flag already set from test 1; do NOT clear it
  const ctxFile = join(TMP, 'ag-ctx.txt');
  const r = run(CONTEXT_HOOK, {
    stdin: fixture('antigravity-PreInvocation.json'),
    env: { MORI_SESSION_CONTEXT_FILE: ctxFile, TMPDIR: TMP },
  });
  assert(r.status === 0, 'ctx-ag: second PreInvocation → exits 0');
  let parsed;
  try { parsed = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(
    Array.isArray(parsed?.injectSteps) && parsed.injectSteps.length === 0,
    'ctx-ag: second PreInvocation → injectSteps empty (throttled)',
    r.stdout,
  );
}

{
  // 3. PreInvocation — no URL configured → injectSteps emits UNCONFIGURED_MESSAGE
  // When the server URL is not set, the health gate returns "unconfigured" and the
  // hook surfaces the setup guide in injectSteps. (No ctx file needed — the setup
  // message takes priority.)
  clearThrottleFlag('ag-conv-no-ctx');
  const env = { ...process.env, TMPDIR: TMP };
  delete env.MORI_SESSION_CONTEXT_FILE;
  const input = JSON.stringify({ conversationId: 'ag-conv-no-ctx', stepIdx: 0 });
  const r = spawnSync(process.execPath, [CONTEXT_HOOK, '--url', ''], {
    input,
    env,
    encoding: 'utf8',
    timeout: 5000,
  });
  assert((r.status ?? -1) === 0, 'ctx-ag: no-ctxfile+unconfigured → exits 0');
  let parsed;
  try { parsed = JSON.parse((r.stdout ?? '').trim()); } catch { /* noop */ }
  assert(
    Array.isArray(parsed?.injectSteps) &&
      parsed.injectSteps.length > 0 &&
      parsed.injectSteps[0]?.ephemeralMessage?.includes('No Mori server is configured'),
    'ctx-ag: no-ctxfile+unconfigured → injectSteps emits UNCONFIGURED_MESSAGE',
    r.stdout,
  );
}

{
  // 3b. PreInvocation — skip-health + no ctx file → injectSteps empty
  // When server is up but no ctx file is configured, nothing is injected.
  clearThrottleFlag('ag-conv-no-ctx-skip');
  const env2 = { ...process.env, TMPDIR: TMP };
  delete env2.MORI_SESSION_CONTEXT_FILE;
  const input2 = JSON.stringify({ conversationId: 'ag-conv-no-ctx-skip', stepIdx: 0 });
  const r2 = spawnSync(process.execPath, [CONTEXT_HOOK, '--url', 'http://127.0.0.1:8968'], {
    input: input2,
    env: { ...env2, MORI_SKIP_HEALTH_CHECK: '1' },
    encoding: 'utf8',
    timeout: 5000,
  });
  assert((r2.status ?? -1) === 0, 'ctx-ag: skip-health+no-ctxfile → exits 0');
  let parsed2;
  try { parsed2 = JSON.parse((r2.stdout ?? '').trim()); } catch { /* noop */ }
  assert(
    Array.isArray(parsed2?.injectSteps) && parsed2.injectSteps.length === 0,
    'ctx-ag: skip-health+no-ctxfile → injectSteps empty',
    r2.stdout,
  );
}

{
  // 4. Empty stdin → injectSteps empty, exit 0
  const r = run(CONTEXT_HOOK, { stdin: '', env: { TMPDIR: TMP } });
  assert(r.status === 0, 'ctx-ag: empty stdin → exits 0');
  let parsed;
  try { parsed = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(
    Array.isArray(parsed?.injectSteps) && parsed.injectSteps.length === 0,
    'ctx-ag: empty stdin → injectSteps empty',
  );
}

{
  // 5. Garbage stdin → exits 0 (fail-open), injectSteps empty
  const r = run(CONTEXT_HOOK, { stdin: '{ bad json }', env: { TMPDIR: TMP } });
  assert(r.status === 0, 'ctx-ag: garbage stdin → exits 0');
}

{
  // 6. Throttle fires exactly once: new conversationId → first call returns true, second false
  const throttleTest = `
    import { firedOnce } from '${join(SCRIPTS, 'lib/throttle.mjs')}';
    process.env.TMPDIR = ${JSON.stringify(TMP)};
    const KEY = 'throttle-test-unique-' + Date.now();
    const first  = firedOnce(KEY);
    const second = firedOnce(KEY);
    const third  = firedOnce(KEY);
    console.log(JSON.stringify({ first, second, third }));
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: throttleTest,
    env: { ...process.env, TMPDIR: TMP },
    encoding: 'utf8',
    timeout: 3000,
  });
  let res;
  try { res = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(res?.first === true,  'throttle: first call returns true');
  assert(res?.second === false, 'throttle: second call returns false');
  assert(res?.third === false,  'throttle: third call returns false');
}

// ── mori-ship-event-antigravity.mjs ──────────────────────────────────────────

console.log('\n── mori-ship-event-antigravity.mjs ──\n');

{
  // 7. PostToolUse canonical mapping
  const canonicalTest = `
    import { toCanonical } from '${join(SCRIPTS, 'lib/canonical.mjs')}';
    const ev = ${fixture('antigravity-PostToolUse.json')};
    const canon = toCanonical(ev, { client: 'antigravity', eventName: 'PostToolUse' });
    console.log(JSON.stringify(canon));
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: canonicalTest,
    encoding: 'utf8',
    timeout: 5000,
  });
  let c;
  try { c = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(c?.session_id === 'ag-conv-xyz-789', 'canonical-ag: conversationId → session_id');
  assert(c?.hook_event_name === 'PostToolUse', 'canonical-ag: eventName → hook_event_name');
  assert(c?.transcript_path === '/tmp/antigravity-transcript-xyz789.jsonl', 'canonical-ag: transcriptPath → transcript_path');
  assert(c?._clientMeta?.client === 'antigravity', 'canonical-ag: _clientMeta.client = antigravity');
  assert(c?._clientMeta?.stepIdx === 3, 'canonical-ag: stepIdx in _clientMeta');
}

{
  // 8. Stop canonical mapping — stepIdx in _clientMeta
  const canonicalTest = `
    import { toCanonical } from '${join(SCRIPTS, 'lib/canonical.mjs')}';
    const ev = ${fixture('antigravity-Stop.json')};
    const canon = toCanonical(ev, { client: 'antigravity', eventName: 'Stop' });
    console.log(JSON.stringify(canon));
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: canonicalTest,
    encoding: 'utf8',
    timeout: 5000,
  });
  let c;
  try { c = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(c?.hook_event_name === 'Stop', 'canonical-ag: Stop event name');
  assert(c?._clientMeta?.stepIdx === 12, 'canonical-ag: Stop stepIdx in _clientMeta');
}

{
  // 9. Ship event: PostToolUse → exits 0 on network failure
  const r = run(SHIP_EVENT, {
    stdin: fixture('antigravity-PostToolUse.json'),
    args: ['--url', 'http://127.0.0.1:19999', '--event', 'PostToolUse'],
    env: { TMPDIR: TMP },
  });
  assert(r.status === 0, 'ship-ag: PostToolUse exits 0 on network failure');
}

{
  // 10. Ship event: Stop with readable transcript → enriches transcript_tail_b64
  const transcriptFile = join(TMP, 'ag-transcript.jsonl');
  writeFileSync(transcriptFile, '{"role":"model","content":"ag test"}\n'.repeat(5));
  const stopEvt = JSON.stringify({
    conversationId: 'ag-conv-xyz-789',
    transcriptPath: transcriptFile,
    stepIdx: 5,
  });
  const enrichTest = `
    import { readFileSync, existsSync } from 'fs';
    const tpath = ${JSON.stringify(transcriptFile)};
    const buf = readFileSync(tpath);
    const tail = buf.length > 65536 ? buf.slice(buf.length - 65536) : buf;
    const b64 = tail.toString('base64');
    const decoded = Buffer.from(b64, 'base64').toString('utf8');
    console.log(decoded.includes('ag test') ? 'ok' : 'fail');
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: enrichTest,
    encoding: 'utf8',
    timeout: 3000,
  });
  assert(r.stdout.trim() === 'ok', 'ship-ag: Stop enrichment encodes transcript correctly');
}

{
  // 11. Empty stdin → exits 0
  const r = run(SHIP_EVENT, {
    stdin: '',
    args: ['--url', 'http://127.0.0.1:19999', '--event', 'PostToolUse'],
    env: { TMPDIR: TMP },
  });
  assert(r.status === 0, 'ship-ag: empty stdin → exits 0');
}

{
  // 12. Garbage stdin → exits 0 (fail-open)
  const r = run(SHIP_EVENT, {
    stdin: '{ bad json }',
    args: ['--url', 'http://127.0.0.1:19999', '--event', 'PostToolUse'],
    env: { TMPDIR: TMP },
  });
  assert(r.status === 0, 'ship-ag: garbage stdin → exits 0');
}

// ── Results ───────────────────────────────────────────────────────────────────

cleanup();

console.log(`\n── Results: ${passed} passed, ${failed} failed ──\n`);
if (failed > 0) process.exit(1);
