# Changelog

## v2.2.26 — serving robustness: off-loop LLM calls + infra housekeeping

**fix(serving): the dream/MCP "pear-shaped" bug — a synchronous LLM call froze the whole server.**
`bifrost.consult()` is the *synchronous* OpenAI client; `consult_advisor` and the dream's
contradiction scan called it directly inside async handlers, freezing the single-worker uvicorn
event loop for the entire 30–90s generation. mori-advisor went unavailable to *every* session (one
shared loop) and the MCP connection dropped — reproducible by triggering an in-server `dream_run`
+ a `/consult` concurrently.

- **`consult_advisor`** now runs the blocking call on a **dedicated `llm_executor`** (lifespan-managed)
  + an `asyncio.Semaphore(6)` (backpressure) + a `wait_for` timeout backstop. The **default executor
  is left untouched** so short `to_thread` tasks aren't starved by long LLM calls — a global default-pool
  bump would only trade loop-freeze for thread-pool exhaustion (caught in `/consult` review).
- **`run_contradiction_scan`** offloads its `consult_fn` calls via `asyncio.to_thread`.
- **`smoke_test`** wraps the blocking `urllib.urlopen()` ingestion probe in `to_thread`.
- The dream's main distill was already offloaded; this closes the remaining serving-loop blockers.
- **A/B validated on a real UAT server:** broken — a deep `/consult` froze `/health` for 85s (15s
  timeouts ×5); fixed — `/consult` *during* a `/dream` held `/health` at ≤9 ms across 200+ samples,
  both completed. Regression test `test_consult_nonblocking`.

**fix(infra): wire the orphan-scan lifecycle (built-but-not-running) + close write-audit gaps.**
- **Orphan scan** — `_orphan_scan_loop()` added to the lifespan: `scan_orphans(days=30, dry_run=True)`
  on a cadence (`MORI_ORPHAN_SCAN_INTERVAL_SEC`, default daily; `MORI_ORPHAN_SCAN_DRY_RUN=false` to
  enable eviction-queue writes). The documented working-tier lifecycle now actually runs.
- **Postgres bugfix** — `scan_orphans` had `INTERVAL '$1 days'` with the `$1` *inside* a string literal
  (zero params reached the server); replaced with a Python-computed `now() - timedelta(days=days)` cutoff.
  The scan never functioned on Postgres before.
- **Write audit** — the `memory_write` MCP tool and `import_standards` wrote to canon with no
  `_write_audit`; both now emit an audit record (MCP write tagged with the calling actor).

Validation: full suite **596 passed**; ruff clean; UAT green both backends (combined lifespan boots
clean, the loop-fix A/B holds, `scan_orphans` lists stale rows). Infra housekeeping co-authored with
Claude Sonnet 4.6.

## v2.2.25 — completeness gate wired at the `store.write` chokepoint (audit-mode)

**The candid bit first:** mori shipped a completeness/anatomy check (`validate_anatomy`, built + 10
green tests on 2026-06-12) that **never had a call site.** Call-graph analysis confirmed it: 0 callers, 0 of 191
execution flows. So canon had **no single admission chokepoint** — the governed intake lane enforced
GOV-002, but the dreamer's own `_write_memory` and the direct MCP write both reach `store.write` with
`_skip_protection=True` and bypassed anatomy entirely. The gate that was supposed to be the product had
a hole in it. We're fixing it in the open rather than quietly, because "the gate is the moat" only
survives a hole in your own gate if you log it loudly.

**feat: one anatomy check at the one door every writer passes through — in AUDIT mode.**

- **`completeness.audit_completeness(body, description, *, seam, name, log)`** — thin AUDIT-mode wrapper
  over `validate_anatomy`. Emits a structured `COMPLETENESS-AUDIT seam=… name=… reason=… severity=…`
  WARNING for non-conforming writes and **never blocks.** Field mapping mirrors the dreamer's call
  (`description` carries the warrant); `memory_type` is deliberately not derived from the store `type`
  taxonomy, so only the universal rules (empty-body / empty-warrant) fire — degrades safely until the
  dreamer self-tags directives.
- **Wired at both chokepoints** — `MemoryStore.write` (SQLite, the seam `SQLiteStore` delegates to) and
  `PostgresStore.write` (the live prod path). One seam per backend; the next new writer inherits the
  check for free instead of re-baking per-caller drift.
- **Quantified the existing exposure** (`scripts/audit_canon_anatomy.py`, read-only): on live canon,
  **151/2772 (5.4%) would fail anatomy** if the gate ever enforced — *all* `empty-warrant`, 150 of them
  in the `working` tier; the `canonical` tier is essentially clean (1/470). The failures are dominated by
  machine-generated `consult-*`/`commit-*` rows. This is *why* audit-first, not enforce-now: the measure
  comes before the lever.
- **The honest good news inside the bug:** the leak was real but it **pooled in the low-stakes layer.**
  150 of the 151 warrantless writes are *working*-tier machine churn; the *canonical* tier — the memories a
  human actually promoted, the ones that compound — is **1/470 clean.** The promoted canon was effectively
  gated by the human promotion step all along; the un-wired chokepoint only let working-tier churn through,
  the tier where it matters least. So "the gate is the product" survives intact: this is a footnote (an
  unreviewed trusted-writer path in the churn layer), not a crisis (load-bearing canon entering unchecked).
- **Contract test** — `test_sqlite_write_invokes_audit_and_does_not_block` asserts `store.write` invokes
  the anatomy check (the chokepoint contract that stops the next writer drifting) **and** that a
  warrantless write still succeeds (audit ≠ block). Plus direct `audit_completeness` log/silence tests.
- **UAT green both backends** — deployment contract PASS on 8970+8972; `dream_run` PG write path wrote 4
  memories; `COMPLETENESS-AUDIT` lines fired on the warrantless deployment probes on *both* seams while
  those writes still committed. Audit-mode contract proven on a real server boot.
- **Not touched:** `MORI_INTAKE_PROMOTION_ENABLED` (correctly OFF — it governs agent-intake auto-promotion,
  an unrelated axis; flipping it would *reduce* governance). The completeness fix is a call site, not a flag.

**Next (deliberately a separate change):** flip a configurable subset (start: `empty-body` →
withhold) from audit to enforce, once the audit window confirms the 5.4% doesn't hide a false-positive class.

## v2.2.24 — H2 scope router (`filter_by_scope`) + NATS replay default

**feat: the generic provenance scope filter goes live on `/brief`.** H2 replaces the special-cased
`get_memories_by_project` routing with a flat, deterministic per-memory scope filter, proven
byte-identical to the legacy oracle for all existing rows — so the cutover is provably subtractive.

- **Migration 15 (`memory_scope_map`)** — nullable `scope` column (JSONB+GIN on Postgres, JSON-text on
  SQLite), additive, no backfill. Absent ⇒ effective scope derived from legacy tags (zero behaviour change).
- **`mori_advisor/scope.py`** — `ScopeMap{tags,match}` + flat `in_scope()` set-membership (no graph, no
  model). **`mori_advisor/resolver.py`** — compiles legacy tags/type into an effective scope; the whole
  `MORI_BRIEF_SCOPE` safe/all flag collapses to the presence of one context tag (`legacy:type-global`).
- **`filter_by_scope`** (both backends) — generic membership, legacy partition/ordering preserved.
  **`brief()` cutover** to it (both call sites). `get_memories_by_project` retained as the parity oracle.
- **Determinism** — a `, id DESC` tiebreaker on both routing ORDER BYs makes brief ordering total and
  parity exact even on tied `(tier, updated_at)` keys.
- **Parity gate** — `test_parity_manifest` asserts byte-identical output across the full routing-dimension
  product, on SQLite and real Postgres. **UAT green both backends.**

**fix(nats): `nats_sub` defaults to `replay=True`.** Non-replay live-tail silently misses messages
published between polls; replay (7-day window) is reliable for coordination. Hardened the docstring.

## v2.2.23 — pre-dream events normaliser

**feat: strip provably-inert scaffolding from the dream prompt before the LLM sees it.**
`_format_events()` emits one `  Tool: <name>` line per `PostToolUse` event and one `  Stopped: <reason>`
line per `Stop` event — these are structural labels, not reasoning signal. At scale (a real session
with hundreds of tool calls) they inflate the prompt materially without adding anything the dreamer
can extract. The normaliser removes them; everything else is preserved verbatim.

- **`normalise_events_text(text)`** in `mori_advisor/dream.py` — deterministic, lossless-on-signal,
  line-anchored filter. The regex (`^  (?:Tool|Stopped): `) is case-sensitive and anchored at the
  line start, so prose that *mentions* `"Tool: Read"` inside an `  Assistant:` line is never stripped.
  Idempotent: `normalise(normalise(x)) == normalise(x)`.
- **`_run_inner()` wire-up** — `events_text = normalise_events_text(self._format_events(events))`.
  The normaliser sits at the capture=recall boundary: volume lever is compression, not censorship.
  `FAILURE (Tool):` lines, `  CWD:`, `  Prompt:`, `  Assistant:` prose, and `Session:` headers
  are all preserved.
- **8 unit tests** (`tests/test_transcript_normaliser.py`) — strip, keep, no-signal,
  idempotency, token-reduction, mixed-interleave, FAILURE preservation, prose-Tool-reference safety.
- **UAT probe** — seeds 4 synthetic sessions
  above `max_real_id`, sets the dream watermark, calls `dream_run` on the Postgres UAT backend,
  verifies `memories` count present, no errors, dream model called, and cleans up on exit.
  5/5 checks pass: 25 `Tool:` lines and 4 `Stopped:` lines stripped per probe run.

## v2.2.22 — provenance scope routing for `/brief` (`MORI_BRIEF_SCOPE`)

**feat: scope-aware retrieval so one project's origin-bound canon can't leak into another
project's brief.** Shared memory across a team's repos means every memory is "valid *where*?"
Without provenance, an out-of-scope memory surfaced in a brief leads the agent to chase APIs that
don't exist in the current repo (a retrieval-interference failure we reproduced across multiple
models). `brief()` gains a `scope` argument (env `MORI_BRIEF_SCOPE`, default `"safe"`):

- **`safe`** (default): cross-project body exposure is provenance-routed. A memory reaches the
  global lane only via an explicit `scope:global` / `scope:cross-project` tag — `type=profile`/
  `pattern` alone no longer auto-globalizes. Out-of-project memories are **zero-knowledge** in the
  passive brief (not even an index): an index teaser of an out-of-scope rule induces the agent to
  hallucinate the missing payload, so the brief withholds it and points to explicit search instead.
  An unscoped brief surfaces the global lane only.
- **`all`**: legacy behaviour (auto-global by type, other-project index shown, unscoped lists
  everything) — opt-in via `scope="all"` or `MORI_BRIEF_SCOPE=all` for solo / single-repo use.

`strict_global` is threaded through all store backends (Postgres, SQLite, in-memory); the store
default is unchanged, so only the brief path opts in (no behaviour change for direct store callers).
Adds `tests/test_provenance_scope.py` plus brief-scope coverage in `tests/test_mcp_tools.py`.

## v2.2.21 — post-compaction re-ground uses SessionStart, not PostCompact

**fix: the legacy Claude Code installers wired post-compaction re-grounding to a `PostCompact`
hook, which Claude Code rejects.** `PostCompact` is observability-only and cannot inject context
the model sees — a `hookSpecificOutput` with `hookEventName: "PostCompact"` fails the harness's
JSON validation, and only a user-facing `systemMessage` survived, so the agent was never actually
nudged to run `/brief --post-compact`. Now wired to the sanctioned `SessionStart` hook with a
`compact` matcher, matching the plugin's `mori-context-hook.mjs` (the plugin path was already
correct — no change there).

- **`scripts/mori-post-compact-brief.{sh,ps1}`**: read the hook payload on stdin and emit a
  `SessionStart` `additionalContext` nudge only when `source == "compact"` — silent on ordinary
  startup/resume/clear, even if wired without a matcher.
- **`scripts/legacy/install-mori-claude.{sh,ps1}`**: wire the brief script to `SessionStart`
  (`matcher: "compact"`), strip the legacy `PostCompact` mori hook on upgrade (dropping the key if
  nothing non-mori remains), and preserve all non-mori hooks. The PowerShell installer also had a
  pre-existing parse error — a `Write-Warning` ahead of `param()` made it un-runnable on Windows —
  now corrected.
- **Docs/example** (`examples/settings.json`, `docs/reference/configuration.md`,
  `docs/reference/slash-commands.md`, `skills/brief/SKILL.md`) updated to describe the
  `SessionStart[source=compact]` mechanism.

Disable with `MORI_POST_COMPACT_BRIEF=false`. Also bumps `pyproject.toml` to `2.2.21` (it had
lagged at `2.2.19` through the `v2.2.20` release).

## v2.2.20 — `/export`: structured canon export for external review

**feat: bundle the canon into one structured Markdown document for external-LLM review,
audit, or dashboard download.** Distinct from `memory_export` (single memory) and
`memory_export_all` (per-file backup): a grouped, reproducible doc you can paste into an
external model (alongside `/consult`) or download from the dashboard. Additive — no
breaking changes.

- **`export_canon` MCP tool + `GET /api/export`** (read-role) + the `/export` skill. Formats:
  `standard` (grouped, with a reproducibility pin header — instance/version/canon-size),
  `consult` (the standard body prefixed by a coherence-review rubric), `json`.
- **`--format consult` scores COHERENCE, not truth.** A reviewer can't see the codebase, so
  the rubric judges only internal consistency — contradictions, redundancy, clarity — and
  emits TD-disposal suggestions (merge/archive/clarify/split), never "is this still true".
  A unit test fails if a truth-scoring verb ever leaks back into the rubric.
- **PII-safe by default.** Output is an allowlist (name/title/body/type/tier/tags/timestamps);
  internal provenance (`origin_session_ids`, `origin_clients`, …) is stripped unless
  `include_provenance=True`.
- New store method `export_rows()` (both backends); hybrid grouper (type → tag → Uncategorized),
  most-retrieved first. New route registered in `verify-deployment.py` (one place, both gates).

## v2.2.19 — measurement layer (passive instruments from real usage)

**feat: instrument the value of curation from production use, not one-off synthetic
benchmarks.** Per the 2026-06 architecture review + a deep `/consult` pass — passive
instruments that emit as a byproduct of normal use. Honest framing: they flag *when* to
re-benchmark and *which canon is dead*; they do **not** prove the cost-thesis by themselves.

- **c1 (fix):** Postgres bumped `retrieval_count` only in `read()` — `list`/`search`/`brief`
  (the real recall paths) didn't, so production measured ~zero memory usage. Now bumped
  (deferred/batched, after the result returns — write-amplification guard). Prerequisite for
  the usage instrument, and a latent bug.
- **a — ingest-shape:** per `/ingest`, `candidates_total`, `convention_ratio` (granularity/
  near-dup signal via `clustering.cluster_keys`), `anchorable_pct` (smoke signal) →
  `ingestion_log` columns + last-ingest gauges.
- **c2 — canon mortality:** `mori_canon_mortality_rate_90d` — the cohort % of canonical
  memories created >90d ago never retrieved (tests the compounding thesis over time) + a
  serving composite index so the gauge never scans.
- **b — TD reason-code:** optional `reason` on approve/reject (taxonomy `too-granular|
  duplicate|stale|low-value|other`, aligned to intake), non-breaking; persisted as
  `write_audit.reason_code` → `mori_td_reason_total{reason}` (fixed labels) +
  `mori_td_reason_coverage`. Also closes a gap: MCP approve/reject now write to the audit too.
- **d — net canon growth:** `mori_net_canon_growth_7d` (approvals − rejections − deletions) —
  the over-production signal.
- Migrations 12–14 (additive, both backends); rides the existing `/metrics` scrape — no new
  endpoints, no backfill. Sunset clause: prune ingest-shape + TD-reason if they don't
  correlate with the next benchmark within 90 days.

## v2.2.18 — TD review roll-up (near-duplicate clustering)

**feat: group near-duplicate review candidates so the Trusted-Dreamer disposes of a
convention once, not N times.**
- New `mori_advisor/clustering.py` — deterministic, embedding-free clustering. Candidates
  are grouped by the longest trailing hyphen-suffix they share (≥2 segments, ≥10 chars), so
  `lineup4-game-state-contract` and `greedy-pig-game-state-contract` roll up under
  `game-state-contract`, while distinct `*-contract` conventions stay apart. This is the
  *near*-dup layer; exact-dup is already handled upstream (intake `content_hash` UNIQUE).
- Review-side presentation only: never changes what the dreamer emits (recall intact) and
  never auto-merges (the TD disposes). Embeddings deferred until the lexical floor proves
  too coarse.
- Wired into both review surfaces, additive:
  - `GET /api/pending/json` → adds `clusters` (grouped by memory `name`); `items` unchanged.
  - `GET /intake/candidates` → adds a `stable_key` join (the convention key lives on the
    submission) + `clusters`; response is now `{status, count, candidates, clusters}`.
  - `clusters` lists only multi-member roll-ups (member ids, not duplicated payloads).
- Validated in UAT against real Postgres: a seeded `learned-game-state-contract` +
  `fact-game-state-contract` rolled up under `game-state-contract` while an unrelated key
  stayed separate; no false clusters among the 24 seeded pending writes.

## v2.2.17 — externalised distillation prompts + dreamer/archivist rewrite

**feat: distillation prompts are now editable text files, tuned without a code change.**
- New `mori_advisor/prompt_loader.py` + `mori_advisor/prompts/{dreamer,archivist}.txt`. The
  dreamer and archivist system prompts load from these files at startup; `MORI_PROMPTS_DIR`
  overrides the location (bind-mount + restart in containers). A missing/empty file falls
  back to a compact in-code prompt (logged) so it can never hard-fail. Packaged via
  `[tool.setuptools.package-data]` so wheels ship them too.

**feat: dreamer/archivist prompt rewrite (unit = convention, not occurrence).**
- Both prompts now direct **one memory per convention, listing locations** — not one per
  occurrence — to address measured over-production (ingest emitted ~one candidate per file).
- Dreamer: **`action` field removed** (the dreamer can't see canon, so CREATE/MERGE/DELETE
  was a guess the write path ignored anyway — it only tagged with it). Added `evidence[]`
  (display-only), reason-first field order, an explicit "empty array is valid" clause
  reconciled with the recall rule, and worked examples (single-site gotcha + multi-site
  convention). `mem.get("action", "CREATE")` already defaulted, so no consumer change.

**fix: ingest prompt assembly buried the output contract.**
- `_distill_batch` appended `focus`/`tier`/`tags` *after* the prompt's format instruction,
  so the assembled prompt ended on "Add these tags to every memory: …" — a weak recency
  position that the format anchor should hold. The output contract is now appended **last**
  (system tail) and at the **bottom of the user payload** (the true recency-most position).
- Regression-tested: `tests/test_prompt_loader.py` asserts the assembled prompt ends on the
  contract, plus loader override/fallback and the dropped-`action` / `UNIT OF OUTPUT` invariants.

## plugin v0.2.0 — config via env vars (MORI_SERVER_URL / MORI_API_KEY), not userConfig

**fix(plugin): the Claude Code plugin never connected on `claude plugin install` (CLI).**
- `userConfig` (`${user_config.*}`) only prompts in the interactive TUI — Claude Code's
  CLI `claude plugin install` does **not** fire the prompt (acknowledged CC bug
  [#39455](https://github.com/anthropics/claude-code/issues/39455) open /
  [#39827](https://github.com/anthropics/claude-code/issues/39827) closed-not-planned).
  So CLI users — the majority — installed mori with no configuration step and the MCP
  client connected nowhere (or to the old localhost default) → "Restart to enable →
  nothing".
- Fix: **dropped `userConfig` entirely; the plugin now reads its server URL and key from
  environment variables** — `${MORI_SERVER_URL}` and `${MORI_API_KEY}` in `.mcp.json`, and
  `process.env.MORI_SERVER_URL` / `MORI_API_KEY` in the hook scripts. This is the pattern
  the 80k-star `claude-mem` plugin and Anthropic's own GitHub/Greptile plugins ship, and
  it works identically on CLI install and in the TUI (no prompt dependency).
- Scripts resolve config as: explicit `--url`/`--api-key` arg (tests/wrappers) → else the
  env var → else unconfigured. `hooks.json` no longer passes `${user_config.*}` args.
- Health sentinel now reports `unconfigured` (with `export MORI_SERVER_URL=…` guidance)
  when the env var is unset, instead of always reading `down`.
- **BREAKING for existing installs:** set `MORI_SERVER_URL` / `MORI_API_KEY` in your
  environment (shell profile, or `setx` on Windows) and reload — the userConfig values no
  longer apply. See the README install section.
- **CI guard updated**: the `plugin-validate` job now asserts `.mcp.json` is parameterised
  by `${MORI_SERVER_URL}` / `${MORI_API_KEY}` with no `user_config` or hardcoded
  localhost, and that `plugin.json` declares no `userConfig`. New Node tests
  (`test_plugin_hooks.mjs` #15–17) execute the hooks and prove they read the env vars.
- Cursor (`mcp.json`) and Antigravity (`mcp_config.json`) are unchanged (separate
  manual-edit config; unifying them onto env vars is a follow-up).
- Plugin + marketplace manifests bumped `0.1.4` → `0.2.0`.

## plugin v0.1.4 — install prompt fix: server_url no longer silently defaults to localhost

**fix(plugin): self-hosted users were never prompted for their Mori server URL.**
- The `server_url` userConfig field was `required: true` **with** a `default` of
  `http://localhost:8968`. A required field that already carries a default is
  considered satisfied, so Claude Code silently accepted the default and never
  showed the prompt at install/enable time. Any user not running Mori on their own
  `localhost:8968` therefore installed the plugin, got no configuration prompt, and
  the bundled MCP client quietly tried `localhost:8968` → `ConnectionRefused` —
  surfacing only as "Restart to enable → nothing". (`api_key`, which has no default,
  *was* prompted — confirming the mechanism.)
- Fix: removed the `default` from `server_url` (kept `required: true`) so the install
  dialog now forces the **Mori server URL** + **API key** prompts alongside the
  install-scope question, for every fresh install. Rewrote the field description to
  state plainly there is no default. Bonus: the SessionStart health sentinel now
  correctly reports `unconfigured` (→ setup guide) on an empty URL instead of always
  reading `down`.
- **CI gate added** (`plugin-validate` job): runs the official `claude plugin validate`
  on both the plugin and marketplace manifests, **plus** a guard asserting `server_url`
  stays `required` with **no** `default` — because `claude plugin validate` itself
  passes the bad combination (it is structurally legal; the harm is UX-only).
- Plugin + marketplace manifests bumped `0.1.3` → `0.1.4`. **Existing installs that
  took the silent default won't re-prompt on update** — clear the plugin cache and
  reinstall, or set `pluginConfigs."mori@mori".options.server_url` manually.

## v2.2.16 — human-review gate for agent-intake promotion (Full two-phase B)

The gap between "candidate assessed UNRELATED" and "in canon" is now an explicit,
human-gated, two-phase flow. **Default routing for an UNRELATED intake candidate
is the human-review queue**; the old unattended auto-promotion is opt-in behind
`MORI_INTAKE_PROMOTION_ENABLED`.

**Phase 1 — surface (`mori_intake/assessor.py`).** On `UNRELATED` (gate, default)
the assessor writes a mori `pending_write` (`source='agent-intake'`) plus a
**bridge-owned `intake_promotion_tickets` row** — the trusted carrier of
candidate_id + submission_ids + trust snapshot + `body_hash`. The pending_write's
provenance JSON carries **only an opaque `ticket_uuid`**, never the ids the
finalizer trusts. The candidate is then claimed `under_review`. Mori writes happen
*before* the claim: a crash re-surfaces with a fresh ticket on the next pass
(self-healing, no silent loss). Flag-on keeps the legacy `promotion_queue` path.

**Phase 1.5 — vote (store `approve()`).** A Trusted Dreamer approving an
`agent-intake` pending_write records a **vote** (`status='human_approved'`) — *no*
canon write. All other sources still apply-on-approve. New `set_pending_status`
store method (SQLite + Postgres) for the finalizer-only terminal transitions.

**Phase 2 — finalize (`mori_intake/canon_writer.py::finalize_once`).** The bridge —
the only component holding both DSNs — reads `human_approved` rows, looks up the
trusted ticket (three forgery guards: provenance carries a ticket_uuid, the ticket
exists, `ticket.canon_name == pending_write.name`), then **re-runs the GOV-002 gate
against the live intake candidate** (approved-body integrity, live-candidate body
integrity pinned to `ticket.body_hash`, eligibility re-check) before writing canon +
lineage and marking the candidate `promoted`. Idempotency via `intake_promotion_map`
mirrors `drain_once`. A reconcile sweep marks TD-rejected candidates `rejected`
(bounded — terminalised to `rejected_reconciled`).

Security posture (post-build `/consult`, deep): an attacker with write access to
only `pending_writes` cannot inject arbitrary canon content — the finalizer discards
the pending body and writes the ticket-bound, GOV-002-revalidated live candidate
body. Defence-in-depth follow-ups (candidate-body immutability trigger under
`under_review`; finalizer advisory lock — the latter applies equally to the existing
`drain_once`) are on the roadmap.

Wiring: the intake CLI passes the mori store into `assess_once` and runs
`finalize_once`; the dream B3 path is explicit `promotion_enabled=True`. Dashboard
`review.html` gains an `agent-intake` source badge and a quieter empty-state.

Tests: `tests/test_human_review_gate.py` — 9 Postgres integration tests (surface,
legacy routing, fail-closed without a store, vote-only approve, finalize-promote,
idempotent re-drive, body-integrity reject, forged-provenance reject, reject
reconcile). UAT: both backends + the intake service boot cleanly with migration
id=11 (`intake_promotion_tickets`).

## v2.2.15 — B2 assessor fails closed on unhandled exceptions (hardening)

Fast-follow from a post-ship `/consult` code review of v2.2.14. Closes a latent
**fail-OPEN crash path** in the assessor: prompt `.format()` ran outside `_classify`'s
`try`, and `assess()` didn't wrap the `_classify` call — so an unhandled exception
(e.g. a future template-drift `KeyError`) would crash the coroutine rather than fail
closed to `NEEDS_REVIEW`.

- `_classify`: the **entire** body (prompt assembly + model call + parse) is now wrapped
  in one fail-closed `try` → `NEEDS_REVIEW`; defensive `isinstance()` name extraction.
- `assess()`: belt-and-braces `try/except` around the per-neighbour `_classify` call →
  `NEEDS_REVIEW`, so no unhandled exception ever escapes the assessor as a fail-open crash.
- Test: an exception during classification must yield `NEEDS_REVIEW`, never a verdict
  (especially `UNRELATED`), and must not reach the model. Full suite 628 passed.
- Dormant (manual `cli --real-assessor` path only). Logged-not-blocking polish from the
  same review: strip ```` ```json ```` fences before parsing; fork the prompt into a
  structured variant rather than appending an override directive.

## v2.2.14 — structured-output verdicts in the B2 governance assessor (P0)

First Stage-2 gate item. Replaces the fast-model assessor's free-text verdict + brittle
`.upper()`/first-word parsing with a **strict `json_schema` structured output**, validated
by Pydantic — removing the fail-open-on-malformed risk in B2.

- **`BifrostClient.consult`** gains a generic, provider-agnostic `response_format`
  passthrough (omitted when unset; callers own the schema).
- **`mori_intake/assess_model.py`**: requests `{"verdict": <enum>}` via strict `json_schema`
  (verified honoured by the fast VK), then `json.loads` + `_VerdictModel`
  (`Literal`, `extra='forbid'`) as the single source of truth for the verdict contract.
  ANY decode / schema-validation / missing-field error → **NEEDS_REVIEW** (fail-closed),
  logging the failure taxonomy (decode vs validation) for telemetry. No free-text fallback.
- **Prompt/schema alignment**: `CONTRADICTION_SCAN_PROMPT` is reused for task semantics, with
  an assessor-local directive that explicitly overrides its "answer with one word"
  instruction so prompt and schema don't conflict. The shared dream-pipeline prompt is
  untouched.
- **Dormant**: only the manual `python -m mori_intake.cli --real-assessor` path runs this;
  the running services don't, and promotion stays flag-off. Safe to ship.
- Tests: `_parse_verdict` contract (unknown enum, extra keys forbidden, missing field,
  non-JSON, non-object → NEEDS_REVIEW) + a wiring assertion; existing assessor mocks updated
  to structured JSON. Full suite 627 passed (4 pre-existing PG soft-delete/restore failures
  unrelated). Bifrost honouring of `json_object` + strict `json_schema` verified live.

## v2.2.13 — Homebrew tap support (env-file loader + `deploy/homebrew/`)

Adds a self-hosted **Homebrew install path** for mori, validated end-to-end on Linuxbrew
(`brew style` + `brew audit --strict` clean; `brew install --build-from-source` + `brew test`
pass). The tap lives at [`fjwood69/homebrew-mori`](https://github.com/fjwood69/homebrew-mori).

- **`_load_user_env()`** (`mori_advisor/main.py`) — loads `~/.config/mori/env` before module-level
  config is read, so a Homebrew/local install configures mori via a plain env file with no bash
  wrapper. Uses `os.environ.setdefault` (a real environment var or a Docker `--env-file` / systemd
  `EnvironmentFile` always wins), no-ops if the file is absent, and runs **only under `__main__`** —
  so imports (tests, the ingestion server) and the existing GCE/Docker deployment are unaffected.
- **`pyproject.toml`** — adds `[build-system]` (`setuptools.build_meta`) + `[project]` +
  `[tool.setuptools.packages.find]` so `pip install --no-deps .` works from the formula. Runtime
  deps stay in `requirements.txt`. Inert for the Docker image (which installs from `requirements.txt`
  and COPYs the source; it never `pip install .`).
- **`deploy/homebrew/`** — `mori-setup.sh` (config wizard: prompts URL/key/model, writes
  `~/.config/mori/env`, offers Linux linger, starts the service, health-checks, opt-in plugin
  wiring) + `mori.env.example`.
- **Note:** the formula is **untested on macOS** (no Mac available) — validated by inspection +
  Linuxbrew; the launchd service path is unverified and flagged in the tap's caveats.

## v2.2.12 — agent-memory governance Stage-1 enablement (write-only intake, hardened)

> **Stage 1 of the governance pipeline (mori #16): write-only intake.** The intake service
> is now deployable as a GCE Quadlet unit and Hermes mirror-writes land as `pending`
> intake candidates. **Nothing promotes to canon** — the running service starts only the
> dedup/TTL worker and never invokes the canon writer (promotion is reachable solely via the
> manual `python -m mori_intake.cli` trigger). `MORI_INTAKE_PROMOTION_ENABLED` stays off.

**Stage-1 preconditions closed (pre-enable hardening, post-`/consult`):**

- **P3 — pending-candidate TTL (tombstone).** New `MORI_INTAKE_PENDING_TTL_HOURS` (default
  168; prod runs 720). The drain worker periodically reaps pending candidates idle (by
  `updated_at`) beyond the TTL. To prevent the drain from resurrecting a reaped candidate
  from its originating submission, the purge **tombstones** those submissions
  (`intake_submissions.purged_at`, migration 9) rather than deleting them — preserving the
  agent-write audit trail and the `UNIQUE(session_id, stable_key)` idempotency guard. The
  drain query excludes tombstoned rows. Cardinality-safe (a submission corroborating any
  non-stale candidate is left live) and race-safe (`FOR UPDATE SKIP LOCKED`). Manual
  operator purge: `python -m mori_intake.cli --purge-pending-hours N`.
- **P1 — provider outbox bound.** The hermes-mori-provider outbox now age-purges terminal
  (`done`/`failed`) rows beyond `terminal_max_age` (default 7d) so the local SQLite file
  cannot grow over long uptimes. Pending rows remain bounded by the existing 100-row
  backpressure cap.
- **DB-level data boundary.** The intake service runs as a least-privilege `intake_app`
  Postgres role with **`CONNECT` on `mori` revoked** — kernel-enforced separation from
  canon, beyond the app's `check_data_boundary()` guard. The intake env carries **no**
  canon DSN. (`deploy/gcp/provision-intake.sh` provisions the role + `intake` DB + verifies
  the boundary.)
- **Resource caps.** `mori-intake.container` sets `MemoryHigh=224M` / `MemoryMax=256M` /
  `CPUQuota=50%` so intake can never OOM-kill or starve canon on the shared 2GB VM; pool
  capped (`MORI_INTAKE_POOL_MAX=4`).
- **Payload guard.** `MORI_INTAKE_MAX_CONTENT_BYTES` (default 64KiB) rejects oversized
  submissions (422) before any DB work. Bind address configurable via `MORI_INTAKE_HOST`.

**Deploy:** `deploy/gcp/quadlet/mori-intake.container` + `provision-intake.sh`; `startup.sh.tpl`
installs the unit on boot and starts it once provisioned (intake DB/role/env persist on `/data`).

## v2.2.11 — agent-memory governance pipeline (dormant) + 3 security/perf criticals + plugin v0.3.x

> **Security/perf criticals are ACTIVE in this release.** The governance pipeline is SHIPPED to
> `main` but DORMANT by default — the intake service is not deployed and
> `MORI_INTAKE_PROMOTION_ENABLED` is off. See the *Governance pipeline* section below.

### Security / performance criticals (active)

**AUTH-001 — arbitrary file read via `consult_advisor` `files` parameter (CRITICAL):**
- The `files` param performed no path validation — a malicious or errant caller could read
  any file the server process could access by supplying an absolute path or `../` traversal.
- Fix: all supplied paths are now resolved to absolute form (`Path.resolve()`) and checked
  against an **allowlist** (`MORI_CONSULT_FILE_ROOTS`, defaults to `cwd`). Resolved paths
  outside every root are rejected outright. A startup `WARNING` fires if
  `MORI_CONSULT_FILE_ROOTS` is unset (default-cwd is permissive for solo deployments but
  should be locked down in team/production).
- Additional layers:
  - Sensitive **basename / suffix / path-segment blocklist** (e.g. `.env`, `*.key`,
    `secrets`, `credentials`, `shadow`, `passwd`) rejects obvious high-value targets even if
    the resolved path is inside a root.
  - `O_NOFOLLOW` flag on the open call — defeats **TOCTOU symlink-swap attacks** where a
    symlink is swapped between `resolve()` and `open()`.
- **Same guard extended** to the skill-loader (`sk` name regex + `SKILLS_DIR` containment
  check) and the standards-import path (`MORI_STANDARDS_ROOTS` containment check), closing
  equivalent traversal surfaces in both.

**PERF-004 — `read_events(limit=0)` fetched the whole table:**
- `limit=0` was silently treated as "no limit" and issued an unguarded `SELECT *` against
  `session_events`, which could be millions of rows.
- Fix: **`limit` semantics normalised** — `None` → unlimited (explicit intent), `0` → empty
  list (empty-result shorthand), `n > 0` → `LIMIT n`. Callers that relied on the old `0 =
  unlimited` behaviour are updated.
- `dream.get_status` previously used `read_events(limit=0)` to discover whether there were
  any unprocessed events — replaced with a new **`count_events_since(id > N)`** method
  (added to `SQLiteStore`, `PostgresStore`, and `BaseStore`) that issues a single `COUNT(*)`
  instead of fetching rows. Eliminates the full-table fetch on every `dream_status` poll.

**PERF-003 — `check_freshness` thundering herd on every `brief()` call:**
- The previous implementation could make up to **20 sequential, blocking LLM calls** during
  each `brief()` — one per memory in the freshness backlog. On a cold or large store this
  made `/brief` unusably slow and consumed significant LLM budget.
- Fix: a three-part overhaul:
  1. **Bounded concurrency** — freshness LLM calls now run concurrently via a semaphore (cap
     5 simultaneous calls) rather than sequentially.
  2. **Batched single-transaction updates** — all freshness verdicts from a given pass are
     written in a single DB transaction instead of one write per call.
  3. **24-hour per-store cache** — a lock + in-flight sentinel prevents concurrent `brief()`
     calls (e.g. on session start + post-compact re-ground) from triggering parallel freshness
     sweeps (no thundering herd). Results are cached for 24h; the cache is invalidated on
     dream run and on explicit `--force-refresh`.
- New **`MORI_FRESHNESS_ON_BRIEF`** env flag (default `true`) — set to `false` to disable
  the brief-time freshness check entirely for high-frequency `/brief` usage patterns.

### Agent-memory governance pipeline (NEW — shipped, opt-in, dormant by default)

A new `mori_intake/` service introduces a **physically-separate, Postgres-only intake path**
for autonomous-agent writes. Agents NEVER write into mori's canon table directly;
**promotion is the only door into canon**.

#### Stream A — intake front door

New `mori_intake/` FastAPI service (separate process, separate Postgres, `MORI_INTAKE_PORT`):
- `POST /intake/submissions` — validates + inserts into `intake_submissions` (immutable raw
  firehose). HTTP handler does nothing but validate and persist; embeddings and dedup are
  entirely async. Returns `202 Accepted` immediately.
- **Eligibility gate** — default-deny: only allow-listed namespace prefixes (`learned-*`,
  `fact-*`; `user` restricted to `preference-*`/`accessibility-*`) are eligible. A
  proposition classifier rejects chatter, scratch notes, and non-claim content.
  `GOV-001` substring deny list blocks explicitly disqualified content patterns.
- **Format regex** — basic structural validation of candidate body before acceptance.
- **Per-key rate limit** (`MORI_INTAKE_RATE_LIMIT_PER_MIN`, default 20 req/min per API
  key) — enforced at the HTTP layer before the eligibility gate.
- **Pool config**: `MORI_INTAKE_POOL_MIN` / `MORI_INTAKE_POOL_MAX`.
- `Dockerfile` updated — `mori_intake/` is now shipped in the image.
- New env vars: `MORI_INTAKE_DATABASE_URL`, `MORI_INTAKE_PORT`.

#### Stream B1 — intra-pile dedup worker

Async background worker drains `intake_submissions`, deduplicates within the intake pile:
- **Exact `content_hash` dedup** (NFKC-normalised, matching mori canon's hash) — coalesces
  repeated submissions into one candidate and bumps a reinforcement counter.
- Emits deduplicated candidates to `intake_candidates` with status `pending`.
- No canon access at this stage — fully local to the intake store.

#### Stream B2 — fast-model vs-canon assessor

Real assessor using the **fast model VK** (not the dream model) to evaluate surviving
candidates against mori canon:
- Fetches candidate body, queries `search_json` on the canon store, presents both to the
  fast model with RELATED / SUPERSEDES / UNRELATED / NEEDS_REVIEW verdict schema.
- **Fail-CLOSED** — `NEEDS_REVIEW` verdict (including any model error or ambiguous response)
  does NOT auto-promote; the candidate stays in `pending` for human review. Only a
  model-confirmed `UNRELATED` verdict advances to the promotion queue.
- RELATED / SUPERSEDES → reinforces the existing canon memory, drops the candidate.

#### Stream B3 — dream-trigger promotion (feature-flagged, additive)

Dream integration that polls `promotion_queue` and promotes `UNRELATED` candidates into mori
canon via the **single canon writer**:
- **`FOR UPDATE SKIP LOCKED`** lease on the promotion queue — safe for concurrent workers,
  no double-promotion. `MORI_INTAKE_LEASE_SECONDS` configures the lease window.
- **Idempotent promotion** — checks `memory_intake_lineage` before insert; a previously
  promoted candidate is a no-op.
- **`memory_intake_lineage` table** — cross-system provenance row linking canon name →
  intake candidate_id → submission_ids[] → trust snapshot. Lives in mori canon (intake must
  not hold canon write creds).
- **GOV-002 promotion-time re-check** — eligibility is re-evaluated at promotion time (not
  just at submission), defeating deferred eligibility bypass. Body-integrity check
  (`content_hash` comparison between candidate and intake record) guards against
  candidate-body tampering between assessment and promotion.
- **Additive and feature-flagged** (`MORI_INTAKE_PROMOTION_ENABLED`, default `false`) — when
  the flag is off, candidates accumulate in the promotion queue but nothing is written to
  canon. The dream pipeline's existing behaviour is completely unchanged whether or not the
  flag is set.

#### hermes-mori-provider v0.3.0

The provider's write path now targets the governed intake service instead of mori's core
`/api/memories` (which would bypass all governance):
- Writes go to `POST /intake/submissions` using eligible-namespace `stable_key` prefixes.
- **Fail-CLOSED** — if `MORI_INTAKE_URL` is unset, writes are queued with an `ERROR` log;
  there is no ungoverned fallback to the old write path.
- **SSRF guard** — a no-redirect opener prevents the provider from being pointed at
  private-IP resources via a MORI_INTAKE_URL override.
- Session binding to API key (not reused across keys).
- `content_hash` computation uses NFKC normalisation unified with the intake service —
  eliminates false hash mismatches in the LWM hash-compare vs canon path.
- Lives under `integrations/hermes-memory-provider/`.

#### Pre-enable hardening (13 Inspector findings + GOV-002 strengthening)

A dedicated hardening pass before this PR was merged closed 13 findings from a structured
security/correctness review:
- GOV-002 re-check now validates **body integrity** (content_hash) as well as eligibility at
  promotion time — tampered or mutated candidate bodies are detected and blocked.
- Additional input-validation tightening, error-path coverage, and async-correctness fixes
  throughout A→B3.

#### Activation note

**The governance pipeline ships DORMANT.** To enable unattended promotion:
1. Deploy the `mori_intake/` service with its own Postgres (`MORI_INTAKE_DATABASE_URL`).
2. Point `hermes-mori-provider` at it (`MORI_INTAKE_URL`).
3. Set `MORI_INTAKE_PROMOTION_ENABLED=true` on the mori-advisor instance.

The following work is gated before enabling unattended promotion in production:
- Structured-output verdict schema (removes free-text parsing from B2).
- Private-IP SSRF guard on intake URL validation (complement to the provider-side guard).
- Human-review gate / trust curve (Slice-3) — working→canonical promotion still requires
  human approval or cross-source corroboration; agent memories do not self-promote to canon.
- Dream-concurrency guard OPS-002 — the dream lease and the B3 promotion worker must not
  race on the same canon write connection.
- End-to-end pipeline test (A → B1 → B2 → B3 → canon round-trip).

### Plugin releases (v0.1.1 – v0.3.3)

The following plugin releases shipped alongside the governance work (full detail in the
plugin-specific headings earlier in this changelog):

| Version | Summary |
|---------|---------|
| v0.1.1 | Cursor & Antigravity hook layers + multi-client `tidy-up.mjs` |
| v0.1.2 | Health sentinel + honest onboarding copy |
| v0.1.3 | TD `/review` concertina cards; `nats_sub` replay fix; `displayName` drop |
| v0.2.0 | Config via env vars (`MORI_SERVER_URL`/`MORI_API_KEY`) — drops `userConfig` |
| v0.3.0 | Skills + hooks only — drops bundled MCP that collided with user's own connection |
| v0.3.1 | Cursor / Antigravity bare-secret configs |
| v0.3.2 | `SessionStart` hook silent when `MORI_SERVER_URL` unset |
| v0.3.3 | README consistent with v0.3 skills-only architecture |

### hermes-mori-provider (v0.1.1 – v0.2.0)

| Version | Summary |
|---------|---------|
| v0.1.1 | Read `MORI_SERVER_URL` + hyphen names (not dots) |
| v0.2.0 | Rebuild write path on real `on_memory_write` contract + two-tier LWM |

*(v0.3.0 covered above under the governance pipeline section.)*

## v2.2.10 — msg_thread Postgres fix + CI Node-24 bumps (#18)

**fix(postgres): `msg_thread` raises on Postgres — `'datetime' object is not subscriptable`:**
- `PostgresStore.get_message_thread()` was returning raw asyncpg Records whose
  `ts` column is a Python `datetime` object (TIMESTAMPTZ via asyncpg). The
  `msg_thread` formatter subscripted it as a string (`row["ts"][:16]`), which
  raised `'datetime.datetime' object is not subscriptable` on every Postgres
  `msg_thread` call. SQLite was unaffected (TEXT column → already a string).
- Fix: new `_coerce_msg_row()` module-level helper coerces all `datetime`-valued
  fields in a msg_log row to ISO-8601 strings before returning, normalising the
  store contract to match SQLite. Applied to root + all replies in
  `get_message_thread()`.
- Regression tests in `tests/test_msg_send.py`: `test_pg_get_message_thread_ts_is_str`
  (direct assertion that `ts` is `str` post-coerce) and `test_pg_msg_send_thread_roundtrip`
  (full send → thread round-trip; both gate on `MORI_TEST_DATABASE_URL` via `@requires_pg`
  — SQLite variants run unconditionally).

**CI/CD: GitHub Actions → Node 24-native (#18):**

Eliminates the "Node.js 20 actions are deprecated" warnings and removes
`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` from CI (it remains in CD while
`docker/login-action` does not yet have a Node-24 native release).

**CI (ci.yml):**
- `actions/checkout` v4 → v5 (Node 24 native)
- `actions/setup-python` v5 → v6 (Node 24 native)
- `docker/setup-buildx-action` v3 → v4 (Node 24 native)
- `docker/build-push-action` v5 → v7 (Node 24 native)
- `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` removed (all actions now Node 24)
- `pytest` runs now pass `-W error::RuntimeWarning` (banked lesson from #23)

**CD (cd.yml):**
- `actions/checkout` v4 → v5
- `docker/setup-buildx-action` v3 → v4
- `docker/login-action` v3 → v3.7.0 (latest; no Node-24 native version yet)
- `docker/metadata-action` v5 → v6 (Node 24 native)
- `docker/build-push-action` v5 → v7 (Node 24 native)
- `tailscale/github-action` v2 → v4 (current stable; authkey still supported)
- `appleboy/ssh-action` v1 → v1.2.5 (latest point release)
- `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` kept for `docker/login-action` (Node 20)

Config-only change — CD is not live-tested on this branch (no tag pushed).
The updated actions are well-established; no behaviour changes expected.

## v2.2.9 — Agent self-view (#16) · Postgres tier fix · msg_send fix (#37) · Windows plugin fix

**`GET /api/pending/mine` (#16 Hermes prereq):**
- New `GET /api/pending/mine` REST endpoint (write role) — returns only the
  authenticated caller's own pending proposals (`proposed_by = actor.key_name`),
  so an agent can see its own backlog + outcomes (approved/rejected) without ever
  seeing another actor's rows. `status` query param (omit → all statuses). Missing
  actor → empty list, not a 500.
- `pending_list_json` (both backends) gains optional `proposed_by` + optional
  `status`; existing single-status call-sites unchanged. `tests/test_pending_mine.py`.

**fix(postgres): NULL `tier` on TD approve:**
- `PostgresStore.write()` now coalesces `tier` to `"working"` (mirrors SQLite's
  `_ensure_tier`), so applying a pending row with a NULL/absent tier can no longer
  violate `memories.tier NOT NULL`. Trigger: `approve()` of an untiered ingestion
  proposal on Postgres — SQLite was protected, Postgres was not. `VALID_TIERS`
  shared via import. Regression tests in `tests/test_td_review.py`.

**fix(#37): msg_send directed-task path:**
- `msg_send()` now persists to the sender's `msg_log` (was publish-only, so
  `msg_thread`/`msg_recv` never saw it); returns the full 36-char UUID (was an
  8-char prefix that `msg_thread`'s exact lookup always missed); `msg_thread` reads
  via the `store` global (was a fresh `MsgStore`, breaking monkeypatch/path
  overrides). `get_thread`/`get_message_thread` accept an 8-char prefix fallback for
  ids already in the wild. `tests/test_msg_send.py` (6 tests).

**fix(plugin):** Windows `mcp.json` uses `user_config` template vars so the
plugin's mori server resolves per the user's config.
## v2.2.8 — Fix: server startup crash in v2.2.6/v2.2.7 (`_lifespan` decorator)

- **fix:** the #23 C cleanup-task edit inadvertently moved the
  `@asynccontextmanager` decorator off `_lifespan` and onto
  `_throttle_cleanup_loop`, leaving `_lifespan` a bare async generator. FastMCP
  enters the lifespan as a context manager at startup, so v2.2.6 and v2.2.7
  **crashed on boot** (`'async_generator' object does not support the
  asynchronous context manager protocol`). Decorator restored to `_lifespan`.
- **test:** `tests/test_startup.py` — boots the lifespan for real and asserts it
  is a proper async context manager (+ that the cleanup loop is a plain
  coroutine). The unit suite + CI never booted the lifespan, so this class of
  startup-wiring bug had no gate; now it does. Validated end-to-end via the UAT
  harness (real container boot, both backends).

## v2.2.7 — Per-key rate limiting (#23 D)

- **feat:** `ApiKeyMiddleware` enforces a per-API-key **token-bucket rate limit**
  after authentication. A denied request is rejected `429` + `Retry-After`
  before the handler runs. Keyed on the authenticated key name; `OPEN_PATHS`
  (`/health`, `/ready`, `/metrics`, …) are never limited.
- **config:** rate limiting is **opt-in** (disabled unless `MORI_RATE_LIMIT` is
  set — recommended `120/min` once sized; `0`/`off` disables). Default-off
  because the limiter covers *all* writes including the high-volume
  `POST /api/events` telemetry stream, so enabling it blindly could throttle
  legitimate ingestion. `MORI_RATE_LIMIT_SCOPE` (`writes` default — only
  POST/PUT/PATCH/DELETE count, targeting runaway autonomous writers; `all` limits
  every guarded request).
  In-memory store (single-instance) via `MORI_THROTTLE_STORE`; the shared
  Postgres adapter for horizontal scale-out is deferred (a startup warning fires
  if `memory` runs with `>1` worker). Idle buckets are evicted periodically.
- **note:** completes #23 (write-API hardening). With #13/#14 (governed write +
  roles), #23 A/B (audit + soft-delete) and C (idempotency), the write surface is
  now role-scoped, audited, reversible, replay-safe, and rate-limited.
- **test:** `tests/test_rate_limit_middleware.py` (7) — limit→429+Retry-After,
  scope (writes vs all), per-key isolation, disabled-config, 401-before-limit,
  open-paths exempt.

## v2.2.6 — Idempotency for POST /api/memories (#23 C) + throttle foundation

- **feat:** `mori_advisor/throttle/` — a pluggable throttling foundation (the
  shared base for #23 C and D). Async store contracts `IdempotencyStore` /
  `RateLimitStore` with in-memory adapters as the single-instance default; the
  Postgres-backed (horizontally-scalable) adapter is deferred to a future issue
  (`MORI_THROTTLE_STORE=postgres` raises until then). Idempotency is modelled as
  a self-healing execution cache (short claim TTL + stale-claim stealing +
  claim-token guard), and the rate limiter as a token bucket — both hardened
  after an advisor review before wiring. 41 unit tests.
- **feat:** `POST /api/memories` honours an **`Idempotency-Key`** header — a
  replay of the same key + body returns the cached response (`Idempotency-Replay:
  true`) and the write runs **exactly once**; the same key with a *different*
  body is rejected `422`; an in-progress claim returns `409` + `Retry-After`.
  Keys are scoped per actor. Deterministic outcomes (2xx/4xx) are cached;
  transient 5xx are not (a retry re-attempts). No key → unchanged behaviour.
- **feat:** startup logs a loud warning if `MORI_THROTTLE_STORE=memory` runs with
  `>1` worker (per-instance counters would silently breach the global limit); a
  periodic task evicts expired idempotency records.
- **config:** `MORI_THROTTLE_STORE` (`memory` default), `MORI_IDEMPOTENCY_CLAIM_TTL`
  (default 30s), `MORI_IDEMPOTENCY_CACHE_TTL` (default 86400s, legacy
  `MORI_IDEMPOTENCY_TTL` honoured).
- **test:** `tests/test_throttle.py` (41) + `tests/test_idempotency_api.py` (6,
  dual-backend) — replay-writes-once, 422 mismatch, 409 in-progress, per-actor
  scoping, key isolation.

## v2.2.5 — Persistent audit trail + soft-delete (#23 A+B)

- **Migration 8 (`write_audit_table`):** new `write_audit` table (id, ts, actor_key_name,
  op, memory_name, content_hash, detail) on both SQLite and Postgres — purely additive,
  transactional, idempotent.
- **Migration 9 (`soft_delete`):** adds `deleted_at` (nullable) to `memories` and replaces
  the inline `UNIQUE(name)` with a partial unique index `WHERE deleted_at IS NULL` on both
  backends. SQLite requires table recreation (no `DROP CONSTRAINT`) — handled via `sqlite_fn`
  that rebuilds the table, indexes, and FTS5 triggers atomically under the runner's
  `BEGIN IMMEDIATE`. Postgres uses one atomic transaction: `ADD COLUMN`, `DROP CONSTRAINT
  memories_name_key`, `CREATE UNIQUE INDEX ... WHERE deleted_at IS NULL`.
- **`_write_audit()` promoted to `async def`** and now inserts a row into `write_audit` on
  every governed operation (propose_new, propose_pending, update_working, approve, reject,
  soft_delete, hard_delete, restore, restore_renamed). All 7 call sites updated to `await`.
  Audit insert failure is logged but never raised — the primary operation is not rolled back.
- **`GET /api/audit`** (dreamer role): returns recent audit rows, filterable by `memory_name`
  and `actor`, paginated (default 100, max 500).
- **`DELETE /api/memories/{name}`** now **soft-deletes by default** (sets `deleted_at`);
  `?hard=true` permanently purges the row. Both paths require dreamer role. Audit op is
  `soft_delete` or `hard_delete` accordingly.
- **`POST /api/memories/{name}/restore`** (dreamer): clears `deleted_at`. If an active row
  already holds the name (supersession), the restored row is renamed to
  `{name}_restored_{ts}` — the superseding row is never clobbered. Both cases are audited.
- **Tombstone filtering centralised** — `_ACTIVE = "deleted_at IS NULL"` sentinel in
  `memory_store.py`; used in every read path: `read`, `get_memory`, `list`, `count`,
  `search` / `_build_search_sql` (FTS JOIN + LIKE fallback), `get_memories_by_project`,
  `get_memories_changed_since`, `check_freshness`, `scan_orphans`, `protect`. Postgres paths
  use `deleted_at IS NULL` inline. `_row_to_dict` is not changed — deleted rows are
  filtered before reaching it.
- **FTS + tombstones:** filtered at query time (`AND m.deleted_at IS NULL` in the FTS JOIN;
  `AND deleted_at IS NULL` in the LIKE fallback). The `AFTER UPDATE` FTS5 trigger keeps the
  index content fresh for a soft-deleted row, but it is never surfaced in search results.
- **Supersession:** the partial unique index only constrains active rows, so a new active
  write can freely reuse a tombstoned name — the "old payments-auth standard superseded by
  the new one" pattern.
- **Dual-backend tests** (`tests/test_audit_softdelete.py`): audit row on write/delete;
  soft-delete invisible from read/list/FTS; partial index allows name reuse; restore simple
  + collision-rename; hard-delete purges; role enforcement on audit/restore/delete endpoints.
  `@requires_pg` gates Postgres-specific cases.
- **`scripts/verify-deployment.py` updated:** `/api/audit` and `/api/memories/{name}/restore`
  added to `WRITE_API_AUTH_ROUTES` so both UAT and CD gates assert the new routes are
  registered and auth-gated.

## v2.2.4 — nats_sub replay fix · TD review-UI polish · plugin compatibility

- **feat:** TD `/review` UI — proposals are now **concertina cards** (compact header: name · source · tier · category · confidence · age; click `>` to expand body + diff + approve/reject) with a **filter-by-category** (focus_mode) control — more fits on screen.
- **fix:** `nats_sub(replay)` reads the stream **tail** (`BY_START_SEQUENCE` from `last_seq − N`) instead of the oldest retained messages, so freshly published messages now appear in `/nats` replay (closes #32). Verified live.
- **fix:** dropped `displayName` from the plugin manifests — added in Claude Code v2.1.143, older clients reject it as a hard validation error that blocks install. It's cosmetic (falls back to `name`). Added a top-level marketplace description (clears the marketplace validate warning). Plugin v0.1.2 → v0.1.3.
- **docs:** README Capabilities adds the Curation queue (#15) and One-click deploy (#30) rows.

## v2.2.3 — Trusted-Dreamer review queue (#15)

- **feat:** Governance loop closed — ingestion's `canonical`/`standard` candidates route to a pending queue (`queue_pending_write`) for trusted-dreamer review instead of writing directly; `working`-tier writes stay direct (the TD is never asked to review high-volume working dreams). `dream.py` is unchanged and ingestion runs independently of the dream pipeline (no shared mutable state — concurrent `/ingest` never interferes with scheduled dreams). `MORI_CURATE=false` preserves direct-write behaviour.
- **feat:** `dashboard/review.html` — a standalone, dreamer-gated admin page (separate from the read-only dashboard): lists pending proposals with source / provenance / confidence and a diff against the existing canonical; approve/reject wired to the race-safe `POST /api/memories/{name}/approve|reject`.
- **feat:** `GET /api/pending/json` (dreamer-gated) + `pending_list_json()` returning enriched pending rows (source, provenance, confidence, focus_mode, existing_body for the diff, created_at).
- **db:** migration 7 (additive, idempotent) — `pending_writes` gains source/provenance/confidence/focus_mode/existing_body/created_at; a **partial** unique index (`memory_name WHERE status='pending'`) on both backends prevents duplicate pending proposals while allowing a memory to be re-proposed and re-approved over its life.
- **fix:** Postgres parity — partial pending-only index + matching `ON CONFLICT` inference (the upsert previously inferred a non-existent partial index → would error on Postgres; a full unique constraint would collide on re-approval). Dual-backend re-approve regression test added.
- Verified: UAT (migration applies on seeded Postgres; deployment contract green on both backends) + CI Postgres job.

## v2.2.2 — Onboarding: server health sentinel + honest setup guidance (plugin v0.1.2)

- **feat:** Session-start server **health sentinel** — the SessionStart hook pings the configured server's `/health` (600ms–2s bound, cached 5 min per session, fail-open, and **only ever the user's own URL — no phone-home**). If the server is unreachable or unconfigured, it injects a clear, honest setup message *before* the user hits a broken `/brief`; if the server is up, it stays silent. Skill-level backstop in `/brief` and `/pensieve` for mid-session outages.
- **feat:** Honest onboarding copy — the setup message names the real requirements (a Docker host **and** an LLM provider key) and links the quickstart; no "one-command" overstatement. Plugin-manager `description` rewritten value-first with the self-hosted/AGPL privacy framing.
- **fix:** the default `localhost:8968` is now health-checked, not short-circuited to "unconfigured" — a running local server (the most common setup) is no longer mis-reported as unconfigured.
- **test:** 96 tests across the health-gate and the three client hook suites.

## v2.2.1 — Cursor & Antigravity hook layers + multi-client tidy-upper (plugin v0.1.1)

- **feat:** Cursor and Antigravity hook layers — per-client Node entrypoints over a shared `lib/` (fail-open wrapper, POST, conversation-keyed throttle, canonical-event normalizer). Cursor: `sessionStart` context + `postToolUse`/`stop` telemetry via the documented standalone `~/.cursor/hooks.json` (Cursor plugin-hook bundling is undocumented). Antigravity: `PreInvocation` once-per-conversation context (conversationId throttle) + `PostToolUse`/`Stop` telemetry via `~/.gemini/config/hooks.json`. Wired by `install-hooks-{cursor,antigravity}.mjs` with absolute paths. Each client's events are normalized client-side to Mori's canonical event schema before POST — the server stays single-schema; client extras ride in an opaque `_clientMeta`.
- **feat:** `plugins/mori/scripts/legacy/tidy-up.mjs` — multi-client cleanup that removes ONLY Mori's bespoke-installed entries (Claude/Cursor/Antigravity MCP + hooks + permissions; skills with `--include-skills`) so the plugin installs clean. Dry-run by default; `--confirm` to write; exact-signature matching, timestamped backups, validation gates (no non-mori key removed), fail-gradual per client. Replaces the Claude-only uninstaller.
- **chore:** post-compaction re-ground remains Claude-only — Cursor (`preCompact` observational) and Antigravity have no compaction event.
- **test:** 127 hook/cleanup tests (Claude 23, Cursor 22, Antigravity 24, tidy-up 58); the shipped Claude scripts are unchanged.

## v2.2.0 — Cross-tool plugin distribution (plugin v0.1.0)

- **feat:** Unified plugin package at `plugins/mori/` for Claude Code, Cursor, and Antigravity — shared `skills/` + Node `scripts/` core with thin per-platform manifest/MCP files. Auto-registers via the plugin system, no `settings.json` surgery; API key via Claude Code `userConfig` (keychain) with `MORI_API_KEY` env fallback.
- **feat:** Claude Code: complete and marketplace-ready. `SessionStart` re-ground hook (`source=compact`) replaces the broken PostCompact `additionalContext` hook (which the harness rejects) — closes #17. Node `mori-context-hook.mjs` + `mori-ship-event.mjs` (telemetry incl. Stop transcript-tail enrichment); no bash/jq dependency. Repo-root `.claude-plugin/marketplace.json` enables `/plugin marketplace add fjwood69/mori` → `/plugin install mori@mori`.
- **feat:** Cursor + Antigravity: MCP connection + skills work day one (skills are a cross-tool open standard); platform-specific hooks are a fast-follow.
- **chore:** Bespoke `install-mori-claude.{sh,ps1}` moved to `scripts/legacy/` (superseded by the plugin); a standalone legacy uninstaller is bundled for migration. Cursor/Antigravity bespoke installers retained pending their plugin hook layers; Cline unchanged.
- **docs:** README + getting-started guides rewritten plugin-first; corrected stale claims (SessionStart not PostCompact for post-compaction re-ground; dual-backend SQLite/Postgres, not SQLite-only).
- **docs:** new *Read the current manual, not your memory* practice in `agent-working-practices` (consult live docs for evolving APIs, not training recall); README *Pairs well with* section positioning Mori (earned memory) alongside Context7 (live library docs).
- See #24 for the plugin distribution tracking issue.

## v2.1.35 — Governed write REST API core: propose/pending/approve/reject/delete (#14)

- **feat:** `POST /api/memories` — propose-not-overwrite, tier-aware write endpoint (role: write).
  New name → working row created (201); canonical or protected name → pending proposal (202, canonical
  row unchanged); working name with same actor → idempotent update (200); different actor → pending
  proposal (202). Strict input validation: name `^[a-zA-Z0-9_-]{1,128}$`, body max 64 KB,
  unexpected fields rejected (400).
- **feat:** `GET /api/pending` — list pending proposals awaiting dreamer review (role: write;
  unapproved agent output is not for read-only eyes).
- **feat:** `POST /api/memories/{name}/approve` — approve a pending write and apply it to the store
  (role: dreamer). Race-safe: SQLite uses `BEGIN IMMEDIATE`; Postgres uses `SELECT … FOR UPDATE`
  inside an explicit transaction. Concurrent approvals cannot duplicate canonical rows.
- **feat:** `POST /api/memories/{name}/reject` — reject a pending write without applying (role: dreamer).
- **feat:** `DELETE /api/memories/{name}` — hard-delete a memory entry (role: dreamer). Soft-delete
  (`deleted_at`) deferred to #16.
- **feat:** Structured audit log line on every governed write/approve/reject/delete:
  `AUDIT op=<op> actor=<key_name> name=<name> content_hash=<sha256[:16]>`. No new table (deferred).
- **feat:** `queue_pending_write()` added to `MemoryStore`, `PostgresStore`, `SQLiteStore`, and
  `BaseStore` — direct pending-write insertion for the REST propose path, bypassing the
  `write()` protection-check heuristic for canonical-not-protected memories.
- **security:** Audited `_a()` bridge for contextvars propagation — no `run_in_executor`/threads;
  `current_actor` propagates correctly into all awaited coroutines. Fail-closed test confirms a
  missing actor raises `PermissionDenied`, never silently passes.
- **test:** `tests/test_write_api.py` — 18 tests covering: capability enforcement (read/write/dreamer
  on all routes), propose-not-overwrite semantics (new/canonical/same-actor/different-actor),
  contextvar-missing fail-closed test, input validation (body size, name chars, unexpected fields,
  missing name), and `_validate_write_payload` unit tests. Both backends via `@requires_pg`.
- **scripts:** `scripts/verify-deployment.py` updated — `GET /api/pending` added to guarded routes;
  write-API auth-gating probes for approve/reject/delete; safe POST+DELETE round-trip probe.
- **docs:** `docs/reference/configuration.md` updated — roles table expanded with REST endpoints;
  Write REST API section with endpoint reference, body schema, and deferred items.
- **deferred to #16:** Per-key token-bucket rate-limiting, `Idempotency-Key` replay cache, soft-delete
  (`deleted_at`), structured audit table. These are prerequisites for autonomous mirror-writer support
  (Hermes, #16) but not needed for the dashboard (#15).

## v2.1.34 — API key capability scoping + host→api TD mode switch (#13)

- **feat:** Added `MORI_API_KEY_ROLES=name:role,...` env var — parsed parallel to `MORI_API_KEYS`
  (format unchanged). Roles: `read < write < dreamer`. Names absent from the roles map default to
  `read` (fail closed). Unknown role strings are rejected at startup with an error log and
  downgraded to `read`.
- **feat:** New `MORI_TD_MODE=host|api` mode switch (default: `host`). In `host` mode, existing
  hostname-based trusted-dreamer logic is unchanged — deployments that do not set this variable
  behave exactly as before. In `api` mode, the API key role is the sole authority for
  write/approve operations.
- **feat:** New module `mori_advisor/policy.py` — `Actor(key_name, role)` dataclass,
  `current_actor` ContextVar (set by `ApiKeyMiddleware` per request, reset on exit),
  `require_role(min_role)` helper that raises `PermissionDenied` on insufficient privilege.
  `can_read` / `can_write` / `can_approve` functions for REST-surface callers.
- **feat:** `require_role` applied as the first call in each privileged MCP tool:
  `memory_write`/`memory_import` → `write`; `memory_approve`/`memory_reject`/`memory_protect` →
  `dreamer`; `memory_delete`/`memory_rollback` → `write`.
- **feat:** `MORI_LOCAL_FULL_ACCESS=true` allows a nil actor (stdio transport) through in `api` mode
  for fully-trusted single-user deployments.
- **test:** `tests/test_policy.py` — 39 tests (26 SQLite + 13 Postgres) covering: read key denied
  for write/dreamer ops; write key denied for dreamer ops; dreamer key allowed for all; host mode
  no-enforcement (backward compat); nil actor fail-closed; `MORI_LOCAL_FULL_ACCESS` bypass; the
  **no-bypass proof** — the same under-privileged call is denied on both the MCP-tool surface
  (via `current_actor` ContextVar) and the REST surface (via `can_write`/`can_approve`).
- **docs:** `docs/reference/configuration.md` updated with new env vars and capability roles section.
- **note:** Audit logging is a planned dependency of #15. The write REST API (#14) and review queue
  (#15) will use the same `require_role` check; in `host` mode they fail closed automatically.

## v2.1.33 — MCP tool test coverage across both backends (issue #19)

- **Test:** Added `tests/test_mcp_tools.py` — 36 tests parametrised over SQLite
  and Postgres (72 cases total), covering every store-touching MCP tool in
  `mori_advisor/main.py`. The coroutine-scan helper `assert_no_coroutines()`
  recursively traverses each tool's return value and fails if any leaf is an
  unawaited coroutine, catching the exact class of bug from issue #12.
- **Store globals:** Monkeypatched via `monkeypatch.setattr("mori_advisor.main.store", ...)`
  (and the derived `memory_store`/`session_log` consistently). No DI refactor — a
  `# TODO` notes this as a future improvement.
- **External stubs:** NATS tools stubbed via `sys.modules["nats"]` replacement;
  bifrost/LLM stubbed via `monkeypatch`; ingestion pipeline stubbed with a fake
  returning a complete result dict so arg-parsing and the coroutine scan still run.
- **Bug-reintroduction check:** Temporarily restoring `async def parse_tags` in
  `PostgresStore` caused `test_memory_req[postgres]` to fail with
  `TypeError: 'coroutine' object is not iterable` while SQLite passed — confirming
  the suite catches issue #12's bug class. Reverted immediately after.
- **Bonus bugs found and fixed:** The suite also discovered three pre-existing schema
  mismatches in `PostgresStore` (wrong column names in `history`/`diff`/`rollback`
  and `pending_list`/`approve`/`reject`) — fixed as part of this PR.
- **CI:** `tests/test_mcp_tools.py` added to the `test-postgres` job so it runs
  against `postgres:16` on every push.

## v2.1.32 — Fix `/req` (`memory_req`) crashing on Postgres backend (issue #12)

- **Bug:** `memory_req` (the `/req` MCP tool) raised `TypeError: 'coroutine' object
  is not iterable` when called against a Postgres-backed instance. The error surfaced
  as a tool error visible to the caller, not as a "Database error: …" string.
- **Root cause:** `PostgresStore.parse_tags` was declared `async def` despite
  containing no awaits. `memory_req` called it bare — `store.parse_tags(raw)` — and
  then iterated the result with `for t in tags:`, which failed because the call
  returned a coroutine object rather than a list. The SQLite backend's `parse_tags`
  is correctly `def` (sync), so SQLite was unaffected.
- **Fix:** removed `async` from `PostgresStore.parse_tags` so it matches the
  `BaseStore` abstract signature (`def parse_tags`) and returns a list directly.
- **Regression test:** `tests/test_rest_api.py::test_pg_get_requirements_returns_list_not_coroutine`
  (gated on `MORI_TEST_DATABASE_URL`). The CI `test-postgres` job now also runs
  `tests/test_rest_api.py` so this class of regression is caught automatically.

## v2.1.31 — Dashboard connect modal: key-first, server URL optional

- With mori serving the dashboard same-origin (v2.1.30), the connect modal now leads with
  the **API key** — the only field needed in the common case. The server URL is an optional
  override (placeholder set to the live page origin; blank = this server), relevant only
  when hosting the page standalone against a remote instance.

## v2.1.30 — Mori serves the dashboard at its root

- **Why:** v2.1.29 shipped the dashboard as a *standalone* static file you had to serve
  yourself and point at an instance — an unusual shape for something called a "dashboard,"
  and a source of friction (which machine? which port? same-origin vs base URL?). A
  dashboard should just *be there* when you open the server.
- **What:** mori now serves the bundled dashboard at its **root URL** (`GET /`, an open
  path). Open `http://<host>:<port>/` in a browser, enter a key, and browse — the page is
  served same-origin, so it targets the very instance it loaded from (no base URL, no CORS
  config). `dashboard/` is now bundled into the image (`COPY dashboard/`); the deployment
  contract (`verify-deployment.py`) asserts `/` returns 200, so a missing page fails the
  gate instead of 404-ing silently. The standalone file remains for hosting elsewhere.

## v2.1.29 — Read REST API + standalone memory-browser dashboard

- **Why:** the shared store was MCP-only — no way for a human (or a non-agent tool) to
  browse memories without a Claude Code session. This adds a small read REST surface and a
  zero-dependency web UI that consumes it, so the store is browsable from any browser.
- **Read REST API (`mori_advisor/main.py`):** `GET /api/memories` (ranked FTS or recency —
  lean list shape via `search_json`, no body), `GET /api/memories/{name}` (full detail incl.
  body + provenance — the lazy-load companion; does **not** bump `retrieval_count`), and
  `GET /api/events` (session event log, newest first). All auto-guarded by `ApiKeyMiddleware`
  (`X-Api-Key`) and wrapped by `CORSMiddleware` (`MORI_CORS_ORIGINS`, default `*`) placed
  *outside* the auth layer so browser preflight isn't 401'd. New `get_memory()` on both
  backends returns an identical 12-key curated dict; `_json_safe_rows` converts Postgres
  `TIMESTAMPTZ`→ISO so JSON serialization can't 500.
- **Standalone dashboard (`dashboard/`):** one self-contained `index.html` (vanilla JS,
  inline CSS, no build step, no CDN) — search + browse memories, click a card to unfurl the
  full body + a provenance footer (created/updated/clients/tier/retrievals/freshness),
  lazy-fetched once and cached. Base URL + API key in `localStorage`; every store-derived
  string HTML-escaped. Runs via `python3 -m http.server` or any static host; points at any
  mori instance. Read-only — delete/write deferred.
- **Contract:** `scripts/verify-deployment.py` now probes all three read routes; the
  by-name probe is **dynamic** (discovers a real name from the list, `SKIP`s on an empty
  store) because `ApiKeyMiddleware` 401s unrouted `/api/*` paths too, so a static 200 probe
  would false-fail.
- **Fixes:** `/metrics` no longer hangs ~10s when NATS is unreachable (`collect_metrics`
  NATS connect now `allow_reconnect=False` + `asyncio.wait_for`); `GET /api/events` no
  longer 500s on Postgres (`TIMESTAMPTZ` now serialized via `_json_safe_rows`).

## v2.1.28 — Schema-migration runner + full-text search

- **Why:** the DB schema was defined ad-hoc across four sources (three SQLite bootstrappers
  + one Postgres `_DDL` string) with no recorded version, so the two backends had silently
  drifted (Postgres-only `delegate_tasks`, `dreamer_config.updated_at` missing on SQLite,
  `freshness_status` nullable on PG). And the guarded-`ALTER` approach could only add nullable
  columns — it had no safe path for the change recall actually needed: full-text search.
- **Migration runner (`mori_advisor/store/migrations.py`):** one ordered `MIGRATIONS` registry
  drives both backends with a `schema_migrations` version table (PK = id). `apply_sqlite` (sync,
  `BEGIN IMMEDIATE` + re-check-under-lock + retry-on-locked) and `apply_postgres` (async, one
  dedicated connection held for the run so the session-scoped `pg_advisory_lock` can't be dropped
  by a pool reset; standby guard). Migration 1 ("baseline") *invokes the existing bootstrap code*
  (not copied DDL), so fresh and populated production DBs converge identically. `ingestion_server`
  bootstrap moved out of import-time into its async lifespan.
- **Drift fixes (0003–0005):** `dreamer_config.updated_at` on SQLite; `delegate_tasks` SQLite
  parity; Postgres `freshness_status` backfilled + `SET NOT NULL DEFAULT 'unknown'`.
- **Full-text search (0006):** SQLite **FTS5** external-content + triggers (porter stemming,
  bm25 ranking, LIKE fallback if FTS5 absent); Postgres generated **`tsvector`** column
  (`COALESCE` + weighted `setweight`) + GIN, `websearch_to_tsquery` + `ts_rank`. Replaces the
  unranked LIKE/ILIKE `search()`; empty query → recency; tags stay structured. `_fts_query`
  builds the SQLite MATCH string programmatically (injection-proof). Vectors deferred (FTS is
  native in both backends; vectors are not).
- **CI:** new `postgres:16` service + dual-backend test job — the Postgres path was never
  CI-tested before. New `tests/test_migrations.py` + `tests/test_search.py`.

## v2.1.27 — `/brief --post-compact` delta re-grounding

- **Why:** the `PostCompact` hook fired a plain `/brief`, which re-injects the *entire*
  memory base right after compaction shrank the context — and runs the per-memory
  `check_freshness` LLM scan every time. At team scale that doesn't scale and buries the
  few things that actually changed. The dedicated flag was promised in v2.1.0 ("planned
  for v2.1; plain /brief is the interim approach") but never built. This implements it as
  a **delta-since-last-brief**.
- **`brief(post_compact=True, since=…)`**: a lean path that surfaces only what changed in
  shared state — new/updated memories (project + global), decisions **superseded** under
  you, and fresh evictions — then a one-line dream state. It **never** runs the freshness
  check, the full memory list, the standards dump, or the other-project index. Delta lists
  cap at 30 with a "…N more — run a full /brief" pointer (no silent truncation, no
  auto-escalation).
- **New store method `get_memories_changed_since`** (SQLite + Postgres, with an
  `updated_at` index on both) and a shared `normalise_since` helper that converts
  `6h`/`7d`/ISO-8601 to the stored UTC form — critical so a `T`-separated ISO string
  doesn't sort greater than every space-separated `updated_at` row and silently match
  nothing. Exclusive `> since` bound; documented as best-effort re-grounding, not
  exactly-once consumption.
- **`since` is session-aware**, resolved client-side by the `/brief` skill: a
  `.mori-last-brief` marker (stamped every brief) → session-start → the server default
  window `MORI_POST_COMPACT_WINDOW` (`6h`).
- **Hook + installers**: the `PostCompact` shipper now instructs `/brief --post-compact`.
  The Cline and Cursor installers gained PostCompact parity (deploy the brief shipper +
  register the hook), matching Claude Code and Antigravity.
- **Tests**: new pytest suite (`tests/`) covering `normalise_since`, the delta store
  method (boundary + scoping), and the guarantee that the post-compact path skips
  `check_freshness`. CI now runs pytest alongside ruff.
- **Docs fix**: corrected the v2.1.0 hook path (`~/.claude/mori-post-compact-brief.sh`,
  was wrongly documented under `~/.claude/hooks/`).

## v2.1.26 — Reboot-safe GCE deployment

- **Why:** a reboot re-runs the startup script, which fetched every secret from Secret Manager
  and `exit 1`'d if `MORI_PG_PASSWORD` came back empty. A single denied/unreachable secret would
  therefore take a *running* instance down on the next reboot — and a hardcoded `useradd -u`
  that mismatched the live user's uid tainted the rootless Podman storage on rebuild.
- **Env reuse on reboot:** `startup.sh.tpl` now reads the persisted `/data/mori-advisor/.env`
  (written first boot, also CD's source of truth) and recovers the pg password from
  `MORI_DATABASE_URL` — **Secret Manager is consulted only on first boot** (no `.env` yet). A
  reboot now survives with Secret Manager denied or unreachable. The `.env` is written once on
  first boot and reused untouched thereafter (preserving CD/manual updates).
- **Self-consistent uid:** dropped the hardcoded `useradd -u 10001`; the user gets a
  system-assigned uid and the Postgres `pgdata` owner is derived from `/etc/subuid`
  (`subuid_base + 998` → container uid 999), so user / storage / pgdata can't drift on a rebuild.
- **`main.tf`:** `MORI_PG_PASSWORD` is now a managed `google_secret_manager_secret` with a
  `secretAccessor` binding for the service account — previously created out-of-band with no
  durable IAM grant, which is how the access silently lapsed. (Existing projects: `terraform
  import google_secret_manager_secret.mori_pg_password MORI_PG_PASSWORD`.)
- Verified: both templates render (`templatefile`) and the rendered scripts pass `bash -n`.

## v2.1.25 — Manage GCE app containers with systemd Quadlet

- **Why:** the run-spec for `mori-advisor`/`mori-ingestion` lived in *two* places — the GCE
  startup script and `cd.yml` — and drift between them was the root cause of the v2.1.16 404
  incident (rootful vs rootless). It also blocks horizontal scaling (multiple ingesters/dreamers).
- **Quadlet units (`deploy/gcp/quadlet/`)**: `mori-advisor`, `mori-ingestion`, `mori-msg` are now
  rootless **systemd Quadlet** `.container` units — one declarative source of truth for how each
  runs. `Restart=always` + `StartLimitIntervalSec=0` (never give up) + a `/dev/tcp` Postgres
  readiness gate in each unit. `dream` cron → `dream.service` + `dream.timer` (every 4h).
- **`startup.sh.tpl` + `main.tf`**: Terraform injects the checked-in unit files **verbatim**
  (`file()` → `templatefile`), so the VM's unit *is* the repo file — the duplication is gone.
  Startup installs the units, brings up the `mori` user manager, `daemon-reload`, starts the
  units, enables `dream.timer`, and removes the legacy crontab line.
- **`cd.yml`**: deploy is now `podman pull` (with a `previous-rollback` tag) + retag `:latest` +
  `systemctl --user restart mori-advisor mori-ingestion mori-msg`. The imperative
  `podman run --replace` blocks and the rootful-stray guardrail are removed. Health + deployment
  contract gates unchanged.
- **Scope:** `mori-pg` stays an imperative `podman run --restart=always` — a stable stateful
  singleton defined in one place, with no Quadlet scaling benefit (and the one data-holding
  container is left in its proven form). Any optional gateway sidecar is likewise out of scope.
- **Verified live on GCE:** all five containers up; the three app units `active`; deployment
  contract PASS; `systemctl --user restart` (CD's mechanism) works. Reboot-survival hardening is
  tracked separately in #7.

## v2.1.24 — Capture assistant reasoning from the Stop hook

- **Reasoning capture:** lifecycle hooks recorded tool calls and user prompts but never the
  assistant's text responses — the plans, analysis, and decisions. Those are now captured.
- **Client (shippers):** on `Stop`, `mori-ship-event.sh` / `.ps1` attach a bounded,
  base64-encoded tail of the session transcript (`transcript_tail_b64`). Pure `tail`+`base64`
  (bash) / native `Get-Content`+`ConvertFrom-Json` (PowerShell) — no python/jq dependency, so
  it works on bare macOS, Linux, and Windows. Any failure ships the original event unchanged.
- **Server:** `_extract_assistant_text()` parses the tail, walks back to the last user-text
  line (treating `tool_result` user lines as part of the turn, so post-tool reasoning is kept),
  skips `isSidechain` subagent lines, and joins the turn's assistant `text` blocks (+ `thinking`
  iff `MORI_CAPTURE_THINKING=true`). Stored in `session_events.assistant_text`.
- **Dream:** `_format_events` surfaces the captured reasoning on Stop events so it's distilled
  into memories alongside prompts and tool calls.
- **Migration:** `assistant_text` column added to `session_events` (SQLite guarded `ALTER`;
  Postgres `ADD COLUMN IF NOT EXISTS`). No new endpoints, MCP tools, or installer allowlist
  changes — re-running the installer redeploys the updated shipper.

## v2.1.23 — Deployment contract gate (UAT ⇄ PRD parity)

- **`scripts/verify-deployment.py`**: single source of truth for "is this instance serving
  correctly?" — asserts open routes (`/health`, `/ready`, `/metrics`) return 200 and every
  auth-guarded feature route returns 401 without a key and 200 with one (registered + auth
  enforced + key accepted). Stdlib-only so it runs inside the slim image.
- **`cd.yml`**: after the health gate, CD runs this script *inside the freshly-deployed
  container* against its own endpoint and **fails the deploy** if the route surface is wrong.
  Previously CD only checked `/health` — which passed even while feature routes 404'd, so
  broken deploys reported green. A tag that passes the contract in UAT now reproduces the same
  surface in PRD because CD enforces the identical assertions.
- **UAT** runs the same script against both backends pre-tag (replaces an inline check). New
  `custom_route` paths are added to `verify-deployment.py` once — both gates pick them up.

## v2.1.22 — Unify deploy on rootless runtime + env-file single source of truth

- **Root-cause fix for the production `/api/git/*` 404s**: the GCE startup script ran
  containers ROOTLESS (as the `mori` user) while CD ran them ROOTFUL (`sudo podman`). The two
  Podman stores are mutually invisible, so each CD spun up a parallel rootful container that
  fought the rootless one for host port 8968 — whichever bound first served; the other
  crash-looped. The rootful copy was also under-configured (auth off, no bifrost/NATS).
- **`cd.yml`**: deploy ROOTLESS as the `mori` user via `sudo su - mori`, matching the startup
  script. `podman run --replace` (atomic — blue/green is impossible under `--network=host`).
  Preflight guardrail removes any stray rootful `mori-*` container. CD reads
  `/data/mori-advisor/.env` and needs no secret values.
- **`startup.sh.tpl`**: write the COMPLETE runtime env to `/data/mori-advisor/.env` (single
  source of truth shared with CD — the two paths can no longer drift). Source `MORI_API_KEYS`
  from Secret Manager — it was previously never set, so clean rebuilds came up in open-auth mode.
- **`post-push.sh` / `post-push.ps1`**: validate the git watermark resolves to a real commit
  before use; fall back to `HEAD~20` on force-push, rebase, fresh clone, or a stale/foreign SHA
  (previously errored silently and ingested nothing).
- **UAT**: `start-uat.sh` now runs a custom-route surface check (401 without key, non-404 with
  key) on both SQLite and Postgres backends — the exact assertion that would have caught the
  404 regression the standard smoke test missed.

## v2.1.21 — Fix CD port conflict + switch to http_app() ASGI startup

- **`cd.yml`**: Before blue/green deploy, kill any process on ports 8968/8969 that
  isn't tracked by rootful Podman (e.g. a startup-script rootless container from a VM
  reboot holding the port and causing every CD attempt to crash in a restart loop).
- **`mori_advisor/main.py`**: Switch from `mcp.run(middleware=...)` to building the
  ASGI app explicitly with `mcp.http_app()` and serving it with `uvicorn.run()` — the
  pattern the FastMCP docs recommend for production. This makes custom_route registration
  unambiguous and decouples the server lifecycle from FastMCP's transport abstraction.

## v2.1.19 — Move Dockerfile to Python 3.13

- **`Dockerfile`**: Change `PYTHON_VERSION` from `3.14.5` to `3.13.4`. FastMCP 3.2.0 has a
  `custom_route` registration bug on Python 3.14 where routes defined after a certain index in
  the handler list silently fail to register. Python 3.13 is the current supported release with
  active security patches — not the EOL-approaching 3.12 we briefly tried. UAT masked this
  because local builds use the system Python (3.12 on typical dev machines). Production was running Python 3.14
  (Dockerfile default), which explains why all new endpoints returned 404 despite the code being
  present in the image.

## v2.1.18 — Pin FastMCP to 3.2.0

- **`requirements.txt`**: Pin `fastmcp==3.2.0`. FastMCP 3.3.1 silently dropped newly-registered
  `custom_route` handlers after a certain point in the handler list — only routes present before
  v2.1.16 were reachable. UAT masked this because the local build resolved 3.2.0 while GitHub
  Actions resolved the latest compatible (3.3.1). (Root cause turned out to be Python 3.14 in the
  Dockerfile, not the FastMCP version — see v2.1.19.)

## v2.1.16 — Git commit ingestion + consult output capture

- **`POST /api/git/ingest`**: New endpoint that ingests git commit messages from a post-push
  hook. Each commit is written as a working-tier project memory tagged `project:<repo>` and
  `pusher:<client>`. Server-side dedup via `ingestion_log.source_hash` makes repeated calls
  idempotent. Watermarks are per `(repo, ref)` so pushes to different branches maintain
  independent ingestion state.
- **`GET /api/git/watermark`**: Narrowly-scoped endpoint that returns the last ingested
  commit SHA for a given `(repo, ref)`. Used by post-push hooks to compute the commit range
  without exposing the full dream state keyspace.
- **`scripts/post-push.sh` / `post-push.ps1`**: Extended with a git commit ingestion block.
  API key is sourced from `~/.claude/.secrets` at push time (not the shell profile). Commit
  body text is included alongside the subject. Output: `[mori] ingested N commit(s) from
  <repo>/<branch>` on success. Hook always exits 0 — never blocks a push.
- **Consult output capture**: Every successful `consult_advisor` call now writes the question,
  focus, and advisor response as a working-tier project memory tagged `consult` and
  `advisor-output`. The dream pipeline reviews and promotes advice that was followed; advice
  that was superseded ages out naturally. Set `MORI_CONSULT_CAPTURE=false` to opt out.

## v2.1.15 — Postgres-first GCP deployment

- **Postgres in GCP startup script**: `deploy/gcp/startup.sh.tpl` now starts a Postgres 16
  container bound to `/data/postgres/pgdata` on the persistent disk as part of the standard boot
  sequence. Postgres data survives VM stops and rebuilds — named container volumes are no longer
  used for stateful data.
- **`MORI_REQUIRE_POSTGRES`**: New env var — if set to `true`, mori-advisor aborts at startup
  when Postgres is unreachable, preventing silent fallback to SQLite. Recommended for all team
  and GCP deployments.
- **`pg_isready` startup gate**: mori-advisor will not start until Postgres accepts connections
  (30×2s timeout with fatal exit). Eliminates the previous race condition where the server could
  start against an unavailable database.
- **pg_dump backup cron**: Daily `pg_dump` to GCS replaces the SQLite Litestream backup cron
  in the GCP deployment path. Backups use GCE metadata server auth — no credentials in env vars.
- **Credentials via Secret Manager**: GCP deployment fetches the Postgres password from GCP
  Secret Manager at boot and writes `MORI_DATABASE_URL` to `/data/mori-advisor/.env` on the
  persistent disk. No credentials in the startup script or repository.
- **Tailscale state preserved across rebuilds**: Startup script restores Tailscale state from
  the persistent disk so the VM retains its Tailscale identity after a rebuild.
- **SSH host keys preserved across rebuilds**: Startup script restores SSH host keys from the
  persistent disk to prevent host-key warnings after VM recreation.
- **skills/brief: remove dead `mori-config` pull step**: The `git -C ~/mori-config pull` step
  in the `/brief` skill was a leftover from an earlier config management approach. Removed from
  both `mori/skills/brief/SKILL.md` and the installed skill files.

## v2.1.14 — Fix Windows Installer Hook Format & Session-Based Auth

- **Windows Installer Hook Format Fix**: Fixed a bug in `scripts/install-mori-claude.ps1` where the `PostToolUse` event hook was missing the `matcher` field (e.g. `matcher: "*"`), which caused Claude Code to reject the generated configuration. The installer now matches the correct hook wrapping behavior of `install-mori-claude.sh`.
- **Session-Based Auth Bypass**: Added an in-memory session tracker `_AUTHENTICATED_SESSIONS` to `ApiKeyMiddleware` to bypass API key headers validation for subsequent POST/DELETE requests belonging to successfully pre-authenticated SSE connections (fixing connection handshake `401 Unauthorized` issues on IDE restart).

## v2.1.13 — Native Prometheus /metrics Exposition

- **Native Prometheus Exposition**: Replaced `/metrics` endpoint implementation with a native Prometheus exposition format (`text/plain; version=0.0.4` / OpenMetrics compatibility) using `prometheus_client` directly, allowing direct scraping by homelab Prometheus instances without an intermediate OTel collector.
- **Pluggable Database Count & Filters**: Extended the memory store interfaces (`count()`, `pending_count()`, `count_messages()`, `count_ingestion()`) for both SQLite and Postgres backends to support filtering (such as memory tier, protection status, message status) directly in the database queries.
- **OTel Backward Compatibility**: Preserved OpenTelemetry gauges update flow inside the `/metrics` endpoint scrape handler, ensuring any configured background push exporter continues receiving metrics updates.

## v2.1.12 — Fix MCP Session Auth Bypass for SSE POST/DELETE Requests

- **Session-Based Auth Bypass**: Added an in-memory session tracker `_AUTHENTICATED_SESSIONS` to `ApiKeyMiddleware`. Once a client successfully authenticates via API key on the initial SSE GET request, subsequent POST/DELETE requests belonging to that session ID bypass the API key header check, fixing `401 Unauthorized` errors on clients that fail to propagate custom headers.

## v2.1.11 — Postgres UAT Dream Run Fixes & Savepoint Isolation

- **Postgres Savepoint Isolation**: Wrapped each `_write_memory()` call in the dream pipeline inside a nested transaction (savepoint) using `async with txn_conn.transaction():` when running on Postgres (`asyncpg`). This prevents individual database write failures (such as unique key constraint violations) from aborting the entire transaction block, ensuring successful memory writes persist and the watermark advances cleanly.
- **Dream datetime fix**: `dream.py` event grouper was slicing `TIMESTAMPTZ` values returned by asyncpg as `datetime` objects — not strings — causing `dream_run` to crash with `TypeError: 'datetime.datetime' object is not subscriptable`. Fixed to use `.isoformat()` when the value has that method.
- **Database Seeding Sequence Reset**: Added automatic primary key sequence resetting to `start-uat.sh` immediately following the `pg_dump` seed step. Resets sequences to `COALESCE(max(id), 1)` for `memories`, `memory_versions`, `pending_writes`, `ingestion_log`, `session_events`, and `delegate_tasks` to prevent constraint conflicts on subsequent insertions.
- **Smoke Test Robustness**: Upgraded `smoke-test.sh` to dynamically report check keys and handle JSON parsing errors robustly, and to gracefully output and skip display for `db_write` (marked as `skipped`) instead of treating it as a test failure when run against the Postgres backend.
- **APP_PORT**: `mori_advisor/main.py` server port is now configurable via `APP_PORT` env var (defaults to 8968). Enables side-by-side UAT instances without rebuilding the image.

## v2.1.10 — Antigravity Installer Profile Parity & PostCompact Hook

- **Target Selection**: Added `--target cli/ide/both` (Bash) and `-Target cli/ide/both` (PowerShell) option to installers, directing MCP config (`mcp_config.json`) and hooks (`hooks.json`) to `~/.gemini/antigravity` (CLI), `~/.gemini/antigravity-ide` (IDE), or both. Default in headless mode is `ide`.
- **PostCompact Hook**: Deployed `mori-post-compact-brief` shipper script and registered the `PostCompact` hook in Antigravity's `hooks.json` configuration, matching Claude Code installer capability to trigger automatic re-grounding via `/brief`.
- **Robust Skill Parsing**: Upgraded the PowerShell skill installer `Deploy-MoriSkills` to support both standard YAML frontmatter blocks (`---`) and bulleted headers (`- name:`).
- **Symlink Diagnostics**: Upgraded the `--doctor` diagnostics in `mori_antigravity_install.py` and `install-mori-antigravity.ps1` to detect and print remediation instructions when the `~/.gemini/config` symlink points to a mismatching variant.

## v2.1.9 — Fix Postgres brief() interface mismatches

- **`get_memories_by_project`**: Rewrote `PostgresStore.get_memories_by_project()` to return the correct three-key dict (`project_memories`, `global_memories`, `other_projects`) matching the SQLite spec. The previous implementation returned `{name: dict}` which caused a `KeyError('project_memories')` in `brief()`.
- **`check_freshness`**: Fixed `PostgresStore.check_freshness()` — was calling `await llm_consult(dict(row))` (wrong: sync function, wrong argument shape, wrong return shape). Now fetches all rows first, releases the connection, calls `llm_consult(system=..., user=..., vk="fast", ...)` synchronously per row with a fresh connection for each write, and returns `{checked, fresh, stale, no, errors}`.

## v2.1.8 — Async Postgres Ingestion & Security Hardening

- **Async Ingestion Pipeline**: Converted `IngestionPipeline` execution flow and ingestion tasks to `async def` and integrated the dynamic `_a()` helper to resolve and await asynchronous `PostgresStore` writes/logs.
- **Async Contradiction Scans**: Refactored `run_contradiction_scan` to be async-native. Under Postgres, it operates inside non-blocking database transactions to perform updates and queue eviction notices.
- **MCP Endpoint Security**: Removed `/mcp` from `OPEN_PATHS` in `ApiKeyMiddleware`, requiring a valid API key for all MCP connections and tool invocations, and added query-based API key support (`api-key` / `api_key`) to support cloud-discovered Claude Code clients.
- **NATS Timeout in Smoke Test**: Wrapped `nats.connect` inside `asyncio.wait_for` with a 2.0 second timeout to prevent the health check/smoke endpoint from hanging indefinitely during auth failures.
- **UAT & Installer Verification**: Verified local UAT execution against the Postgres standby node, resolved a double-quoting JSON parsing issue and a missing matcher field in the Claude Code settings installer, and confirmed full installer idempotency.

## v2.1.6 — Fix Postgres dream transaction poisoning

Fix: PostgresStore.write() uses SELECT-then-INSERT, which causes
`UniqueViolationError` when the dream model produces duplicate memory names.
One error poisons the entire transaction, losing all 12+ memories and the
watermark update.

Replaced with `INSERT ... ON CONFLICT (name) DO UPDATE` — matching SQLite's
atomic upsert. Also adds origin array merging, canonical tier preservation,
and protection flag preservation on update.

## v2.1.0 — Named API key authentication + PostCompact re-grounding

### New: PostCompact re-grounding hook

A `PostCompact` hook (`~/.claude/mori-post-compact-brief.sh`) is now installed
alongside the other Mori lifecycle hooks. It fires after every context compression
and injects a prompt instructing the agent to run `/brief` — re-establishing NATS
messages, pending mori-msg items, and session state from before compaction.

Enabled by default. Opt out with `MORI_POST_COMPACT_BRIEF=false`.

A dedicated `/brief --post-compact` flag that pulls the compact summary directly
is planned; plain `/brief` is the correct interim approach.

### New: per-client named API keys

Mori now authenticates every request at the transport layer — MCP tools, event
endpoints, and the dream trigger — using named API keys. Previously only 4 HTTP
endpoints were protected by a single shared key; the entire MCP surface was open.

**Key format:** `MORI_API_KEYS=name:secret,name:secret,...`

Each client gets its own named key. The name appears in logs and audit trail.
Secrets are 32-byte hex strings generated via `python3 -c "import secrets; print(secrets.token_hex(32))"` or the new `mori-key_generate` MCP tool.

**New modules:**
- `mori_advisor/auth.py` — key loading, `check_key()` with `hmac.compare_digest`, `generate_key()`
- `mori_advisor/middleware.py` — Starlette `BaseHTTPMiddleware`; applied via `mcp.run(middleware=[...])`

**Open paths** (always accessible, no key required): `/health`, `/ready`, `/metrics`

**Open mode:** if no keys are configured, the server starts with a warning and
accepts all connections — preserves backward compatibility for Tailscale-only
deployments.

**Backward compat:** existing `MORI_ADVISOR_API_KEY` deployments continue working
without config changes — the single key is loaded as `{"legacy": <key>}`.

**New MCP tool:** `mori-key_generate name="clientname"` — generates a secret and
returns the line to add to `MORI_API_KEYS`.

**Smoke test:** `/api/smoke` now includes an `auth` check showing configured client names.

**Migration:** see [docs/reference/configuration.md — Authentication](docs/reference/configuration.md#authentication).

---

## v2.0.0 — Dual-backend store (SQLite + PostgreSQL)

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/f4fee0826da3ab8b234f8677fa8f96f37ce07e88/docs/assets/header-blank.svg)

### New: pluggable persistence layer — SQLite (solo) or PostgreSQL (team)

Mori now supports PostgreSQL as a drop-in replacement for SQLite, selected at
runtime via `MORI_DATABASE_URL`. SQLite remains the default — zero breaking
change for existing deployments.

**Why this matters:** solo deployments stay on SQLite (no deps, no ops). Team
deployments with concurrent dream runs, PITR backups, or multi-pod write
contention activate PostgreSQL by setting one environment variable.

**New modules:**
- `mori_advisor/store/` — `BaseStore` ABC, `SQLiteStore` (delegation wrapper over
  existing `MemoryStore` / `SessionLog` / `MsgStore`), `PostgresStore` (asyncpg pool)
- `mori_advisor/store/__init__.py` — `get_store()` factory, selects backend from env
- `mori_advisor/cli/export.py` — dump SQLite to JSONL (dependency-safe order, WAL flush)
- `mori_advisor/cli/import_.py` — load JSONL into either backend (idempotent, type-coerced)

**All callers updated:** `main.py`, `dream.py`, `ingestion.py`, `ingestion_server.py`,
`utils.py` — store layer injected via `store=` kwarg, `db_path=` fallbacks preserved.

**PostgreSQL notes:**
- asyncpg pool, `statement_cache_size=0` (pgBouncer session mode compatible)
- JSONB for tag arrays, TIMESTAMPTZ for all timestamps
- Serialization errors (SQLSTATE 40001) retried up to 3× with exponential backoff
- `asyncpg` is optional — not required for SQLite deployments

**Deploy directory restructured:**
- `deploy/solo/` — SQLite posture (Docker Compose, replaces `deploy/homelab/` for Docker users)
- `deploy/team/` — PostgreSQL + pgBouncer (Docker Compose, WAL-G documented)
- `deploy/homelab/` — retained for backward compatibility (raw Podman + systemd units)

**Migration:** export from SQLite, import to Postgres, verify counts match, flip
`MORI_DATABASE_URL`. Rollback: remove the variable, restart — SQLite file untouched.
See [docs/reference/team-configuration.md](docs/reference/team-configuration.md).

**UAT results:** 68/68 memories, 5006/5006 session events verified across both
backends verified before tagging.

---

## v1.1.0 — Inter-agent messaging (mori-msg)

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/f4fee0826da3ab8b234f8677fa8f96f37ce07e88/docs/assets/header-blank.svg)

### New: `mori-msg` — addressed, typed, reply-threaded messages between agents

Agents can now delegate tasks, ask questions, and share decisions across the device network without a shared session. Messages are picked up at the next `/brief` — no mid-session push, no extra infrastructure.

**New MCP tools:** `mori-msg_send`, `mori-msg_recv`, `mori-msg_thread`

**New daemon:** `mori_advisor/msg_daemon.py` — long-running durable JetStream pull consumer. Same image as `mori-advisor`, different entrypoint. Sole writer to `msg.db`; dispatches by type:
- `decision` → written directly to `memory_store` (no human session needed)
- `task` → persisted + auto-acked; appears in next `/brief`
- `question` / `broadcast` → persisted for `/brief` pickup
- `done` / `ack` / `reply` → update referenced message status

**Infrastructure:** new `MORI_MSG` JetStream stream (`mori.msg.*` + `mori.reply.*`, 7-day retention). Separate `msg.db` (not `memories.db`) — sole writer is the daemon, clean WAL ownership.

**Updated pod stack:** `mori-advisor` (8968) + `mori-ingestion` (8969) + `mori-dream` (internal) + `mori-msg` (internal daemon)

**Skills:** `/brief` calls `mori-msg_recv(unacked=True)` at session start; `/wrap` broadcasts session summary to `mori-msg`; new `/msg` skill for direct inbox/send/thread use.

**Opt-in headless CC:** `MORI_MSG_HEADLESS_ENABLED=true` + `MORI_MSG_HEADLESS_TRUSTED=<hostnames>`. Off by default.

---

## v1.0.0 — AGPL-3.0 licence, defensive publication

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/97ee8bb6b52ba12cabcb6ce308a75ce12f7367c5/docs/assets/header-blank.svg)

### Licence: MIT → AGPL-3.0

Mori is now released under the [GNU Affero General Public License v3.0](LICENSE).

Under AGPL-3.0, if you run Mori as a network service and modify the source code, you must release those modifications under AGPL-3.0. A commercial licence removes this requirement — see [COMMERCIAL.md](COMMERCIAL.md).

### Defensive publication — prior art established

[DISCLOSURE.md](DISCLOSURE.md) is a formal technical disclosure establishing prior art for the inventions in Mori: the dream pipeline, PreCompact synchronous distillation, multi-instance memory coherence, three-tier memory lifecycle, trusted dreamer governance, universal ingestion pipeline, and git push cross-instance notification. Published to prevent third-party patenting of these methods.

### What v1.0 represents

Mori has been running in production across a multi-device homelab since May 2026, accumulating 5,000+ session events and 60+ canonical memories across Claude Code, Cursor, and Cline instances. The 1.0 milestone reflects a stable core:

- **Dream pipeline** — automatic session distillation via lifecycle hooks
- **Session grounding** — `/brief` loads shared memories at session start
- **Universal ingestion** — PDFs, images, transcripts, git history → memories
- **Cross-device messaging** — NATS pub/sub, `/wrap`, git push notifications
- **Governance** — trusted dreamers, pending write approval, full version history
- **Smoke test** — `/api/smoke` endpoint for pre-deploy verification

---

## v0.1.14 — Fix GitPush NATS publish

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/ea4eb044f8c22bff2ea064cb7aec75a41f1d1303/docs/assets/header-blank.svg)

### Fix: `asyncio.create_task` GC bug in GitPush NATS publish

`asyncio.create_task(_nats_publish_git_push(...))` discards the task reference — Python only holds a weak reference, so the task is garbage collected before it runs and the NATS message is never sent. Changed to `await _nats_publish_git_push(body)`. Also removes the now-redundant local `import asyncio` inside `nats_sub` (moved to module level in v0.1.13).

---

## v0.1.13 — Git push NATS notification

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/842fbfb3912db78e52a2e6a692e4f3f5bc3fff95/docs/assets/header-blank.svg)

### New: git push NATS notification hook

When you push to any git repo with the hook installed, a `GitPush` event is published immediately to NATS — so every other active Claude Code instance sees the push in real time via `/nats sub` and `/brief` replay.

**New files:**
- `scripts/post-push.sh` / `scripts/post-push.ps1` — the hook itself; always `exit 0`, fire-and-forget
- `scripts/install-git-hooks.sh` / `scripts/install-git-hooks.ps1` — one-command install per repo
- `docs/reference/git-hooks.md` — installation guide

**Server change (`main.py`):**
- `_nats_publish_git_push` helper — publishes to `cc.<client>` immediately on receipt, bypassing the dream pipeline for instant cross-device visibility
- `/api/events/raw` handler — fires the NATS publish via `asyncio.create_task` after logging `GitPush` events

**Install:**
```bash
# From the mori repo root
./scripts/install-git-hooks.sh

# Other repos
./scripts/install-git-hooks.sh --repo ~/path/to/your-other-repo
```

Set `MORI_URL`, `MORI_API_KEY`, `MORI_CLIENT` in your environment — see `docs/reference/git-hooks.md`.

---

## v0.1.12 — NATS import fix

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/1eb4fa8efffcc66643da9ad3ad85ad70319629283/docs/assets/header-blank.svg)

### Fix: `TimeoutError` import path

`nats.js.errors` does not export `TimeoutError` — caused `ImportError` on deploy (Python 3.14). Changed to `nats.errors.TimeoutError`.

---

## v0.1.11 — `/wrap` skill, NATS replay fix

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/559229efffcc66643da9ad3ad85ad70319629283/docs/assets/header-blank.svg)

### New `/wrap` skill

Session wrap-up as a single command — captures work before a release. Runs the full sign-off sequence:

- **Summarise** — writes a concise session summary
- **cc-share** — publishes to cross-session storage (7-day TTL)
- **NATS** — broadcasts one-liner to the message bus
- **Dream** — flushes undreamed events to durable memory

Use before every release tag to avoid losing session context when the MCP server restarts.

### NATS replay fix

`nats_sub(replay=True)` silently returned "No NATS messages" because the `cc` JetStream stream was never created. The replay branch now auto-creates the stream on first call and cleans up ephemeral consumers after each read.

Includes the lint fix from CI: removed unused `StreamConfig` import and ruff-organised inline imports.

---

## v0.1.9 — /update skill deployment fixed

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/89af2974c249b473e426199e3e574c05c4119364/docs/assets/header-blank.svg)

### `/update` skill deployment — three fixes

The `/update` MCP tool generates shell commands to deploy skills to all Claude profile
directories on a target device. Three bugs prevented it from working at all.

**Skills now shipped in the Docker image**

`skills/` directory was not included in the Dockerfile `COPY` — `MORI_SKILLS_DIR` was
unset and `_list_skills()` always returned an empty list. Fixed:

```dockerfile
COPY skills/ ./skills/
ENV MORI_SKILLS_DIR=/app/skills
```

**Correct subdirectory format**

Skills were stored as flat files (`skills/brief.skill.md`) but Claude Code expects and
`_list_skills()` looks for subdirectory format (`skills/brief/SKILL.md`). All 7 skill files
renamed to match:

```
skills/brief/SKILL.md
skills/consult/SKILL.md
skills/dream/SKILL.md
skills/ingest/SKILL.md
skills/nats/SKILL.md
skills/pensieve/SKILL.md
skills/req/SKILL.md
```

**Bash generation for Linux devices**

`_update_all()` always emitted PowerShell syntax regardless of device family, producing broken
commands for Linux targets. Fixed with a family branch — Linux devices now get
bash heredoc commands; Windows devices retain PowerShell output.

**Usage after this release:**

```
/update my-linux-device all   → pasteable bash that deploys all 7 skills to 4 profile dirs
/update my-windows-device all  → pasteable PowerShell equivalent
```

---

## v0.1.8 — Project-scoped /brief, dream auto-tagging

![Mori — A shared memory layer for AI coding agents](https://raw.githubusercontent.com/fjwood69/mori/89af2974c249b473e426199e3e574c05c4119364/docs/assets/header-blank.svg)

### Project-scoped `/brief`

`/brief` previously loaded all memories up to a hard cap of 50 — bifrost sessions got mori
memories they didn't need, and busy projects lost relevant memories to truncation. Project
scoping fixes both problems simultaneously.

**Three new `/brief` invocations:**

| Command | Effect |
|---|---|
| `/brief` | Unscoped — existing behaviour, all memories up to cap |
| `/brief --project <name>` | Scoped to a project — right memories in full |
| `/brief --auto` | Auto-detect project from working directory |

**Three-bucket loading (scoped mode):**
- **Project memories** — canonical always in full; working ≤14 days in full; working >14 days as summary only
- **Global memories** — `scope:global`, `scope:cross-project`, type `profile`/`pattern` — always loaded regardless of project
- **Other-project index** — one line per project with count; cross-project awareness without loading cost

Output header:
```
**Mori Brief — project: mori** (23 project + 18 global memories)
153 memories from other projects — /pensieve to explore
```

**Implementation:**
- `memory_store.get_memories_by_project()` — all filtering pushed to SQLite; no superset-then-filter in Python
- `brief()` MCP tool gains `project`, `include_global`, `include_index` parameters
- Requirements filtered to current project when scoped
- Graceful fallback to unscoped on any exception

### Dream pipeline auto-tagging

New memories written by the dream pipeline are now automatically tagged `project:<name>` based
on the working directory of the session that produced them.

**Resolver chain** (first match wins):
1. `.mori-project` file — place at repo root (or any parent) with the project name as content
2. `MORI_PROJECT` environment variable — for CI or non-interactive shells
3. `git rev-parse --show-toplevel` — uses the git repository root directory name as fallback

New methods on `DreamPipeline`: `_resolve_project(cwd)` and `_extract_project_from_events(events)`.

### Backfill migration script

`scripts/backfill_project_tags.py` — one-time idempotent pass to tag existing memories.
Maps name prefixes to project tags (`project-mori-*` → `project:mori`, etc.) and adds
`scope:global` to profiles, patterns, and cross-cutting memories. Safe to re-run.

```bash
python scripts/backfill_project_tags.py /data/mori-advisor/memories.db --dry-run
python scripts/backfill_project_tags.py /data/mori-advisor/memories.db
```

### Docs

- `docs/for-teams.md` — new **Project scoping** section: commands, resolver chain, backfill instructions, cost-annotated example configs updated with actual `--auto` / `--project` commands
- `docs/reference/slash-commands.md` — `--project` and `--auto` flags documented with cost table

---

## v0.1.4 — Remote client ingestion

### Remote client ingestion (`mori_ingest_content`)

Solves the remote-server boundary problem. `mori_ingest` resolves paths
server-side — unusable when mori-advisor runs on GCE and the client is on a
different machine. `mori_ingest_content` flips the model: the MCP client
(Claude Code, Cursor, etc.) reads files locally, base64-encodes them, and
sends bytes over the wire. The server processes in memory.

- **`Chunk.from_content()`** — create chunks from raw bytes rather than filesystem paths
- **`parse_content()`** on all 5 parsers (text, PDF, image, transcript, git) — in-memory
  extraction; git parser accepts pre-collected `git log --patch` stdout as `text/x-git-log`
- **`IngestionJob` + `_run_pipeline()`** — shared execution engine used by both
  `ingest()` (path-based) and `ingest_content()` (wire-based); no logic duplication
- **`_parser_for_mime()`** — MIME routing table maps content types to registered parsers
- **New MCP tool**: `mori_ingest_content` — accepts `[{name, content_b64, mime_type}]`
- **`/ingest` skill updated** — dual-mode: resolves paths locally, reads + encodes files,
  calls `mori_ingest_content`; batches ≤20 files/call; git log collected client-side
- **Dedup**: SHA256 computed from decoded bytes; `source_uri = "<content:name>"`
- **Allow lists**: `mcp__mori__mori_ingest_content` added across all bridge installers


## v0.1.3 — Universal Ingestion, model refactor, shared utilities

### Ingestion pipeline (`/ingest`)

Cold-start problem solved. Feed Mori any source material — PDFs, screenshots,
CC transcripts, git history, plain text — and the pipeline extracts durable
memories into the shared store using the same distillation logic as dream.

- **5 parsers**: text/code, PDF (pymupdf preferred, pypdf2 fallback), image/vision
  (Pillow → base64 → Kimi K2.6 via OpenAI Vision format), CC transcripts (.jsonl
  with `--since` filter via first-event timestamp), git history (`git log` +
  diffs via subprocess)
- **Three-tier execution**: preview (parse-only, zero-cost), dry-run (full LLM
  but no writes), ingest (commits everything)
- **Persistence**: `ingestion_log` table with SHA256 dedup, `--force` to re-ingest
- **Cost guard**: `--max-cost` per-source with token estimation (heuristic — not
  pixel-perfect for image-heavy PDFs)
- **Focus extraction**: architecture, decisions, conventions, gotchas
- **New MCP tools**: `mori_ingest`, `mori_ingest_status`, `mori_ingest_preview`
- **Slash command**: `/ingest --source <path> [--preview | --dry-run] [--focus decisions] [--since 30d]`

### Model architecture refactor

Three distinct model roles, each with its own VK and env var:

| Role | Default model | Default VK | Use |
|------|--------------|------------|-----|
| Advisor | `moonshotai/kimi-k2.6` | `moku-advisor-local` | `/consult` strategic guidance |
| Dream | `moonshotai/kimi-k2.6` | `moku-dream-local` | Dream pipeline + ingestion distillation |
| Fast | `Novita/deepseek/deepseek-v4-flash` | `moku-fast-local` | Contradiction scans, cheap checks |

New env vars: `MORI_ADVISOR_MODEL`, `MORI_DREAM_MODEL`, `MORI_FAST_MODEL`,
`MORI_BIFROST_ADVISOR_VK`, `MORI_BIFROST_DREAM_VK`, `MORI_BIFROST_FAST_VK`.

### Shared utilities

`utils.py` extracted from dream.py — `parse_model_json_response()` and
`run_contradiction_scan()` now shared between dream and ingestion pipelines.
Reduces duplication, single point of maintenance for JSON response parsing.

### Vision support

`BifrostClient.consult_vision()` — multimodal ingestion routes images through
Kimi K2.6 via standard OpenAI Vision content array format. Dream model only
(fast model DeepSeek V4 Flash does not support vision).

### Fixes

- **VK_CONFIG**: corrected from `mori-*-local` to `moku-*-local` to match actual Bifrost DB keys

### Installer improvements

Brought all three bridge installers (Claude Code, Cursor, Antigravity) to full parity:

- **Doctor mode** (`-Doctor` / `--doctor`) — validates settings.json, MCP config, server health, event hooks, permissions seeding, and skills; each check includes an actionable fix hint
- **UpgradeSkills** (`-UpgradeSkills` / `--upgrade-skills`) — skips already-deployed skill folders by default; flag forces refresh
- **MCP permissions seeding** — `permissions.allow` populated with all 31 `mcp__mori__*` tools; eliminates per-call permission prompts in Claude Code and Cursor
- **Hook discriminator** (`_mori_managed: true`) — hook entries now carry a reserved field; merge identifies Mori hooks by field rather than command-string substring; backwards-compatible fallback for old installs
- **Hook merge fix** — per-event in-place merge preserves non-Mori hooks; previous behaviour replaced the entire hooks object on re-run
- **MCP allow list expanded** from 13 to 31 tools — previous list missing `pensieve`, `standards_reload`, all `mori_ingest_*` tools, and extended memory management tools
- **Headless detection** (PS1) — wizard prompts suppressed when required args supplied on CLI

### Docs

- Configuration reference updated with model role and VK env vars
- `.env.example` updated with three model roles and Bifrost VK section
- Slash commands reference documents `/ingest`
- `docs/getting-started/claude-code.md` — new installer flags, Verify It's Working, Troubleshooting, and Known Limitations sections added
- Changelog created (this file)

## v0.1.2 — Security fixes, Antigravity IDE, built-in standards

### Security
- Command injection fix: `/update` tool sanitises skill names before shell interpolation
- LLM-in-transaction fix: contradiction scan runs outside the DB write lock
- Concurrency fix: MemoryStore and SessionLog use per-method short-lived connections
- Hostname spoofing fix: client param removed from memory_write MCP tool

### New features
- Google Antigravity IDE setup documentation and bridge installer scripts
- Built-in standards shipped in Docker image by default
- External service access standards document
- NATS slash command: nats.skill.md for `/nats ping`, `/nats sub`, `/nats pub`

### Improvements
- Dream pipeline contradiction scan routed to fast model (Novita DS V4 Flash)
- README Provider Policy section replaced with Recommended Models table
- Updated image URLs in README
- mori-cline-plugin v0.1.2 with event hooks and spooler
- Alpine security patches in Dockerfile

### Docs
- Antigravity IDE getting-started guide
- External service access standards
- mori-shipper VS Code extension README

## v0.1.1 — mori-shipper VS Code extension

- mori-shipper VS Code extension (v0.1.1) — ships events from VS Code-native CC instances
- README images and terminology update

## v0.1.0 — Initial release

- Dream pipeline: session event distillation into durable memories
- Persistent memory store with versioning, attribution, protection
- Session context (`/brief`) with standards injection and freshness checks
- Strategic advisor (`/consult`) with focus areas
- Cross-device NATS messaging
- Skill deployment (`/update`)
- Requirements tracking (`/req`)
- Memory governance: trusted dreamers, pending writes, approval workflow
- Export/import for portability
- Docker Compose, Podman, macOS native, Windows, GCP deployment paths
- Claude Code, Antigravity, and Cline integration
