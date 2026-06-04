"""Tests for full-text search (Stage C) on both backends.

SQLite FTS5 + Postgres tsvector. The Postgres cases run only when
MORI_TEST_DATABASE_URL is set (CI service / dev box).
"""

import asyncio
import os

import pytest

from mori_advisor.memory_store import _fts_query

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")


# ── _fts_query helper (pure, no DB) ──────────────────────────────────────────


def test_fts_query_sanitization():
    assert _fts_query("reboot survival") == '"reboot" OR "survival"'
    # Quotes / operators / punctuation must not leak into the MATCH string.
    assert _fts_query('drop"; AND *:( NEAR') == '"drop" OR "AND" OR "NEAR"'
    assert _fts_query("") == ""
    assert _fts_query("!!! ???") == ""  # no usable tokens → caller falls back


# ── SQLite FTS5 ──────────────────────────────────────────────────────────────


def _sqlite_store(tmp_path):
    from mori_advisor.store import get_store

    store = get_store(tmp_path / "memories.db")
    store.bootstrap()
    return store


def test_sqlite_fts_find_rank_update_delete(tmp_path):
    store = _sqlite_store(tmp_path)
    store.write(
        name="alpha-note",
        title="Quadlet deployment",
        body="systemd units for reboot survival",
        type="project",
        tier="working",
    )
    store.write(
        name="beta-note",
        title="Grocery list",
        body="milk eggs bread",
        type="project",
        tier="working",
    )

    res = store.search(query="reboot")
    assert "alpha-note" in res
    assert "beta-note" not in res
    assert "| Memory | Category |" in res  # SQLite Markdown-table format preserved

    # Stemming (porter): "deployments" matches "deployment".
    assert "alpha-note" in store.search(query="deployments")

    # Update — the FTS triggers must reflect the new body.
    store.write(
        name="alpha-note",
        title="Quadlet deployment",
        body="now about kubernetes pods",
        type="project",
        tier="working",
    )
    assert "alpha-note" not in store.search(query="reboot")
    assert "alpha-note" in store.search(query="kubernetes")

    # Delete — the FTS delete trigger must drop it from the index.
    store.delete("alpha-note")
    assert "alpha-note" not in store.search(query="kubernetes")


def test_sqlite_empty_query_recency_and_filters(tmp_path):
    store = _sqlite_store(tmp_path)
    store.write(name="m-one", title="first", body="x", type="project", tier="working")
    store.write(name="m-two", title="second", body="y", type="decision", tier="working")

    # Empty query → recency listing (no FTS), both present.
    allres = store.search(query=None)
    assert "m-one" in allres and "m-two" in allres

    # Structured filter still applies alongside FTS.
    only_decision = store.search(query=None, type_filter="decision")
    assert "m-two" in only_decision and "m-one" not in only_decision


# ── Postgres tsvector (gated) ────────────────────────────────────────────────


@requires_pg
def test_pg_fts_find_rank_update_delete():
    from mori_advisor.store.postgres_store import PostgresStore

    async def run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        async with store.pool.acquire() as c:
            await c.execute("DELETE FROM memories")  # clean slate (shared test DB)
        await store.write(
            name="alpha-note",
            title="Quadlet deployment",
            body="systemd units for reboot survival",
            type="project",
            tier="working",
        )
        await store.write(
            name="beta-note",
            title="Grocery list",
            body="milk eggs bread",
            type="project",
            tier="working",
        )
        out = {
            "find": await store.search(query="reboot"),
            "stem": await store.search(query="deployments"),
        }
        await store.write(
            name="alpha-note",
            title="Quadlet deployment",
            body="now about kubernetes pods",
            type="project",
            tier="working",
        )
        out["after_update_reboot"] = await store.search(query="reboot")
        out["after_update_kube"] = await store.search(query="kubernetes")
        await store.delete("alpha-note")
        out["after_delete"] = await store.search(query="kubernetes")
        await store.pool.close()
        return out

    out = asyncio.run(run())
    assert "alpha-note" in out["find"] and "beta-note" not in out["find"]
    assert "**alpha-note**" in out["find"]  # PG bullet-list format preserved
    assert "alpha-note" in out["stem"]  # stemming via 'english' config
    assert "alpha-note" not in out["after_update_reboot"]  # generated column updated
    assert "alpha-note" in out["after_update_kube"]
    assert "alpha-note" not in out["after_delete"]  # row gone
