/**
 * lib/setup-message.mjs — Shared onboarding messages for the Mori plugin (Node ESM)
 *
 * Exported constants used by the context hooks when the Mori server is not
 * reachable. One source of truth so all three hook variants (Claude, Cursor,
 * Antigravity) surface the same text.
 *
 * SETUP_MESSAGE     — server is configured but not responding
 * UNCONFIGURED_MESSAGE — no server URL has been set at all
 */

/**
 * Emitted when the server URL is set but the server is not reachable.
 * Honest about what is needed (Docker host + LLM provider key).
 * Does NOT promise a hosted SaaS — none exists.
 */
export const SETUP_MESSAGE =
  'Mori keeps your memory on your own server — and it isn\'t reachable yet. ' +
  'Standing one up takes a Docker host and an LLM provider key (Novita, DeepInfra, OpenAI, …): ' +
  'clone the repo, add your provider key to `.env`, then `docker compose up -d`. ' +
  'Full quickstart: https://github.com/fjwood69/mori#quickstart . ' +
  'Already have a server running? Check that MORI_SERVER_URL points at it (and MORI_API_KEY is set), then reload.';

/**
 * Emitted when no server URL has been configured at all.
 * Same guidance, but leads with the missing configuration fact.
 */
export const UNCONFIGURED_MESSAGE =
  'No Mori server is configured (MORI_SERVER_URL is not set). ' +
  'Mori keeps your memory on your own server — standing one up takes a Docker host and an LLM provider key ' +
  '(Novita, DeepInfra, OpenAI, …): ' +
  'clone the repo, add your provider key to `.env`, then `docker compose up -d`. ' +
  'Full quickstart: https://github.com/fjwood69/mori#quickstart . ' +
  'Then point the plugin at it by exporting MORI_SERVER_URL (e.g. http://localhost:8968) ' +
  'and MORI_API_KEY (name:secret), and reload.';
