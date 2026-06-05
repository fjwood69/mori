/**
 * lib/canonical.mjs — Canonical event schema normalizer (Node ESM)
 *
 * Export: toCanonical(clientEvent, { client, eventName })
 *
 * Returns an object that matches the canonical field set that mori-ship-event.mjs
 * ships to /api/events/raw. Client-specific fields are mapped to canonical names;
 * any client-specific leftovers that have no canonical mapping are placed under
 * _clientMeta: { client, ...extras } so the server can log them opaquely.
 *
 * ── CANONICAL FIELD LIST ────────────────────────────────────────────────────
 * Derived from mori-ship-event.mjs: the script reads raw stdin and POSTs it
 * verbatim (or after Stop enrichment) to /api/events/raw. Claude Code delivers
 * these fields in the hook event JSON (documented by Anthropic):
 *
 *   session_id          string   Unique session identifier
 *   hook_event_name     string   Event name (SessionStart, PostToolUse, Stop, …)
 *   transcript_path     string   Absolute path to the session transcript .jsonl
 *   tool_name           string   Tool name (PostToolUse events)
 *   tool_input          object   Tool input params (PostToolUse events)
 *   tool_response       object   Tool response/output (PostToolUse events)
 *   source              string   Session source (SessionStart: startup/resume/clear/compact)
 *   cwd                 string   Working directory at hook time
 *   workspace_roots     array    Array of workspace root objects [{ path }]
 *   conversation_id     string   Conversation identifier (some clients/events)
 *   transcript_tail_b64 string   Base64 tail of transcript (added by Stop enrichment)
 *
 * _clientMeta is always present and contains at minimum { client }.
 * ────────────────────────────────────────────────────────────────────────────
 *
 * ── PER-CLIENT FIELD MAPPINGS ───────────────────────────────────────────────
 * Cursor (snake_case inputs):
 *   conversation_id   → session_id        (Cursor uses this as the session key)
 *   hook_event_name   → hook_event_name   (identity)
 *   transcript_path   → transcript_path   (identity)
 *   workspace_roots   → workspace_roots   (identity; array of { path } objects)
 *   [other fields]    → _clientMeta.rest
 *
 *   Cursor event names map to canonical names:
 *     postToolUse → PostToolUse
 *     stop        → Stop
 *     sessionStart → SessionStart
 *     preToolUse  → PreToolUse
 *     preCompact  → PreCompact
 *
 * Antigravity (camelCase inputs):
 *   conversationId    → session_id
 *   transcriptPath    → transcript_path
 *   stepIdx           → _clientMeta.stepIdx
 *   error             → _clientMeta.error
 *   [hook_event_name is supplied via --event CLI flag, not in stdin]
 *
 *   Antigravity event names are already PascalCase (PostToolUse, Stop, PreInvocation).
 * ────────────────────────────────────────────────────────────────────────────
 */

// Map Cursor camelCase/lowerCamelCase event names to canonical PascalCase
const CURSOR_EVENT_NAME_MAP = {
  sessionstart: 'SessionStart',
  posttooluse: 'PostToolUse',
  pretooluse: 'PreToolUse',
  stop: 'Stop',
  precompact: 'PreCompact',
};

/**
 * Normalise a Cursor hook event JSON object to the canonical schema.
 *
 * @param {object} ev        Parsed Cursor event object (snake_case)
 * @param {string} eventName Event name string (from hook_event_name field or external)
 * @returns {object}         Canonical event object
 */
function fromCursor(ev, eventName) {
  const {
    conversation_id,
    hook_event_name,
    transcript_path,
    workspace_roots,
    tool_name,
    tool_input,
    tool_response,
    source,
    cwd,
    ...rest
  } = ev;

  // Resolve the canonical event name
  const rawName = eventName || hook_event_name || '';
  const canonicalEventName =
    CURSOR_EVENT_NAME_MAP[rawName.toLowerCase()] || rawName;

  return {
    session_id: conversation_id || ev.session_id || '',
    hook_event_name: canonicalEventName,
    transcript_path: transcript_path || '',
    ...(tool_name !== undefined && { tool_name }),
    ...(tool_input !== undefined && { tool_input }),
    ...(tool_response !== undefined && { tool_response }),
    ...(source !== undefined && { source }),
    ...(cwd !== undefined && { cwd }),
    ...(workspace_roots !== undefined && { workspace_roots }),
    _clientMeta: { client: 'cursor', ...rest },
  };
}

/**
 * Normalise an Antigravity hook event JSON object to the canonical schema.
 * The event name comes from the --event CLI flag (not the stdin payload).
 *
 * @param {object} ev        Parsed Antigravity event object (camelCase)
 * @param {string} eventName Event name string supplied from CLI (PascalCase)
 * @returns {object}         Canonical event object
 */
function fromAntigravity(ev, eventName) {
  const {
    conversationId,
    transcriptPath,
    stepIdx,
    error,
    ...rest
  } = ev;

  return {
    session_id: conversationId || ev.session_id || '',
    hook_event_name: eventName || '',
    transcript_path: transcriptPath || ev.transcript_path || '',
    _clientMeta: {
      client: 'antigravity',
      ...(stepIdx !== undefined && { stepIdx }),
      ...(error !== undefined && { error }),
      ...rest,
    },
  };
}

/**
 * Convert a client-specific event object to the canonical mori event schema.
 *
 * @param {object} clientEvent                  Parsed event JSON from stdin
 * @param {{ client: string, eventName: string }} opts
 *   client    — 'cursor' | 'antigravity'
 *   eventName — event name string (for Antigravity: from --event flag;
 *               for Cursor: from hook_event_name field or override)
 * @returns {object} Canonical event object
 */
export function toCanonical(clientEvent, { client, eventName }) {
  if (!clientEvent || typeof clientEvent !== 'object') {
    return {
      session_id: '',
      hook_event_name: eventName || '',
      transcript_path: '',
      _clientMeta: { client },
    };
  }

  switch (client) {
    case 'cursor':
      return fromCursor(clientEvent, eventName);
    case 'antigravity':
      return fromAntigravity(clientEvent, eventName);
    default:
      // Unknown client: pass through with _clientMeta wrapper
      return { ...clientEvent, _clientMeta: { client } };
  }
}
