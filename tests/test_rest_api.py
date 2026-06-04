"""Tests for the read REST API layer.

- `search_json()` on both backends (the structured search behind GET /api/memories).
- `_read_api_params()` — the pure query-param parser/clamp (HTTP contract tests in
  verify-deployment.py are too coarse for these edge cases).

Postgres cases run only when MORI_TEST_DATABASE_URL is set.
"""

import asyncio
import os

import pytest

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

# The stable dict shape both backends must return.
API_KEYS = {"name", "title", "type", "tier", "tags", "updated_at", "description"}

# Full detail shape returned by get_memory() on both backends.
DETAIL_KEYS = {
    "name",
    "title",
    "type",
    "tier",
    "tags",
    "description",
    "body",
    "created_at",
    "updated_at",
    "origin_clients",
    "retrieval_count",
    "freshness_status",
}


def _req(query_string: str):
    from starlette.requests import Request

    return Request(
        {"type": "http", "method": "GET", "query_string": query_string.encode(), "headers": []}
    )


# ── _read_api_params (pure) ──────────────────────────────────────────────────


def test_read_api_params_defaults_and_clamp():
    from mori_advisor.main import _read_api_params

    assert _read_api_params(_req(""))["limit"] == 50  # default
    assert _read_api_params(_req("limit=500"))["limit"] == 200  # clamp high
    assert _read_api_params(_req("limit=0"))["limit"] == 1  # clamp low
    assert _read_api_params(_req("limit=notanint"))["limit"] == 50  # bad → default
    assert _read_api_params(_req("query="))["query"] is None  # empty → None

    p = _read_api_params(_req("query=reboot&type=project&tag=infra&client=nuc&since=7d&limit=5"))
    assert p == {
        "query": "reboot",
        "type_filter": "project",
        "tag": "infra",
        "client": "nuc",
        "since": "7d",
        "limit": 5,
    }


def test_json_safe_rows():
    """Postgres read_events returns datetime (TIMESTAMPTZ) — must become an ISO string
    so JSONResponse can serialize it (regression: GET /api/events 500 on Postgres)."""
    import json
    from datetime import datetime, timezone

    from mori_advisor.main import _json_safe_rows

    rows = [{"id": 1, "name": "x", "timestamp": datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)}]
    out = _json_safe_rows(rows)
    assert isinstance(out[0]["timestamp"], str) and out[0]["timestamp"].startswith("2026-06-04")
    assert out[0]["id"] == 1 and out[0]["name"] == "x"
    json.dumps(out)  # must not raise


# ── SQLite search_json ───────────────────────────────────────────────────────


def _sqlite_mem(tmp_path):
    from mori_advisor.store import get_store

    s = get_store(tmp_path / "memories.db")
    s.bootstrap()
    return s._mem


def test_sqlite_search_json(tmp_path):
    mem = _sqlite_mem(tmp_path)
    mem.write(
        name="alpha-x",
        title="Quadlet deployment",
        body="systemd reboot survival",
        type="project",
        tier="working",
        tags=["infra"],
    )
    mem.write(
        name="beta-y",
        title="Grocery",
        body="milk eggs",
        type="decision",
        tier="working",
        tags=["personal"],
    )

    hit = mem.search_json(query="reboot")
    assert len(hit) == 1 and hit[0]["name"] == "alpha-x"
    assert set(hit[0].keys()) == API_KEYS
    assert hit[0]["tags"] == ["infra"]  # parsed to a list

    assert {m["name"] for m in mem.search_json()} == {"alpha-x", "beta-y"}  # empty → recency
    assert [m["name"] for m in mem.search_json(type_filter="decision")] == ["beta-y"]
    assert [m["name"] for m in mem.search_json(tag="infra")] == ["alpha-x"]


# ── Postgres search_json (gated) ─────────────────────────────────────────────


@requires_pg
def test_pg_search_json():
    from mori_advisor.store.postgres_store import PostgresStore

    async def run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        async with store.pool.acquire() as c:
            await c.execute("DELETE FROM memories")
        await store.write(
            name="alpha-x",
            title="Quadlet deployment",
            body="systemd reboot survival",
            type="project",
            tier="working",
            tags=["infra"],
        )
        await store.write(
            name="beta-y",
            title="Grocery",
            body="milk eggs",
            type="decision",
            tier="working",
            tags=["personal"],
        )
        out = {
            "hit": await store.search_json(query="reboot"),
            "all": await store.search_json(),
            "filt": await store.search_json(type_filter="decision"),
        }
        await store.pool.close()
        return out

    out = asyncio.run(run())
    assert len(out["hit"]) == 1 and out["hit"][0]["name"] == "alpha-x"
    assert set(out["hit"][0].keys()) == API_KEYS
    assert out["hit"][0]["tags"] == ["infra"]  # JSONB → list
    assert isinstance(out["hit"][0]["updated_at"], str)  # datetime → isoformat string
    assert {m["name"] for m in out["all"]} == {"alpha-x", "beta-y"}
    assert [m["name"] for m in out["filt"]] == ["beta-y"]


# ── SQLite get_memory ────────────────────────────────────────────────────────


def test_sqlite_get_memory(tmp_path):
    mem = _sqlite_mem(tmp_path)
    mem.write(
        name="detail-test",
        title="Detail Test Memory",
        body="Some body content here",
        type="project",
        tier="working",
        tags=["infra", "test"],
        description="A test memory for get_memory",
    )

    result = mem.get_memory("detail-test")
    assert result is not None
    assert set(result.keys()) == DETAIL_KEYS
    assert result["name"] == "detail-test"
    assert result["title"] == "Detail Test Memory"
    assert result["body"] == "Some body content here"
    assert isinstance(result["tags"], list)
    assert set(result["tags"]) == {"infra", "test"}
    assert isinstance(result["origin_clients"], list)
    assert isinstance(result["retrieval_count"], int)

    # Miss case
    assert mem.get_memory("does-not-exist") is None


# ── Postgres get_memory (gated) ──────────────────────────────────────────────


@requires_pg
def test_pg_get_memory():
    from mori_advisor.store.postgres_store import PostgresStore

    async def run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        async with store.pool.acquire() as c:
            await c.execute("DELETE FROM memories WHERE name = 'detail-pg-test'")
        await store.write(
            name="detail-pg-test",
            title="PG Detail Test",
            body="postgres body content",
            type="project",
            tier="working",
            tags=["pg", "test"],
            description="PG memory for get_memory test",
        )
        result = await store.get_memory("detail-pg-test")
        miss = await store.get_memory("does-not-exist-pg")
        await store.pool.close()
        return result, miss

    result, miss = asyncio.run(run())
    assert result is not None
    assert set(result.keys()) == DETAIL_KEYS
    assert result["name"] == "detail-pg-test"
    assert result["body"] == "postgres body content"
    assert isinstance(result["tags"], list)
    assert set(result["tags"]) == {"pg", "test"}  # JSONB → list
    assert isinstance(result["origin_clients"], list)
    assert miss is None
