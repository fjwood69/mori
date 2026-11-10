"""v2.3.18: the global lane of /brief admits canonical rows only (board, 10 Nov 2026).

Environment: the reference instance in api mode with the v2.3.16 canon lock. Threat model: the mori boundary — a write
key acting through mori's MCP/REST surface. A write key cannot produce a canonical row on its own, so canonical is the
only tier that means "a reviewer accepted it"; before this release a working row tagged ``scope:global`` reached every
session. The project lane is unchanged (canonical + working), and ``tier_gate=False`` (the ``/req`` listing only)
bypasses tier on both lanes, so a demoted requirement does not vanish from its list.
"""

from __future__ import annotations

import inspect

import pytest

from tests.test_canon_lock import _ms
from tests.test_canon_lock_more import _exec
from tests.test_mcp_tools import BACKENDS, _run_with_backend


async def _a(x):
    return await x if inspect.isawaitable(x) else x


async def _put(store, name, tier, tags, title=None):
    """Write a row at ``tier`` directly (canonical with the seed token — the only way tests create canon)."""
    from mori_advisor.canon_authority import CanonAuthority
    from mori_advisor.provenance import SYSTEM

    r = await _a(
        _ms(store)._write(
            name=name,
            title=title or f"{name} title",
            description=f"{tier} row for a test",
            type="pattern",
            tier=tier,
            body=f"BODY-OF-{name}",
            tags=tags,
            provenance=SYSTEM,
            _canon_authority=CanonAuthority("test-seed") if tier == "canonical" else None,
        )
    )
    assert r.accepted, r


async def _seed(store):
    await _put(store, "g-canon", "canonical", ["scope:global"])
    await _put(store, "g-work", "working", ["scope:global"])
    await _put(store, "g-eph", "ephemeral", ["scope:global"])
    await _put(store, "x-work", "working", ["scope:cross-project"])
    # Tagged for the demo project as well: it belongs to demo's project lane, not to demo's global lane.
    await _put(store, "demo-work-global", "working", ["project:demo", "scope:global"])
    await _put(store, "demo-work", "working", ["project:demo"])


def _names(lane):
    return {m["name"] for m in lane}


# ── the rule itself ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tier,expected", [("canonical", True), ("working", False), ("ephemeral", False)]
)
@pytest.mark.parametrize("project", ["demo", None])
def test_the_global_lane_admits_canonical_only(tier, expected, project):
    from mori_advisor.resolver import brief_membership

    row = {"name": "r", "type": "pattern", "tier": tier, "tags": ["scope:global"]}
    assert brief_membership(row, project, strict_global=True) is expected


def test_the_project_lane_is_unchanged():
    from mori_advisor.resolver import brief_membership

    for tier, expected in (("canonical", True), ("working", True), ("ephemeral", False)):
        row = {
            "name": "r",
            "type": "pattern",
            "tier": tier,
            "tags": ["project:demo", "scope:global"],
        }
        assert brief_membership(row, "demo", strict_global=True) is expected, tier


@pytest.mark.parametrize("tier", ["working", "ephemeral"])
def test_tier_gate_false_bypasses_tier_on_both_lanes(tier):
    """/req is the one caller: a demoted requirement must still list."""
    from mori_advisor.resolver import brief_membership

    glob = {"name": "r", "type": "requirement", "tier": tier, "tags": ["scope:global"]}
    proj = {"name": "r", "type": "requirement", "tier": tier, "tags": ["project:mori"]}
    assert brief_membership(glob, "mori", strict_global=True, tier_gate=False) is True
    assert brief_membership(glob, None, strict_global=True, tier_gate=False) is True
    assert brief_membership(proj, "mori", strict_global=True, tier_gate=False) is True


def test_a_stored_scope_map_is_still_bound_by_tier():
    """A row whose stored scope map says global (H2-native) is gated by tier exactly like a tagged one."""
    from mori_advisor.resolver import brief_membership

    for tier, expected in (("canonical", True), ("working", False)):
        row = {"name": "r", "type": "pattern", "tier": tier, "tags": [], "scope": '{"tags": []}'}
        assert brief_membership(row, "demo", strict_global=True) is expected, tier
        assert brief_membership(row, None, strict_global=True) is expected, tier


def test_scope_still_decides_before_tier():
    """Tier never lets in a row that scope keeps out."""
    from mori_advisor.resolver import brief_membership

    row = {"name": "r", "type": "pattern", "tier": "canonical", "tags": ["project:other"]}
    assert brief_membership(row, "demo", strict_global=True) is False
    assert brief_membership(row, "demo", strict_global=True, tier_gate=False) is False


# ── both backends' filter_by_scope ────────────────────────────────────────────────────
#
# The project lane is tier-gated TWICE in each store: the candidates query prefilters
# ``tier IN ('canonical', 'working')`` and the Python filter calls ``brief_membership`` (tier_gate=True).
# Because of the prefilter, switching the Python filter alone to ``tier_gate=False`` changes nothing
# here — mutants S5/P5 (v2.3.18 run) survive as EQUIVALENT, not as a test gap. Removing both layers
# (S7/P7) is killed: SQLite by test_filter_by_scope::test_candidate_tier_project_row_dropped_not_globalised
# and test_parity_manifest_byte_identical; Postgres by test_parity_manifest_pg. If you change or drop
# that SQL prefilter, S5/P5 stop being equivalent: the Python gate becomes the only tier guard on the
# project lane (as it already is on the post-compact delta's project rows), so add a test that fails
# when it is bypassed.


@pytest.mark.parametrize("backend", BACKENDS)
def test_filter_by_scope_global_lane_is_canonical_only(backend, tmp_path, monkeypatch):
    async def run(store):
        await _seed(store)
        for project in ("demo", ""):
            lanes = await _a(
                _ms(store).filter_by_scope(project, include_global=True, strict_global=True)
            )
            assert _names(lanes["global_memories"]) == {"g-canon"}, (
                project,
                lanes["global_memories"],
            )
            assert all(m["tier"] == "canonical" for m in lanes["global_memories"])
        demo = await _a(_ms(store).filter_by_scope("demo", include_global=True, strict_global=True))
        assert _names(demo["project_memories"]) == {"demo-work-global", "demo-work"}, demo

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_filter_by_scope_counts_what_the_global_lane_withholds(backend, tmp_path, monkeypatch):
    """Working AND ephemeral global rows are counted; a row shown in this call's project lane is not."""

    async def run(store):
        await _seed(store)
        demo = await _a(_ms(store).filter_by_scope("demo", include_global=True, strict_global=True))
        assert demo["global_withheld"] == 3, (
            demo
        )  # g-work, g-eph, x-work — not demo-work-global (shown)
        unscoped = await _a(_ms(store).filter_by_scope("", include_global=True, strict_global=True))
        assert unscoped["global_withheld"] == 4, unscoped  # + demo-work-global, a global row here
        off = await _a(_ms(store).filter_by_scope("demo", include_global=False, strict_global=True))
        assert off["global_withheld"] == 0 and off["global_memories"] == []

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_stored_scope_map_row_is_bound_by_tier_in_the_store(backend, tmp_path, monkeypatch):
    async def run(store):
        await _put(store, "h2-work", "working", ["unrelated"])
        await _put(store, "h2-canon", "canonical", ["unrelated"])
        for name in ("h2-work", "h2-canon"):
            await _exec(
                store,
                "UPDATE memories SET scope = ? WHERE name = ?",
                "UPDATE memories SET scope = $1 WHERE name = $2",
                '{"tags": []}',
                name,
            )
        lanes = await _a(
            _ms(store).filter_by_scope("demo", include_global=True, strict_global=True)
        )
        assert _names(lanes["global_memories"]) == {"h2-canon"}, lanes["global_memories"]
        assert lanes["global_withheld"] == 1

    _run_with_backend(backend, tmp_path, monkeypatch, run)


# ── the brief ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_scoped_and_safe_briefs_show_no_unreviewed_global_row(backend, tmp_path, monkeypatch):
    from mori_advisor.main import brief

    async def run(store):
        await _seed(store)
        scoped = await brief(project="demo")
        safe = await brief()
        for out in (scoped, safe):
            assert "**g-canon**" in out, out
            for name in ("g-work", "g-eph", "x-work"):
                assert f"**{name}**" not in out, (name, out)
        assert "**demo-work-global**" in scoped  # its own project's lane
        assert "**demo-work-global**" not in safe
        assert "3 global memories are not shown until reviewed (not canonical)" in scoped, scoped
        assert "4 global memories are not shown until reviewed" in safe, safe

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_withheld_line_when_nothing_is_withheld(backend, tmp_path, monkeypatch):
    from mori_advisor.main import brief

    async def run(store):
        await _put(store, "g-canon", "canonical", ["scope:global"])
        for out in (await brief(project="demo"), await brief()):
            assert "not shown until reviewed" not in out, out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_post_compact_delta_applies_the_same_rule(backend, tmp_path, monkeypatch):
    from mori_advisor.main import brief

    async def run(store):
        await _seed(store)
        for project in ("demo", None):
            out = await brief(post_compact=True, project=project, since="1h")
            assert "**g-canon**" in out, (project, out)
            for name in ("g-work", "g-eph", "x-work"):
                assert f"**{name}**" not in out, (project, name, out)
        demo = await brief(post_compact=True, project="demo", since="1h")
        assert "**demo-work-global**" in demo, demo

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_req_still_lists_a_working_global_requirement(backend, tmp_path, monkeypatch):
    """The one tier_gate=False caller keeps listing (test_req_list's req-g, restated against this release)."""
    from mori_advisor.main import memory_req

    async def run(store):
        r = store.write(
            name="req-glob",
            title="A global requirement",
            body="b",
            type="requirement",
            tier="working",
            tags=["scope:global", "status-pending", "pri-low"],
        )
        await _a(r)
        out = await memory_req(status="all")
        assert any(line.startswith("req-glob") for line in out.splitlines()), out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_scope_all_mode_stays_outside_the_guarantee(backend, tmp_path, monkeypatch):
    """Written down, not dormant: scope="all" (opt-in) keeps its two raw bypasses. Its unscoped brief counts every
    row, any tier; its unscoped delta names a working global row. Its SCOPED brief is still tier-gated."""
    from mori_advisor.main import brief

    async def run(store):
        await _seed(store)
        unscoped = await brief(scope="all")
        assert "**Shared memories:** 6 loaded" in unscoped, unscoped
        delta = await brief(post_compact=True, project=None, since="1h", scope="all")
        assert "**g-work**" in delta and "**g-eph**" in delta, delta
        scoped = await brief(project="demo", scope="all")
        assert "**g-canon**" in scoped and "**g-work**" not in scoped, scoped

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_withheld_line_shows_when_every_global_row_is_withheld(backend, tmp_path, monkeypatch):
    from mori_advisor.main import brief

    async def run(store):
        await _put(store, "g-work", "working", ["scope:global"])
        for out in (await brief(project="demo"), await brief()):
            assert "**g-work**" not in out and "**Global memories:**" not in out, out
            assert "1 global memory is not shown until reviewed (not canonical)" in out, out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_delta_superseded_feed_applies_the_same_rule(backend, tmp_path, monkeypatch):
    """A superseded working global row is not named; a superseded canonical one is, and its working replacement is
    shown as [withheld]."""
    from mori_advisor.main import brief

    async def run(store):
        await _put(store, "g-old-canon", "canonical", ["scope:global"])
        await _put(store, "g-new-work", "working", ["scope:global"])
        await _put(store, "g-old-work", "working", ["scope:global"])
        for old in ("g-old-canon", "g-old-work"):
            await _exec(
                store,
                "UPDATE memories SET superseded_by = 'g-new-work', updated_at = datetime('now') WHERE name = ?",
                "UPDATE memories SET superseded_by = 'g-new-work', updated_at = now() WHERE name = $1",
                old,
            )
        for project in ("demo", None):
            out = await brief(post_compact=True, project=project, since="1h")
            assert "**g-old-canon** → [withheld]" in out, (project, out)
            assert "**g-old-work**" not in out and "**g-new-work**" not in out, (project, out)

    _run_with_backend(backend, tmp_path, monkeypatch, run)
