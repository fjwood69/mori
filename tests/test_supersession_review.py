"""v2.3.11 — supersession half (D4–D5; board ruling 2026-10-03).

Report-only supersession by default (MORI_SUPERSESSION_MODE exact-value rule), decided pairs keyed
by (memory_name, counterpart), the scan fed ONLY the memories actually written (B-C2), per-run
outcomes persisted for cron dreams (B-C4), and the reviewer's tools: unsupersede and decide.

Every D4/D5 test runs on BOTH backends — the two scan branches have different transaction
structure. State is asserted in SQL.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_recovery_path import BACKENDS, GOOD_DESC, _a, requires_pg, run

from mori_advisor import metrics as mx
from mori_advisor import utils as ut


class Classifier:
    """consult_fn stub: records every call, answers a fixed verdict."""

    def __init__(self, verdict="SUPERSEDES"):
        self.verdict = verdict
        self.calls: list[str] = []

    def __call__(self, system, user, vk, max_tokens, temperature, reasoning_effort=None):
        self.calls.append(user)
        return self.verdict

    consult = __call__


def _sup(mode, result):
    return (
        mx.prom_registry.get_sample_value(
            "mori_supersessions_total", {"mode": mode, "result": result}
        )
        or 0.0
    )


def _verdicts(outcome):
    return (
        mx.prom_registry.get_sample_value(
            "mori_classifier_verdicts_total", {"site": "contradiction", "outcome": outcome}
        )
        or 0.0
    )


async def _canon(h, name, body="Canonical body."):
    await h.write(name, body)
    await h.q("UPDATE memories SET tier = 'canonical' WHERE name = $1", name)


async def _scan(h, new_names, clf):
    mems = [{"name": n, "title": n, "body": f"Body of {n}."} for n in new_names]
    return await ut.run_contradiction_scan(mems, consult_fn=clf, store=h.store)


async def _queue(h, cand):
    return await h.q(
        "SELECT id, reason, detail, counterpart, resolved, resolution FROM eviction_queue "
        "WHERE memory_name = $1 ORDER BY id",
        cand,
    )


async def _superseded_is_null(h, name) -> bool:
    rows = await h.q(
        "SELECT COUNT(*) AS n FROM memories WHERE name = $1 AND deleted_at IS NULL "
        "AND superseded_by IS NULL",
        name,
    )
    return rows[0]["n"] == 1


# ── D4: mode ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,mode",
    [
        (None, "report_only"),
        ("", "report_only"),
        ("report_only", "report_only"),
        ("yes", "report_only"),
        ("1", "report_only"),
        ("true", "report_only"),
        ("writes", "report_only"),
        ("write", "write"),
        ("WRITE ", "write"),
    ],
)
def test_supersession_mode_exact_value_rule(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(ut.SUPERSESSION_MODE_ENV, raw)
    assert ut.supersession_mode() == mode


@pytest.mark.parametrize("backend", BACKENDS)
def test_report_only_is_the_default_and_writes_only_a_proposal(backend, tmp_path, monkeypatch):
    monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)

    async def t(h):
        await _canon(h, "mori-old")
        await h.write("mori-new", "New body.")
        before = _sup("report_only", "proposed")
        out = await _scan(h, ["mori-new"], Classifier())
        assert (out.applied, out.proposed) == (0, 1)
        assert await _superseded_is_null(h, "mori-old")
        q = await _queue(h, "mori-old")
        assert [(r["reason"], r["detail"], r["counterpart"], bool(r["resolved"])) for r in q] == [
            ("supersession_proposed", "Proposed: superseded by 'mori-new'", "mori-new", False)
        ]
        assert _sup("report_only", "proposed") == before + 1

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("raw,applies", [("yes", False), ("1", False), ("WRITE ", True)])
def test_only_exact_write_applies(backend, tmp_path, monkeypatch, raw, applies):
    monkeypatch.setenv(ut.SUPERSESSION_MODE_ENV, raw)

    async def t(h):
        await _canon(h, "mori-old")
        await h.write("mori-new", "New body.")
        out = await _scan(h, ["mori-new"], Classifier())
        assert out.applied == (1 if applies else 0)
        assert (not await _superseded_is_null(h, "mori-old")) is applies
        reasons = [r["reason"] for r in await _queue(h, "mori-old")]
        assert reasons == (["superseded"] if applies else ["supersession_proposed"])

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_failed_insert_is_a_failed_supersession_not_a_classifier_error(
    backend, tmp_path, monkeypatch
):
    """M8: the classifier answered; the WRITE failed. Counted as failed, not as error."""
    monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)

    async def t(h):
        await _canon(h, "mori-old")
        await h.write("mori-new", "New body.")
        if h.pg:
            await h.q(
                "ALTER TABLE eviction_queue ADD CONSTRAINT inj_fail "
                "CHECK (memory_name <> 'mori-old') NOT VALID"
            )
        else:
            await h.q(
                "CREATE TRIGGER inj_fail BEFORE INSERT ON eviction_queue "
                "WHEN NEW.memory_name = 'mori-old' BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        errors, failed = _verdicts("error"), _sup("report_only", "failed")
        try:
            out = await _scan(h, ["mori-new"], Classifier())
        finally:
            await h.q(
                "ALTER TABLE eviction_queue DROP CONSTRAINT inj_fail"
                if h.pg
                else "DROP TRIGGER inj_fail"
            )
        assert (out.failed, out.proposed) == (1, 0)
        assert _verdicts("error") == errors
        assert _sup("report_only", "failed") == failed + 1
        assert await _queue(h, "mori-old") == []

    run(backend, tmp_path, t)


# ── D4: decided pairs ────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("mode", ["report_only", "write"])
def test_a_decided_pair_is_never_reclassified(backend, tmp_path, monkeypatch, mode):
    monkeypatch.setenv(ut.SUPERSESSION_MODE_ENV, mode)

    async def t(h):
        await _canon(h, "mori-old")
        await h.write("mori-new", "New body.")
        await h.write("mori-other", "Other body.")
        await _scan(h, ["mori-new"], Classifier())
        again = Classifier()
        out = await _scan(h, ["mori-new"], again)
        assert again.calls == []
        # report-only: the open proposal makes the pair decided; write: the candidate is now
        # superseded and leaves the candidate pool before any pair check.
        assert out.already_decided == (1 if mode == "report_only" else 0)
        # Negative control: a DIFFERENT new memory against the same candidate is classified
        # (report-only only: in write mode the candidate is now superseded and leaves the pool).
        if mode == "report_only":
            other = Classifier("UNRELATED")
            await _scan(h, ["mori-other"], other)
            assert len(other.calls) == 1

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_dismissed_pair_stays_closed_and_undone_pair_is_propose_only(
    backend, tmp_path, monkeypatch
):
    async def t(h):
        await _canon(h, "mori-a")
        await _canon(h, "mori-b")
        await h.write("mori-new", "New body.")
        monkeypatch.setenv(ut.SUPERSESSION_MODE_ENV, "write")
        # a: applied in write mode, then undone by a human -> may be re-classified, proposal only
        # b: proposed in report-only, then dismissed -> closed for good
        await _scan(h, ["mori-new"], Classifier())  # applies both (both canonical, same prefix)
        await _a(h.store.unsupersede("mori-a", note="wrong", actor="td"))
        await _a(h.store.unsupersede("mori-b", note="wrong", actor="td"))
        pid = None
        monkeypatch.setenv(ut.SUPERSESSION_MODE_ENV, "write")
        clf = Classifier()
        out = await _scan(h, ["mori-new"], clf)
        assert len(clf.calls) == 2  # both undone pairs are re-classified...
        assert (out.applied, out.proposed) == (0, 2)  # ...but even in WRITE mode only proposed
        assert await _superseded_is_null(h, "mori-a")
        pid = [r["id"] for r in await _queue(h, "mori-b") if r["reason"] == "supersession_proposed"]
        msg = await _a(h.store.decide_supersession(pid[0], "dismiss", note="no", actor="td"))
        assert msg.startswith("Dismissed")
        clf2 = Classifier()
        await _scan(h, ["mori-new"], clf2)
        assert clf2.calls == []  # a: open proposal (decided); b: dismissed (decided)

    run(backend, tmp_path, t)


# ── D5: unsupersede ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_unsupersede_clears_to_null_bumps_updated_at_resolves_and_audits(
    backend, tmp_path, monkeypatch
):
    monkeypatch.setenv(ut.SUPERSESSION_MODE_ENV, "write")

    async def t(h):
        await _canon(h, "mori-old")
        await h.write("mori-new", "New body.")
        # A tombstoned namesake that is ALSO superseded must keep its flag (id-scoped clear).
        await h.write("mori-old-ns", "x")
        await h.q(
            "UPDATE memories SET name = 'mori-old', deleted_at = '2026-01-01', "
            "superseded_by = 'someone' WHERE name = 'mori-old-ns'"
        )
        await _scan(h, ["mori-new"], Classifier())
        await h.q("UPDATE memories SET updated_at = '2020-01-01' WHERE name = 'mori-old'")
        msg = await _a(h.store.unsupersede("mori-old", note="false positive", actor="td-key"))
        assert msg == "Memory 'mori-old' unsuperseded (was superseded by 'mori-new')."
        assert await _superseded_is_null(h, "mori-old")  # NULL verbatim, never ''
        row = await h.row("mori-old")
        assert str(row["updated_at"]) > "2020-01-02"
        dead = await h.q(
            "SELECT superseded_by FROM memories WHERE name = 'mori-old' AND deleted_at IS NOT NULL"
        )
        assert [d["superseded_by"] for d in dead] == ["someone"]
        q = await _queue(h, "mori-old")
        assert [(r["reason"], bool(r["resolved"]), r["resolution"]) for r in q] == [
            ("superseded", True, "unsuperseded")
        ]
        audit = await h.q(
            "SELECT actor_key_name FROM write_audit WHERE op = 'unsupersede' AND memory_name = $1",
            "mori-old",
        )
        assert [a["actor_key_name"] for a in audit] == ["td-key"]

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_unsupersede_noop_and_marker_row(backend, tmp_path):
    async def t(h):
        await h.write("plain", "x")
        msg = await _a(h.store.unsupersede("plain"))
        assert "nothing to do" in msg
        assert await h.q("SELECT op FROM write_audit WHERE op = 'unsupersede'") == []
        # Superseded with NO queue row (legacy/hand SQL): the undo still marks the pair.
        await h.write("hand", "x")
        await h.q("UPDATE memories SET superseded_by = 'other' WHERE name = 'hand'")
        await _a(h.store.unsupersede("hand"))
        q = await _queue(h, "hand")
        assert [(r["counterpart"], bool(r["resolved"]), r["resolution"]) for r in q] == [
            ("other", True, "unsuperseded")
        ]

    run(backend, tmp_path, t)


# ── D5: decide ───────────────────────────────────────────────────────────────


async def _proposal(h, cand="mori-old", new="mori-new"):
    await _canon(h, cand)
    await h.write(new, "New body.")
    await _scan(h, [new], Classifier())
    return [r["id"] for r in await _queue(h, cand) if r["reason"] == "supersession_proposed"][0]


@pytest.mark.parametrize("backend", BACKENDS)
def test_decide_apply(backend, tmp_path, monkeypatch):
    monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)

    async def t(h):
        pid = await _proposal(h)
        msg = await _a(h.store.decide_supersession(pid, "apply", note="yes", actor="td"))
        assert msg == "Applied: 'mori-old' is now superseded by 'mori-new'."
        assert (await h.row("mori-old"))["superseded_by"] == "mori-new"
        q = await _queue(h, "mori-old")
        assert [(r["reason"], bool(r["resolved"]), r["resolution"]) for r in q] == [
            ("supersession_proposed", True, "applied"),
            ("superseded", False, None),
        ]
        ops = await h.q("SELECT op FROM write_audit WHERE op = 'supersede'")
        assert len(ops) == 1
        again = await _a(h.store.decide_supersession(pid, "apply"))
        assert "already decided" in again

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "spoil", ["cand_superseded", "cand_deleted", "new_superseded", "new_deleted"]
)
def test_decide_apply_refuses_when_either_side_moved(backend, tmp_path, monkeypatch, spoil):
    monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)

    async def t(h):
        pid = await _proposal(h)
        target = "mori-old" if spoil.startswith("cand") else "mori-new"
        if spoil.endswith("superseded"):
            await h.q("UPDATE memories SET superseded_by = 'elsewhere' WHERE name = $1", target)
        else:
            await _a(h.store.soft_delete(target))
        msg = await _a(h.store.decide_supersession(pid, "apply"))
        assert msg.startswith("Not applied"), msg
        q = [r for r in await _queue(h, "mori-old") if r["id"] == pid]
        assert not q[0]["resolved"]  # the proposal stays open for review

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_decide_refuses_non_proposals_and_bad_decisions(backend, tmp_path):
    async def t(h):
        await h.q(
            "INSERT INTO eviction_queue (memory_name, reason, detail) VALUES ('x', 'orphan', '')"
        )
        oid = (await h.q("SELECT id FROM eviction_queue WHERE reason = 'orphan'"))[0]["id"]
        assert "not a proposal" in await _a(h.store.decide_supersession(oid, "apply"))
        assert "Unknown decision" in await _a(h.store.decide_supersession(oid, "approve"))
        assert "not found" in await _a(h.store.decide_supersession(999999, "dismiss"))

    run(backend, tmp_path, t)


@requires_pg
def test_pg_concurrent_double_apply_applies_once(tmp_path, monkeypatch):
    monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)

    async def t(h):
        pid = await _proposal(h)
        a, b = await asyncio.gather(
            h.store.decide_supersession(pid, "apply"), h.store.decide_supersession(pid, "apply")
        )
        assert sorted([a.startswith("Applied"), b.startswith("Applied")]) == [False, True]
        assert len([r for r in await _queue(h, "mori-old") if r["reason"] == "superseded"]) == 1

    run("postgres", tmp_path, t)


# ── B-C2: only written memories are scanned (real dream + real ingestion) ────


@pytest.mark.parametrize("backend", BACKENDS)
def test_dream_scans_only_accepted_writes_and_persists_outcomes(backend, tmp_path, monkeypatch):
    """A real DreamPipeline.run() on a real store: one accepted memory, one empty body, one
    invalid entry, one DOWNGRADED under anatomy enforce (empty reason). Only the accepted one may
    reach the classifier. Also the v2.3.0 regression: on SQLite the dream wrote nothing at all."""
    from mori_advisor.dream import DreamPipeline

    monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)
    monkeypatch.setenv("MORI_ANATOMY_ENFORCE", "enforce")

    async def t(h):
        await _canon(h, "mori-canon")
        clf = Classifier("UNRELATED")
        dp = DreamPipeline(db_path=tmp_path / "memories.db", bifrost_client=clf, store=h.store)
        events = [{"id": 101, "session_id": "s1", "client": "t", "event_name": "Stop"}]
        monkeypatch.setattr(dp.session_log, "read_events", lambda **kw: events, raising=False)
        monkeypatch.setattr(dp, "_run_intake_promotion", lambda *a, **k: _noop())
        monkeypatch.setattr(dp, "_format_events", lambda ev: "events")
        monkeypatch.setattr(dp, "_call_dream_model", lambda text: "model output")
        good = "A real body with a reason given for its existence and enough substance."
        monkeypatch.setattr(
            dp,
            "_parse_response",
            lambda r: [
                {"path": "mori/conventions/accepted", "body": good, "reason": GOOD_DESC},
                {"path": "mori/conventions/empty", "body": "", "reason": GOOD_DESC},
                {"no_path": True},
                {"path": "mori/conventions/downgraded", "body": good, "reason": ""},
            ],
        )
        await dp.run()
        assert await h.row("mori-conventions-accepted") is not None  # SQLite: wrote at all
        assert await h.row("mori-conventions-downgraded") is None
        assert [c.split("\n")[0] for c in clf.calls] == ["new: mori-conventions-accepted"]
        sup = await _a(h.store.get_dream_state("last_run_supersessions"))
        assert json.loads(sup) == {"applied": 0, "proposed": 0, "failed": 0, "already_decided": 0}

    run(backend, tmp_path, t)


async def _noop():
    return None


@pytest.mark.parametrize("backend", BACKENDS)
def test_ingestion_scans_only_accepted_writes(backend, tmp_path, monkeypatch):
    """Board ghost test: a written working memory, a curated canonical candidate (routed to TD
    review, NOT written) and a low-confidence skip — the classifier sees only the first."""
    from mori_advisor.ingestion import IngestionPipeline

    monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)
    monkeypatch.delenv("MORI_CURATE", raising=False)

    async def t(h):
        await _canon(h, "mori-canon")
        src = tmp_path / "notes.md"
        src.write_text("# Notes\n\nSome content worth ingesting.\n")
        clf = Classifier("UNRELATED")
        ip = IngestionPipeline(
            db_path=tmp_path / "memories.db", bifrost_client=clf, memory_store=h.mem, store=h.store
        )
        mem = {"body": "Body.", "description": GOOD_DESC, "title": "t"}
        monkeypatch.setattr(
            ip,
            "_distill_batch",
            lambda *a, **k: [
                {**mem, "name": "mori-written", "confidence": 0.9, "tier": "working"},
                {**mem, "name": "mori-curated", "confidence": 0.9, "tier": "canonical"},
                {**mem, "name": "mori-lowconf", "confidence": 0.2, "tier": "working"},
            ],
        )
        await ip.ingest([str(src)], force=True)
        assert [c.split("\n")[0] for c in clf.calls] == ["new: mori-written"]
        assert await h.row("mori-curated") is None  # routed to review, not in canon

    run(backend, tmp_path, t)


# ── Visibility, export, migration 17 ─────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
def test_memory_review_lists_proposals_and_pair_states(backend, tmp_path, monkeypatch):
    from test_mcp_tools import _apply_store

    monkeypatch.delenv(ut.SUPERSESSION_MODE_ENV, raising=False)

    async def t(h):
        pid = await _proposal(h)
        _apply_store(monkeypatch, h.store)
        from mori_advisor.main import memory_review

        out = await memory_review()
        assert f"[{pid}] **mori-old**: Proposed: superseded by 'mori-new'" in out
        assert "Pairs: 0 dismissed" in out

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_canon_export_excludes_superseded_rows(backend, tmp_path):
    async def t(h):
        await _canon(h, "mori-live")
        await _canon(h, "mori-gone")
        await h.q("UPDATE memories SET superseded_by = 'mori-live' WHERE name = 'mori-gone'")
        rows = await _a(h.mem.export_rows(tiers=("canonical",)))
        assert [r["name"] for r in rows] == ["mori-live"]

    run(backend, tmp_path, t)


@pytest.mark.parametrize("backend", BACKENDS)
def test_migration_17_backfills_pairs_and_single_incarnation_memory_ids(backend, tmp_path):
    """Addition 5: memory_id is backfilled ONLY where a name has one incarnation."""
    from mori_advisor.store.migrations import _recovery_keys_postgres, _recovery_keys_sqlite

    async def t(h):
        await h.write("one", "x")
        await h.write("two", "a")
        await _a(h.store.soft_delete("two"))
        await h.write("two", "b")
        await h.q(
            "INSERT INTO memory_versions (memory_name, title, body, version_note) VALUES "
            "('one', 't', 'v', 'legacy'), ('two', 't', 'v', 'legacy')"
        )
        await h.q(
            "INSERT INTO eviction_queue (memory_name, reason, detail) VALUES "
            "('c1', 'superseded', 'Superseded by ''n1'''), "
            "('c2', 'supersession_proposed', 'Proposed: superseded by ''n2''')"
        )
        if h.pg:
            async with h.store.pool.acquire() as conn:
                async with conn.transaction():
                    await _recovery_keys_postgres(conn)
        else:
            conn = h.mem._get_conn()
            try:
                _recovery_keys_sqlite(conn, None)
                conn.commit()
            finally:
                conn.close()
        legacy = {
            r["memory_name"]: r["memory_id"]
            for r in await h.q(
                "SELECT memory_name, memory_id FROM memory_versions WHERE version_note = 'legacy'"
            )
        }
        one_id = (await h.row("one"))["id"]
        assert legacy == {"one": one_id, "two": None}
        pairs = {
            r["memory_name"]: r["counterpart"]
            for r in await h.q("SELECT memory_name, counterpart FROM eviction_queue")
        }
        assert pairs == {"c1": "n1", "c2": "n2"}

    run(backend, tmp_path, t)


# ── D5 surface: role gating ──────────────────────────────────────────────────


def test_review_tools_require_dreamer(tmp_path, monkeypatch):
    from test_policy import _actor_context, _patch_policy

    from mori_advisor.main import memory_supersession_decide, memory_unsupersede
    from mori_advisor.policy import Actor

    _patch_policy(monkeypatch, "api")

    async def go():
        with _actor_context(Actor("ci", "write")):
            a = await memory_unsupersede("x")
            b = await memory_supersession_decide(1, "apply")
        return a, b

    a, b = asyncio.run(go())
    assert "dreamer" in a and "dreamer" in b
