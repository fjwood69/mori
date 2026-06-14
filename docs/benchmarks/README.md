# mori — benchmarks

How we measure mori, what held up, and what didn't. We pre-register designs, run
powered samples, and **publish the nulls** — including on our own prior claims. The
numbers below are drawn from a memory-research program of **1,300+ agent runs** (1,311
recorded run directories at the time of writing; the headline provenance result rests on
~100 *clean, reported* runs across two models — see [Run accounting](#run-accounting)).

These are real experiments with real limits. Where a result is one task, or a stress
test rather than a field rate, we say so in the same breath as the number.

---

## Headline result — provenance prevents cross-repo interference

**Claim:** when an agent's brief contains memory from a *different* repository, it
confidently reaches for APIs that don't exist in the repo it's actually working —
*retrieval interference*. Scoping that memory out of the brief eliminates it.

**Setup.** Task: implement an inline `==highlight==` → `<mark>` extension in
[`gomarkdown/markdown`](https://github.com/gomarkdown/markdown) (a Go Markdown library).
The brief was seeded with eight origin-bound memories naming APIs from a *different* Go
Markdown library ([`goldmark`](https://github.com/yuin/goldmark)) — `ScanDelimiter`,
`PrecendingCharacter`, `goldmark.Extender`, etc. — which do not exist in the target. One
was deliberately mistyped as a reusable `pattern` (the realistic "auto-globalised leak").

**Metric.** *Phantom-API attempt* — a transcript contains an attempt to use a
goldmark-only identifier (a binary per run). The agent runs headless; the brief is
injected as static session-start context.

**Conditions.** `scope=all` (legacy — the poison is surfaced) vs `scope=safe`
(provenance routing — cross-project origin-bound canon withheld).

**Result (two independent model families):**

| Model | `scope=all` (poison) | `scope=safe` (routed) | Fisher exact |
|---|---|---|---|
| MiniMax-M3 (≈428B MoE) | **20/20** phantom | **0/20** phantom | *p* ≈ 0.00000 |
| MiMo V2.5 Pro (≈1.02T MoE) | **10/10** phantom | **0/10** phantom | *p* = 0.000011 |

Perfect separation in both, on two architecturally distinct frontier-class open MoE
models. The provenance result is **model-independent**.

**Honest scope of the claim.**
- **One task, one repo pair.** Model idiosyncrasy is ruled out; *task/domain*
  idiosyncrasy is not. The next rigour step is a second task/domain, not a third model.
- **Constructed poison.** The eight memories were deliberately seeded and one
  deliberately mistyped — an upper-bound stress test of what canon-drift does naturally
  over months, **not** a natural-incidence rate. The mechanism is what generalises, not
  the 100% figure.
- **Harness note.** The MiMo runs disabled the sub-agent/Task tool, because in our
  harness it routed to a different model; immaterial to the phantom signal (a property of
  the main agent's reasoning) but disclosed for completeness.

**What it changes:** in `scope=safe` (the default, `MORI_BRIEF_SCOPE`), a memory reaches
another project's brief only via an explicit `scope:global` / `scope:cross-project` tag;
`type=profile`/`pattern` alone no longer auto-globalises, and out-of-project memory is
zero-knowledge in the passive brief (no index teaser — an out-of-scope title alone is
enough to trigger the hallucination).

---

## The nulls we publish

### The human gate is not a speed lever
A pre-registered, powered test (3 arms — ungated / human-gated / random-thinned; n=30 per
arm; four rotated rounds) found the human curation gate **did not** accelerate the
compounding curve over keeping everything (gated vs ungated Δ ≈ +1.0 discovery, *p* =
0.80). The one robust signal was a lower spiral/failure hazard under curation — i.e.
governance reads as **harm-avoidance, not speed**. Full write-up:
[keystone-null.md](keystone-null.md).

### Delivering memory *bodies* doesn't beat titles
mori's brief is a titled index. We tested whether delivering full memory **bodies**
instead of titles is worth the token cost (n=30/arm, same-scope). Every process metric
favoured bodies *directionally* but none cleared significance, and completion leaned
slightly *worse* (24/30 vs 29/30, Fisher *p* = 0.10). A third confirmation of the same
"more content ≠ faster" null. The brief stays titles-first.

### Curation's discovery-cost win is directional and task-relative
A cold-start *discovery-cost* task (files read/grepped before first edit) suggested
human-curated canon cut re-exploration ~22% → ~51% vs auto-extraction (n≈10, one repo).
On a second repo the curation win **flipped negative** — when the task was a near-clone of
an existing feature, an occurrence a task-blind curator dropped was the answer. We report
it as directional, never as the headline.

---

## Run accounting

Honesty about the denominator:

| Bucket | Runs |
|---|---|
| Total recorded run directories (whole program) | **1,311** |
| — keystone / governance experiments | 381 |
| — transfer / provenance experiments | 175 |
| — compounding / discovery / other | ~755 |
| Provenance result — *clean, reported* runs | ~100 (80 MiniMax-M3 + 20 MiMo) |

The gap between "recorded" and "reported" is smoke tests, harness-contaminated runs
(e.g. a mis-routed model), and resource-killed retries — excluded from reported figures,
not from this count. Stats: Fisher exact for binary outcomes; Mann-Whitney + bootstrap CI
for continuous; medians reported.

---

*Designs are pre-registered through an independent multi-model review panel before
spend; we'd rather show you where a number flips than a marketing curve.*
