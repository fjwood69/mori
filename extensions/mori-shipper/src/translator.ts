import type { EventLogEntry, ClineMessage, ClineContentBlock, ContinueSession } from "./types";

/**
 * Translate a Cline message into zero or more Mori event entries.
 */
export function translateClineMessage(
  msg: ClineMessage,
  clientName: string,
): EventLogEntry[] {
  const entries: EventLogEntry[] = [];
  const sessionId = msg.conversationId || `cline:${msg.ts || Date.now()}`;

  if (msg.role === "user") {
    const prompt = extractTextContent(msg.content);
    if (prompt) {
      entries.push({
        event_name: "UserPromptSubmit",
        session_id: sessionId,
        client: clientName,
        prompt,
      });
    }
  } else if (msg.role === "assistant") {
    const blocks = Array.isArray(msg.content) ? msg.content : [];
    for (const block of blocks) {
      if (block.type === "tool_use" && block.name) {
        entries.push({
          event_name: "PostToolUse",
          session_id: sessionId,
          client: clientName,
          tool_name: block.name,
          tool_input: block.input ? JSON.stringify(block.input) : undefined,
        });
      }
    }
  }

  return entries;
}

/**
 * Translate a Continue session into Mori event entries.
 */
export function translateContinueSession(
  session: ContinueSession,
  clientName: string,
): EventLogEntry[] {
  const entries: EventLogEntry[] = [];
  const sessionId = session.sessionId || `continue:${Date.now()}`;

  if (!session.messages) return entries;

  for (const msg of session.messages) {
    if (msg.role === "user" && msg.content) {
      entries.push({
        event_name: "UserPromptSubmit",
        session_id: sessionId,
        client: clientName,
        prompt: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content),
      });
    }

    if (msg.role === "assistant" && msg.toolCalls) {
      for (const tc of msg.toolCalls) {
        if (tc.name) {
          entries.push({
            event_name: "PostToolUse",
            session_id: sessionId,
            client: clientName,
            tool_name: tc.name,
            tool_input: tc.arguments ? JSON.stringify(tc.arguments) : undefined,
          });
        }
      }
    }
  }

  return entries;
}

function extractTextContent(
  content: string | ClineContentBlock[],
): string {
  if (typeof content === "string") return content;
  return content
    .filter((b): b is ClineContentBlock & { text: string } => b.type === "text" && typeof b.text === "string")
    .map((b) => b.text)
    .join("\n");
}