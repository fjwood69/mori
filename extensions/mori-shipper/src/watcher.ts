import * as path from "path";
import * as os from "os";
import * as fs from "fs";
import type { FSWatcher } from "chokidar";

function homeDir(): string {
  if (process.platform === "win32") {
    return process.env["USERPROFILE"] || os.homedir();
  }
  return os.homedir();
}

function clineStoragePath(): string {
  return path.join(
    homeDir(),
    ".vscode",
    "extensions",
    "saoudrizwan.claude-dev",
    "globalStorage",
    "api_conversation_history.json",
  );
}

function clineStoragePathOss(): string {
  return path.join(
    homeDir(),
    ".vscode-oss",
    "extensions",
    "saoudrizwan.claude-dev",
    "globalStorage",
    "api_conversation_history.json",
  );
}

function continueSessionsPath(): string {
  return path.join(homeDir(), ".continue", "sessions");
}

export interface WatchTarget {
  name: string;
  type: "file" | "directory";
  paths: string[]; // resolved paths — first existing one wins
  handler: (filePath: string) => Promise<void>;
}

export function resolveClineTarget(handler: (filePath: string) => Promise<void>): WatchTarget {
  const paths: string[] = [clineStoragePath(), clineStoragePathOss()];
  return { name: "cline", type: "file", paths, handler };
}

export function resolveContinueTarget(handler: (filePath: string) => Promise<void>): WatchTarget {
  return { name: "continue", type: "directory", paths: [continueSessionsPath()], handler };
}

export function resolveOpenCodeTarget(
  customPath: string,
  handler: (filePath: string) => Promise<void>,
): WatchTarget | null {
  const base = customPath || path.join(homeDir(), ".opencode", "sessions");
  return { name: "opencode", type: "directory", paths: [base], handler };
}

export function startWatcher(target: WatchTarget, chokidar: any): FSWatcher | null {
  // Find the first path that exists. If none exists yet, use the first one
  // (chokidar will pick it up when it's created).
  const watchPath = target.paths.find((p) => {
    try {
      return fs.existsSync(p);
    } catch {
      return false;
    }
  }) || target.paths[0];

  const dirPath = target.type === "file" ? path.dirname(watchPath) : watchPath;

  try {
    if (!fs.existsSync(dirPath)) {
      fs.mkdirSync(dirPath, { recursive: true });
    }
  } catch {
    // Cannot create directory — watcher will fail gracefully
  }

  try {
    const watcher = chokidar.watch(watchPath, {
      persistent: true,
      ignoreInitial: true,
      awaitWriteFinish: {
        stabilityThreshold: 500,
        pollInterval: 100,
      },
    });

    const debounceTimers = new Map<string, NodeJS.Timeout>();

    watcher.on("change", (filePath: string) => {
      const existing = debounceTimers.get(filePath);
      if (existing) clearTimeout(existing);
      debounceTimers.set(
        filePath,
        setTimeout(() => {
          debounceTimers.delete(filePath);
          target.handler(filePath).catch((err) =>
            console.error(`[mori-shipper] ${target.name} handler error:`, err),
          );
        }, 500),
      );
    });

    watcher.on("add", (filePath: string) => {
      // For directory watches, new files = new sessions
      if (target.type === "directory") {
        target.handler(filePath).catch((err) =>
          console.error(`[mori-shipper] ${target.name} add handler error:`, err),
        );
      }
    });

    watcher.on("error", (err: Error) => {
      console.error(`[mori-shipper] ${target.name} watcher error:`, err.message);
    });

    return watcher;
  } catch (err) {
    console.error(`[mori-shipper] Failed to start ${target.name} watcher:`, err);
    return null;
  }
}