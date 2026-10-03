/**
 * lib/enrich.mjs — Stop-event enrichment for the Claude Code shipper (Node ESM)
 *
 * Kept out of mori-ship-event.mjs so tests can import it without running the hook.
 */

import { existsSync, readFileSync } from 'fs';

// If hook_event_name === "Stop" and transcript_path is readable, add the last 64 KB as
// transcript_tail_b64. Any failure → null (the caller ships the original body unchanged).

export function enrichStopEvent(parsed, mode) {
  if (mode !== 'raw') return null;
  if ((parsed.hook_event_name || '') !== 'Stop') return null;
  const tpath = parsed.transcript_path;
  if (!tpath || typeof tpath !== 'string') return null;
  try {
    if (!existsSync(tpath)) return null;
    const buf = readFileSync(tpath);
    const tail = buf.length > 65536 ? buf.slice(buf.length - 65536) : buf;
    return { ...parsed, transcript_tail_b64: tail.toString('base64') };
  } catch {
    return null;
  }
}
