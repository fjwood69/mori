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
