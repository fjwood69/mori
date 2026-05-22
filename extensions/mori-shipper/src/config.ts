import * as os from "os";
import { workspace } from "vscode";
import type { ShipperConfig } from "./types";

export function loadConfig(): ShipperConfig {
  const cfg = workspace.getConfiguration("moriShipper");
  return {
    apiUrl: cfg.get<string>("apiUrl", "http://localhost:8968/api/events/raw"),
    apiKey: cfg.get<string>("apiKey", ""),
    clientName: cfg.get<string>("clientName", "") || os.hostname(),
    enableCline: cfg.get<boolean>("enableCline", true),
    enableContinue: cfg.get<boolean>("enableContinue", true),
    enableOpenCode: cfg.get<boolean>("enableOpenCode", false),
    openCodePath: cfg.get<string>("openCodePath", ""),
  };
}