/**
 * test_install_hooks_cursor.mjs — Hermetic tests for install-hooks-cursor.mjs
 *
 * Run: node plugins/mori/tests/test_install_hooks_cursor.mjs
 */

import { buildHookConfig, mergeHooksFile } from '../scripts/install-hooks-cursor.mjs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPTS = resolve(__dirname, '../scripts');

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

console.log('\n── install-hooks-cursor.mjs (minimal) ──\n');

{
  const { config } = buildHookConfig(false, SCRIPTS, 'http://127.0.0.1:8968', 'testkey');
  assert(Object.keys(config.hooks).length === 3, 'minimal: 3 events');
  assert('sessionStart' in config.hooks, 'minimal: sessionStart');
  assert('postToolUse' in config.hooks, 'minimal: postToolUse');
  assert('stop' in config.hooks, 'minimal: stop');
  assert(
    config.hooks.postToolUse[0].command.includes('--event postToolUse'),
    'minimal: postToolUse event flag',
  );
}

console.log('\n── install-hooks-cursor.mjs (parity) ──\n');

{
  const { config } = buildHookConfig(true, SCRIPTS, 'http://127.0.0.1:8968', '');
  assert(Object.keys(config.hooks).length === 5, 'parity: 5 events');
  assert('beforeSubmitPrompt' in config.hooks, 'parity: beforeSubmitPrompt');
  assert('postToolUseFailure' in config.hooks, 'parity: postToolUseFailure');
  assert(
    config.hooks.beforeSubmitPrompt[0].command.includes('beforeSubmitPrompt'),
    'parity: beforeSubmitPrompt flag',
  );
}

console.log('\n── mergeHooksFile preserves non-mori entries ──\n');

{
  const existing = {
    version: 1,
    hooks: {
      postToolUse: [{ command: '/usr/bin/my-custom-hook.sh' }],
    },
  };
  const { config } = buildHookConfig(false, SCRIPTS, 'http://127.0.0.1:8968', '');
  const merged = mergeHooksFile(existing, config);
  const cmds = merged.hooks.postToolUse.map((e) => e.command);
  assert(cmds.some((c) => c.includes('my-custom-hook')), 'merge: preserves custom hook');
  assert(cmds.some((c) => c.includes('mori-ship-event-cursor')), 'merge: adds mori hook');
  assert(cmds.length === 2, 'merge: two postToolUse entries');
}

console.log(`\n── Results: ${passed} passed, ${failed} failed ──\n`);
if (failed > 0) process.exit(1);
