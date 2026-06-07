"""Unit tests for Stream B2 — real fast-model assessor.

Always runs — no database, no network.  All external dependencies (CanonReader,
BifrostClient) are replaced with mocks.

Covers
------
* SUPERSEDES verdict from model → AssessmentResult(SUPERSEDES, matched_name, score).
* RELATED verdict from model → AssessmentResult(RELATED, matched_name, score).
* All neighbours → UNRELATED → AssessmentResult(UNRELATED, None, 0.0).
* Empty store (no canon neighbours) → UNRELATED.
* Malformed / unexpected model output → UNRELATED, not a crash.
* Model raises an exception → UNRELATED, not a crash.
* First SUPERSEDES/RELATED stops the scan (earlier neighbours win).
* search callable raises an exception → UNRELATED, not a crash.
* Assessor exposes NO write method (data-boundary enforcement).
* fetch_body is called for each neighbour and its content reaches the prompt.
* When fetch_body returns empty, description/body from search result is used.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from mori_intake.assess_model import CanonReader, make_canon_assessor
from mori_intake.assessor import AssessmentResult


def _run(assess, body: str, content_hash: str) -> AssessmentResult:
    """Run the (now async) assess function synchronously in tests.

    ``make_canon_assessor`` returns a coroutine function (async) because the
    Postgres-native ``CanonReader`` callables are async.  Unit tests use sync
    mock callables, but the returned ``assess`` is still a coroutine — so we
    wrap in ``asyncio.run`` here rather than changing every call site.
    """
    return asyncio.run(assess(body, content_hash))


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_reader(
    search_results: list[dict],
    body_map: dict[str, str] | None = None,
) -> CanonReader:
    """Return a CanonReader mock.

    Parameters
    ----------
    search_results:
        What the search callable returns.
    body_map:
        ``{name: body_text}`` — what fetch_body returns per name.
        Missing names return an empty string (simulating not-found).
    """
    body_map = body_map or {}

    search_fn = MagicMock(return_value=search_results)
    fetch_body_fn = MagicMock(side_effect=lambda name: body_map.get(name, ""))

    return CanonReader(search=search_fn, fetch_body=fetch_body_fn)


def _v(word: str) -> str:
    """Wrap a verdict word as the structured JSON the model now returns."""
    return json.dumps({"verdict": word})


def _make_client(responses: list[str]) -> MagicMock:
    """Return a client mock whose consult() yields successive *responses*.

    The B2 assessor now requests structured output, so a recognised verdict word
    is wrapped as the ``{"verdict": "<W>"}`` JSON the model returns. Non-verdict
    responses (prose, empty string) pass through RAW so the fail-closed parse
    paths are still exercised.
    """
    wrapped = [_v(r) if r in {"SUPERSEDES", "RELATED", "UNRELATED"} else r for r in responses]
    client = MagicMock()
    client.consult = MagicMock(side_effect=wrapped)
    return client


def _canon_row(name: str, title: str = "A title", body: str = "Body text.") -> dict:
    """Minimal canonical memory dict as returned by search."""
    return {"name": name, "title": title, "body": body, "tier": "canonical"}


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestMakeCanonAssessor:
    # ── happy-path verdict mapping ────────────────────────────────────────────

    def test_supersedes_verdict_returned(self):
        """Model says SUPERSEDES → AssessmentResult with that verdict and matched name."""
        reader = _make_reader(
            [_canon_row("canon-alpha")],
            body_map={"canon-alpha": "Full body text of alpha."},
        )
        client = _make_client(["SUPERSEDES"])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Some new learning body.", "deadbeef" * 8)

        assert isinstance(result, AssessmentResult)
        assert result.verdict == "SUPERSEDES"
        assert result.matched_canon_name == "canon-alpha"
        assert result.score > 0.0

    def test_related_verdict_returned(self):
        """Model says RELATED → AssessmentResult(RELATED, matched_name, score)."""
        reader = _make_reader(
            [_canon_row("canon-beta")],
            body_map={"canon-beta": "Full body of beta."},
        )
        client = _make_client(["RELATED"])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Another body.", "0" * 64)

        assert result.verdict == "RELATED"
        assert result.matched_canon_name == "canon-beta"
        assert result.score > 0.0

    def test_unrelated_when_all_neighbours_unrelated(self):
        """All neighbours → UNRELATED → AssessmentResult(UNRELATED, None, 0.0)."""
        reader = _make_reader(
            [_canon_row("canon-gamma"), _canon_row("canon-delta")],
            body_map={
                "canon-gamma": "Gamma body text.",
                "canon-delta": "Delta body text.",
            },
        )
        client = _make_client(["UNRELATED", "UNRELATED"])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Truly novel claim.", "a" * 64)

        assert result.verdict == "UNRELATED"
        assert result.matched_canon_name is None
        assert result.score == 0.0

    # ── empty store ───────────────────────────────────────────────────────────

    def test_empty_store_returns_unrelated(self):
        """No canon neighbours → UNRELATED without calling the model."""
        reader = _make_reader([])
        client = _make_client([])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body doesn't matter.", "b" * 64)

        assert result.verdict == "UNRELATED"
        assert result.matched_canon_name is None
        assert result.score == 0.0
        client.consult.assert_not_called()

    # ── malformed model output ────────────────────────────────────────────────

    def test_malformed_model_output_returns_needs_review(self):
        """Model returns something unrecognised → NEEDS_REVIEW (fail closed), not a crash.

        Changed from UNRELATED in delta-hardening: unrecognised output is
        uncertain, not novel.  NEEDS_REVIEW leaves the candidate pending for
        human review rather than silently auto-promoting.
        """
        reader = _make_reader([_canon_row("canon-epsilon")], body_map={"canon-epsilon": "Body."})
        # Model returns prose instead of a single word.
        client = _make_client(["I think they are somewhat related but different."])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body text.", "c" * 64)

        assert result.verdict == "NEEDS_REVIEW"

    def test_empty_model_response_returns_needs_review(self):
        """Empty model response → NEEDS_REVIEW (fail closed), not a crash.

        Changed from UNRELATED in delta-hardening.
        """
        reader = _make_reader([_canon_row("canon-zeta")], body_map={"canon-zeta": "Body."})
        client = _make_client([""])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body text.", "d" * 64)

        assert result.verdict == "NEEDS_REVIEW"

    def test_none_model_response_returns_needs_review(self):
        """None model response → NEEDS_REVIEW (fail closed), not a crash.

        Changed from UNRELATED in delta-hardening.
        """
        reader = _make_reader([_canon_row("canon-eta")], body_map={"canon-eta": "Body."})
        client = MagicMock()
        client.consult = MagicMock(return_value=None)

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body text.", "e" * 64)

        assert result.verdict == "NEEDS_REVIEW"

    # ── model exception ───────────────────────────────────────────────────────

    def test_model_exception_returns_needs_review(self):
        """Model raises → NEEDS_REVIEW (fail closed) for that neighbour, no crash.

        Changed from UNRELATED in delta-hardening: a model failure is uncertain,
        not evidence that the candidate is novel.
        """
        reader = _make_reader([_canon_row("canon-theta")], body_map={"canon-theta": "Body."})
        client = MagicMock()
        client.consult = MagicMock(side_effect=RuntimeError("Bifrost timeout"))

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body text.", "f" * 64)

        assert result.verdict == "NEEDS_REVIEW"
        assert result.matched_canon_name is None

    # ── search callable exception ─────────────────────────────────────────────

    def test_search_exception_returns_needs_review(self):
        """search callable raises → NEEDS_REVIEW (fail closed), no crash.

        Changed from UNRELATED in delta-hardening: search failure is uncertain,
        not evidence of novelty.
        """
        reader = CanonReader(
            search=MagicMock(side_effect=Exception("DB gone")),
            fetch_body=MagicMock(return_value=""),
        )
        client = _make_client([])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body.", "g" * 64)

        assert result.verdict == "NEEDS_REVIEW"
        client.consult.assert_not_called()

    # ── scan stops at first match ─────────────────────────────────────────────

    def test_first_match_wins_scan_stops(self):
        """First SUPERSEDES match stops the scan — later neighbours are not checked."""
        reader = _make_reader(
            [
                _canon_row("canon-first"),
                _canon_row("canon-second"),
                _canon_row("canon-third"),
            ],
            body_map={
                "canon-first": "First body.",
                "canon-second": "Second body.",
                "canon-third": "Third body.",
            },
        )
        # First neighbour → SUPERSEDES, should stop immediately.
        client = _make_client(["SUPERSEDES", "RELATED", "UNRELATED"])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body.", "h" * 64)

        assert result.verdict == "SUPERSEDES"
        assert result.matched_canon_name == "canon-first"
        # Only one consult call should have been made.
        assert client.consult.call_count == 1

    def test_second_match_wins_when_first_unrelated(self):
        """Second RELATED neighbour wins when first is UNRELATED."""
        reader = _make_reader(
            [_canon_row("canon-a"), _canon_row("canon-b")],
            body_map={"canon-a": "A body.", "canon-b": "B body."},
        )
        client = _make_client(["UNRELATED", "RELATED"])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body.", "i" * 64)

        assert result.verdict == "RELATED"
        assert result.matched_canon_name == "canon-b"
        assert client.consult.call_count == 2

    # ── tier filtering ────────────────────────────────────────────────────────

    def test_non_canonical_rows_filtered_out(self):
        """search rows with tier != 'canonical' are filtered before model call."""
        reader = _make_reader(
            [
                {"name": "working-mem", "title": "T", "body": "B", "tier": "working"},
                {"name": "ephemeral-mem", "title": "T", "body": "B", "tier": "ephemeral"},
            ]
        )
        client = _make_client([])

        assess = make_canon_assessor(reader, client)
        result = _run(assess, "Body.", "j" * 64)

        # No canonical rows → UNRELATED without model call.
        assert result.verdict == "UNRELATED"
        client.consult.assert_not_called()

    # ── top_k parameter ───────────────────────────────────────────────────────

    def test_top_k_limits_model_calls(self):
        """top_k=2 → at most 2 model calls even when store returns more rows."""
        reader = _make_reader(
            [
                _canon_row("n1"),
                _canon_row("n2"),
                _canon_row("n3"),
                _canon_row("n4"),
            ],
            body_map={"n1": "B1", "n2": "B2", "n3": "B3", "n4": "B4"},
        )
        client = _make_client(["UNRELATED", "UNRELATED"])

        assess = make_canon_assessor(reader, client, top_k=2)
        result = _run(assess, "Body.", "k" * 64)

        assert result.verdict == "UNRELATED"
        assert client.consult.call_count == 2

    # ── content_hash argument is accepted (not used in logic) ─────────────────

    def test_content_hash_ignored(self):
        """The content_hash arg is accepted but not forwarded to the model."""
        reader = _make_reader(
            [_canon_row("canon-x")],
            body_map={"canon-x": "X body."},
        )
        client = _make_client(["UNRELATED", "UNRELATED"])

        assess = make_canon_assessor(reader, client)
        r1 = _run(assess, "Body.", "aaaa" * 16)
        r2 = _run(assess, "Body.", "bbbb" * 16)

        # Same verdict regardless of hash — hash is not part of the prompt.
        assert r1.verdict == r2.verdict == "UNRELATED"

    # ── Fix 2: full body reaches the prompt ───────────────────────────────────

    def test_fetch_body_called_for_each_neighbour(self):
        """fetch_body is called once per canonical neighbour processed."""
        reader = _make_reader(
            [_canon_row("n-alpha"), _canon_row("n-beta")],
            body_map={"n-alpha": "Alpha full body.", "n-beta": "Beta full body."},
        )
        client = _make_client(["UNRELATED", "UNRELATED"])

        assess = make_canon_assessor(reader, client)
        _run(assess, "Candidate body.", "m" * 64)

        # fetch_body must have been called for each neighbour that was assessed.
        reader.fetch_body.assert_any_call("n-alpha")
        reader.fetch_body.assert_any_call("n-beta")

    def test_full_body_content_reaches_prompt_not_description(self):
        """The full body from fetch_body is passed to the prompt, not the description."""
        full_body_text = "This is the FULL body content — distinctive sentinel text."
        description_text = "Short description — should NOT appear in prompt."

        # search returns a row with a description field but NO body field.
        search_result = {
            "name": "mem-with-desc",
            "title": "Some title",
            "description": description_text,
            "tier": "canonical",
            # No 'body' key in search result — common for search_json output.
        }
        reader = _make_reader(
            [search_result],
            body_map={"mem-with-desc": full_body_text},
        )

        captured_prompts: list[str] = []

        def _capture_consult(**kwargs):
            captured_prompts.append(kwargs.get("system", ""))
            return _v("UNRELATED")

        client = MagicMock()
        client.consult = MagicMock(side_effect=_capture_consult)

        assess = make_canon_assessor(reader, client)
        _run(assess, "Candidate body.", "n" * 64)

        assert len(captured_prompts) == 1
        assert full_body_text in captured_prompts[0], "Full body text should appear in the prompt"
        assert description_text not in captured_prompts[0], (
            "Description text should NOT appear when fetch_body returned content"
        )

    def test_fetch_body_fallback_to_search_result_when_empty(self):
        """When fetch_body returns '' the search result body/description is used."""
        fallback_body = "Fallback body from search result."

        reader = _make_reader(
            [_canon_row("mem-gone", body=fallback_body)],
            body_map={"mem-gone": ""},  # fetch_body returns empty
        )

        captured_prompts: list[str] = []

        def _capture_consult(**kwargs):
            captured_prompts.append(kwargs.get("system", ""))
            return _v("UNRELATED")

        client = MagicMock()
        client.consult = MagicMock(side_effect=_capture_consult)

        assess = make_canon_assessor(reader, client)
        _run(assess, "Candidate body.", "o" * 64)

        assert len(captured_prompts) == 1
        assert fallback_body in captured_prompts[0], (
            "Fallback body from search result should appear when fetch_body returns empty"
        )

    # ── Fix 1: assessor exposes no write method ───────────────────────────────

    def test_assessor_callable_has_no_write_attribute(self):
        """The assess callable must not expose a write method or a store reference."""
        reader = _make_reader([])
        client = _make_client([])

        assess = make_canon_assessor(reader, client)

        # The returned callable should not have a 'write' attribute,
        # a 'store' attribute, or any other obvious write-path handle.
        assert not hasattr(assess, "write"), "assess must not expose a write method"
        assert not hasattr(assess, "store"), "assess must not hold a store reference"
        assert not hasattr(assess, "_store"), "assess must not hold a private store reference"

    def test_canon_reader_is_frozen(self):
        """CanonReader is a frozen dataclass — fields cannot be replaced after construction."""
        reader = _make_reader([])
        with pytest.raises((AttributeError, TypeError)):
            reader.search = MagicMock()  # type: ignore[misc]

        with pytest.raises((AttributeError, TypeError)):
            reader.fetch_body = MagicMock()  # type: ignore[misc]


def test_consult_requested_with_structured_response_format():
    """The assessor must request strict json_schema structured output on the fast VK."""
    from mori_intake.assess_model import _VERDICT_RESPONSE_FORMAT

    reader = _make_reader([_canon_row("canon-rf")], body_map={"canon-rf": "Body."})
    client = _make_client(["UNRELATED"])
    assess = make_canon_assessor(reader, client)
    _run(assess, "Body.", "p" * 64)

    _, kwargs = client.consult.call_args
    assert kwargs.get("response_format") == _VERDICT_RESPONSE_FORMAT
    assert kwargs.get("vk") == "fast"


class TestParseVerdict:
    """Direct unit tests for the structured-verdict parser (fail-closed contract)."""

    def test_valid_json_verdicts(self):
        from mori_intake.assess_model import _parse_verdict

        assert _parse_verdict('{"verdict": "SUPERSEDES"}', "n") == "SUPERSEDES"
        assert _parse_verdict('{"verdict": "RELATED"}', "n") == "RELATED"
        assert _parse_verdict('{"verdict": "UNRELATED"}', "n") == "UNRELATED"

    def test_whitespace_wrapped_json_ok(self):
        from mori_intake.assess_model import _parse_verdict

        assert _parse_verdict('  \n {"verdict": "UNRELATED"} \n', "n") == "UNRELATED"

    def test_unknown_enum_value_fails_closed(self):
        from mori_intake.assess_model import _parse_verdict

        assert _parse_verdict('{"verdict": "MAYBE"}', "n") == "NEEDS_REVIEW"

    def test_extra_keys_forbidden_fails_closed(self):
        from mori_intake.assess_model import _parse_verdict

        # extra='forbid' — a model that adds keys is rejected, never trusted.
        assert _parse_verdict('{"verdict": "UNRELATED", "confidence": 0.9}', "n") == "NEEDS_REVIEW"

    def test_missing_verdict_key_fails_closed(self):
        from mori_intake.assess_model import _parse_verdict

        assert _parse_verdict('{"foo": "bar"}', "n") == "NEEDS_REVIEW"

    def test_non_json_fails_closed(self):
        from mori_intake.assess_model import _parse_verdict

        assert _parse_verdict("UNRELATED", "n") == "NEEDS_REVIEW"  # bare word, not JSON
        assert _parse_verdict("I think they are related.", "n") == "NEEDS_REVIEW"

    def test_empty_and_none_fail_closed(self):
        from mori_intake.assess_model import _parse_verdict

        assert _parse_verdict("", "n") == "NEEDS_REVIEW"
        assert _parse_verdict(None, "n") == "NEEDS_REVIEW"  # type: ignore[arg-type]

    def test_json_non_object_fails_closed(self):
        from mori_intake.assess_model import _parse_verdict

        # Valid JSON but not the expected object → fail closed.
        assert _parse_verdict('"UNRELATED"', "n") == "NEEDS_REVIEW"
        assert _parse_verdict('["UNRELATED"]', "n") == "NEEDS_REVIEW"


# ── Live-Bifrost smoke test (always skipped in CI) ────────────────────────────


@pytest.mark.skipif(
    not __import__("os").environ.get("MORI_B2_LIVE_TEST"),
    reason="Live Bifrost smoke test — set MORI_B2_LIVE_TEST=1 to run manually.",
)
def test_live_bifrost_smoke(tmp_path):
    """Smoke-test the real assessor against a live Bifrost + Postgres canon.

    NOT part of the automated suite.  Set ``MORI_B2_LIVE_TEST=1`` to run.
    Requires ``MORI_CANON_TEST_DATABASE_URL`` (Postgres) because
    ``canon_reader()`` is Postgres-only by design.  If only a SQLiteStore is
    available the test is skipped with a clear message.
    """
    import asyncio
    import os

    canon_dsn = os.environ.get("MORI_CANON_TEST_DATABASE_URL", "")
    if not canon_dsn:
        pytest.skip(
            "MORI_CANON_TEST_DATABASE_URL not set — live smoke test requires Postgres canon"
        )

    from mori_advisor.bifrost_client import BifrostClient
    from mori_advisor.store.postgres_store import PostgresStore
    from mori_intake.assess_model import make_canon_reader_from_store

    async def _run_smoke():
        from mori_advisor.store.migrations import MIGRATIONS, apply_postgres

        store = PostgresStore(canon_dsn)
        await apply_postgres(store, MIGRATIONS)
        try:
            client = BifrostClient()
            reader = make_canon_reader_from_store(store)
            assess = make_canon_assessor(reader, client)
            # assess is a coroutine — await it.
            result = await assess("The sky appears blue due to Rayleigh scattering.", "live" * 16)
            return result
        finally:
            await store.pool.close()

    result = asyncio.run(_run_smoke())
    assert result.verdict in ("SUPERSEDES", "RELATED", "UNRELATED")
    assert result.matched_canon_name is None or isinstance(result.matched_canon_name, str)
