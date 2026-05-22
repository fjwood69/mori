import * as vscode from "vscode";
import * as chokidar from "chokidar";
import * as fs from "fs";
import { loadConfig } from "./config";
import { DiffEngine } from "./diff";
import {
  translateClineMessage,
  translateContinueSession,
} from "./translator";
import { shipEvent } from "./shipper";
import { Spooler } from "./spooler";
import {
  startWatcher,
  resolveClineTarget,
  resolveContinueTarget,
  resolveOpenCodeTarget,
} from "./watcher";

let watchers: ReturnType<typeof startWatcher>[] = [];
let spooler: Spooler | null = null;
let diffEngine: DiffEngine | null = null;

export function activate(context: vscode.ExtensionContext): void {
  console.log("[mori-shipper] Activating...");

  diffEngine = new DiffEngine(context.globalState);

  // Initialise spooler — retry handler calls shipEvent
  spooler = new Spooler(async (entry) => {
    const cfg = loadConfig();
    const result = await shipEvent(cfg.apiUrl, cfg.apiKey, entry);
    return result.ok;
  });

  // Replay any pending spooled events from a previous session
  spooler.replayAll();

  // Start watchers based on current config
  startAllWatchers(context);

  // Watch for config changes — restart watchers if settings change
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("moriShipper")) {
        console.log("[mori-shipper] Config changed — restarting watchers");
        stopAllWatchers();
        startAllWatchers(context);
      }
    }),
  );

  console.log("[mori-shipper] Activated");
}

export function deactivate(): void {
  console.log("[mori-shipper] Deactivating...");
  stopAllWatchers();
  spooler?.flushSync();
  console.log("[mori-shipper] Deactivated");
}

// ── Helpers ──────────────────────────────────────────────────

function stopAllWatchers(): void {
  for (const w of watchers) {
    if (w) w.close();
  }
  watchers = [];
}

function startAllWatchers(context: vscode.ExtensionContext): void {
  const cfg = loadConfig();
  const de = diffEngine!;
  const sp = spooler!;

  if (cfg.enableCline) {
    const target = resolveClineTarget(async (filePath) => {
      const messages = de.findNewClineMessages(filePath);
      for (const msg of messages) {
        const entries = translateClineMessage(msg, cfg.clientName);
        for (const entry of entries) {
          const result = await shipEvent(cfg.apiUrl, cfg.apiKey, entry);
          if (!result.ok) {
            sp.enqueue(entry, result.error || `HTTP ${result.statusCode}`);
          }
        }
      }
    });
    const w = startWatcher(target, chokidar);
    if (w) {
      watchers.push(w);
      console.log("[mori-shipper] Cline watcher started");
    }
  }

  if (cfg.enableContinue) {
    const target = resolveContinueTarget(async (filePath) => {
      const session = de.findNewContinueSession(filePath);
      if (!session) return;
      const entries = translateContinueSession(session, cfg.clientName);
      for (const entry of entries) {
        const result = await shipEvent(cfg.apiUrl, cfg.apiKey, entry);
        if (!result.ok) {
          sp.enqueue(entry, result.error || `HTTP ${result.statusCode}`);
        }
      }
    });
    const w = startWatcher(target, chokidar);
    if (w) {
      watchers.push(w);
      console.log("[mori-shipper] Continue watcher started");
    }
  }

  if (cfg.enableOpenCode) {
    const target = resolveOpenCodeTarget(cfg.openCodePath, async (filePath) => {
      const session = de.findNewContinueSession(filePath);
      if (!session) return;
      const entries = translateContinueSession(session, cfg.clientName);
      for (const entry of entries) {
        const result = await shipEvent(cfg.apiUrl, cfg.apiKey, entry);
        if (!result.ok) {
          sp.enqueue(entry, result.error || `HTTP ${result.statusCode}`);
        }
      }
    });
    if (target) {
      const w = startWatcher(target, chokidar);
      if (w) {
        watchers.push(w);
        console.log("[mori-shipper] OpenCode watcher started");
      }
    }
  }
}