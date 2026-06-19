"""Phase 4b — store-level oracle-diff for filter_by_scope (SQLite).

Asserts the generic scope filter reproduces get_memories_by_project lane-for-lane
(project_memories / global_memories order + the other_projects index) across the
project × strict_global matrix, for a corpus spanning every routing class. This is
the SQLite half of the Phase 5 parity gate; the full Cartesian manifest + Postgres
+ phantom-API end-to-end come in Phase 5.

Comparison is on name+order per lane (not full row bodies) because both methods
bump retrieval_count as a side effect — the volatile counters would diverge between
the two calls. Lane assignment + ordering + index is the parity that drives the
brief text.
"""

import pytest


def _store(tmp_path):
    from mori_advisor.store import get_store

    s = get_store(tmp_path / "m.db")
    s.bootstrap()
    return s._mem if hasattr(s, "_mem") else s


def _write(ms, name, **kw):
    d = dict(title=name, body="body", type="project", tier="working", tags=[])
    d.update(kw)
    return ms.write(name=name, **d)


def _set_columns(ms, name, **cols):
    """Set columns the public write() path doesn't expose yet (tier coercion / the
    scope column — write-path threading is a later phase). Deliberate direct SQL so
    Phase 4b can exercise the read filter against states write() can't yet author."""
    conn = ms._get_conn()
    try:
        assigns = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE memories SET {assigns} WHERE name = ?", (*cols.values(), name))
        conn.commit()
    finally:
        conn.close()


def _seed(ms):
    # project foo — canonical + working
    _write(ms, "foo-canon", tags=["project:foo"], tier="canonical")
    _write(ms, "foo-work", tags=["project:foo"], tier="working")
    # auto-global types
    _write(ms, "pattern-leak", type="pattern", tags=[])
    _write(ms, "profile-leak", type="profile", tags=[])
    # explicit globals
    _write(ms, "explicit-global", tags=["scope:global"])
    _write(ms, "xproj-bar", tags=["scope:cross-project", "project:bar"])
    # the edge: explicit global AND tagged for foo → legacy puts it in the project lane
    _write(ms, "global-and-foo", tags=["scope:global", "project:foo"], tier="working")
    # other project + bare unrouted + multi-project
    _write(ms, "bar-mem", tags=["project:bar"])
    _write(ms, "bare-unrouted", tags=[])  # type=project, no tags → surfaced nowhere
    _write(ms, "multi", tags=["project:foo", "project:bar"])


def _names(lane):
    return [m["name"] for m in lane]


@pytest.mark.parametrize("project", ["foo", "bar", "baz"])
@pytest.mark.parametrize("strict_global", [True, False])
@pytest.mark.parametrize("include_global", [True, False])
def test_filter_by_scope_matches_oracle(tmp_path, project, strict_global, include_global):
    ms = _store(tmp_path)
    _seed(ms)

    legacy = ms.get_memories_by_project(
        project, include_global=include_global, strict_global=strict_global
    )
    scoped = ms.filter_by_scope(project, include_global=include_global, strict_global=strict_global)

    ctx = f"project={project} strict={strict_global} include_global={include_global}"
    assert _names(scoped["project_memories"]) == _names(legacy["project_memories"]), (
        f"project lane mismatch [{ctx}]"
    )
    assert _names(scoped["global_memories"]) == _names(legacy["global_memories"]), (
        f"global lane mismatch [{ctx}]"
    )
    assert scoped["other_projects"] == legacy["other_projects"], f"index mismatch [{ctx}]"


def test_candidate_tier_project_row_dropped_not_globalised(tmp_path):
    """A project:foo row at candidate tier is excluded from the project lane by the
    tier gate and must NOT leak into the global lane — and filter_by_scope must
    agree with the oracle on this (the tier asymmetry is the subtle parity edge)."""
    ms = _store(tmp_path)
    _seed(ms)
    _write(ms, "foo-cand", tags=["project:foo"])
    _set_columns(ms, "foo-cand", tier="candidate")  # write() coerces tier; force it

    legacy = ms.get_memories_by_project("foo", include_global=True, strict_global=False)
    scoped = ms.filter_by_scope("foo", include_global=True, strict_global=False)
    for res in (legacy, scoped):
        assert "foo-cand" not in _names(res["project_memories"])
        assert "foo-cand" not in _names(res["global_memories"])


def test_explicit_scope_column_overrides_project_tag(tmp_path):
    """An H2-native row tagged project:foo but with an explicit scope excluding foo
    is surfaced nowhere — explicit scope wins (no legacy parallel; legacy rows have
    no scope column, so this does not affect parity)."""
    ms = _store(tmp_path)
    _write(ms, "narrowed", tags=["project:foo"], tier="working")
    _set_columns(ms, "narrowed", scope='{"tags": ["repo:other"], "match": "any"}')

    scoped = ms.filter_by_scope("foo", include_global=True, strict_global=True)
    assert "narrowed" not in _names(scoped["project_memories"])
    assert "narrowed" not in _names(scoped["global_memories"])
    # but it IS surfaced when the context carries its declared tag
    import mori_advisor.scope as scope_mod
    from mori_advisor.resolver import compile_memory_scope

    row = {"tags": ["project:foo"], "type": "project", "scope": '{"tags": ["repo:other"]}'}
    assert scope_mod.in_scope(compile_memory_scope(row), {"repo:other"})
