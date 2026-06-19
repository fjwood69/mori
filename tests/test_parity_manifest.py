"""Phase 5a — the oracle-diff parity manifest (SQLite, byte-identical).

The Phase 4b test compares lane name+order on a hand corpus. This is the stronger
claim: enumerate the FULL Cartesian product of routing dimensions, write one memory
per combination (plus superseded/deleted rows that BOTH paths must exclude), and
assert filter_by_scope returns dicts BYTE-IDENTICAL to get_memories_by_project —
full body, every field except the volatile retrieval counters — across the
project × strict_global × include_global setting matrix.

Scope: legacy rows only (NULL scope column). That is the exact subsumption
commitment — an H2-native row with an explicit scope map has no oracle equivalent
and is covered by test_filter_by_scope, not here.

Both methods run on SEPARATE identically-seeded stores so the retrieval bump each
performs doesn't cross-contaminate; the two volatile fields are still stripped
before comparison because their wall-clock timestamps can differ by a tick.

Ordering note: a `, id DESC` tiebreaker was added to BOTH the oracle and
filter_by_scope ORDER BYs (Fred-approved), making the sort total — so order is now
deterministic even on tied (tier, updated_at) keys. This manifest still seeds
distinct timestamps for the main matrix; test_tiebreaker_makes_tied_order_deterministic
exercises the all-tied case directly. Membership is also asserted unconditionally as
a multiset.
"""

import asyncio
import itertools
import os

import pytest

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
requires_pg = pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")

# Routing dimensions.
_PROJECT_TAGS = ([], ["project:foo"], ["project:bar"], ["project:foo", "project:bar"])
_GLOBAL_TAGS = ([], ["scope:global"], ["scope:cross-project"])
_TYPES = ("project", "profile", "pattern", "decision")
_TIERS = ("canonical", "working", "candidate")

_VOLATILE = ("last_retrieved_at", "retrieval_count")


def _store(tmp_path, name):
    from mori_advisor.store import get_store

    s = get_store(tmp_path / name)
    s.bootstrap()
    return s._mem if hasattr(s, "_mem") else s


def _force(ms, name, **cols):
    conn = ms._get_conn()
    try:
        assigns = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE memories SET {assigns} WHERE name = ?", (*cols.values(), name))
        conn.commit()
    finally:
        conn.close()


def _ts(i: int) -> str:
    """Deterministic distinct ascending UTC timestamp for row i (avoids sort-key
    ties so the ordered byte comparison is well-defined and store-independent)."""
    return f"2026-06-19 {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}"


def _seed(ms):
    """Write one memory per (project_tags × global_tag × type × tier) combination,
    forcing tier (write() coerces non-working/canonical) and a deterministic distinct
    (created_at, updated_at) via SQL. Adds a superseded and a soft-deleted row that
    both paths must drop."""
    i = 0
    for ptags, gtag, mtype, tier in itertools.product(_PROJECT_TAGS, _GLOBAL_TAGS, _TYPES, _TIERS):
        i += 1
        nm = f"m{i:03d}"
        ms.write(
            name=nm,
            title=nm,
            body="body",
            type=mtype,
            tier="working",
            tags=[*ptags, *gtag],
        )
        _force(ms, nm, tier=tier, created_at=_ts(i), updated_at=_ts(i))
    # rows both paths must exclude regardless of scope
    ms.write(name="gone-super", title="x", body="b", type="project", tags=["project:foo"])
    _force(ms, "gone-super", superseded_by="m001")
    ms.write(name="gone-del", title="x", body="b", type="project", tags=["project:foo"])
    _force(ms, "gone-del", deleted_at="2026-01-01 00:00:00")
    return i


def _strip(lane):
    return [{k: v for k, v in m.items() if k not in _VOLATILE} for m in lane]


def test_parity_manifest_byte_identical(tmp_path):
    legacy_store = _store(tmp_path, "legacy.db")
    scope_store = _store(tmp_path, "scope.db")
    n_legacy = _seed(legacy_store)
    n_scope = _seed(scope_store)
    assert n_legacy == n_scope == len(_PROJECT_TAGS) * len(_GLOBAL_TAGS) * len(_TYPES) * len(_TIERS)

    mismatches = []
    for project, strict, incl in itertools.product(
        ("foo", "bar", "baz"), (True, False), (True, False)
    ):
        legacy = legacy_store.get_memories_by_project(
            project, include_global=incl, strict_global=strict
        )
        scoped = scope_store.filter_by_scope(project, include_global=incl, strict_global=strict)
        for lane in ("project_memories", "global_memories"):
            # Unconditional membership parity (order-insensitive multiset).
            if sorted(m["name"] for m in scoped[lane]) != sorted(m["name"] for m in legacy[lane]):
                mismatches.append(
                    f"project={project} strict={strict} incl={incl} {lane} MEMBERSHIP: "
                    f"legacy={sorted(m['name'] for m in legacy[lane])} "
                    f"scope={sorted(m['name'] for m in scoped[lane])}"
                )
            # Byte-identical full body + order (the , id DESC tiebreaker makes the
            # sort total, so order is deterministic even before the distinct timestamps).
            elif _strip(scoped[lane]) != _strip(legacy[lane]):
                ln = [m["name"] for m in legacy[lane]]
                sn = [m["name"] for m in scoped[lane]]
                mismatches.append(
                    f"project={project} strict={strict} incl={incl} {lane} BODY/ORDER: "
                    f"legacy={ln} scope={sn}"
                )
        if scoped["other_projects"] != legacy["other_projects"]:
            mismatches.append(
                f"project={project} strict={strict} incl={incl} other_projects: "
                f"legacy={legacy['other_projects']} scope={scoped['other_projects']}"
            )
    assert not mismatches, "parity broken:\n" + "\n".join(mismatches)


def test_tiebreaker_makes_tied_order_deterministic(tmp_path):
    """All rows share an identical (tier, updated_at), so the ONLY differentiator is
    the `, id DESC` tiebreaker. Both the oracle and the filter must then produce the
    same deterministic order (id-descending = reverse insertion) — this is the fix
    for the tie divergence (oracle pre-filter scan vs filter load-then-filter, which
    resolved equal sort keys differently before the tiebreaker)."""
    legacy_store = _store(tmp_path, "legacy_tie.db")
    scope_store = _store(tmp_path, "scope_tie.db")
    for ms in (legacy_store, scope_store):
        for k in range(1, 13):  # project lane, all tied
            nm = f"t{k:02d}"
            ms.write(
                name=nm, title=nm, body="body", type="project", tier="working", tags=["project:foo"]
            )
            _force(ms, nm, updated_at="2026-06-19 00:00:00")
        for k in range(1, 5):  # global lane (auto-global pattern), all tied
            nm = f"g{k:02d}"
            ms.write(name=nm, title=nm, body="body", type="pattern", tier="working", tags=[])
            _force(ms, nm, updated_at="2026-06-19 00:00:00")

    legacy = legacy_store.get_memories_by_project("foo", include_global=True, strict_global=False)
    scoped = scope_store.filter_by_scope("foo", include_global=True, strict_global=False)
    for lane in ("project_memories", "global_memories"):
        assert [m["name"] for m in scoped[lane]] == [m["name"] for m in legacy[lane]], lane
    # concretely deterministic: id DESC ⇒ reverse insertion order
    assert [m["name"] for m in legacy["project_memories"]] == [
        f"t{k:02d}" for k in range(12, 0, -1)
    ]
    assert [m["name"] for m in legacy["global_memories"]] == [f"g{k:02d}" for k in range(4, 0, -1)]


# ── Postgres parity (gated on MORI_TEST_DATABASE_URL) ─────────────────────────


@requires_pg
def test_parity_manifest_pg():
    """PG twin of the parity manifest on the live (possibly shared) test DB.

    Runs on whatever rows already exist — both methods see the same foreign rows,
    so legacy==filter parity holds regardless. Project tags are uniquely prefixed
    so the project lane is isolated to this test's rows (ordered comparison valid);
    the global lane may interleave foreign rows and tie on timestamps, so it is
    asserted as a multiset (membership), consistent with the SQLite tie caveat.
    Byte-identical ordering is proven backend-agnostically by the SQLite manifest;
    here we prove the PG SQL paths reproduce the oracle's membership + partition.
    """
    from datetime import datetime, timezone

    from mori_advisor.store.postgres_store import PostgresStore

    pfx = "pgp_"
    foo, bar = f"project:{pfx}foo", f"project:{pfx}bar"
    proj_variants = ([], [foo], [bar], [foo, bar])

    def _dt(i: int) -> datetime:
        # asyncpg validates the Python type before the ::timestamptz cast, so bind a
        # real tz-aware datetime (the string form _ts() works only for SQLite).
        return datetime(2026, 6, 19, i // 3600, (i // 60) % 60, i % 60, tzinfo=timezone.utc)

    async def run():
        store = PostgresStore(PG_URL)
        await store.bootstrap()
        async with store.pool.acquire() as conn:
            await conn.execute("DELETE FROM memories WHERE name LIKE $1", pfx + "%")
        i = 0
        updates = []
        for ptags, gtag, mtype, tier in itertools.product(
            proj_variants, _GLOBAL_TAGS, _TYPES, _TIERS
        ):
            i += 1
            nm = f"{pfx}{i:03d}"
            await store.write(
                name=nm, title=nm, body="body", type=mtype, tier="working", tags=[*ptags, *gtag]
            )
            updates.append((nm, tier, _dt(i)))
        async with store.pool.acquire() as conn:
            await conn.executemany(
                "UPDATE memories SET tier=$2, created_at=$3, updated_at=$3 WHERE name=$1",
                updates,
            )
        out = []
        for project, strict, incl in itertools.product(
            (f"{pfx}foo", f"{pfx}bar", f"{pfx}absent"), (True, False), (True, False)
        ):
            legacy = await store.get_memories_by_project(
                project, include_global=incl, strict_global=strict
            )
            scoped = await store.filter_by_scope(project, include_global=incl, strict_global=strict)
            out.append((project, strict, incl, legacy, scoped))
        async with store.pool.acquire() as conn:
            await conn.execute("DELETE FROM memories WHERE name LIKE $1", pfx + "%")
        await store.pool.close()
        return out

    rows = asyncio.run(run())
    mismatches = []
    for project, strict, incl, legacy, scoped in rows:
        ctx = f"project={project} strict={strict} incl={incl}"
        # Project lane — isolated to this test's rows: ordered names must match.
        if [m["name"] for m in scoped["project_memories"]] != [
            m["name"] for m in legacy["project_memories"]
        ]:
            mismatches.append(f"{ctx} project lane order")
        # Global lane — may interleave foreign rows / tie: multiset membership.
        if sorted(m["name"] for m in scoped["global_memories"]) != sorted(
            m["name"] for m in legacy["global_memories"]
        ):
            mismatches.append(f"{ctx} global lane membership")
        if dict(scoped["other_projects"]) != dict(legacy["other_projects"]):
            mismatches.append(f"{ctx} other_projects")
    assert not mismatches, "PG parity broken:\n" + "\n".join(mismatches)
