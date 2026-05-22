import * as crypto from "crypto";
import * as fs from "fs";
import type { Memento } from "vscode";
import type { ClineMessage, ContinueSession } from "./types";

const GLOBAL_STATE_KEY = "moriShipper:lastKnownIndex";

export class DiffEngine {
  private fileHashes = new Map<string, string>();
  private globalState: Memento | null = null;

  constructor(globalState?: Memento) {
    if (globalState) {
      this.globalState = globalState;
    }
  }

  setGlobalState(gs: Memento): void {
    this.globalState = gs;
  }

  /** Compute SHA256 of file content. Returns hex digest. */
  private hashContent(content: string): string {
    return crypto.createHash("sha256").update(content).digest("hex");
  }

  /**
   * Check if a file has changed since last seen.
   * Returns true if content is new/changed, false if identical.
   */
  hasChanged(filePath: string, content: string): boolean {
    const hash = this.hashContent(content);
    const prev = this.fileHashes.get(filePath);
    if (prev === hash) return false;
    this.fileHashes.set(filePath, hash);
    return prev !== undefined; // skip on first read
  }

  /**
   * For Cline's append-only array: find new elements since last known index.
   * Returns the new messages and updates the stored index.
   */
  findNewClineMessages(filePath: string): ClineMessage[] {
    let raw: string;
    try {
      raw = fs.readFileSync(filePath, "utf-8");
    } catch {
      return [];
    }

    if (!this.hasChanged(filePath, raw)) return [];

    let messages: ClineMessage[];
    try {
      messages = JSON.parse(raw);
    } catch {
      return [];
    }

    if (!Array.isArray(messages) || messages.length === 0) return [];

    const stateKey = `${GLOBAL_STATE_KEY}:${filePath}`;
    const lastIndex = this.globalState?.get<number>(stateKey, -1) ?? -1;

    if (lastIndex >= messages.length) {
      // File was truncated or reset — start fresh
      return [];
    }

    const newMessages = messages.slice(lastIndex + 1);
    if (newMessages.length > 0) {
      this.globalState?.update(stateKey, messages.length - 1);
    }

    return newMessages;
  }

  /**
   * For Continue's per-session files: track by filename.
   * Returns the parsed session if the file is new/changed.
   */
  findNewContinueSession(filePath: string): ContinueSession | null {
    let raw: string;
    try {
      raw = fs.readFileSync(filePath, "utf-8");
    } catch {
      return null;
    }

    if (!this.hasChanged(filePath, raw)) return null;

    try {
      const session: ContinueSession = JSON.parse(raw);
      return session;
    } catch {
      return null;
    }
  }
}