/**
 * mori-post-compact-hook-antigravity.mjs — NOT IMPLEMENTED
 *
 * This file is a stub. The original implementation used `PostCompact` with
 * `hookSpecificOutput.additionalContext` — neither exists in Antigravity 2.0's
 * hook schema. The hook was silently doing nothing.
 *
 * Antigravity 2.0 hook events: PreToolUse, PostToolUse, PreInvocation,
 * PostInvocation, Stop. There is no PostCompact event.
 *
 * Context injection in AG uses PreInvocation → injectSteps → ephemeralMessage:
 *   { "injectSteps": [{ "ephemeralMessage": "run /brief --post-compact..." }] }
 *
 * TODO: Implement as a PreInvocation hook that:
 *   1. Reads the transcriptPath from stdin to detect whether compaction occurred
 *      since the last invocation (look for a compaction marker in the JSONL).
 *   2. If compaction detected: return injectSteps with an ephemeralMessage nudge
 *      and write a session flag to avoid repeating the nudge.
 *   3. Otherwise: return {}.
 *
 * Until this is implemented, post-compact re-grounding in Antigravity requires
 * manually running /brief --post-compact after a compaction.
 *
 * Usage (wired by install-hooks-antigravity.mjs into ~/.gemini/config/hooks.json):
 *   node /abs/path/mori-post-compact-hook-antigravity.mjs
 */

// Stub — exits cleanly with no output so the hook doesn't error on install.
process.exit(0);
