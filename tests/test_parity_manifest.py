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

Ordering note: the seed forces a DETERMINISTIC distinct (created_at, updated_at)
per row so the (tier, updated_at) sort key is total. With genuine ties on that key
SQLite's order is undefined and differs between the oracle's pre-filtered scan and
the filter's load-then-filter scan — a tie order is NOT part of the routing
contract. The manifest therefore proves byte-identical ordering under a well-defined
key AND unconditional multiset membership; tie resolution is out of scope (a
candidate deterministic tiebreaker for the cutover is logged for Fred).
"""

import itertools

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
            # Byte-identical full body + order (valid: distinct sort keys, no ties).
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
