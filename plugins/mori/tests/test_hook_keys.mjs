/**
 * test_hook_keys.mjs — #88: key files, status-checked POSTs, the Claude shipper's
 * foreign-event guard and the rejected-key notice at session start.
 *
 * Run: node plugins/mori/tests/test_hook_keys.mjs
 *
 * Hermetic: a local http server stands in for mori; TMPDIR and HOME point at temp dirs, so
 * the per-user state directory (lib/state.mjs) is private to this run.
 */

import { spawn } from 'child_process';
import { createServer } from 'http';
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync,
} from 'fs';
import { tmpdir } from 'os';
import { dirname, join, resolve } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PLUGIN = resolve(__dirname, '..');
const SCRIPTS = join(PLUGIN, 'scripts');
const SHIP_EVENT = join(SCRIPTS, 'mori-ship-event.mjs');
const CONTEXT_HOOK = join(SCRIPTS, 'mori-context-hook.mjs');
const UID = typeof process.getuid === 'function' ? String(process.getuid()) : 'user';
const SECRET = 'hook-keys-test-secret-88';
const SENTINEL = 'RESPONSE-BODY-SENTINEL-88';

// Everything this process writes through lib/state.mjs lands in a private TMPDIR.
const TMP = mkdtempSync(join(tmpdir(), 'mori-hookkeys-'));
process.env.TMPDIR = TMP;
const HOME = mkdtempSync(join(tmpdir(), 'mori-hookkeys-home-'));

const config = await import('../scripts/lib/config.mjs');
const post = await import('../scripts/lib/post.mjs');
const state = await import('../scripts/lib/state.mjs');
const { CLAUDE_EVENTS, isForeignEvent } = await import('../scripts/lib/claude-events.mjs');
state._resetStateDirForTests();

let passed = 0;
let failed = 0;

function assert(condition, name, detail = '') {
  if (condition) {
    console.log(`  PASS  ${name}`);
    passed++;
  } else {
    console.error(`  FAIL  ${name}${detail ? ': ' + String(detail).slice(0, 400) : ''}`);
    failed++;
  }
}

function readState(name, tmp = TMP) {
  const p = join(tmp, `mori-${UID}`, name);
  return existsSync(p) ? readFileSync(p, 'utf8') : '';
}

function keyFile(dir, name, body, mode = 0o600) {
  const p = join(dir, name);
  writeFileSync(p, body);
  chmodSync(p, mode);
  return p;
}

/** A stand-in mori server. `respond(req, res)` decides each answer; requests are recorded. */
function startServer(respond) {
  const requests = [];
  const sockets = new Set();
  const server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      requests.push({ method: req.method, url: req.url, headers: req.headers, body });
      respond(req, res);
    });
  });
  server.on('connection', (s) => { sockets.add(s); s.on('close', () => sockets.delete(s)); });
  return new Promise((ok) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      ok({
        url: `http://127.0.0.1:${port}`,
        requests,
        close: () => new Promise((done) => {
          for (const s of sockets) s.destroy();
          server.close(() => done());
        }),
      });
    });
  });
}

/** Resolve to 'DEADLINE' if `promise` hasn't settled in `ms` — a hang must FAIL, not stall CI. */
function withDeadline(promise, ms) {
  let timer;
  return Promise.race([
    promise,
    new Promise((ok) => { timer = setTimeout(() => ok('DEADLINE'), ms); }),
  ]).finally(() => clearTimeout(timer));
}

function status(code, body = '') {
  return (_req, res) => { res.writeHead(code, { 'Content-Type': 'text/plain' }); res.end(body); };
}

/** Run a hook script as a child (async, so the in-process server can answer it). */
function runHook(script, args, input, env) {
  return new Promise((ok) => {
    const child = spawn(process.execPath, [script, ...args], { env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (c) => { stdout += c; });
    child.stderr.on('data', (c) => { stderr += c; });
    const timer = setTimeout(() => child.kill('SIGKILL'), 15000);
    child.on('close', (code) => { clearTimeout(timer); ok({ code, stdout, stderr }); });
    child.stdin.end(input);
  });
}

/** A child env with no mori configuration inherited from the developer's shell. */
function cleanEnv(tmp, extra = {}) {
  const env = { ...process.env, TMPDIR: tmp, HOME };
  for (const k of ['MORI_SERVER_URL', 'MORI_API_KEY', 'MORI_API_KEY_FILE', 'MORI_SESSION_CONTEXT_FILE',
    'MORI_SKIP_HEALTH_CHECK', 'CLAUDE_PLUGIN_ROOT']) delete env[k];
  return { ...env, ...extra };
}

// ---- 1. lib/config ---------------------------------------------------------------

console.log('\n── 1. lib/config: key resolution and key-file checks ──\n');

{
  const dir = mkdtempSync(join(HOME, 'keys-'));
  const good = keyFile(dir, 'good', 'file-key\n');
  const envFile = keyFile(dir, 'envfile', 'env-file-key\n');

  let c = config.resolveConfig({ apiKeyFile: good, apiKey: 'flag-key' },
    { MORI_API_KEY_FILE: envFile, MORI_API_KEY: 'env-key' });
  assert(c.apiKey === 'file-key' && c.keySource === 'file' && c.problem === null,
    'config: --api-key-file beats MORI_API_KEY_FILE, MORI_API_KEY and --api-key', JSON.stringify(c));

  c = config.resolveConfig({ apiKey: 'flag-key' }, { MORI_API_KEY_FILE: envFile, MORI_API_KEY: 'env-key' });
  assert(c.apiKey === 'env-file-key' && c.keySource === 'env-file', 'config: MORI_API_KEY_FILE beats MORI_API_KEY');

  c = config.resolveConfig({ apiKey: 'flag-key' }, { MORI_API_KEY: 'env-key' });
  assert(c.apiKey === 'env-key' && c.keySource === 'env', 'config: MORI_API_KEY beats the deprecated flag');

  c = config.resolveConfig({ apiKey: 'flag-key' }, {});
  assert(c.apiKey === 'flag-key' && c.keySource === 'flag', 'config: deprecated --api-key still works, last');

  c = config.resolveConfig({}, {});
  assert(c.apiKey === '' && c.keySource === 'none' && c.problem === null, 'config: no key → none, no problem');

  const loose = keyFile(dir, 'loose', 'loose-key\n', 0o644);
  let r = config.readKeyFile(loose);
  assert(!r.key && /0644/.test(r.problem || ''), 'config: 0644 key file refused, message names 0644', JSON.stringify(r));
  r = config.readKeyFile(keyFile(dir, 'group', 'g\n', 0o640));
  assert(!r.key && /0640/.test(r.problem || ''), 'config: 0640 key file refused');

  const link = join(dir, 'link');
  symlinkSync(good, link);
  r = config.readKeyFile(link);
  assert(!r.key && /symlink/.test(r.problem || ''), 'config: symlinked key file refused', JSON.stringify(r));

  r = config.readKeyFile(keyFile(dir, 'empty', ''));
  assert(!r.key && /empty/.test(r.problem || ''), 'config: empty key file is an error');
  r = config.readKeyFile(keyFile(dir, 'blank', '  \n\n'));
  assert(!r.key && /empty/.test(r.problem || ''), 'config: whitespace-only key file is an error');

  r = config.readKeyFile('~/' + join(dir.slice(HOME.length + 1), 'good'), { home: HOME });
  assert(r.key === 'file-key', '~ expands to the home directory', JSON.stringify(r));

  const missing = join(dir, 'nope');
  r = config.readKeyFile(missing);
  assert(!r.key && (r.problem || '').includes(missing), 'config: missing key file → error naming the path', JSON.stringify(r));
  c = config.resolveConfig({ apiKeyFile: missing }, { MORI_API_KEY: 'env-key' });
  assert(c.problem && c.apiKey === '', 'config: a bad key file is a problem, never a silent fall-through to the env key');

  r = config.readKeyFile(dir);
  assert(!r.key && r.problem, 'config: a directory is not a key file');

  r = config.readKeyFile(keyFile(dir, 'crlf', 'crlf-key\r\nsecond line\r\n'));
  assert(r.key === 'crlf-key', 'config: first line only, CRLF trimmed');

  r = config.readKeyFile(join(tmpdir(), 'outside-home'), { platform: 'win32', home: HOME });
  assert(!r.key && /outside the home/.test(r.problem || ''), 'config (win32): a key file outside home is refused');
}

// ---- 2. lib/post -----------------------------------------------------------------

console.log('\n── 2. lib/post: every status is classified and recorded ──\n');

{
  const srv = await startServer((req, res) => {
    const code = Number(new URL(req.url, 'http://x').searchParams.get('code') || '202');
    status(code, `${SENTINEL} echo ${req.headers['x-api-key'] || ''}`)(req, res);
  });
  const at = (code) => `${srv.url}/api/events/raw?code=${code}`;

  let r = await post.postEvent({ url: at(202), apiKey: SECRET, body: { a: 1 }, tag: 't-ok' });
  assert(r.ok && r.status === 202 && r.reason === null, 'post: 202 → ok');
  assert(srv.requests.at(-1).headers['x-api-key'] === SECRET, 'post: key sent in X-Api-Key');
  assert(srv.requests.at(-1).body === '{"a":1}', 'post: object body sent as JSON');

  r = await post.postEvent({ url: at(202), body: 'raw' });
  assert(!('x-api-key' in srv.requests.at(-1).headers), 'post: no key → no X-Api-Key header');

  r = await post.postEvent({ url: at(401), apiKey: SECRET, body: '{}', tag: 'mori-cursor' });
  assert(!r.ok && r.reason === 'auth' && r.status === 401, 'post: 401 → auth', JSON.stringify(r));
  assert(state.recentAuthFailure('mori-cursor')?.status === 401, 'post: 401 sets the auth marker');
  assert(/HTTP 401 \(auth\)/.test(readState('hook.log')), 'post: 401 logged', readState('hook.log'));

  r = await post.postEvent({ url: at(403), apiKey: SECRET, body: '{}', tag: 't403' });
  assert(r.reason === 'auth', 'post: 403 → auth');
  for (const [code, reason] of [[429, 'rate'], [404, 'client'], [400, 'client'], [500, 'server'], [503, 'server']]) {
    r = await post.postEvent({ url: at(code), apiKey: SECRET, body: '{}', tag: 't' });
    assert(!r.ok && r.reason === reason, `post: ${code} → ${reason}`, JSON.stringify(r));
  }
  const counters = readState('counters.jsonl').trim().split('\n').map((l) => JSON.parse(l).reason);
  assert(['auth', 'rate', 'client', 'server'].every((x) => counters.includes(x)), 'post: each failure counted', counters.join(','));

  // Per-tool marker: Claude Code's working key must not clear Cursor's rejection (#88 review).
  await post.postEvent({ url: at(202), apiKey: SECRET, body: '{}', tag: 'mori-ship' });
  assert(state.recentAuthFailure('mori-cursor')?.status === 401, 'post: another tool’s success leaves this tool’s auth marker');
  assert(state.recentAuthFailure('mori-ship') === null, 'post: one tool’s 401 is not reported as another’s');
  await post.postEvent({ url: at(202), apiKey: SECRET, body: '{}', tag: 'mori-cursor' });
  assert(state.recentAuthFailure('mori-cursor') === null, 'post: the same tool’s success clears its auth marker');

  // Plain http to loopback: no warning.
  assert(!existsSync(join(TMP, `mori-${UID}`, 'warn-plain-http-key')), 'post: plain http to loopback is not warned about');

  await srv.close();

  const all = readState('hook.log') + readState('counters.jsonl');
  assert(!all.includes(SECRET), 'post: key never in hook.log or counters.jsonl');
  assert(!all.includes(SENTINEL), 'post: response body never logged');
}

{
  // Never READ the body: a 401 whose body never finishes must still return promptly as auth.
  const srv = await startServer((_req, res) => {
    res.writeHead(401, { 'Content-Type': 'text/plain' });
    res.write(SENTINEL); // ...and never end()
  });
  const t0 = Date.now();
  const r = await withDeadline(
    post.postEvent({ url: `${srv.url}/api/events/raw`, apiKey: SECRET, body: '{}', timeoutMs: 3000, tag: 't-body' }), 6000);
  const ms = Date.now() - t0;
  assert(r.reason === 'auth' && ms < 2000, 'post: the response body is not read (unfinished body → prompt auth)', `${JSON.stringify(r)} in ${ms} ms`);
  await srv.close();
}

{
  const srv = await startServer(() => { /* never respond */ });
  const t0 = Date.now();
  const r = await withDeadline(
    post.postEvent({ url: `${srv.url}/api/events/raw`, body: '{}', timeoutMs: 400, tag: 't-hang' }), 5000);
  const ms = Date.now() - t0;
  assert(r.reason === 'timeout' && ms < 3000, 'post: a server that never answers → timeout within the limit', `${JSON.stringify(r)} in ${ms} ms`);
  await srv.close();

  const closed = await startServer(status(202));
  const deadUrl = `${closed.url}/api/events/raw`;
  await closed.close();
  const n = await post.postEvent({ url: deadUrl, body: '{}', tag: 't-net' });
  assert(n.reason === 'network', 'post: connection refused → network', JSON.stringify(n));

  const bad = await post.postEvent({ url: 'not a url', body: '{}', tag: 't-bad' });
  assert(!bad.ok, 'post: an invalid URL never throws');

  // Plain http with a key to a non-loopback host (TEST-NET-1, unroutable): warned once.
  await withDeadline(
    post.postEvent({ url: 'http://192.0.2.1:9/api/events/raw', apiKey: SECRET, body: '{}', timeoutMs: 200, tag: 't-plain' }), 5000);
  assert(existsSync(join(TMP, `mori-${UID}`, 'warn-plain-http-key')), 'post: key over plain http to a remote host is warned about');
  assert(!readState('hook.log').includes(SECRET), 'post: key not logged on timeout/network paths');
}

// ---- 3. Claude shipper as a child process ----------------------------------------

console.log('\n── 3. Claude shipper: foreign events, config problems, shipping ──\n');

{
  const tmp = mkdtempSync(join(tmpdir(), 'mori-hookkeys-ship-'));
  const kdir = mkdtempSync(join(HOME, 'shipkeys-'));
  const kf = keyFile(kdir, 'api-key', `${SECRET}\n`);
  const srv = await startServer(status(202));
  const base = ['--url', srv.url, '--client', 'test-client', '--api-key-file', kf];

  let r = await runHook(SHIP_EVENT, base, JSON.stringify({ hook_event_name: 'postToolUse', tool_name: 'x' }), cleanEnv(tmp));
  assert(r.code === 0 && srv.requests.length === 0, 'shipper: Cursor payload (postToolUse) → no request', `${r.code} ${srv.requests.length}`);
  assert(r.stderr === '' && readState('hook.log', tmp) === '', 'shipper: Cursor payload logs nothing', r.stderr);

  r = await runHook(SHIP_EVENT, [], JSON.stringify({ hook_event_name: 'beforeSubmitPrompt' }), cleanEnv(tmp));
  assert(r.code === 0 && r.stderr === '' && readState('hook.log', tmp) === '',
    'shipper: Cursor payload with NO config is silent (guard runs before the URL check)', r.stderr);
  assert(readState('counters.jsonl', tmp).includes('foreign-event'), 'shipper: foreign events counted');

  r = await runHook(SHIP_EVENT, base, JSON.stringify({ hook_event_name: 'PostToolUse', tool_name: 'Read' }), cleanEnv(tmp));
  assert(r.code === 0 && srv.requests.length === 1, 'shipper: Claude payload (PostToolUse) → one request');
  const req = srv.requests.at(-1);
  assert(req.url === '/api/events/raw?client=test-client', 'shipper: posts to /api/events/raw?client=', req.url);
  assert(req.headers['x-api-key'] === SECRET, 'shipper: key from --api-key-file sent');

  r = await runHook(SHIP_EVENT, base, JSON.stringify({ tool_name: 'x' }), cleanEnv(tmp));
  assert(srv.requests.length === 2, 'shipper: payload without hook_event_name → one request');

  r = await runHook(SHIP_EVENT, base, 'not json at all', cleanEnv(tmp));
  assert(srv.requests.length === 3 && srv.requests.at(-1).body === 'not json at all', 'shipper: malformed JSON still shipped as-is');

  r = await runHook(SHIP_EVENT, ['--client', 'c', '--mode', 'precompact'], JSON.stringify({ hook_event_name: 'PreCompact' }),
    cleanEnv(tmp, { MORI_SERVER_URL: srv.url, MORI_API_KEY_FILE: kf }));
  assert(srv.requests.at(-1).url === '/api/precompact?client=c', 'shipper: --mode precompact → /api/precompact (env config)');

  const n = srv.requests.length;
  const noUrlTmp = mkdtempSync(join(tmpdir(), 'mori-hookkeys-nourl-'));
  r = await runHook(SHIP_EVENT, [], JSON.stringify({ hook_event_name: 'Stop' }), cleanEnv(noUrlTmp));
  const lines = r.stderr.split('\n').filter(Boolean);
  assert(r.code === 0 && lines.length === 1 && /MORI_SERVER_URL/.test(lines[0]),
    'shipper: no URL → exactly one stderr line, exit 0', r.stderr);
  assert(!/Error|\n\s+at /.test(r.stderr), 'shipper: no URL → no exception text');
  r = await runHook(SHIP_EVENT, [], JSON.stringify({ hook_event_name: 'Stop' }), cleanEnv(noUrlTmp));
  assert(r.stderr === '', 'shipper: the no-URL warning is rate-limited', r.stderr);
  assert(srv.requests.length === n, 'shipper: no URL → no request');

  const loose = keyFile(kdir, 'loose', `${SECRET}\n`, 0o644);
  r = await runHook(SHIP_EVENT, ['--url', srv.url, '--api-key-file', loose], JSON.stringify({ hook_event_name: 'Stop' }),
    cleanEnv(mkdtempSync(join(tmpdir(), 'mori-hookkeys-loose-'))));
  assert(srv.requests.length === n && /0644/.test(r.stderr), 'shipper: 0644 key file → no request, stderr names 0644', r.stderr);
  assert(!r.stderr.includes(SECRET), 'shipper: key never echoed on stderr');

  await srv.close();

  // A rejected key → the marker the context hook reads (tag 'mori-ship').
  const deny = await startServer(status(401, SENTINEL));
  const authTmp = mkdtempSync(join(tmpdir(), 'mori-hookkeys-auth-'));
  r = await runHook(SHIP_EVENT, ['--url', deny.url, '--api-key-file', kf], JSON.stringify({ hook_event_name: 'Stop' }), cleanEnv(authTmp));
  assert(r.code === 0 && /rejected/.test(r.stderr), 'shipper: 401 → exit 0 with a stderr warning', r.stderr);
  assert(readState('auth-failure-mori-ship', authTmp).includes('401'), 'shipper: 401 → Claude Code auth marker');
  assert(!(readState('hook.log', authTmp) + r.stderr).includes(SECRET), 'shipper: key never logged on 401');
  await deny.close();

  // ---- 6. Claude context hook (Claude half) --------------------------------------

  console.log('\n── 6. Claude context hook: the rejected key reaches the session ──\n');

  const ctx = join(authTmp, 'ctx.txt');
  writeFileSync(ctx, 'NORMAL-CONTEXT');
  const start = JSON.stringify({ hook_event_name: 'SessionStart', source: 'startup', session_id: 's-88' });
  const hookEnv = (t) => cleanEnv(t, { MORI_SKIP_HEALTH_CHECK: '1', MORI_SESSION_CONTEXT_FILE: ctx });

  r = await runHook(CONTEXT_HOOK, ['--url', 'http://127.0.0.1:9'], start, hookEnv(authTmp));
  let out;
  try { out = JSON.parse(r.stdout).hookSpecificOutput.additionalContext; } catch { out = ''; }
  assert(/HTTP 401/.test(out) && /NOT/.test(out), 'context: recent 401 → the rejected-key message', r.stdout);
  assert(!out.includes('NORMAL-CONTEXT'), 'context: the notice replaces the context file this time');

  const cursorOnly = mkdtempSync(join(tmpdir(), 'mori-hookkeys-cur-'));
  mkdirSync(join(cursorOnly, `mori-${UID}`), { mode: 0o700 });
  writeFileSync(join(cursorOnly, `mori-${UID}`, 'auth-failure-mori-cursor'), JSON.stringify({ ts: Date.now(), status: 401 }), { mode: 0o600 });
  r = await runHook(CONTEXT_HOOK, ['--url', 'http://127.0.0.1:9'], start, hookEnv(cursorOnly));
  try { out = JSON.parse(r.stdout).hookSpecificOutput.additionalContext; } catch { out = ''; }
  assert(out === 'NORMAL-CONTEXT', 'context: Cursor’s rejection is not reported in a Claude Code session', r.stdout);

  const stale = mkdtempSync(join(tmpdir(), 'mori-hookkeys-stale-'));
  mkdirSync(join(stale, `mori-${UID}`), { mode: 0o700 });
  writeFileSync(join(stale, `mori-${UID}`, 'auth-failure-mori-ship'), JSON.stringify({ ts: Date.now() - 2 * 86_400_000, status: 401 }), { mode: 0o600 });
  r = await runHook(CONTEXT_HOOK, ['--url', 'http://127.0.0.1:9'], start, hookEnv(stale));
  try { out = JSON.parse(r.stdout).hookSpecificOutput.additionalContext; } catch { out = ''; }
  assert(out === 'NORMAL-CONTEXT', 'context: a rejection older than 24 h is not shown', r.stdout);

  const clean = mkdtempSync(join(tmpdir(), 'mori-hookkeys-clean-'));
  r = await runHook(CONTEXT_HOOK, ['--url', 'http://127.0.0.1:9'], start, hookEnv(clean));
  try { out = JSON.parse(r.stdout).hookSpecificOutput.additionalContext; } catch { out = ''; }
  assert(out === 'NORMAL-CONTEXT', 'context: no marker → unchanged behaviour', r.stdout);

  for (const d of [tmp, noUrlTmp, authTmp, cursorOnly, stale, clean]) rmSync(d, { recursive: true, force: true });
}

// ---- K2. Cursor installer: an explicit --api-key beats an exported MORI_API_KEY ----

console.log('\n── K2. Cursor installer key precedence (board ruling) ──\n');

{
  const INSTALLER = join(SCRIPTS, 'install-hooks-cursor.mjs');
  const install = async (args, envKey) => {
    const home = mkdtempSync(join(tmpdir(), 'mori-hookkeys-k2-'));
    mkdirSync(join(home, '.cursor'), { recursive: true, mode: 0o700 });
    const env = { ...cleanEnv(home), HOME: home, USERPROFILE: home };
    if (envKey) env.MORI_API_KEY = envKey;
    const r = await runHook(INSTALLER, ['--url', 'http://127.0.0.1:9', ...args], '', env);
    const kf = join(home, '.config', 'mori', 'api-key');
    const key = existsSync(kf) ? readFileSync(kf, 'utf8').trim() : null;
    rmSync(home, { recursive: true, force: true });
    return { ...r, key };
  };

  let r = await install(['--api-key', 'NEW-FLAG-KEY'], 'STALE-ENV-KEY');
  assert(r.code === 0 && r.key === 'NEW-FLAG-KEY', 'K2: --api-key beats a different exported MORI_API_KEY', `${r.key} ${r.stderr}`);
  assert(/both set and differ; using --api-key/.test(r.stderr), 'K2: the disagreement is warned about', r.stderr);
  assert(!r.stderr.includes('STALE-ENV-KEY') && !r.stderr.includes('NEW-FLAG-KEY'), 'K2: the warning names neither key');

  r = await install(['--api-key', 'SAME-KEY'], 'SAME-KEY');
  assert(r.key === 'SAME-KEY' && !/differ/.test(r.stderr), 'K2: equal flag and env → no warning', r.stderr);

  r = await install([], 'ENV-ONLY-KEY');
  assert(r.key === 'ENV-ONLY-KEY' && !/differ/.test(r.stderr), 'K2: MORI_API_KEY alone is used (the wrapper’s path)', r.stderr);
}

// ---- 4. Parity: CLAUDE_EVENTS = the events hooks.json wires -----------------------

console.log('\n── 4. Parity: the foreign-event allow-list ──\n');

{
  const wired = Object.keys(JSON.parse(readFileSync(join(PLUGIN, 'hooks', 'hooks.json'), 'utf8')).hooks);
  assert(
    wired.length === CLAUDE_EVENTS.length && [...wired].sort().join() === [...CLAUDE_EVENTS].sort().join(),
    'parity: CLAUDE_EVENTS equals the event keys of hooks/hooks.json',
    `${wired} vs ${CLAUDE_EVENTS}`,
  );
  assert(isForeignEvent({ hook_event_name: 'postToolUse' }), 'events: postToolUse (Cursor) is foreign — case-sensitive');
  assert(isForeignEvent({ hook_event_name: 'stop' }), 'events: stop (Cursor) is foreign');
  assert(!isForeignEvent({ hook_event_name: 'Stop' }), 'events: Stop is ours');
  assert(!isForeignEvent({}) && !isForeignEvent({ hook_event_name: '' }) && !isForeignEvent(null),
    'events: a missing or empty name is not foreign (ship it)');
}

rmSync(TMP, { recursive: true, force: true });
rmSync(HOME, { recursive: true, force: true });

console.log(`\n── Results: ${passed} passed, ${failed} failed ──\n`);
if (failed > 0) process.exit(1);
