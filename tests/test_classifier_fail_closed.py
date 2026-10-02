"""v2.3.9 — fail-closed one-word classifiers (freshness, contradiction scan, intake assessor).

The gateway's "DS V4 Flash" rule began serving a reasoning model (DeepSeek V4.1 Flash). With the
classifiers' small max_tokens it spent the whole budget thinking and returned EMPTY content with
finish_reason=length. Freshness mapped every non-NO/STALE reply to "fresh" and stamped canonical
memories fresh on zero answer tokens; the contradiction scan went silently blind.

The fake engine is the real BifrostClient + real OpenAI SDK over an in-process HTTP transport,
returning what the served model returned in production: empty content + finish_reason=length
when reasoning is on; the answer in one or two tokens when the request carries
reasoning_effort="none". Every negative test is paired with a positive control proving the write
path it guards actually fires. Store-touching tests run on SQLite always and on Postgres when
MORI_TEST_DATABASE_URL is set (CI's Postgres job), with identical assertions.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mori_advisor import bifrost_client as bc
from mori_advisor import metrics as mx
from mori_advisor import utils as ut

PG_URL = os.environ.get("MORI_TEST_DATABASE_URL", "")
REPO = Path(__file__).resolve().parent.parent

FRESHNESS = ("YES", "NO", "STALE")
SCAN = ("SUPERSEDES", "RELATED", "UNRELATED")
REASONING_LEAK = "We need answer exactly one word: YES, NO, or STALE. Need"


# ── Fake engine: what the served model did in production ─────────────────────


class _Engine:
    """In-process stand-in for the gateway + a reasoning model.

    ``answer`` maps the request's user message to the reply text. A request WITHOUT
    ``reasoning_effort="none"`` gets what V4.1 Flash returned on 2026-10-02: empty content,
    finish_reason=length. ``status`` lets a test make the gateway fail (HTTP 400 is not retried
    by the SDK, so it surfaces as one exception).
    """

    def __init__(self, answer="YES", status: int = 200):
        self.answer = answer
        self.status = status
        self.requests: list[dict] = []

    def reply_for(self, user: str) -> str:
        return self.answer(user) if callable(self.answer) else self.answer

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "rejected"}})
        if body.get("reasoning_effort") == "none":
            content, finish, tokens = self.reply_for(body["messages"][-1]["content"]), "stop", 2
        else:
            content, finish, tokens = "", "length", body.get("max_tokens", 16)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "accounts/fireworks/models/deepseek-v4p1-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": finish,
                    }
                ],
                "usage": {
                    "prompt_tokens": 191,
                    "completion_tokens": tokens,
                    "total_tokens": 191 + tokens,
                },
                "extra_fields": {"provider": "fireworks"},
            },
        )

    def users(self) -> list[str]:
        return [r["messages"][-1]["content"] for r in self.requests]


def _client_over(monkeypatch, engine: _Engine) -> bc.BifrostClient:
    transport = httpx.MockTransport(engine.handler)
    monkeypatch.setattr(
        bc, "DefaultHttpxClient", lambda **kw: httpx.Client(transport=transport, **kw)
    )
    return bc.BifrostClient(base_url="http://bifrost.test", timeout=5)


def _sample(name: str, labels: dict) -> float:
    return mx.prom_registry.get_sample_value(name, labels) or 0.0


def _verdicts(site: str, outcome: str) -> float:
    return _sample("mori_classifier_verdicts_total", {"site": site, "outcome": outcome})


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    import mori_advisor.memory_store as ms

    monkeypatch.delenv(ut.CLASSIFIER_REASONING_EFFORT_ENV, raising=False)
    ms._freshness_cache.clear()
    yield
    ms._freshness_cache.clear()


# ── Store backends (identical assertions on SQLite and Postgres) ─────────────


class _Backend:
    def __init__(self, kind: str, tmp_path: Path):
        self.kind = kind
        # A unique first dash-token keeps the scan's prefix-LIKE candidates inside this test.
        self.prefix = "fc" + uuid.uuid4().hex[:10]
        self.loop = None
        if kind == "sqlite":
            from mori_advisor.store.sqlite_store import SQLiteStore

            self.store = SQLiteStore(tmp_path / "mem.db")
            self.store.bootstrap()
        else:
            from mori_advisor.store.postgres_store import PostgresStore

            self.loop = asyncio.new_event_loop()
            self.store = PostgresStore(PG_URL)
            self.run(self.store.bootstrap())

    def run(self, value):
        if not inspect.isawaitable(value):
            return value
        if self.loop is not None:
            return self.loop.run_until_complete(value)
        return asyncio.run(value)

    def name(self, suffix: str) -> str:
        return f"{self.prefix}-{suffix}"

    def write(
        self, suffix: str, tier: str = "canonical", body: str = "Some infrastructure detail."
    ):
        self.run(
            self.store.write(
                name=self.name(suffix),
                title=f"Memory {suffix}",
                type="project",
                tier=tier,
                body=body,
                tags=["infrastructure"],
                _skip_protection=True,
            )
        )

    def soft_delete(self, suffix: str):
        self.run(self.store.soft_delete(self.name(suffix)))

    def rows(self, suffix: str) -> list[dict]:
        cols = ("id", "freshness_status", "freshness_checked_at", "superseded_by", "deleted_at")
        sql = f"SELECT {', '.join(cols)} FROM memories WHERE name = {{}} ORDER BY id"
        if self.kind == "sqlite":
            conn = self.store.get_conn()
            try:
                found = conn.execute(sql.format("?"), (self.name(suffix),)).fetchall()
            finally:
                conn.close()
            return [dict(zip(cols, r)) for r in found]

        async def _q():
            async with self.store.pool.acquire() as conn:
                return [dict(r) for r in await conn.fetch(sql.format("$1"), self.name(suffix))]

        return self.run(_q())

    def active(self, suffix: str) -> dict:
        (row,) = [r for r in self.rows(suffix) if r["deleted_at"] is None]
        return row

    def evictions(self, suffix: str) -> int:
        sql = "SELECT count(*) FROM eviction_queue WHERE memory_name = {}"
        if self.kind == "sqlite":
            conn = self.store.get_conn()
            try:
                return conn.execute(sql.format("?"), (self.name(suffix),)).fetchone()[0]
            finally:
                conn.close()

        async def _q():
            async with self.store.pool.acquire() as conn:
                return await conn.fetchval(sql.format("$1"), self.name(suffix))

        return self.run(_q())

    def freshness(self, llm_consult) -> dict:
        return self.run(self.store.check_freshness(llm_consult, limit=50))

    def scan(self, client, suffixes: list[str], pipeline: str = "dream") -> int:
        """Run the scan through the REAL production wrapper (dream or ingestion)."""
        from mori_advisor.dream import DreamPipeline
        from mori_advisor.ingestion import IngestionPipeline

        new = [
            {"name": self.name(s), "title": f"Memory {s}", "body": f"Body of {s}."}
            for s in suffixes
        ]
        if pipeline == "dream":
            owner = SimpleNamespace(client=client, db_path=None, store=self.store)
            return self.run(DreamPipeline._contradiction_scan(owner, new))
        owner = SimpleNamespace(client=client, db_path=None, _store=self.store)
        return self.run(IngestionPipeline._contradiction_scan(owner, new))

    def fail_eviction_insert_for(self, suffix: str):
        """Make the database itself reject the eviction-queue INSERT for one memory."""
        target = self.name(suffix)
        if self.kind == "sqlite":
            conn = self.store.get_conn()
            try:
                conn.execute(
                    f"CREATE TRIGGER {self.prefix}_fail BEFORE INSERT ON eviction_queue "
                    f"WHEN NEW.memory_name = '{target}' BEGIN SELECT RAISE(ABORT, 'injected'); END"
                )
                conn.commit()
            finally:
                conn.close()
            return

        async def _q():
            async with self.store.pool.acquire() as conn:
                await conn.execute(
                    f"CREATE FUNCTION {self.prefix}_fail() RETURNS trigger AS $$ BEGIN "
                    f"IF NEW.memory_name = '{target}' THEN RAISE EXCEPTION 'injected'; END IF; "
                    "RETURN NEW; END $$ LANGUAGE plpgsql"
                )
                await conn.execute(
                    f"CREATE TRIGGER {self.prefix}_fail BEFORE INSERT ON eviction_queue "
                    f"FOR EACH ROW EXECUTE FUNCTION {self.prefix}_fail()"
                )

        self.run(_q())
        self.trigger = True

    def close(self):
        if self.kind != "pg":
            return

        async def _cleanup():
            async with self.store.pool.acquire() as conn:
                if getattr(self, "trigger", False):
                    await conn.execute(
                        f"DROP TRIGGER IF EXISTS {self.prefix}_fail ON eviction_queue"
                    )
                    await conn.execute(f"DROP FUNCTION IF EXISTS {self.prefix}_fail()")
                await conn.execute(
                    "DELETE FROM eviction_queue WHERE memory_name LIKE $1", f"{self.prefix}-%"
                )
                await conn.execute("DELETE FROM memories WHERE name LIKE $1", f"{self.prefix}-%")
            await self.store.pool.close()

        self.run(_cleanup())
        self.loop.close()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "pg", marks=pytest.mark.skipif(not PG_URL, reason="MORI_TEST_DATABASE_URL not set")
        ),
    ]
)
def backend(request, tmp_path):
    b = _Backend(request.param, tmp_path)
    yield b
    b.close()


# ── D1: the parser ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,allowed,expected",
    [
        ("YES", FRESHNESS, "YES"),
        ("yes", FRESHNESS, "YES"),
        ("  YES \n", FRESHNESS, "YES"),
        ("YES.", FRESHNESS, "YES"),  # step 2: one trailing "."
        ("**STALE**", FRESHNESS, "STALE"),  # step 3
        ("`NO`", FRESHNESS, "NO"),
        ('"YES"', FRESHNESS, "YES"),
        ("'stale'", FRESHNESS, "STALE"),
        ("*YES*.", FRESHNESS, "YES"),  # "." stripped before "*" (pinned order)
        ("SUPERSEDES", SCAN, "SUPERSEDES"),
        ("related.", SCAN, "RELATED"),
        ("ok", ("OK",), "OK"),  # control for the Kelvin-sign row below
    ],
)
def test_parser_accepts_exactly_one_allowed_token(text, allowed, expected):
    assert ut.parse_one_word_verdict(text, allowed) == expected


@pytest.mark.parametrize(
    "text,allowed",
    [
        ("", FRESHNESS),
        (None, FRESHNESS),
        (REASONING_LEAK, FRESHNESS),  # contains every token — substring matching would accept it
        ("YES NO", FRESHNESS),
        ("YES,", FRESHNESS),  # the strip set is deliberately NOT widened
        ("YES..", FRESHNESS),  # only ONE trailing "."
        ('"YES."', FRESHNESS),  # "." inside the quotes is not trailing at step 2
        ("```\nYES\n```", FRESHNESS),  # fenced block: no second whitespace strip after step 3
        ("FRESH", FRESHNESS),  # not an allowed answer (the old code counted it as fresh)
        ("\ufb06ALE", FRESHNESS),  # U+FB06 ligature: .upper() == "STALE"
        ("YE\u017f", FRESHNESS),  # U+017F long s: .upper() == "YES"
        ("Y\u0415S", FRESHNESS),  # Cyrillic Е
        ("\u0405TALE", FRESHNESS),  # Cyrillic Ѕ
        ("N\u041e", FRESHNESS),  # Cyrillic О
        ("YES\u200b", FRESHNESS),  # zero-width space survives str.strip()
        ("\u200bYES", FRESHNESS),
        ("SUPERSEDE", SCAN),
        ("\u017fUPERSEDES", SCAN),  # .upper() == "SUPERSEDES"
        ("SUPERSEDES,", SCAN),
        ("o\u212a", ("OK",)),  # Kelvin sign
    ],
)
def test_parser_rejects_everything_else(text, allowed):
    assert ut.parse_one_word_verdict(text, allowed) is None


def test_the_non_ascii_gate_is_load_bearing():
    """Without step 4, case-folding alone would turn these look-alikes INTO allowed tokens."""
    assert "\ufb06ALE".upper() == "STALE"
    assert "YE\u017f".upper() == "YES"
    assert "\u017fUPERSEDES".upper() == "SUPERSEDES"
    assert "\u212a".lower() == "k"


# ── D2: reasoning control is classifier-only ──────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected", [(None, "none"), ("", None), ("   ", None), ("low", "low"), (" none ", "none")]
)
def test_classifier_reasoning_effort_tri_state(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv(ut.CLASSIFIER_REASONING_EFFORT_ENV, raw)
    assert ut.classifier_reasoning_effort() == expected


def test_consult_sends_reasoning_effort_only_when_given(monkeypatch):
    engine = _Engine("YES")
    client = _client_over(monkeypatch, engine)
    client.consult(system="s", user="u", vk="fast", reasoning_effort="none")
    client.consult(system="s", user="u", vk="fast")
    assert engine.requests[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in engine.requests[1]


def test_advisor_dream_and_vision_calls_never_carry_reasoning_effort(monkeypatch):
    """Board condition 6: the env var must not become a client default."""
    monkeypatch.setenv(ut.CLASSIFIER_REASONING_EFFORT_ENV, "none")
    engine = _Engine("ok")
    client = _client_over(monkeypatch, engine)
    client.consult(system="s", user="u", vk="advisor")
    client.consult(system="s", user="u", vk="dream", max_tokens=16384)
    client.consult_vision(system="s", user_text="u", images=["data:image/png;base64,AAAA"])
    assert len(engine.requests) == 3
    assert all("reasoning_effort" not in r for r in engine.requests)


def test_only_the_three_classifier_sites_read_the_reasoning_env():
    """Structural guard: classifier_reasoning_effort() is called from the classifier sites only."""
    callers = sorted(
        str(p.relative_to(REPO))
        for root in ("mori_advisor", "mori_intake")
        for p in (REPO / root).rglob("*.py")
        if "classifier_reasoning_effort()" in p.read_text()
    )
    assert callers == [
        "mori_advisor/memory_store.py",  # freshness (SQLite)
        "mori_advisor/store/postgres_store.py",  # freshness (Postgres)
        "mori_advisor/utils.py",  # contradiction scan (+ the helper's own docstring)
        "mori_intake/assess_model.py",  # intake assessor
    ]


# ── D3: truncation is visible ─────────────────────────────────────────────────


def test_truncated_reply_is_counted_and_still_ok(monkeypatch):
    engine = _Engine("YES")  # no reasoning_effort on the request ⇒ empty + finish_reason=length
    client = _client_over(monkeypatch, engine)
    truncated = _sample("mori_llm_truncated_total", {"vk": "fast"})
    ok = _sample(
        "mori_llm_call_duration_seconds_count",
        {"vk": "fast", "outcome": "ok", "provider": "fireworks"},
    )

    assert client.consult(system="s", user="u", vk="fast", max_tokens=10) == ""

    assert _sample("mori_llm_truncated_total", {"vk": "fast"}) == truncated + 1
    assert (
        _sample(
            "mori_llm_call_duration_seconds_count",
            {"vk": "fast", "outcome": "ok", "provider": "fireworks"},
        )
        == ok + 1
    )


def test_a_complete_reply_is_not_counted_as_truncated(monkeypatch):
    engine = _Engine("YES")
    client = _client_over(monkeypatch, engine)
    truncated = _sample("mori_llm_truncated_total", {"vk": "fast"})
    assert client.consult(system="s", user="u", vk="fast", reasoning_effort="none") == "YES"
    assert _sample("mori_llm_truncated_total", {"vk": "fast"}) == truncated


def test_every_alert_series_exists_at_zero_on_a_fresh_registry():
    """`increase()` cannot see a series go from absent to N, so the first failure burst after a
    restart would be invisible unless every series the alert reads is created at import."""
    code = (
        "import json; from mori_advisor import metrics as m; "
        "print(json.dumps({"
        "'verdicts': [m.prom_registry.get_sample_value('mori_classifier_verdicts_total', "
        "{'site': s, 'outcome': o}) for s, os_ in m.CLASSIFIER_OUTCOMES.items() for o in os_], "
        "'truncated': [m.prom_registry.get_sample_value('mori_llm_truncated_total', {'vk': v}) "
        "for v in ('advisor', 'dream', 'fast')]}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["verdicts"] == [0.0] * 15
    assert got["truncated"] == [0.0] * 3


def test_every_outcome_a_site_can_emit_is_pre_initialised():
    """The pre-initialised set must cover what the code emits (verdict words + unparseable/error)."""
    from mori_advisor.memory_store import _FRESHNESS_VERDICTS

    assert set(mx.CLASSIFIER_OUTCOMES["freshness"]) == {v.lower() for v in _FRESHNESS_VERDICTS} | {
        "unparseable",
        "error",
    }
    for site in ("contradiction", "intake"):
        assert set(mx.CLASSIFIER_OUTCOMES[site]) == {v.lower() for v in SCAN} | {
            "unparseable",
            "error",
        }


# ── Freshness: a non-answer writes nothing and stays re-eligible ─────────────


@pytest.mark.parametrize("bad", ["", REASONING_LEAK, "FRESH", "YE\u017f", "\ufb06ALE", "YES,"])
def test_freshness_non_answer_writes_nothing_then_a_real_answer_writes(
    backend, monkeypatch, caplog, bad
):
    import mori_advisor.memory_store as ms

    backend.write("fresh-target")
    target = backend.name("fresh-target")
    reply = {"value": bad}
    engine = _Engine(lambda user: reply["value"] if user == target else "YES")
    client = _client_over(monkeypatch, engine)
    unparseable = _verdicts("freshness", "unparseable")

    with caplog.at_level(logging.WARNING):
        backend.freshness(client.consult)

    row = backend.active("fresh-target")
    assert row["freshness_status"] == "unknown"
    assert row["freshness_checked_at"] is None
    assert target not in ms._freshness_cache  # in-flight sentinel cleared: immediately re-eligible
    assert _verdicts("freshness", "unparseable") == unparseable + 1
    assert any("unparseable verdict" in r.getMessage() for r in caplog.records)

    # Positive control: the same row, next brief, a real answer — the write path fires.
    reply["value"] = "YES"
    backend.freshness(client.consult)
    row = backend.active("fresh-target")
    assert row["freshness_status"] == "fresh"
    assert row["freshness_checked_at"] is not None
    assert [u for u in engine.users() if u == target] == [target, target]
    assert all(r["reasoning_effort"] == "none" for r in engine.requests)


@pytest.mark.parametrize("answer,status", [("STALE", "stale"), ("NO", "no"), ("yes.", "fresh")])
def test_freshness_exact_answers_are_persisted(backend, monkeypatch, answer, status):
    backend.write("fresh-exact")
    target = backend.name("fresh-exact")
    client = _client_over(monkeypatch, _Engine(lambda user: answer if user == target else "YES"))
    backend.freshness(client.consult)
    assert backend.active("fresh-exact")["freshness_status"] == status


def test_freshness_call_error_writes_nothing_then_recovers(backend, monkeypatch):
    import mori_advisor.memory_store as ms

    backend.write("fresh-error")
    target = backend.name("fresh-error")
    engine = _Engine("YES", status=400)
    client = _client_over(monkeypatch, engine)
    errors = _verdicts("freshness", "error")

    backend.freshness(client.consult)

    row = backend.active("fresh-error")
    assert (row["freshness_status"], row["freshness_checked_at"]) == ("unknown", None)
    assert target not in ms._freshness_cache
    assert _verdicts("freshness", "error") >= errors + 1

    engine.status = 200  # positive control
    backend.freshness(client.consult)
    assert backend.active("fresh-error")["freshness_status"] == "fresh"


def test_freshness_reasoning_escape_hatch_omits_the_field(backend, monkeypatch):
    monkeypatch.setenv(ut.CLASSIFIER_REASONING_EFFORT_ENV, "")
    backend.write("fresh-hatch")
    engine = _Engine("YES")
    client = _client_over(monkeypatch, engine)
    backend.freshness(client.consult)
    assert engine.requests and all("reasoning_effort" not in r for r in engine.requests)
    # With the field omitted a reasoning model truncates — and that is an error, not "fresh".
    assert backend.active("fresh-hatch")["freshness_status"] == "unknown"


# ── Contradiction scan ────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", REASONING_LEAK, "SUPERSEDE", "\u017fUPERSEDES", "SUPERSEDES,"])
def test_scan_non_answer_writes_nothing_then_supersedes_writes(backend, monkeypatch, caplog, bad):
    backend.write("old")
    backend.write("new", tier="working")
    engine = _Engine(bad)
    client = _client_over(monkeypatch, engine)
    unparseable = _verdicts("contradiction", "unparseable")

    with caplog.at_level(logging.WARNING):
        assert backend.scan(client, ["new"]) == 0

    assert backend.active("old")["superseded_by"] is None
    assert backend.evictions("old") == 0
    assert _verdicts("contradiction", "unparseable") == unparseable + 1
    assert any("unparseable verdict" in r.getMessage() for r in caplog.records)

    # Positive control: an exact SUPERSEDES writes.
    engine.answer = "SUPERSEDES"
    assert backend.scan(client, ["new"]) == 1
    assert backend.active("old")["superseded_by"] == backend.name("new")
    assert backend.evictions("old") == 1


def test_scan_never_supersedes_the_memory_with_itself(backend, monkeypatch):
    """P2-2: a canonical ingest matches its own name prefix; the guard is by primary key."""
    backend.write("self")  # the "new" memory, already written canonical (ingestion writes first)
    backend.write("other")  # a genuine candidate — the positive control
    engine = _Engine("SUPERSEDES")
    client = _client_over(monkeypatch, engine)

    assert backend.scan(client, ["self"]) == 1

    assert backend.active("self")["superseded_by"] is None
    assert backend.active("other")["superseded_by"] == backend.name("self")
    assert not any(u.endswith(f"existing: {backend.name('self')}") for u in engine.users())


def test_scan_never_touches_a_soft_deleted_namesake(backend, monkeypatch):
    """`memories.name` is unique only among active rows; the UPDATE is keyed by id."""
    backend.write("dup", body="old version")
    backend.soft_delete("dup")
    backend.write("dup", body="current version")
    backend.write("newer", tier="working")
    client = _client_over(monkeypatch, _Engine("SUPERSEDES"))

    assert backend.scan(client, ["newer"]) == 1

    rows = backend.rows("dup")
    deleted = [r for r in rows if r["deleted_at"] is not None]
    active = [r for r in rows if r["deleted_at"] is None]
    assert len(deleted) == 1 and len(active) == 1
    assert active[0]["superseded_by"] == backend.name("newer")
    assert deleted[0]["superseded_by"] is None


def test_scan_call_error_is_a_warning_and_writes_nothing(backend, monkeypatch, caplog):
    backend.write("old")
    backend.write("new", tier="working")
    engine = _Engine("SUPERSEDES", status=400)
    client = _client_over(monkeypatch, engine)
    errors = _verdicts("contradiction", "error")

    with caplog.at_level(logging.WARNING):
        assert backend.scan(client, ["new"]) == 0

    assert backend.active("old")["superseded_by"] is None
    assert _verdicts("contradiction", "error") == errors + 1
    assert any("Contradiction check failed" in r.getMessage() for r in caplog.records)

    engine.status = 200  # positive control
    assert backend.scan(client, ["new"]) == 1


def test_a_failed_supersession_write_is_atomic_and_does_not_poison_the_rest(
    backend, monkeypatch, caplog
):
    """Each supersession (UPDATE + eviction-queue INSERT) lands whole or not at all.

    Postgres: without a savepoint the failed INSERT aborts the scan's transaction — the next
    supersession fails and the final COMMIT rolls back what was counted. SQLite: without a
    rollback the next commit lands the half-applied UPDATE without its eviction-queue row.
    """
    backend.write("a-first")  # lower id: scanned first (candidates are ORDER BY id)
    backend.write("b-second")
    backend.write("new", tier="working")
    backend.fail_eviction_insert_for("a-first")
    client = _client_over(monkeypatch, _Engine("SUPERSEDES"))
    errors = _verdicts("contradiction", "error")

    with caplog.at_level(logging.WARNING):
        assert backend.scan(client, ["new"]) == 1

    assert backend.active("a-first")["superseded_by"] is None
    assert backend.evictions("a-first") == 0
    assert backend.active("b-second")["superseded_by"] == backend.name("new")
    assert backend.evictions("b-second") == 1
    assert _verdicts("contradiction", "error") == errors + 1
    assert any("supersession write failed" in r.getMessage() for r in caplog.records)


def test_a_failed_candidate_query_does_not_poison_later_memories(backend, monkeypatch):
    """A name Postgres rejects server-side (NUL byte) fails that memory's candidate query; on
    Postgres the savepoint keeps the scan's transaction usable for the next memory. SQLite stores
    the name, so there the first memory simply scans — either way exactly one supersession lands."""
    backend.write("old")
    backend.write("new", tier="working")
    client = _client_over(monkeypatch, _Engine("SUPERSEDES"))
    bad = {"name": backend.name("bad\x00name"), "title": "bad", "body": "Body of bad."}
    good = {"name": backend.name("new"), "title": "Memory new", "body": "Body of new."}

    from mori_advisor.dream import DreamPipeline

    owner = SimpleNamespace(client=client, db_path=None, store=backend.store)
    assert backend.run(DreamPipeline._contradiction_scan(owner, [bad, good])) == 1
    assert backend.active("old")["superseded_by"] is not None
    assert backend.evictions("old") == 1


@pytest.mark.parametrize("pipeline", ["dream", "ingestion"])
def test_both_scan_wrappers_pass_reasoning_effort_through(backend, monkeypatch, pipeline):
    """P2-2b: a wrapper that drops the kwarg would TypeError into silent blindness."""
    backend.write("old")
    backend.write("new", tier="working")
    engine = _Engine("SUPERSEDES")
    client = _client_over(monkeypatch, engine)

    assert backend.scan(client, ["new"], pipeline=pipeline) == 1
    assert engine.requests and all(r.get("reasoning_effort") == "none" for r in engine.requests)
    assert all(r["max_tokens"] == 16 for r in engine.requests)


# ── Intake assessor ───────────────────────────────────────────────────────────


def _assess(client, neighbour_body="Existing canonical body."):
    from mori_intake.assess_model import CanonReader, make_canon_assessor

    reader = CanonReader(
        search=lambda query, limit: [{"name": "canon-a", "title": "Canon A", "tier": "canonical"}],
        fetch_body=lambda name: neighbour_body,
    )
    return asyncio.run(make_canon_assessor(reader, client)("Candidate body.", "hash"))


def test_intake_sends_reasoning_effort_and_counts_the_verdict(monkeypatch):
    engine = _Engine('{"verdict": "RELATED"}')
    client = _client_over(monkeypatch, engine)
    related = _verdicts("intake", "related")

    assert _assess(client).verdict == "RELATED"
    assert engine.requests[0]["reasoning_effort"] == "none"
    assert engine.requests[0]["response_format"]["type"] == "json_schema"
    assert _verdicts("intake", "related") == related + 1


def test_intake_truncated_reply_fails_closed(monkeypatch):
    monkeypatch.setenv(ut.CLASSIFIER_REASONING_EFFORT_ENV, "")  # field omitted ⇒ engine truncates
    client = _client_over(monkeypatch, _Engine('{"verdict": "RELATED"}'))
    unparseable = _verdicts("intake", "unparseable")
    assert _assess(client).verdict == "NEEDS_REVIEW"
    assert _verdicts("intake", "unparseable") == unparseable + 1


def test_intake_call_error_fails_closed(monkeypatch):
    client = _client_over(monkeypatch, _Engine("x", status=400))
    errors = _verdicts("intake", "error")
    assert _assess(client).verdict == "NEEDS_REVIEW"
    assert _verdicts("intake", "error") == errors + 1


# ── D4: freshness on brief is opt-in ─────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [(None, False), ("true", True), ("TRUE", True), ("false", False), ("yes", False)],
)
def test_freshness_on_brief_is_opt_in(raw, expected):
    env = {k: v for k, v in os.environ.items() if k != "MORI_FRESHNESS_ON_BRIEF"}
    if raw is not None:
        env["MORI_FRESHNESS_ON_BRIEF"] = raw
    out = subprocess.run(
        [sys.executable, "-c", "import mori_advisor.main as m; print(m.FRESHNESS_ON_BRIEF)"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == str(expected)
