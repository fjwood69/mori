import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import * as crypto from "crypto";
import type { EventLogEntry, SpoolEntry } from "./types";

const SPOOL_DIR = path.join(os.homedir(), ".mori-shipper", "queue");
const DEAD_DIR = path.join(os.homedir(), ".mori-shipper", "dead");
const MAX_RETRIES = 10;

const BACKOFFS = [10_000, 30_000, 60_000, 120_000, 300_000, 600_000];

function backoff(retries: number): number {
  if (retries >= BACKOFFS.length) return 600_000;
  return BACKOFFS[retries];
}

export class Spooler {
  private timers = new Map<string, NodeJS.Timeout>();
  private onRetry: (entry: EventLogEntry) => Promise<boolean>;

  constructor(onRetry: (entry: EventLogEntry) => Promise<boolean>) {
    this.onRetry = onRetry;
    this.ensureDirs();
  }

  private ensureDirs(): void {
    fs.mkdirSync(SPOOL_DIR, { recursive: true });
    fs.mkdirSync(DEAD_DIR, { recursive: true });
  }

  spoolDir(): string {
    return SPOOL_DIR;
  }

  deadDir(): string {
    return DEAD_DIR;
  }

  /** Write a failed event to the spool queue and schedule retry. */
  enqueue(entry: EventLogEntry, error: string): void {
    const id = crypto.randomUUID();
    const spool: SpoolEntry = {
      id,
      event: entry,
      retries: 0,
      firstFailedAt: new Date().toISOString(),
      lastRetryAt: new Date().toISOString(),
      lastError: error,
    };
    fs.writeFileSync(path.join(SPOOL_DIR, `${id}.json`), JSON.stringify(spool, null, 2), "utf-8");
    this.scheduleRetry(id, 0);
  }

  /** Replay all pending spools (called on extension activate). */
  replayAll(): void {
    let files: string[];
    try {
      files = fs.readdirSync(SPOOL_DIR);
    } catch {
      return;
    }

    for (const file of files) {
      if (!file.endsWith(".json")) continue;
      const filePath = path.join(SPOOL_DIR, file);
      try {
        const raw = fs.readFileSync(filePath, "utf-8");
        const spool: SpoolEntry = JSON.parse(raw);
        const id = spool.id || file.replace(".json", "");
        this.scheduleRetry(id, spool.retries);
      } catch {
        // Corrupted spool file — move to dead
        try {
          fs.renameSync(filePath, path.join(DEAD_DIR, file));
        } catch {
          // best effort
        }
      }
    }
  }

  /** Flush all pending retries synchronously (called on deactivate). */
  flushSync(): void {
    // Clear all pending timers and attempt one final retry per spool
    for (const [id, timer] of this.timers) {
      clearTimeout(timer);
      this.timers.delete(id);
    }

    let files: string[];
    try {
      files = fs.readdirSync(SPOOL_DIR);
    } catch {
      return;
    }

    for (const file of files) {
      if (!file.endsWith(".json")) continue;
      const filePath = path.join(SPOOL_DIR, file);
      try {
        const raw = fs.readFileSync(filePath, "utf-8");
        const spool: SpoolEntry = JSON.parse(raw);
        // Synchronous retry — fire and forget
        this.onRetry(spool.event).catch(() => {
          // Moved to dead by retry handler
        });
      } catch {
        // skip
      }
    }
  }

  private scheduleRetry(id: string, currentRetries: number): void {
    if (currentRetries >= MAX_RETRIES) {
      this.moveToDead(id);
      return;
    }

    const delay = backoff(currentRetries);
    const timer = setTimeout(async () => {
      this.timers.delete(id);
      await this.retryOne(id, currentRetries);
    }, delay);
    this.timers.set(id, timer);
  }

  private async retryOne(id: string, currentRetries: number): Promise<void> {
    const filePath = path.join(SPOOL_DIR, `${id}.json`);
    let spool: SpoolEntry;
    try {
      const raw = fs.readFileSync(filePath, "utf-8");
      spool = JSON.parse(raw);
    } catch {
      return; // spool file gone — nothing to retry
    }

    spool.retries = currentRetries + 1;
    spool.lastRetryAt = new Date().toISOString();

    try {
      const ok = await this.onRetry(spool.event);
      if (ok) {
        // Success — remove spool
        fs.unlinkSync(filePath);
        return;
      }
    } catch (err) {
      spool.lastError = String(err);
    }

    // Failed again — write updated spool and schedule next retry
    fs.writeFileSync(filePath, JSON.stringify(spool, null, 2), "utf-8");
    this.scheduleRetry(id, spool.retries);
  }

  private moveToDead(id: string): void {
    const src = path.join(SPOOL_DIR, `${id}.json`);
    const dst = path.join(DEAD_DIR, `${id}.json`);
    try {
      fs.renameSync(src, dst);
    } catch {
      // best effort
    }
  }
}