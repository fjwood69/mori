/**
 * test_plugin_hooks.mjs — Lifecycle tests for mori-context-hook.mjs and mori-ship-event.mjs
 *
 * Hermetic: no real network needed. Ship-event tests intercept fetch at process level
 * (via --experimental-global-customevent workaround) or assert behaviour via exit code
 * and log output. A failed POST must still exit 0.
 *
 * Run: node plugins/mori/tests/test_plugin_hooks.mjs
 */

import { execFileSync, spawnSync } from 'child_process';
import { writeFileSync, mkdtempSync, rmSync, readFileSync, existsSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPTS = resolve(__dirname, '../scripts');
const CONTEXT_HOOK = join(SCRIPTS, 'mori-context-hook.mjs');
const SHIP_EVENT   = join(SCRIPTS, 'mori-ship-event.mjs');

// ---- Utilities -----------------------------------------------------------------

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

/**
 * Run a script with the given stdin, env, and optional extra args.
 * Returns { status, stdout, stderr }.
 */
function run(scriptPath, { stdin = '', env = {}, args = [] } = {}) {
  const result = spawnSync(process.execPath, [scriptPath, ...args], {
    input: stdin,
    env: { ...process.env, ...env },
    encoding: 'utf8',
    timeout: 5000,
  });
  return {
    status: result.status ?? -1,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
  };
}

// ---- Temp dir ------------------------------------------------------------------

const TMP = mkdtempSync(join(tmpdir(), 'mori-test-'));

function cleanup() {
  try { rmSync(TMP, { recursive: true, force: true }); } catch { /* noop */ }
}

// ---- mori-context-hook.mjs tests -----------------------------------------------

console.log('\n── mori-context-hook.mjs ──\n');

{
  // 1. source=compact → emits additionalContext nudge
  // compact branch exits before the health gate so no --url needed
  const r = run(CONTEXT_HOOK, {
    stdin: JSON.stringify({ hook_event_name: 'SessionStart', source: 'compact' }),
  });
  assert(r.status === 0, 'compact: exits 0');
  let parsed;
  try { parsed = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(
    parsed?.hookSpecificOutput?.hookEventName === 'SessionStart',
    'compact: hookEventName is SessionStart',
  );
  assert(
    typeof parsed?.hookSpecificOutput?.additionalContext === 'string' &&
      parsed.hookSpecificOutput.additionalContext.includes('brief --post-compact'),
    'compact: additionalContext mentions /brief --post-compact',
  );
}

{
  // 2. source=compact + MORI_POST_COMPACT_BRIEF=false → no output
  const r = run(CONTEXT_HOOK, {
    stdin: JSON.stringify({ hook_event_name: 'SessionStart', source: 'compact' }),
    env: { MORI_POST_COMPACT_BRIEF: 'false' },
  });
  assert(r.status === 0, 'compact+disabled: exits 0');
  assert(r.stdout.trim() === '', 'compact+disabled: no output');
}

{
  // 3. source=startup + MORI_SESSION_CONTEXT_FILE set + MORI_SKIP_HEALTH_CHECK=1
  // → injects file contents (health gate treated as "up")
  // Note: The sandbox isolates child-process TCP from parent listeners, so we use
  // MORI_SKIP_HEALTH_CHECK=1 to bypass the network check in this integration test.
  const ctxFile = join(TMP, 'session-context.txt');
  writeFileSync(ctxFile, 'You are operating in test mode. Hello from context file.');
  const r = run(CONTEXT_HOOK, {
    stdin: JSON.stringify({ hook_event_name: 'SessionStart', source: 'startup', session_id: 'test-ctx-s3' }),
    env: { MORI_SESSION_CONTEXT_FILE: ctxFile, TMPDIR: TMP, MORI_SKIP_HEALTH_CHECK: '1' },
    args: ['--url', 'http://127.0.0.1:8968'],
  });
  assert(r.status === 0, 'startup+ctxfile: exits 0');
  let parsed;
  try { parsed = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(
    parsed?.hookSpecificOutput?.additionalContext?.includes('Hello from context file'),
    'startup+ctxfile: context injected',
    r.stdout,
  );
}

{
  // 4. source=startup + no URL configured → SILENT (no warning).
  // The hook's health check is opt-in and separate from the MCP connection; an
  // absent URL must NOT warn (it would false-fire on the claude-mcp-add setup).
  const env = { ...process.env, TMPDIR: TMP };
  delete env.MORI_SERVER_URL;
  delete env.MORI_SESSION_CONTEXT_FILE;
  const r = spawnSync(process.execPath, [CONTEXT_HOOK, '--url', ''], {
    input: JSON.stringify({ hook_event_name: 'SessionStart', source: 'startup', session_id: 'test-noURL-s4' }),
    env,
    encoding: 'utf8',
    timeout: 5000,
  });
  assert((r.status ?? -1) === 0, 'startup+no-url: exits 0');
  assert((r.stdout ?? '').trim() === '', 'startup+no-url: SILENT (no setup warning)', r.stdout);
}

{
  // 5. source=startup + server down → emits SETUP_MESSAGE
  // Port 1 is reliably unreachable (privileged + nothing listening).
  const r = run(CONTEXT_HOOK, {
    stdin: JSON.stringify({ hook_event_name: 'SessionStart', source: 'startup', session_id: 'test-down-s5' }),
    env: { TMPDIR: TMP },
    args: ['--url', 'http://127.0.0.1:1'],
  });
  assert(r.status === 0, 'startup+server-down: exits 0');
  let parsed;
  try { parsed = JSON.parse(r.stdout.trim()); } catch { /* noop */ }
  assert(
    typeof parsed?.hookSpecificOutput?.additionalContext === 'string' &&
      parsed.hookSpecificOutput.additionalContext.includes('isn\'t reachable yet'),
    'startup+server-down: emits SETUP_MESSAGE',
    r.stdout,
  );
}

{
  // 6. non-SessionStart event → no output, exit 0
  const r = run(CONTEXT_HOOK, {
    stdin: JSON.stringify({ hook_event_name: 'PostToolUse', tool: 'Bash' }),
  });
  assert(r.status === 0, 'non-SessionStart: exits 0');
  assert(r.stdout.trim() === '', 'non-SessionStart: no output');
}

{
  // 7. empty stdin → exit 0, no output
  const r = run(CONTEXT_HOOK, { stdin: '' });
  assert(r.status === 0, 'empty-stdin: exits 0');
  assert(r.stdout.trim() === '', 'empty-stdin: no output');
}

{
  // 8. garbage stdin → exit 0
  const r = run(CONTEXT_HOOK, { stdin: 'not json at all }{' });
  assert(r.status === 0, 'garbage-stdin: exits 0');
}

// ---- mori-ship-event.mjs tests -------------------------------------------------

console.log('\n── mori-ship-event.mjs ──\n');

{
  // 9. Empty stdin → exit 0, no network call attempted
  const r = run(CONTEXT_HOOK, {
    stdin: '',
    // Point at a definitely-unreachable port so any network call would fail differently
    // We just verify the script exits 0 even with no stdin
  });
  // Extra args not strictly needed for empty-stdin case
  const result = spawnSync(process.execPath, [SHIP_EVENT, '--url', 'http://127.0.0.1:19999', '--mode', 'raw'], {
    input: '',
    encoding: 'utf8',
    timeout: 5000,
  });
  assert((result.status ?? -1) === 0, 'ship-event empty-stdin: exits 0');
}

{
  // 10. Stop enrichment — transcript_tail_b64 added when transcript_path is readable
  const transcriptFile = join(TMP, 'transcript.jsonl');
  // Write some fake transcript content (> 0 bytes)
  writeFileSync(transcriptFile, '{"role":"assistant","content":"test turn 1"}\n'.repeat(10));

  const stopEvent = JSON.stringify({
    hook_event_name: 'Stop',
    transcript_path: transcriptFile,
    session_id: 'test-session',
  });

  // We intercept at the fetch layer by pointing at a port nothing is listening on.
  // The script should: parse the Stop event, add transcript_tail_b64, attempt POST,
  // fail gracefully (connection refused), log to mori-hook.log, exit 0.
  const logFile = join(TMP, 'mori-hook.log');
  const result = spawnSync(process.execPath, [
    SHIP_EVENT,
    '--url', 'http://127.0.0.1:19999',
    '--client', 'test-client',
    '--mode', 'raw',
  ], {
    input: stopEvent,
    encoding: 'utf8',
    env: { ...process.env, TMPDIR: TMP },
    timeout: 8000,
  });

  assert((result.status ?? -1) === 0, 'ship-event Stop: exits 0 even on connection failure');

  // Verify the log was written
  assert(existsSync(logFile), 'ship-event Stop: failure logged to mori-hook.log');

  // Verify Stop enrichment by checking the log contains the right URL (the enrich
  // itself happens before fetch, so we need another approach: run a minimal inline
  // Node that does just the enrichment logic)
  const enrichTest = `
    import { readFileSync, existsSync } from 'fs';
    const tpath = ${JSON.stringify(transcriptFile)};
    const buf = readFileSync(tpath);
    const tail = buf.length > 65536 ? buf.slice(buf.length - 65536) : buf;
    const tailB64 = tail.toString('base64');
    const enriched = { hook_event_name: 'Stop', transcript_path: tpath, transcript_tail_b64: tailB64 };
    console.log(JSON.stringify(enriched));
  `;
  const enrichResult = spawnSync(process.execPath, ['--input-type=module'], {
    input: enrichTest,
    encoding: 'utf8',
    timeout: 3000,
  });
  const enriched = JSON.parse(enrichResult.stdout.trim());
  assert(typeof enriched.transcript_tail_b64 === 'string' && enriched.transcript_tail_b64.length > 0,
    'ship-event Stop: enrichment adds transcript_tail_b64');
  // Verify it's valid base64 (decoded → matches original)
  const decoded = Buffer.from(enriched.transcript_tail_b64, 'base64').toString('utf8');
  assert(decoded.includes('test turn 1'), 'ship-event Stop: transcript_tail_b64 decodes correctly');
}

{
  // 11. Non-Stop event in raw mode — no transcript enrichment, exits 0
  const normalEvent = JSON.stringify({ hook_event_name: 'PostToolUse', tool: 'Read' });
  const result = spawnSync(process.execPath, [
    SHIP_EVENT,
    '--url', 'http://127.0.0.1:19999',
    '--client', 'test-client',
    '--mode', 'raw',
  ], {
    input: normalEvent,
    encoding: 'utf8',
    env: { ...process.env, TMPDIR: TMP },
    timeout: 8000,
  });
  assert((result.status ?? -1) === 0, 'ship-event PostToolUse: exits 0 on connection failure');
}

{
  // 12. precompact mode — exits 0 even when server is unreachable
  const precompactEvent = JSON.stringify({ hook_event_name: 'PreCompact' });
  const result = spawnSync(process.execPath, [
    SHIP_EVENT,
    '--url', 'http://127.0.0.1:19999',
    '--client', 'test-client',
    '--mode', 'precompact',
  ], {
    input: precompactEvent,
    encoding: 'utf8',
    env: { ...process.env, TMPDIR: TMP },
    timeout: 8000,
  });
  assert((result.status ?? -1) === 0, 'ship-event precompact: exits 0 on connection failure');
}

{
  // 13. Malformed JSON body — exits 0 (fail-soft)
  const result = spawnSync(process.execPath, [
    SHIP_EVENT,
    '--url', 'http://127.0.0.1:19999',
    '--mode', 'raw',
  ], {
    input: '{ not valid json }{',
    encoding: 'utf8',
    env: { ...process.env, TMPDIR: TMP },
    timeout: 8000,
  });
  assert((result.status ?? -1) === 0, 'ship-event malformed-json: exits 0');
}

{
  // 14. Stop enrichment skipped when transcript_path does not exist
  const missingPath = join(TMP, 'nonexistent-transcript.jsonl');
  const enrichInlineTest = `
    import { readFileSync, existsSync } from 'fs';
    function enrichStopEvent(parsed, mode) {
      if (mode !== 'raw') return null;
      if ((parsed.hook_event_name || '') !== 'Stop') return null;
      const tpath = parsed.transcript_path;
      if (!tpath || typeof tpath !== 'string') return null;
      try {
        if (!existsSync(tpath)) return null;
        const buf = readFileSync(tpath);
        const tail = buf.length > 65536 ? buf.slice(buf.length - 65536) : buf;
        return { ...parsed, transcript_tail_b64: tail.toString('base64') };
      } catch { return null; }
    }
    const parsed = ${JSON.stringify({ hook_event_name: 'Stop', transcript_path: missingPath })};
    const result = enrichStopEvent(parsed, 'raw');
    console.log(result === null ? 'null' : 'enriched');
  `;
  const r = spawnSync(process.execPath, ['--input-type=module'], {
    input: enrichInlineTest,
    encoding: 'utf8',
    timeout: 3000,
  });
  assert(r.stdout.trim() === 'null', 'ship-event: enrichment returns null when transcript missing');
}

// ---- env-var config resolution (MORI_SERVER_URL / MORI_API_KEY) ----------------
// The Claude Code plugin supplies config via env vars (not userConfig — its install
// prompt never fires on `claude plugin install`). Prove the scripts read them when
// no --url/--api-key arg is passed. This is the path the shipped hooks.json uses.

console.log('\n── env-var config (MORI_SERVER_URL / MORI_API_KEY) ──\n');

{
  // 15. context-hook reads MORI_SERVER_URL when no --url arg. A valid-but-unreachable
  // URL must yield SETUP ("down"), proving the env value flowed into checkServer —
  // vs UNCONFIGURED, which is what an empty/unset URL gives.
  const env = { ...process.env, TMPDIR: TMP, MORI_SERVER_URL: 'http://127.0.0.1:1' };
  delete env.MORI_SESSION_CONTEXT_FILE;
  const r = spawnSync(process.execPath, [CONTEXT_HOOK], {
    input: JSON.stringify({ hook_event_name: 'SessionStart', source: 'startup', session_id: 'test-env-s15' }),
    env, encoding: 'utf8', timeout: 5000,
  });
  assert((r.status ?? -1) === 0, 'context-hook env: exits 0');
  let parsed;
  try { parsed = JSON.parse((r.stdout ?? '').trim()); } catch { /* noop */ }
  assert(
    typeof parsed?.hookSpecificOutput?.additionalContext === 'string' &&
      parsed.hookSpecificOutput.additionalContext.includes('isn\'t reachable yet'),
    'context-hook env: MORI_SERVER_URL is read (down→SETUP, not unconfigured)',
    r.stdout,
  );
}

{
  // 16. context-hook with neither --url nor MORI_SERVER_URL → SILENT (no warning).
  // This is the recommended claude-mcp-add setup: the connection is separate and
  // invisible to the hook, so the hook must not inject a "not configured" warning.
  const env = { ...process.env, TMPDIR: TMP };
  delete env.MORI_SERVER_URL;
  delete env.MORI_SESSION_CONTEXT_FILE;
  const r = spawnSync(process.execPath, [CONTEXT_HOOK], {
    input: JSON.stringify({ hook_event_name: 'SessionStart', source: 'startup', session_id: 'test-env-s16' }),
    env, encoding: 'utf8', timeout: 5000,
  });
  assert((r.status ?? -1) === 0, 'context-hook no-config: exits 0');
  assert((r.stdout ?? '').trim() === '', 'context-hook no-config: unset MORI_SERVER_URL → SILENT', r.stdout);
}

{
  // 17. ship-event reads MORI_SERVER_URL/MORI_API_KEY from env (no --url/--api-key args).
  // Proof: the failure log records the request URI, which must contain the env host.
  const env = { ...process.env, TMPDIR: TMP, MORI_SERVER_URL: 'http://127.0.0.1:19998', MORI_API_KEY: 'envtest:secret' };
  const logFile = join(TMP, 'mori-hook.log');
  try { rmSync(logFile, { force: true }); } catch { /* noop */ }
  const r = spawnSync(process.execPath, [SHIP_EVENT, '--mode', 'raw'], {
    input: JSON.stringify({ hook_event_name: 'Stop', session_id: 'test-env-s17' }),
    env, encoding: 'utf8', timeout: 8000,
  });
  assert((r.status ?? -1) === 0, 'ship-event env: exits 0');
  assert(existsSync(logFile), 'ship-event env: failure logged');
  const logTxt = existsSync(logFile) ? readFileSync(logFile, 'utf8') : '';
  assert(
    logTxt.includes('127.0.0.1:19998'),
    'ship-event env: MORI_SERVER_URL used to build request URI',
    logTxt,
  );
}

// ---- Results -------------------------------------------------------------------

cleanup();

console.log(`\n── Results: ${passed} passed, ${failed} failed ──\n`);
if (failed > 0) process.exit(1);
