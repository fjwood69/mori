/**
 * mori-post-compact-hook-antigravity.mjs — Mori PostCompact hook for Antigravity (Node ESM)
 *
 * Fires on the Antigravity `PostCompact` event (after context compaction).
 * Returns:
 *   {
 *     "systemMessage": "Context compressed — running /brief --post-compact to re-ground.",
 *     "hookSpecificOutput": {
 *       "hookEventName": "PostCompact",
 *       "additionalContext": "Context was just compressed. Before doing anything else, run `/brief --post-compact` to re-ground — a lightweight delta of what changed in shared state since the last brief (new/superseded memories, pending mori-msg items, NATS traffic)."
 *     }
 *   }
 *
 * Always exits 0 (fail-open). Any error → write nothing, exit 0.
 *
 * Usage (wired by install-hooks-antigravity.mjs into ~/.gemini/config/hooks.json):
 *   node /abs/path/mori-post-compact-hook-antigravity.mjs
 */

import { runFailOpen } from './lib/fail-open.mjs';

function main() {
  if (process.env.MORI_POST_COMPACT_BRIEF === 'false') {
    process.stdout.write(JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'PostCompact',
        additionalContext: '',
      },
    }) + '\n');
    process.exit(0);
  }

  process.stdout.write(JSON.stringify({
    systemMessage: "Context compressed — running /brief --post-compact to re-ground.",
    hookSpecificOutput: {
      hookEventName: "PostCompact",
      additionalContext: "Context was just compressed. Before doing anything else, run `/brief --post-compact` to re-ground — a lightweight delta of what changed in shared state since the last brief (new/superseded memories, pending mori-msg items, NATS traffic)."
    }
  }) + '\n');
  process.exit(0);
}

runFailOpen(main);
