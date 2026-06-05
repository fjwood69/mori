/**
 * mori-context-hook.mjs — Mori cross-platform Claude Code SessionStart hook (Node ESM)
 *
 * Re-grounds the agent at session boundaries using the SANCTIONED SessionStart
 * mechanism documented by Claude Code — NOT a PostCompact/sentinel workaround.
 * (PostCompact is observability-only and cannot inject context; SessionStart
 * re-fires after a compaction with source="compact" and supports additionalContext.)
 *
 * Wired to the SessionStart event. Branches on the `source` field:
 *
 *   source === "compact":
 *     Fires when the session resumes after a context compaction. Emits a nudge to
 *     run /brief --post-compact for a lightweight delta re-ground.
 *     Disable with MORI_POST_COMPACT_BRIEF=false.
 *
 *   source === "startup" | "resume" | "clear":
 *     New / resumed / cleared session. If MORI_SESSION_CONTEXT_FILE points at a
 *     readable file, injects its contents as additionalContext. Public default:
 *     the env var is unset, so nothing is injected. Private deployments point it
 *     at an operational-context file (kept out of this public repo).
 *
 * Design:
 *   - SessionStart fires exactly once per boundary, so there is NO per-prompt
 *     wallpaper risk and no throttle is needed (unlike a UserPromptSubmit hook).
 *   - Output cap is 10,000 chars (harness limit); a context file over that is
 *     truncated by the harness — keep context files small.
 *   - Fail open: any error -> write nothing, exit 0. A crashing hook disables the
 *     entire event block, so robustness beats diagnostics here.
 *   - Node built-ins only (process, fs). No npm packages. ESM.
 *
 * Invoked by the Claude Code harness as:
 *   node "${CLAUDE_PLUGIN_ROOT}/scripts/mori-context-hook.mjs"
 */

import { readFileSync, existsSync } from 'fs';

function emit(additionalContext) {
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'SessionStart',
        additionalContext,
      },
    }) + '\n',
  );
}

async function main() {
  let raw = '';
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    raw = Buffer.concat(chunks).toString('utf8').trim();
  } catch {
    process.exit(0);
  }

  if (!raw) process.exit(0);

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    process.exit(0);
  }

  if ((payload.hook_event_name || '').trim() !== 'SessionStart') {
    process.exit(0);
  }

  const source = (payload.source || '').trim();

  if (source === 'compact') {
    if (process.env.MORI_POST_COMPACT_BRIEF === 'false') process.exit(0);
    emit(
      'Context was just compacted. Before doing anything else, run `/brief --post-compact` ' +
        'to re-ground — a lightweight delta of what changed in shared state since the last ' +
        'brief (new/superseded memories, pending mori-msg items, NATS traffic). Run it first, ' +
        'then continue.',
    );
    process.exit(0);
  }

  const ctxFile = process.env.MORI_SESSION_CONTEXT_FILE;
  if (ctxFile) {
    try {
      if (existsSync(ctxFile)) {
        const body = readFileSync(ctxFile, 'utf8').trim();
        if (body) emit(body);
      }
    } catch {
      process.exit(0);
    }
  }

  process.exit(0);
}

main().catch(() => process.exit(0));
