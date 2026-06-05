/**
 * test_health_gate.mjs — Hermetic tests for lib/health-gate.mjs (Node ESM)
 *
 * Tests:
 *   checkServer()   — "up" (live server + 404), "down" (closed port), "unconfigured"
 *   getCached()     — miss, hit within TTL, expired after TTL
 *   setCached()     — writes, then getCached reads
 *   Error safety    — never throws
 *
 * The "up" and "404" cases use a tiny inline http.Server on an ephemeral port.
 * The "down" case uses a port that is provably closed.
 * The "unconfigured" cases use empty / garbage / non-http URLs — no network call.
 *
 * Run: node plugins/mori/tests/test_health_gate.mjs
 */

import { createServer } from 'http';
import { mkdtempSync, rmSync, writeFileSync, existsSync, unlinkSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPTS   = resolve(__dirname, '../scripts');

// Import the module under test
const { checkServer, getCached, setCached } = await import(join(SCRIPTS, 'lib/health-gate.mjs'));

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

// ── Temp dir (used as TMPDIR for cache files) ─────────────────────────────────

const TMP = mkdtempSync(join(tmpdir(), 'mori-hg-test-'));
process.env.TMPDIR = TMP;

function cleanup() {
  try { rmSync(TMP, { recursive: true, force: true }); } catch { /* noop */ }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Start an HTTP server on an ephemeral port and return { server, url, port }.
 * The server responds with the given statusCode.
 */
function startServer(statusCode) {
  return new Promise((resolve, reject) => {
    const server = createServer((_req, res) => {
      res.writeHead(statusCode);
      res.end(statusCode === 200 ? '{"status":"ok"}' : '');
    });
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      resolve({ server, port, url: `http://127.0.0.1:${port}` });
    });
  });
}

/** Find a port that is definitely closed (nothing listening). */
async function findClosedPort() {
  // Bind a server, record the port, close it, return the port.
  const srv = await startServer(200);
  const port = srv.port;
  await new Promise((res) => srv.server.close(res));
  // Give the OS a moment to fully release the port
  await new Promise((res) => setTimeout(res, 50));
  return port;
}

// ── Tests ─────────────────────────────────────────────────────────────────────

console.log('\n── checkServer() — unconfigured cases ──\n');

{
  const result = await checkServer('');
  assert(result === 'unconfigured', 'empty string → unconfigured');
}

{
  const result = await checkServer(null);
  assert(result === 'unconfigured', 'null → unconfigured');
}

{
  const result = await checkServer(undefined);
  assert(result === 'unconfigured', 'undefined → unconfigured');
}

{
  const result = await checkServer('   ');
  assert(result === 'unconfigured', 'whitespace-only → unconfigured');
}

{
  // Default placeholder — no network call should be made
  const result = await checkServer('http://localhost:8968');
  assert(result === 'unconfigured', 'default placeholder → unconfigured (no fetch)');
}

{
  const result = await checkServer('http://localhost:8968/');
  assert(result === 'unconfigured', 'default placeholder with trailing slash → unconfigured');
}

{
  const result = await checkServer('not-a-url');
  assert(result === 'unconfigured', 'unparseable string → unconfigured');
}

{
  const result = await checkServer('ftp://example.com');
  assert(result === 'unconfigured', 'non-http(s) scheme → unconfigured');
}

console.log('\n── checkServer() — down (closed port) ──\n');

{
  const closedPort = await findClosedPort();
  const start = Date.now();
  const result = await checkServer(`http://127.0.0.1:${closedPort}`);
  const elapsed = Date.now() - start;
  assert(result === 'down', `closed port → down (port ${closedPort})`);
  // Should resolve quickly — network refusal is fast; well under 600ms timeout
  assert(elapsed < 2000, `closed port resolved in < 2s (actual: ${elapsed}ms)`);
}

console.log('\n── checkServer() — up (live server, 200) ──\n');

{
  const { server, url } = await startServer(200);
  try {
    const result = await checkServer(url);
    assert(result === 'up', `200 response → up (${url})`);
  } finally {
    await new Promise((res) => server.close(res));
  }
}

console.log('\n── checkServer() — up (live server, 404) ──\n');

{
  // 404 should be treated as "up" — server alive but /health not found (older servers)
  const { server, url } = await startServer(404);
  try {
    const result = await checkServer(url);
    assert(result === 'up', `404 response → up (server alive, no /health route)`);
  } finally {
    await new Promise((res) => server.close(res));
  }
}

console.log('\n── checkServer() — down (500 response) ──\n');

{
  const { server, url } = await startServer(503);
  try {
    const result = await checkServer(url);
    assert(result === 'down', `503 response → down`);
  } finally {
    await new Promise((res) => server.close(res));
  }
}

console.log('\n── Session cache — getCached / setCached ──\n');

{
  // Cache miss on fresh sessionId
  const state = getCached('test-session-miss-' + Date.now());
  assert(state === null, 'cache miss on new sessionId → null');
}

{
  // Write then read within TTL
  const sid = 'test-session-hit-' + Date.now();
  setCached(sid, 'up');
  const state = getCached(sid);
  assert(state === 'up', 'setCached("up") then getCached → "up"');
}

{
  // Write "down", read back
  const sid = 'test-session-down-' + Date.now();
  setCached(sid, 'down');
  const state = getCached(sid);
  assert(state === 'down', 'setCached("down") then getCached → "down"');
}

{
  // Write "unconfigured", read back
  const sid = 'test-session-unconf-' + Date.now();
  setCached(sid, 'unconfigured');
  const state = getCached(sid);
  assert(state === 'unconfigured', 'setCached("unconfigured") then getCached → "unconfigured"');
}

{
  // Simulate expired TTL by manually writing a stale cache file
  const sid = 'test-session-expired-' + Date.now();
  const safe = sid.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 128);
  const cacheFile = join(TMP, `mori-health-${safe}.json`);
  const staleTs = Date.now() - 6 * 60 * 1000; // 6 minutes ago → expired (TTL = 5min)
  writeFileSync(cacheFile, JSON.stringify({ state: 'up', ts: staleTs }), 'utf8');
  const state = getCached(sid);
  assert(state === null, 'expired cache entry (6 min old, TTL=5 min) → null (cache miss)');
}

{
  // Corrupt cache file → null (no throw)
  const sid = 'test-session-corrupt-' + Date.now();
  const safe = sid.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 128);
  const cacheFile = join(TMP, `mori-health-${safe}.json`);
  writeFileSync(cacheFile, 'not valid json}{', 'utf8');
  let threw = false;
  let result = null;
  try {
    result = getCached(sid);
  } catch {
    threw = true;
  }
  assert(!threw, 'getCached on corrupt file → no throw');
  assert(result === null, 'getCached on corrupt file → null');
}

{
  // setCached with TMPDIR pointing at a non-writable path → no throw
  const origTmp = process.env.TMPDIR;
  process.env.TMPDIR = '/this-path-does-not-exist-mori-test';
  let threw = false;
  try {
    setCached('test-nowrite', 'up');
  } catch {
    threw = true;
  }
  process.env.TMPDIR = origTmp;
  assert(!threw, 'setCached to unwritable TMPDIR → no throw (fail-open)');
}

{
  // checkServer never throws — wrap in try/catch and assert
  let threw = false;
  try {
    // Pass a garbage string that new URL() will reject → returns "unconfigured", no throw
    await checkServer('::::not-a-real-url::::');
  } catch {
    threw = true;
  }
  assert(!threw, 'checkServer with garbage URL → no throw');
}

// ── Results ───────────────────────────────────────────────────────────────────

cleanup();

console.log(`\n── Results: ${passed} passed, ${failed} failed ──\n`);
if (failed > 0) process.exit(1);
