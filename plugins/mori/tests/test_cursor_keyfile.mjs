/**
 * test_cursor_keyfile.mjs — #88 Cursor installer and session-start auth notice.
 *
 * Run: node plugins/mori/tests/test_cursor_keyfile.mjs
 *
 * HOME and TMPDIR are pointed at a temp directory. The key string must not
 * appear in hooks.json, in --dry-run output, or in the process argv the
 * installer writes.
 */

import { spawnSync } from 'child_process';
import {
  chmodSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, rmSync,
  symlinkSync, writeFileSync,
} from 'fs';
import { tmpdir } from 'os';
import { join, resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPTS = resolve(__dirname, '../scripts');
const INSTALLER = join(SCRIPTS, 'install-hooks-cursor.mjs');
const CONTEXT_HOOK = join(SCRIPTS, 'mori-context-hook-cursor.mjs');
const SECRET = 'cursor-test-secret-88';
const UID = typeof process.getuid === 'function' ? String(process.getuid()) : 'user';

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

function freshHome() {
  const home = mkdtempSync(join(tmpdir(), 'mori-cursor-home-'));
  mkdirSync(join(home, '.cursor'), { recursive: true, mode: 0o700 });
  return home;
}

function childEnv(home, extra = {}) {
  const env = { ...process.env, HOME: home, USERPROFILE: home };
  delete env.MORI_API_KEY;
  delete env.MORI_API_KEY_FILE;
  delete env.MORI_SERVER_URL;
  return { ...env, ...extra };
}

function runNode(script, args, env) {
  return spawnSync(process.execPath, [script, ...args], {
    env,
    encoding: 'utf8',
    timeout: 8000,
  });
}

function mode(path) {
  return lstatSync(path).mode & 0o777;
}

function hookCommands(hooksPath) {
  const doc = JSON.parse(readFileSync(hooksPath, 'utf8'));
  const cmds = [];
  for (const entries of Object.values(doc.hooks || {})) {
    for (const entry of entries || []) cmds.push(entry.command || '');
  }
  return cmds;
}

const homes = [];

console.log('\n── Cursor installer: key file, never the key ──\n');

{
  const home = freshHome();
  homes.push(home);
  const r = runNode(
    INSTALLER,
    ['--url', 'http://127.0.0.1:8968'],
    childEnv(home, { MORI_API_KEY: SECRET }),
  );
  const hooksPath = join(home, '.cursor', 'hooks.json');
  const keyPath = join(home, '.config', 'mori', 'api-key');
  const hooks = readFileSync(hooksPath, 'utf8');
  const cmds = hookCommands(hooksPath);
  assert(r.status === 0, 'install: exits 0', r.stderr);
  assert(cmds.some((c) => c.includes('--api-key-file "')), 'install: hooks.json references --api-key-file', cmds.join('\n'));
  assert(!hooks.includes(SECRET), 'install: hooks.json contains no substring of the key');
  assert(cmds.every((c) => !c.includes('--api-key "')), 'install: hooks.json has no --api-key flag');
  assert(readFileSync(keyPath, 'utf8').trim() === SECRET, 'install: key file holds the key');
  assert(mode(keyPath) === 0o600, 'install: key file is 0600', mode(keyPath).toString(8));
  assert(mode(dirname(keyPath)) === 0o700, 'install: key dir is 0700', mode(dirname(keyPath)).toString(8));
  assert(mode(hooksPath) === 0o600, 'install: hooks.json is 0600', mode(hooksPath).toString(8));
}

console.log('\n── migration of a legacy --api-key entry ──\n');

{
  const home = freshHome();
  homes.push(home);
  const hooksPath = join(home, '.cursor', 'hooks.json');
  writeFileSync(hooksPath, JSON.stringify({
    version: 1,
    hooks: {
      stop: [{
        command: `node "/opt/mori-ship-event-cursor.mjs" --url "http://127.0.0.1:8968" --api-key "${SECRET}"`,
      }],
    },
  }));
  const r = runNode(INSTALLER, ['--url', 'http://127.0.0.1:8968'], childEnv(home));
  const hooks = readFileSync(hooksPath, 'utf8');
  const keyPath = join(home, '.config', 'mori', 'api-key');
  assert(r.status === 0, 'migrate: exits 0', r.stderr);
  assert(readFileSync(keyPath, 'utf8').trim() === SECRET, 'migrate: harvested key is in the key file');
  assert(!hooks.includes(SECRET), 'migrate: key is gone from hooks.json');
  assert(
    hookCommands(hooksPath).some((c) => c.includes('--api-key-file "')),
    'migrate: hooks.json now references the file',
  );
}

console.log('\n── unparseable legacy key aborts ──\n');

{
  const home = freshHome();
  homes.push(home);
  const hooksPath = join(home, '.cursor', 'hooks.json');
  const original = JSON.stringify({
    version: 1,
    hooks: {
      stop: [{ command: 'node "/opt/mori-ship-event-cursor.mjs" --url "http://127.0.0.1:8968" --api-key' }],
    },
  });
  writeFileSync(hooksPath, original);
  const r = runNode(INSTALLER, ['--url', 'http://127.0.0.1:8968'], childEnv(home));
  assert(r.status === 1, 'unparseable: exits 1', String(r.status));
  assert(/could not be read|not be lost/i.test(r.stderr), 'unparseable: error names the lost key', r.stderr);
  assert(readFileSync(hooksPath, 'utf8') === original, 'unparseable: hooks.json is left unchanged');
}

console.log('\n── dry-run prints no key ──\n');

{
  const home = freshHome();
  homes.push(home);
  const r = runNode(
    INSTALLER,
    ['--url', 'http://127.0.0.1:8968', '--dry-run'],
    childEnv(home, { MORI_API_KEY: SECRET }),
  );
  const out = `${r.stdout}\n${r.stderr}`;
  assert(r.status === 0, 'dry-run: exits 0', r.stderr);
  assert(out.includes('Would write the API key to:'), 'dry-run: says it would write the key file');
  assert(!out.includes(SECRET), 'dry-run: output contains no key');
  assert(!existsSyncSafe(join(home, '.cursor', 'hooks.json')), 'dry-run: writes nothing');
}

function existsSyncSafe(p) {
  try { lstatSync(p); return true; } catch { return false; }
}

console.log('\n── symlinked hooks.json keeps its link ──\n');

{
  const home = freshHome();
  homes.push(home);
  const target = join(home, 'real-hooks.json');
  writeFileSync(target, '{ "version": 1, "hooks": {} }\n');
  const link = join(home, '.cursor', 'hooks.json');
  symlinkSync(target, link);
  const r = runNode(
    INSTALLER,
    ['--url', 'http://127.0.0.1:8968'],
    childEnv(home, { MORI_API_KEY: SECRET }),
  );
  assert(r.status === 0, 'symlink: exits 0', r.stderr);
  assert(lstatSync(link).isSymbolicLink(), 'symlink: hooks.json is still a symlink');
  const body = readFileSync(target, 'utf8');
  assert(
    hookCommands(target).some((c) => c.includes('--api-key-file "')),
    'symlink: target was updated',
    body,
  );
  assert(!body.includes(SECRET), 'symlink: target does not contain the key');
}

console.log('\n── unsafe URL is rejected ──\n');

{
  const home = freshHome();
  homes.push(home);
  const r = runNode(
    INSTALLER,
    ['--url', 'http://127.0.0.1:8968/$HOME'],
    childEnv(home, { MORI_API_KEY: SECRET }),
  );
  assert(r.status === 1, 'unsafe url: exits 1');
  assert(/unsafe/i.test(r.stderr), 'unsafe url: error names the problem', r.stderr);
  const quoted = runNode(
    INSTALLER,
    ['--url', 'http://127.0.0.1:8968/a"b'],
    childEnv(home, { MORI_API_KEY: SECRET }),
  );
  assert(quoted.status === 1, 'quoted url: exits 1');
}

console.log('\n── context hook surfaces a rejected key ──\n');

{
  const tmp = mkdtempSync(join(tmpdir(), 'mori-cursor-state-'));
  homes.push(tmp);
  const state = join(tmp, `mori-${UID}`);
  mkdirSync(state, { recursive: true, mode: 0o700 });
  chmodSync(state, 0o700);
  writeFileSync(
    join(state, 'auth-failure-mori-cursor'),
    JSON.stringify({ ts: Date.now(), status: 401 }),
    { mode: 0o600 },
  );
  const ctxFile = join(tmp, 'ctx.txt');
  writeFileSync(ctxFile, 'CONTEXT-FILE-SHOULD-NOT-WIN');
  const withStdin = spawnSync(process.execPath, [CONTEXT_HOOK, '--url', 'http://127.0.0.1:8968'], {
    input: JSON.stringify({ hook_event_name: 'sessionStart', conversation_id: 'conv-auth' }),
    env: childEnv(tmp, {
      TMPDIR: tmp,
      MORI_SKIP_HEALTH_CHECK: '1',
      MORI_SESSION_CONTEXT_FILE: ctxFile,
    }),
    encoding: 'utf8',
    timeout: 8000,
  });
  assert(withStdin.status === 0, 'auth notice: exits 0', withStdin.stderr);
  let parsed;
  try { parsed = JSON.parse((withStdin.stdout || '').trim()); } catch { /* noop */ }
  assert(
    typeof parsed?.additional_context === 'string' &&
      parsed.additional_context.includes('HTTP 401') &&
      parsed.additional_context.includes('NOT'),
    'auth notice: additional_context names the rejection',
    withStdin.stdout,
  );
  assert(
    !String(withStdin.stdout).includes('CONTEXT-FILE-SHOULD-NOT-WIN'),
    'auth notice: stops before the context file',
  );
}

{
  const tmp = mkdtempSync(join(tmpdir(), 'mori-cursor-state-'));
  homes.push(tmp);
  const ctxFile = join(tmp, 'ctx.txt');
  writeFileSync(ctxFile, 'Hello from ctx file.');
  const r = spawnSync(process.execPath, [CONTEXT_HOOK, '--url', 'http://127.0.0.1:8968'], {
    input: JSON.stringify({ hook_event_name: 'sessionStart', conversation_id: 'conv-clean' }),
    env: childEnv(tmp, {
      TMPDIR: tmp,
      MORI_SKIP_HEALTH_CHECK: '1',
      MORI_SESSION_CONTEXT_FILE: ctxFile,
    }),
    encoding: 'utf8',
    timeout: 8000,
  });
  let parsed;
  try { parsed = JSON.parse((r.stdout || '').trim()); } catch { /* noop */ }
  assert(r.status === 0, 'no marker: exits 0');
  assert(
    parsed?.additional_context?.includes('Hello from ctx file.'),
    'no marker: context file is injected as before',
    r.stdout,
  );
}

for (const home of homes) {
  try { rmSync(home, { recursive: true, force: true }); } catch { /* noop */ }
}

console.log(`\n── Results: ${passed} passed, ${failed} failed ──\n`);
if (failed > 0) process.exit(1);
