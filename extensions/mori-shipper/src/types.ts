export interface EventLogEntry {
  event_name: string;
  session_id: string;
  client: string;
  tool_name?: string;
  tool_input?: string;
  tool_response?: string;
  tool_error?: string;
  model?: string;
  cwd?: string;
  transcript_path?: string;
  prompt?: string;
  stop_reason?: string;
}

export interface SpoolEntry {
  id: string;
  event: EventLogEntry;
  retries: number;
  firstFailedAt: string;
  lastRetryAt: string;
  lastError: string;
}

export interface ShipperConfig {
  apiUrl: string;
  apiKey: string;
  clientName: string;
  enableCline: boolean;
  enableContinue: boolean;
  enableOpenCode: boolean;
  openCodePath: string;
}

export interface ClineMessage {
  role: "user" | "assistant";
  content: string | ClineContentBlock[];
  ts?: number;
  conversationId?: string;
}

export interface ClineContentBlock {
  type: string;
  name?: string;
  input?: Record<string, unknown>;
  text?: string;
}

export interface ContinueSession {
  sessionId: string;
  title?: string;
  messages?: ContinueMessage[];
}

export interface ContinueMessage {
  role: "user" | "assistant";
  content?: string;
  toolCalls?: ContinueToolCall[];
}

export interface ContinueToolCall {
  name?: string;
  arguments?: Record<string, unknown>;
}