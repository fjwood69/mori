import * as http from "http";
import * as https from "https";
import * as url from "url";
import type { EventLogEntry } from "./types";

export interface ShipResult {
  ok: boolean;
  statusCode?: number;
  error?: string;
}

/**
 * POST an event entry to the Mori server.
 * Uses Node built-in http/https — no axios dependency.
 */
export function shipEvent(
  apiUrl: string,
  apiKey: string,
  entry: EventLogEntry,
): Promise<ShipResult> {
  return new Promise((resolve) => {
    const parsed = url.parse(apiUrl);
    const isHttps = parsed.protocol === "https:";
    const transport = isHttps ? https : http;

    const queryClient = entry.client ? `?client=${encodeURIComponent(entry.client)}` : "";

    const body = JSON.stringify(entry);
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body).toString(),
    };
    if (apiKey) {
      headers["X-Api-Key"] = apiKey;
    }

    const options: http.RequestOptions = {
      hostname: parsed.hostname,
      port: parsed.port ? parseInt(parsed.port, 10) : isHttps ? 443 : 80,
      path: `${parsed.pathname}${queryClient}`,
      method: "POST",
      headers,
      timeout: 10000,
    };

    const req = transport.request(options, (res) => {
      let data = "";
      res.on("data", (chunk: Buffer) => (data += chunk.toString()));
      res.on("end", () => {
        if (res.statusCode === 202) {
          resolve({ ok: true, statusCode: res.statusCode });
        } else {
          resolve({ ok: false, statusCode: res.statusCode, error: data });
        }
      });
    });

    req.on("error", (err) => {
      resolve({ ok: false, error: err.message });
    });

    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, error: "timeout" });
    });

    req.write(body);
    req.end();
  });
}