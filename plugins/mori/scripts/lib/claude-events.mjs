/**
 * lib/claude-events.mjs — the Claude Code hook events this plugin is wired for (Node ESM)
 *
 * #88: Cursor executes Claude Code plugin hooks as a compatibility feature, but without
 * Claude Code's settings env and with its own event names (lower camelCase: postToolUse,
 * beforeSubmitPrompt, stop). The Claude shipper must ignore those — Cursor's own mori
 * plugin ships them — or each Cursor event would be shipped twice (once configured) or
 * fail once per event (today).
 *
 * The list MUST equal the event keys of plugins/mori/hooks/hooks.json; a parity test fails
 * the build when they disagree. Comparison is exact and case-sensitive: lower-casing would
 * re-admit Cursor's `postToolUse` as `PostToolUse`.
 */

export const CLAUDE_EVENTS = Object.freeze([
  'SessionStart',
  'PostToolUse',
  'PostToolUseFailure',
  'UserPromptSubmit',
  'Stop',
  'PreCompact',
]);

/**
 * True when the payload names an event this plugin is not wired for (another host).
 * A missing/empty hook_event_name is NOT foreign: ship it (some hosts omit the field).
 */
export function isForeignEvent(payload) {
  const name = payload && typeof payload === 'object' ? payload.hook_event_name : undefined;
  if (typeof name !== 'string' || name === '') return false;
  return !CLAUDE_EVENTS.includes(name);
}
