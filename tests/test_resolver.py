"""Unit tests for the legacy→scope resolver (mori_advisor.resolver).

Beyond per-rule compilation, the final block is a *pure-function parity check*:
for a fixture corpus it asserts the compiled scope filter (compile_memory_scope +
compile_context_tags + in_scope) surfaces exactly the set a reference
re-implementation of the legacy ``get_memories_by_project`` membership predicate
would. This is the no-DB rehearsal of the Phase 5 oracle-diff gate.
"""

from mori_advisor.resolver import (
    LEGACY_TYPE_GLOBAL_TAG,
    UNROUTED_TAG,
    compile_context_tags,
    compile_memory_scope,
)
from mori_advisor.scope import in_scope

# ── compile_memory_scope ─────────────────────────────────────────────────────


def test_project_tag_becomes_scope_tag():
    s = compile_memory_scope({"tags": ["project:mori"], "type": "decision"})
    assert s.tags == frozenset({"project:mori"})
    assert s.match == "any"
    assert not s.is_global


def test_multiple_project_tags_all_retained():
    s = compile_memory_scope({"tags": ["project:mori", "project:bifrost"], "type": "decision"})
    assert s.tags == frozenset({"project:mori", "project:bifrost"})


def test_explicit_global_is_global():
    assert compile_memory_scope({"tags": ["scope:global"], "type": "decision"}).is_global
    assert compile_memory_scope({"tags": ["scope:cross-project"], "type": "decision"}).is_global


def test_explicit_global_wins_over_project_tags():
    # global lane surfaces the row regardless of its project: tags
    s = compile_memory_scope({"tags": ["scope:global", "project:mori"], "type": "pattern"})
    assert s.is_global


def test_profile_pattern_get_legacy_type_global():
    for t in ("profile", "pattern"):
        s = compile_memory_scope({"tags": [], "type": t})
        assert s.tags == frozenset({LEGACY_TYPE_GLOBAL_TAG})


def test_profile_with_project_gets_both():
    s = compile_memory_scope({"tags": ["project:mori"], "type": "profile"})
    assert s.tags == frozenset({"project:mori", LEGACY_TYPE_GLOBAL_TAG})


def test_non_auto_global_type_no_legacy_tag():
    s = compile_memory_scope({"tags": ["project:mori"], "type": "decision"})
    assert LEGACY_TYPE_GLOBAL_TAG not in s.tags


def test_explicit_scope_column_wins():
    row = {
        "tags": ["project:mori"],
        "type": "profile",
        "scope": '{"tags": ["repo:bifrost"], "match": "all"}',
    }
    s = compile_memory_scope(row)
    assert s.tags == frozenset({"repo:bifrost"})
    assert s.match == "all"  # legacy tags/type ignored once scope is explicit


def test_null_or_empty_scope_falls_through_to_legacy():
    for raw in (None, ""):
        s = compile_memory_scope({"tags": ["project:mori"], "type": "decision", "scope": raw})
        assert s.tags == frozenset({"project:mori"})


def test_missing_keys_do_not_raise():
    # no tags, no auto-global type → UNROUTED, not global (surfaced nowhere by brief)
    assert compile_memory_scope({}).tags == frozenset({UNROUTED_TAG})
    assert compile_memory_scope({"type": "profile"}).tags == frozenset({LEGACY_TYPE_GLOBAL_TAG})


def test_unrouted_row_is_never_in_scope():
    # bare, project-less, non-global, non-auto-global → matches no context, ever
    s = compile_memory_scope({"tags": [], "type": "decision"})
    assert not s.is_global
    assert not in_scope(s, compile_context_tags("mori", strict_global=False))
    assert not in_scope(s, compile_context_tags("mori", strict_global=True))


def test_non_list_tags_defensive():
    # a stray scalar must not raise on the hot path; degrades to unrouted, not global
    assert compile_memory_scope({"tags": "project:mori", "type": "decision"}).tags == frozenset(
        {UNROUTED_TAG}
    )


# ── compile_context_tags ─────────────────────────────────────────────────────


def test_context_safe_mode():
    ctx = compile_context_tags("mori", strict_global=True)
    assert ctx == frozenset({"project:mori"})  # no legacy:type-global in safe


def test_context_all_mode():
    ctx = compile_context_tags("mori", strict_global=False)
    assert ctx == frozenset({"project:mori", LEGACY_TYPE_GLOBAL_TAG})


def test_context_unscoped_safe_is_empty():
    assert compile_context_tags("", strict_global=True) == frozenset()
    assert compile_context_tags(None, strict_global=True) == frozenset()


def test_context_unscoped_all_has_only_legacy():
    assert compile_context_tags("", strict_global=False) == frozenset({LEGACY_TYPE_GLOBAL_TAG})


# ── Pure-function parity vs a reference legacy predicate ──────────────────────


def _legacy_surfaced(row: dict, project: str, strict_global: bool) -> bool:
    """Reference re-implementation of the legacy oracle's UNION membership.

    Mirrors get_memories_by_project's project-lane OR global-lane predicate
    (ignoring tier/ordering, which are partition concerns, not membership).
    A row is surfaced iff it is project-tagged for P, OR global by explicit tag,
    OR (when not strict) an auto-global profile/pattern type.
    """
    tags = set(row.get("tags") or [])
    if f"project:{project}" in tags:
        return True
    if tags & {"scope:global", "scope:cross-project"}:
        return True
    if not strict_global and row.get("type") in {"profile", "pattern"}:
        return True
    return False


def _scope_surfaced(row: dict, project: str, strict_global: bool) -> bool:
    """The H2 path: compile both sides, apply the flat filter."""
    return in_scope(
        compile_memory_scope(row),
        compile_context_tags(project, strict_global),
    )


# A corpus spanning every routing class.
_CORPUS = [
    {"name": "p_decision", "tags": ["project:mori"], "type": "decision"},
    {"name": "p_profile", "tags": ["project:mori"], "type": "profile"},
    {"name": "q_decision", "tags": ["project:bifrost"], "type": "decision"},
    {"name": "q_profile", "tags": ["project:bifrost"], "type": "profile"},
    {"name": "explicit_global", "tags": ["scope:global"], "type": "decision"},
    {
        "name": "explicit_xproj",
        "tags": ["scope:cross-project", "project:bifrost"],
        "type": "decision",
    },
    {"name": "bare_profile", "tags": [], "type": "profile"},
    {"name": "bare_pattern", "tags": [], "type": "pattern"},
    {"name": "bare_decision", "tags": [], "type": "decision"},
    {"name": "multi_project", "tags": ["project:mori", "project:bifrost"], "type": "decision"},
]


def test_parity_truth_table_all_dims():
    for project in ("mori", "bifrost", "absent"):
        for strict in (True, False):
            for row in _CORPUS:
                legacy = _legacy_surfaced(row, project, strict)
                scoped = _scope_surfaced(row, project, strict)
                assert scoped == legacy, (
                    f"{row['name']} project={project} strict={strict}: "
                    f"legacy={legacy} scope={scoped}"
                )
