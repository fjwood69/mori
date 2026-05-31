# Technical Disclosure: Mori — Shared Memory Layer for AI Coding Agents

**Author:** Frederick Wood  
**Conception date:** April 8, 2026  
**First code implementation:** May 9, 2026 (private `ai-stack` repository)  
**Public repository:** https://github.com/fjwood69/mori  
**Public release:** May 21, 2026  
**Purpose:** Defensive publication to establish prior art and prevent
third-party patenting of the inventions described herein.

---

## Abstract

This document describes the architecture, methods, and novel mechanisms
implemented in Mori — a shared memory layer for AI coding agents. The primary
purpose of this disclosure is to establish prior art for the inventions
described, permanently placing them in the public domain and preventing any
third party from obtaining patent protection over these methods.

All inventions described were conceived and implemented by Frederick Wood
independently. The conception date of April 8, 2026 is evidenced by git
commit history in the private `dotfiles` repository showing the first
structured cross-device session memory system. The first automated
implementation is evidenced by git commit history in the private `ai-stack`
repository dated May 9, 2026.

---

## 1. The Dream Pipeline — Automatic Session Distillation

### 1.1 Problem

AI coding agents have no persistent memory between sessions. Each session
starts cold. Existing memory tools require explicit instrumentation —
developers or agents must call `store()` or `add()` functions manually,
resulting in incomplete capture.

### 1.2 Method

Mori implements automatic session distillation via the following method:

**Step 1 — Event capture via lifecycle hooks:**
AI coding agent IDEs expose lifecycle hook mechanisms. Mori registers HTTP
endpoints as hook handlers:

- `PostToolUse` — fires after every tool call
- `PostToolUseFailure` — fires on tool call errors
- `UserPromptSubmit` — fires on user input
- `Stop` — fires on session end
- `PreCompact` — fires before context compression

Events are POSTed to `POST /api/events/raw` and appended to an append-only
event log with session ID, client hostname, working directory, and
transcript path.

**Step 2 — Scheduled distillation:**
A cron scheduler runs the dream pipeline at configurable intervals (default:
30 minutes). The pipeline reads all events since a high-water watermark,
batches them, and sends them to a configurable LLM with a structured
distillation prompt. The prompt extracts durable knowledge — architectural
decisions, patterns, conventions, gotchas — returning structured JSON memory
candidates.

**Step 3 — Memory writing and watermark advancement:**
Extracted memories are written to a versioned memory store within a single
SQLite transaction. The watermark advances to the last processed event ID.

**Step 4 — Contradiction scan:**
After memory writing, a lightweight contradiction scan checks new memories
against existing canonical memories sharing a name prefix. A fast LLM model
performs binary classification: `SUPERSEDES` or `UNRELATED`. Superseded
memories are marked and queued for eviction.

### 1.3 Novel elements

- Automatic event capture via IDE lifecycle hooks without code changes
- Lifecycle hook capture → append-only event log → scheduled LLM distillation
  → versioned memory store as a unified pipeline
- High-water watermark mechanism for incremental processing
- Separation of distillation model (quality-optimised) from contradiction
  scan model (speed-optimised)

---

## 2. PreCompact Synchronous Dream — Memory Preservation at Context Boundary

### 2.1 Problem

When a context window fills and the IDE performs context compression, knowledge
from the current session not yet distilled is at risk of being lost.

### 2.2 Method

Mori implements a synchronous dream trigger that fires when the `PreCompact`
lifecycle hook is received, before context compression occurs:

1. IDE fires `PreCompact` hook before context compression
2. Mori receives the hook at `POST /api/precompact`
3. Mori runs the full dream pipeline synchronously
4. The agent waits for the response (typically 10-30 seconds)
5. Context compression proceeds with session knowledge preserved

### 2.3 Novel elements

- Triggering LLM-based session distillation synchronously at the context
  compression boundary to prevent knowledge loss
- Use of the `PreCompact` IDE lifecycle hook as a memory preservation trigger
- The agent-waits-for-distillation pattern before context compression

---

## 3. Multi-Instance Memory Coherence

### 3.1 Problem

Developers run multiple AI coding agent instances simultaneously with no
knowledge of what other instances have decided or discovered.

### 3.2 Method

- All agent instances send lifecycle hook events to the same Mori server
- Each event is tagged with a client identifier (device hostname)
- The dream pipeline distils events from all instances into a unified memory
  store, synthesising cross-instance knowledge
- The `mori-brief` session grounding tool loads the unified memory store at
  session start for any instance

### 3.3 Session grounding vs per-query RAG

Mori uses session grounding rather than per-query retrieval-augmented
generation. A curated set of memories is loaded once at session start,
establishing a shared context baseline that persists for the entire session.

### 3.4 Novel elements

- Multi-instance event capture → unified distillation → shared session
  grounding as a coherence mechanism for concurrent AI coding agent instances
- Per-client attribution in the event log and memory store
- Session grounding (load-once at session start) as an alternative to
  per-query RAG for AI coding agent memory

---

## 4. Three-Tier Memory Lifecycle

### 4.1 Design

Three tiers with different retention and eviction characteristics:

- **Ephemeral** — session summaries, auto-expire unless promoted
- **Working** — patterns and decisions, flagged after 30 days without retrieval
- **Canonical** — explicitly promoted by trusted dreamers, indefinite retention

### 4.2 Freshness check method

For canonical memories tagged with infrastructure, dependency, or tooling
tags, Mori runs a freshness check during session grounding: a lightweight
LLM prompt asks whether the memory is still accurate (YES/NO/STALE). STALE
memories are suppressed from session grounding and queued for review.

### 4.3 Novel elements

- Three-tier lifecycle with different eviction policies per tier applied to
  AI coding agent session memory
- Freshness check — lightweight LLM validation of canonical memory accuracy
  before session grounding injection
- Eviction queue — flagging memories for human review rather than automatic
  deletion

---

## 5. Trusted Dreamer Governance Model

### 5.1 Design

Designated agent instances ("trusted dreamers") write directly to canonical
memory. Other instances have writes queued in a `pending_writes` table for
approval by a trusted dreamer.

Every memory write creates a version snapshot enabling history, diff, and
rollback. Every write records `origin_session_ids` and `origin_clients` for
audit purposes.

### 5.2 Novel elements

- Trusted dreamer model — hostname-based designation of agent instances with
  elevated memory write permissions
- Pending write approval workflow for non-trusted agent instances
- Full versioning with rollback applied to AI agent session memory
- Per-session, per-client attribution for memory audit

---

## 6. Universal Ingestion Pipeline

### 6.1 Design

The same LLM distillation method as the dream pipeline applied to arbitrary
source material — PDFs, images (multimodal vision), CC session transcripts,
git history, text and code — enabling bootstrapping of the memory store.

Files are uploaded via multipart HTTP to a dedicated ingestion server,
parsed using format-appropriate parsers, distilled by LLM, and written to
the shared memory store with SHA256 content hash deduplication.

### 6.2 Novel elements

- Application of AI session distillation methods to arbitrary external source
  material for memory store bootstrapping
- SHA256 content-hash deduplication
- Separate ingestion pod architecture isolating heavy batch processing from
  the latency-sensitive MCP tool server

---

## 7. Hierarchical Pattern Library (Conceived, Partial Implementation)

### 7.1 Design

Pre-built corpora of canonical memories scoped to organisational role, line
of business, or domain, loaded selectively at session start:

```
/patterns
  /firm-wide          ← loads for all agents
  /line-of-business
    /markets
      /quant-devs
      /ui-devs
    /retail-banking
  /platform
```

### 7.2 Novel elements

- Hierarchical, role-scoped pre-built memory corpora for AI coding agent
  session grounding
- Organisational knowledge inheritance — firm-wide patterns inherited by all
  role-specific pattern sets
- Pattern libraries as institutional knowledge distribution for AI coding
  agents across large organisations

---

## 8. Git Push Cross-Instance Notification

### 8.1 Design

A post-push git hook publishes a `GitPush` event to the Mori event log and
immediately to the NATS message bus. Other agent instances receive this
notification in their next session brief, providing real-time cross-device
push awareness without polling.

### 8.2 Novel elements

- Wiring git post-push hooks to an AI agent session memory system for
  cross-instance push awareness
- Immediate NATS publish for git push events bypassing the scheduled dream
  pipeline for real-time notification

---

## Prior Art Statement

All inventions described were conceived and implemented by Frederick Wood
independently. Evidence of prior conception:

- **April 8, 2026** — first structured cross-device session memory system in
  private `dotfiles` repository (manual precursor to the dream pipeline)
- **May 9, 2026** — first automated implementation in private `ai-stack`
  repository (`ai-stack/moku-advisor/`)
- **May 12, 2026** — dream pipeline extracted to standalone repository
  (`fjwood69/mori`, then `fjwood69/moku`)
- **May 21, 2026** — public release
- **May 31, 2026** — this disclosure published

This document is published as a defensive publication to permanently place
these inventions in the public domain and prevent any third party from
obtaining patent protection over the methods described herein.

---

## Implementation Reference

- Dream pipeline: `mori_advisor/dream.py`
- Event capture: `mori_advisor/main.py` (`/api/events/raw`, `/api/precompact`)
- Memory store: `mori_advisor/memory_store.py`
- Ingestion: `mori_advisor/ingestion.py`, `mori_advisor/ingestion_server.py`
- Session grounding: `skills/brief/SKILL.md`, `mori_advisor/main.py`
- Contradiction scan: `mori_advisor/dream.py` (`_contradiction_scan()`)
- Freshness check: `mori_advisor/main.py` (`check_freshness()`)
- Git push notification: `scripts/post-push.sh`, `scripts/post-push.ps1`

Source: https://github.com/fjwood69/mori (AGPL-3.0)

---

*Frederick Wood, May 31, 2026*
