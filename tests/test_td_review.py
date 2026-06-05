"""Trusted-Dreamer (TD) review queue tests — issue #15.

Acceptance criteria:

1. Routing predicate:
   - canonical/standard ingestion candidate → lands in pending (not store)
   - working candidate → direct to store
   - MORI_CURATE=false → canonical candidate writes direct (back-compat)

2. Enriched pending_list_json:
   - Returns all enriched fields incl. existing_body and diff source
   - A second proposal for the same name updates (not duplicates) the pending row

3. Approve applies pending + removes it from pending queue (reuse #14 approve).

4. Reject discards.

Both SQLite (always) and Postgres (if MORI_TEST_DATABASE_URL is set) via the
@requires_pg pattern from test_write_api.py.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Backend parametrisation ────────────────────────────────────────────────────

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

BACKENDS = ["sqlite"]
if PG_URL:
    BACKENDS.append("postgres")


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _a(val):
    return await val if inspect.isawaitable(val) else val


def _make_sqlite_store(tmp_path: Path):
    from mori_advisor.store import get_store

    s = get_store(tmp_path / "memories.db")
    s.bootstrap()
    return s


async def _make_pg_store():
    from mori_advisor.store.postgres_store import PostgresStore

    s = PostgresStore(PG_URL)
    await s.bootstrap()
    async with s.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE memories, memory_versions, pending_writes, "
            "eviction_queue, ingestion_log, session_events, "
            "dream_state, dreamer_config, msg_log CASCADE"
        )
    return s


def _run_with_backend(backend: str, tmp_path: Path, coro_fn):
    async def run():
        if backend == "sqlite":
            store = _make_sqlite_store(tmp_path)
        else:
            store = await _make_pg_store()
        try:
            return await coro_fn(store)
        finally:
            if hasattr(store, "pool") and store.pool:
                await store.pool.close()

    return asyncio.run(run())


def _make_pipeline(store, monkeypatch):
    """Build an IngestionPipeline wired to *store*, with a stub BifrostClient."""
    from mori_advisor.bifrost_client import BifrostClient
    from mori_advisor.ingestion import IngestionPipeline

    client = MagicMock(spec=BifrostClient)
    client.consult.return_value = "[]"

    # memory_store is store._mem for SQLite, store itself for Postgres
    mem_store = store._mem if hasattr(store, "_mem") else store

    pipeline = IngestionPipeline(
        db_path=getattr(store, "db_path", ":memory:"),
        bifrost_client=client,
        memory_store=mem_store,
        store=store,
    )
    return pipeline


# ── 1. Routing predicate — canonical/standard → pending ───────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_canonical_candidate_routes_to_pending(backend, tmp_path, monkeypatch):
    """A canonical-tier ingestion candidate must land in the pending queue, not the store."""
    monkeypatch.setenv("MORI_CURATE", "true")

    async def run(store):
        pipeline = _make_pipeline(store, monkeypatch)

        memories = [
            {
                "name": "arch-decision",
                "title": "Architectural Decision",
                "description": "A key architectural choice",
                "body": "Use hexagonal architecture for clean separation.",
                "tier": "canonical",
                "confidence": 0.9,
                "tags": ["architecture"],
            }
        ]
        await pipeline._write_memories(
            memories, tier="canonical", tags=[], source_uri="test/arch.md", focus_mode="decisions"
        )

        # Must be in pending, not in the memory store
        items = await _a(store.pending_list_json(status="pending"))
        names = [i["name"] for i in items]
        assert "arch-decision" in names, f"Expected pending, got: {names}"

        # Must NOT be written directly
        mem_store = store._mem if hasattr(store, "_mem") else store
        result = await _a(mem_store.read("arch-decision"))
        assert "not found" in result.lower() or "no memory" in result.lower(), (
            f"canonical memory should not be in store: {result}"
        )

    _run_with_backend(backend, tmp_path, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_standard_candidate_routes_to_pending(backend, tmp_path, monkeypatch):
    """A standard-tier ingestion candidate must land in the pending queue."""
    monkeypatch.setenv("MORI_CURATE", "true")

    async def run(store):
        pipeline = _make_pipeline(store, monkeypatch)

        memories = [
            {
                "name": "team-convention",
                "title": "Team Convention",
                "description": "Coding convention",
                "body": "Use snake_case for all variables.",
                "tier": "standard",
                "confidence": 0.85,
                "tags": ["convention"],
            }
        ]
        await pipeline._write_memories(
            memories, tier="standard", tags=[], source_uri="test/conv.md", focus_mode="conventions"
        )

        items = await _a(store.pending_list_json(status="pending"))
        names = [i["name"] for i in items]
        assert "team-convention" in names

    _run_with_backend(backend, tmp_path, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_working_candidate_writes_direct(backend, tmp_path, monkeypatch):
    """A working-tier ingestion candidate must be written directly to the store."""
    monkeypatch.setenv("MORI_CURATE", "true")

    async def run(store):
        pipeline = _make_pipeline(store, monkeypatch)

        memories = [
            {
                "name": "scratch-note",
                "title": "Scratch Note",
                "description": "Temporary observation",
                "body": "The deploy takes ~3 minutes.",
                "tier": "working",
                "confidence": 0.8,
                "tags": [],
            }
        ]
        await pipeline._write_memories(
            memories, tier="working", tags=[], source_uri="test/notes.md", focus_mode="all"
        )

        # Must be in the memory store
        mem_store = store._mem if hasattr(store, "_mem") else store
        result = await _a(mem_store.read("scratch-note"))
        assert "scratch-note" in result or "Scratch Note" in result, (
            f"working memory should be in store: {result}"
        )

        # Must NOT be in pending
        items = await _a(store.pending_list_json(status="pending"))
        names = [i["name"] for i in items]
        assert "scratch-note" not in names

    _run_with_backend(backend, tmp_path, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mori_curate_false_canonical_writes_direct(backend, tmp_path, monkeypatch):
    """MORI_CURATE=false must bypass the review queue for canonical candidates."""
    monkeypatch.setenv("MORI_CURATE", "false")

    async def run(store):
        pipeline = _make_pipeline(store, monkeypatch)

        memories = [
            {
                "name": "canon-direct",
                "title": "Direct Canonical",
                "description": "Bypassed by MORI_CURATE=false",
                "body": "This should write directly.",
                "tier": "canonical",
                "confidence": 0.9,
                "tags": [],
            }
        ]
        await pipeline._write_memories(
            memories, tier="canonical", tags=[], source_uri="test.md", focus_mode="all"
        )

        # Must be in the memory store, not pending
        mem_store = store._mem if hasattr(store, "_mem") else store
        result = await _a(mem_store.read("canon-direct"))
        assert "canon-direct" in result or "Direct Canonical" in result, (
            f"MORI_CURATE=false: should be in store: {result}"
        )

        items = await _a(store.pending_list_json(status="pending"))
        names = [i["name"] for i in items]
        assert "canon-direct" not in names

    _run_with_backend(backend, tmp_path, run)


# ── 2. Enriched pending_list_json ─────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_pending_list_json_enriched_fields(backend, tmp_path, monkeypatch):
    """pending_list_json must return all enriched fields incl. existing_body."""
    monkeypatch.setenv("MORI_CURATE", "true")

    async def run(store):
        # First write an existing memory so existing_body can be captured
        mem_store = store._mem if hasattr(store, "_mem") else store
        await _a(
            mem_store.write(
                name="existing-mem",
                title="Existing Memory",
                description="Already in store",
                body="The original body content.",
                tier="canonical",
            )
        )

        # Now queue a pending write for the same name
        await _a(
            store.queue_pending_write(
                name="existing-mem",
                title="Existing Memory Updated",
                description="Updated version",
                body="The proposed new body content.",
                tier="canonical",
                source="ingestion",
                provenance={"source": "test/file.md", "focus": "decisions"},
                confidence=0.88,
                focus_mode="decisions",
            )
        )

        items = await _a(store.pending_list_json(status="pending"))
        assert len(items) == 1
        item = items[0]

        assert item["name"] == "existing-mem"
        assert item["source"] == "ingestion"
        assert item["confidence"] == pytest.approx(0.88)
        assert item["focus_mode"] == "decisions"
        assert item["tier"] == "canonical"
        assert item["existing_body"] == "The original body content."
        assert item["body"] == "The proposed new body content."
        # provenance may be a dict or JSON string; either is acceptable
        assert item["provenance"] is not None

    _run_with_backend(backend, tmp_path, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_second_proposal_updates_not_duplicates(backend, tmp_path, monkeypatch):
    """A second pending proposal for the same name must UPDATE the row, not insert a new one."""
    monkeypatch.setenv("MORI_CURATE", "true")

    async def run(store):
        await _a(
            store.queue_pending_write(
                name="dup-test",
                title="First Proposal",
                body="First body",
                tier="canonical",
                source="ingestion",
                confidence=0.7,
            )
        )
        # Second proposal for the same name
        await _a(
            store.queue_pending_write(
                name="dup-test",
                title="Second Proposal",
                body="Improved body",
                tier="canonical",
                source="ingestion",
                confidence=0.9,
            )
        )

        items = await _a(store.pending_list_json(status="pending"))
        dup_items = [i for i in items if i["name"] == "dup-test"]
        assert len(dup_items) == 1, f"Expected 1 pending row, got {len(dup_items)}"
        # Latest candidate wins
        assert dup_items[0]["title"] == "Second Proposal"
        assert dup_items[0]["body"] == "Improved body"
        assert dup_items[0]["confidence"] == pytest.approx(0.9)

    _run_with_backend(backend, tmp_path, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_pending_list_json_new_memory_no_existing_body(backend, tmp_path, monkeypatch):
    """When no existing memory with the same name exists, existing_body must be None."""

    async def run(store):
        await _a(
            store.queue_pending_write(
                name="brand-new-memory",
                title="Brand New",
                body="This is entirely new.",
                tier="canonical",
                source="ingestion",
            )
        )
        items = await _a(store.pending_list_json(status="pending"))
        item = next((i for i in items if i["name"] == "brand-new-memory"), None)
        assert item is not None
        assert item["existing_body"] is None

    _run_with_backend(backend, tmp_path, run)


# ── 3. Approve applies + removes pending ──────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_approve_applies_and_removes_pending(backend, tmp_path, monkeypatch):
    """Approving a pending write must commit the memory and mark the row approved."""

    async def run(store):
        mem_store = store._mem if hasattr(store, "_mem") else store
        await _a(
            store.queue_pending_write(
                name="approve-me",
                title="Approval Test",
                description="Should be approved",
                body="The approved body.",
                tier="canonical",
                source="ingestion",
                confidence=0.92,
            )
        )

        items = await _a(store.pending_list_json(status="pending"))
        item = next((i for i in items if i["name"] == "approve-me"), None)
        assert item is not None, "Pending row not found before approve"
        write_id = item["id"]

        result = await _a(mem_store.approve(write_id, note="looks good", reviewer="td"))
        assert "approved" in result.lower()

        # Memory must now be in the store
        mem_result = await _a(mem_store.read("approve-me"))
        assert "approve-me" in mem_result or "Approval Test" in mem_result

        # Pending row must no longer appear as pending
        pending_after = await _a(store.pending_list_json(status="pending"))
        pending_names = [i["name"] for i in pending_after]
        assert "approve-me" not in pending_names

    _run_with_backend(backend, tmp_path, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_reapprove_same_name_no_unique_violation(backend, tmp_path, monkeypatch):
    """A canonical memory re-proposed and re-approved AFTER a prior approval must not
    raise a UNIQUE violation. Regression: Postgres previously used a full
    UNIQUE(memory_name, status), so the second 'approved' row collided; the partial
    'pending'-only unique index (matching SQLite) is the fix."""
    monkeypatch.setenv("MORI_CURATE", "true")

    async def run(store):
        mem_store = store._mem if hasattr(store, "_mem") else store

        # First proposal → approve
        await _a(
            store.queue_pending_write(
                name="evolving-pattern",
                title="v1",
                body="First canonical body.",
                tier="canonical",
                source="ingestion",
                confidence=0.8,
            )
        )
        first = next(
            i
            for i in await _a(store.pending_list_json(status="pending"))
            if i["name"] == "evolving-pattern"
        )
        r1 = await _a(mem_store.approve(first["id"], note="v1 ok", reviewer="td"))
        assert "approved" in r1.lower()

        # Same name re-proposed (an evolution) while a prior 'approved' row exists
        await _a(
            store.queue_pending_write(
                name="evolving-pattern",
                title="v2",
                body="Updated canonical body.",
                tier="canonical",
                source="ingestion",
                confidence=0.95,
            )
        )
        second = next(
            i
            for i in await _a(store.pending_list_json(status="pending"))
            if i["name"] == "evolving-pattern"
        )
        # This second approve raised a UNIQUE violation on Postgres before the fix.
        r2 = await _a(mem_store.approve(second["id"], note="v2 ok", reviewer="td"))
        assert "approved" in r2.lower()

        mem = await _a(mem_store.read("evolving-pattern"))
        assert "Updated canonical body" in mem or "v2" in mem

    _run_with_backend(backend, tmp_path, run)


# ── 4. Reject discards ────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_reject_discards_pending(backend, tmp_path, monkeypatch):
    """Rejecting a pending write must mark it rejected and not write it to the store."""

    async def run(store):
        mem_store = store._mem if hasattr(store, "_mem") else store
        await _a(
            store.queue_pending_write(
                name="reject-me",
                title="Rejection Test",
                body="This should not survive.",
                tier="canonical",
                source="ingestion",
            )
        )

        items = await _a(store.pending_list_json(status="pending"))
        item = next((i for i in items if i["name"] == "reject-me"), None)
        assert item is not None
        write_id = item["id"]

        result = await _a(mem_store.reject(write_id, note="not good enough", reviewer="td"))
        assert "rejected" in result.lower()

        # Memory must NOT be in the store
        mem_result = await _a(mem_store.read("reject-me"))
        assert "not found" in mem_result.lower() or "no memory" in mem_result.lower()

        # Must not appear in pending
        pending_after = await _a(store.pending_list_json(status="pending"))
        assert all(i["name"] != "reject-me" for i in pending_after)

    _run_with_backend(backend, tmp_path, run)


# ── 5. Low-confidence skip ────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_low_confidence_skipped(backend, tmp_path, monkeypatch):
    """Memories with confidence < 0.5 must be skipped regardless of tier."""
    monkeypatch.setenv("MORI_CURATE", "true")

    async def run(store):
        pipeline = _make_pipeline(store, monkeypatch)

        memories = [
            {
                "name": "low-conf",
                "title": "Low Confidence",
                "body": "Uncertain content.",
                "tier": "canonical",
                "confidence": 0.3,
                "tags": [],
            }
        ]
        await pipeline._write_memories(memories, tier="canonical", tags=[])

        items = await _a(store.pending_list_json(status="pending"))
        names = [i["name"] for i in items]
        assert "low-conf" not in names

        mem_store = store._mem if hasattr(store, "_mem") else store
        result = await _a(mem_store.read("low-conf"))
        assert "not found" in result.lower() or "no memory" in result.lower()

    _run_with_backend(backend, tmp_path, run)
