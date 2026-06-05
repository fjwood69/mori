/**
 * lib/fail-open.mjs — Fail-open wrapper for mori hook handlers (Node ESM)
 *
 * Export: runFailOpen(asyncHandler)
 *
 * Awaits the async handler. If it throws for any reason — parse error, network
 * error, logic error — writes nothing to stdout and exits 0. Hook scripts must
 * never block the agent; robustness beats diagnostics.
 *
 * Usage:
 *   import { runFailOpen } from './lib/fail-open.mjs';
 *   runFailOpen(async () => { ... your logic ... });
 */

/**
 * @param {() => Promise<void>} asyncHandler
 */
export function runFailOpen(asyncHandler) {
  asyncHandler().catch(() => {
    process.exit(0);
  });
}
