"""Section C (v2.3.15): `/req list`. `memory_req` lists requirements under the brief's scope rules (board, 2 Nov).

Facts this pins (checked before building, 2 Nov):
- Requirements are tagged `project-<p>` (a hyphen, the /req convention), while brief scope reads `project:<p>`. Without
  a read-time mapping, every requirement compiles to UNROUTED, and a scoped list would show nothing.
- `memory_req` with no project listed every project's requirements (a cross-project leak), and neither backend's
  `get_requirements` excluded deleted or superseded rows.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from tests.test_b1_fix import _sql
from tests.test_mcp_tools import BACKENDS, _run_with_backend

ROOT = Path(__file__).resolve().parent.parent


async def _w(store, name, tags, title=None, tier="working"):
    r = store.write(
        name=name,
        title=title or f"Title of {name}",
        body=f"body of {name}",
        type="requirement",
        tier=tier,
        tags=tags,
    )
    return await r if inspect.isawaitable(r) else r


async def _seed(store):
    await _w(store, "req-a", ["project-mori", "status-pending", "pri-high"])
    await _w(store, "req-b", ["project-mori", "status-done", "pri-medium"])
    await _w(store, "req-c", ["project-other", "status-pending", "pri-low"])
    await _w(store, "req-d", ["project-mori", "status-blocked"])  # no priority
    await _w(store, "req-e", ["project-mori", "pri-medium"])  # no status
    await _w(store, "req-g", ["scope:global", "status-pending", "pri-low"])  # explicitly global
    await _w(store, "req-dual", ["project-mori", "project-other", "status-in-progress", "pri-high"])
    await _w(store, "req-tier", ["project-mori", "status-pending", "pri-low"], tier="ephemeral")
    await _w(store, "req-del", ["project-mori", "status-pending", "pri-high"])
    await _w(store, "req-sup", ["project-mori", "status-pending", "pri-high"])
    await _sql(
        store,
        "UPDATE memories SET deleted_at = datetime('now') WHERE name = ?",
        "UPDATE memories SET deleted_at = now() WHERE name = $1",
        "req-del",
    )
    await _sql(
        store,
        "UPDATE memories SET superseded_by = 'req-a' WHERE name = ?",
        "UPDATE memories SET superseded_by = 'req-a' WHERE name = $1",
        "req-sup",
    )


ALL = {
    "req-a",
    "req-b",
    "req-c",
    "req-d",
    "req-e",
    "req-g",
    "req-dual",
    "req-tier",
    "req-del",
    "req-sup",
}


def _listed(out: str) -> set[str]:
    return {line.split()[0] for line in out.splitlines() if line.startswith("req-")}


@pytest.mark.parametrize("backend", BACKENDS)
def test_project_lists_its_requirements_through_the_hyphen_mapping(backend, tmp_path, monkeypatch):
    """`project-mori` counts as `project:mori` for listing (8 in the design); the global one is included."""
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        out = await memory_req(project="mori", status="all")
        assert _listed(out) == {
            "req-a",
            "req-b",
            "req-d",
            "req-e",
            "req-g",
            "req-dual",
            "req-tier",
        }, out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_deleted_and_superseded_are_never_listed(backend, tmp_path, monkeypatch):
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        out = await memory_req(all_projects=True, status="all")
        assert not ({"req-del", "req-sup"} & _listed(out)), out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_default_status_open_is_pending_in_progress_and_blocked(backend, tmp_path, monkeypatch):
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        out = await memory_req(project="mori")
        assert _listed(out) == {"req-a", "req-d", "req-g", "req-dual", "req-tier"}, out
        assert _listed(await memory_req(project="mori", status="done")) == {"req-b"}

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_priority_filter(backend, tmp_path, monkeypatch):
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        assert _listed(await memory_req(project="mori", priority="high")) == {"req-a", "req-dual"}

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_project_lists_only_global_and_counts_the_rest(backend, tmp_path, monkeypatch):
    """The scope rule binds the tool itself, not just the skill (11). None of the rest are named."""
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        out = await memory_req(status="all")
        assert _listed(out) == {"req-g"}, out
        for name in ALL - {"req-g"}:
            assert name not in out, (name, out)
        assert "7 withheld" in out, out  # live, non-global: a, b, c, d, e, dual, tier

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_tag_does_not_bypass_scope(backend, tmp_path, monkeypatch):
    """C1 #1: the old 'tag overrides project/status' path leaked across projects."""
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        out = await memory_req(tag="pri-high", status="all")
        assert _listed(out) == set(), out
        assert "req-a" not in out and "req-dual" not in out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_all_projects_lists_across_projects(backend, tmp_path, monkeypatch):
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        out = await memory_req(all_projects=True, status="all")
        assert _listed(out) == ALL - {"req-del", "req-sup"}, out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_limit_applies_after_filtering_and_the_counts_cover_the_full_set(
    backend, tmp_path, monkeypatch
):
    """C1 #3."""
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        out = await memory_req(project="mori", status="all", limit=2)
        assert len(_listed(out)) == 2, out
        assert "2 of 7 shown" in out, out

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_footer_counts_status_and_missing_fields(backend, tmp_path, monkeypatch):
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        out = await memory_req(project="mori", status="all")
        footer = out.splitlines()[-1]
        for part in (
            "3 pending",
            "1 in-progress",
            "1 blocked",
            "1 done",
            "1 no status",
            "missing status: 1",
            "missing priority: 1",
        ):
            assert part in footer, (part, footer)

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_done_items_say_self_attested_with_the_last_writer(backend, tmp_path, monkeypatch):
    """The data can say who last wrote a done requirement, not who marked it done (design C.2)."""
    from mori_advisor.main import memory_req

    async def run(store):
        await _seed(store)
        line = next(
            x
            for x in (await memory_req(project="mori", status="done")).splitlines()
            if x.startswith("req-b")
        )
        assert "self-attested" in line and "last write by" in line, line
        await _sql(
            store,
            "DELETE FROM write_audit WHERE memory_name = ?",
            "DELETE FROM write_audit WHERE memory_name = $1",
            "req-b",
        )
        line = next(
            x
            for x in (await memory_req(project="mori", status="done")).splitlines()
            if x.startswith("req-b")
        )
        assert "self-attested" in line and "unattributed" in line, line

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_one_line_per_requirement_escaped_and_truncated(backend, tmp_path, monkeypatch):
    """C1 #7 / R2 #7: fields from storage are untrusted: no table breaks, no newlines, no backticks."""
    from mori_advisor.main import memory_req

    async def run(store):
        await _w(
            store,
            "req-x",
            ["project-mori", "status-pending", "pri-low"],
            title="evil | title\nnext `code` " + "y" * 80,
        )
        out = await memory_req(project="mori")
        lines = [x for x in out.splitlines() if x.startswith("req-x")]
        assert len(lines) == 1, out
        line = lines[0]
        assert "`" not in line and "\n" not in line
        assert "evil | title" not in line  # the pipe is escaped
        title = line.split("  ")[-1]
        assert len(title) <= 61, title  # 60 characters plus an ellipsis

    _run_with_backend(backend, tmp_path, monkeypatch, run)


@pytest.mark.parametrize("backend", BACKENDS)
def test_listing_writes_nothing(backend, tmp_path, monkeypatch):
    """Read-only (7): row counts of memories, versions and the audit log are unchanged by listing."""
    from mori_advisor.main import memory_req

    async def counts(store):
        out = []
        for t in ("memories", "memory_versions", "write_audit", "pending_writes"):
            if hasattr(store, "pool") and store.pool is not None:
                async with store.pool.acquire() as c:
                    out.append(await c.fetchval(f"SELECT COUNT(*) FROM {t}"))
            else:
                conn = store._mem._get_conn()
                out.append(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                conn.close()
        return out

    async def run(store):
        await _seed(store)
        before = await counts(store)
        await memory_req(project="mori", status="all")
        await memory_req(all_projects=True, status="done")
        await memory_req()
        assert await counts(store) == before

    _run_with_backend(backend, tmp_path, monkeypatch, run)


def test_memory_req_source_has_no_write_call():
    """Standing guard: the listing path contains no write, approve, delete or queue call."""
    src = (ROOT / "mori_advisor" / "main.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "memory_req"
    )
    called = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not called & {
        "write",
        "approve",
        "reject",
        "delete",
        "queue_pending_write",
        "soft_delete",
        "update",
    }, called


def test_tier_gate_is_kept_for_briefs_and_off_for_listing():
    """C1 #4: brief_membership keeps its project-lane tier gate by default; listing turns it off."""
    from mori_advisor.resolver import brief_membership

    row = {"name": "r", "type": "requirement", "tier": "ephemeral", "tags": ["project:mori"]}
    assert brief_membership(row, "mori", strict_global=True) is False
    assert brief_membership(row, "mori", strict_global=True, tier_gate=False) is True


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_row_whose_membership_cannot_be_decided_is_withheld(backend, tmp_path, monkeypatch):
    """Fail closed: if membership raises for a row, that row is counted as withheld, never listed."""
    import mori_advisor.resolver as resolver
    from mori_advisor.main import memory_req

    real = resolver.brief_membership

    def flaky(row, *a, **k):
        if row.get("name") == "req-a":
            raise ValueError("malformed")
        return real(row, *a, **k)

    async def run(store):
        await _seed(store)
        monkeypatch.setattr(resolver, "brief_membership", flaky)
        out = await memory_req(project="mori", status="all")
        assert "req-a" not in _listed(out) and "req-b" in _listed(out), out
        assert "2 withheld by scope" in out.splitlines()[-1], (
            out
        )  # req-c (another project) + req-a (undecidable)

    _run_with_backend(backend, tmp_path, monkeypatch, run)
