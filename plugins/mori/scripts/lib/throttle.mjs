/**
 * lib/throttle.mjs — Once-per-conversation flag using temp files (Node ESM)
 *
 * Export: firedOnce(key)
 *
 * Returns true the FIRST time it is called for a given key within the current
 * process lifetime (or across processes sharing the same $TMPDIR), then false
 * for all subsequent calls with the same key.
 *
 * Mechanism: writes a flag file at $TMPDIR/mori-conv-<sanitised-key>. The flag
 * file persists until $TMPDIR is cleared (typically on reboot or session end),
 * so a new conversation with the same conversationId on the same machine in the
 * same day will correctly see "already fired" — which is the desired behaviour
 * for once-per-conversation injection.
 *
 * Key sanitisation: replaces any char that is not [a-zA-Z0-9_-] with '_' so the
 * file name is safe on all OS / file-systems.
 *
 * On any I/O error, returns false (fail-open: skip injection rather than crash).
 */

import { existsSync, writeFileSync } from 'fs';

/**
 * @param {string} key   Typically a conversationId.
 * @returns {boolean}    true only the first time for this key.
 */
export function firedOnce(key) {
  if (!key) return false;
  const safe = String(key).replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 200);
  const flagPath = `${process.env.TMPDIR || '/tmp'}/mori-conv-${safe}`;
  try {
    if (existsSync(flagPath)) return false;
    writeFileSync(flagPath, '1', { flag: 'wx' }); // exclusive create; fails if exists
    return true;
  } catch {
    // Race condition (another process created it) → treat as already fired
    return false;
  }
}
