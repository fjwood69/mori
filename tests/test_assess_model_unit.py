"""Unit tests for Stream B2 — real fast-model assessor.

Always runs — no database, no network.  All external dependencies (mori store,
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
* search_json raises an exception → UNRELATED, not a crash.
* Existing B1 stub path continues to work (DefaultStub tests from B1 remain
  in test_intake_assessor_unit.py; we only verify the B2 factory here).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mori_intake.assess_model import make_canon_assessor
from mori_intake.assessor import AssessmentResult

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_store(search_results: list[dict]) -> MagicMock:
    """Return a store mock whose search_json returns *search_results*."""
    store = MagicMock()
    store.search_json = MagicMock(return_value=search_results)
    return store


def _make_client(responses: list[str]) -> MagicMock:
    """Return a client mock whose consult() yields successive *responses*."""
    client = MagicMock()
    client.consult = MagicMock(side_effect=responses)
    return client


def _canon_row(name: str, title: str = "A title", body: str = "Body text.") -> dict:
    """Minimal canonical memory dict as returned by search_json."""
    return {"name": name, "title": title, "body": body, "tier": "canonical"}


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestMakeCanonAssessor:
    # ── happy-path verdict mapping ────────────────────────────────────────────

    def test_supersedes_verdict_returned(self):
        """Model says SUPERSEDES → AssessmentResult with that verdict and matched name."""
        store = _make_store([_canon_row("canon-alpha")])
        client = _make_client(["SUPERSEDES"])

        assess = make_canon_assessor(store, client)
        result = assess("Some new learning body.", "deadbeef" * 8)

        assert isinstance(result, AssessmentResult)
        assert result.verdict == "SUPERSEDES"
        assert result.matched_canon_name == "canon-alpha"
        assert result.score > 0.0

    def test_related_verdict_returned(self):
        """Model says RELATED → AssessmentResult(RELATED, matched_name, score)."""
        store = _make_store([_canon_row("canon-beta")])
        client = _make_client(["RELATED"])

        assess = make_canon_assessor(store, client)
        result = assess("Another body.", "0" * 64)

        assert result.verdict == "RELATED"
        assert result.matched_canon_name == "canon-beta"
        assert result.score > 0.0

    def test_unrelated_when_all_neighbours_unrelated(self):
        """All neighbours → UNRELATED → AssessmentResult(UNRELATED, None, 0.0)."""
        store = _make_store([_canon_row("canon-gamma"), _canon_row("canon-delta")])
        client = _make_client(["UNRELATED", "UNRELATED"])

        assess = make_canon_assessor(store, client)
        result = assess("Truly novel claim.", "a" * 64)

        assert result.verdict == "UNRELATED"
        assert result.matched_canon_name is None
        assert result.score == 0.0

    # ── empty store ───────────────────────────────────────────────────────────

    def test_empty_store_returns_unrelated(self):
        """No canon neighbours → UNRELATED without calling the model."""
        store = _make_store([])
        client = _make_client([])

        assess = make_canon_assessor(store, client)
        result = assess("Body doesn't matter.", "b" * 64)

        assert result.verdict == "UNRELATED"
        assert result.matched_canon_name is None
        assert result.score == 0.0
        client.consult.assert_not_called()

    # ── malformed model output ────────────────────────────────────────────────

    def test_malformed_model_output_defaults_to_unrelated(self):
        """Model returns something unrecognised → UNRELATED, not a crash."""
        store = _make_store([_canon_row("canon-epsilon")])
        # Model returns prose instead of a single word.
        client = _make_client(["I think they are somewhat related but different."])

        assess = make_canon_assessor(store, client)
        result = assess("Body text.", "c" * 64)

        assert result.verdict == "UNRELATED"

    def test_empty_model_response_defaults_to_unrelated(self):
        """Empty model response → UNRELATED, not a crash."""
        store = _make_store([_canon_row("canon-zeta")])
        client = _make_client([""])

        assess = make_canon_assessor(store, client)
        result = assess("Body text.", "d" * 64)

        assert result.verdict == "UNRELATED"

    def test_none_model_response_defaults_to_unrelated(self):
        """None model response (e.g. model returned empty) → UNRELATED."""
        store = _make_store([_canon_row("canon-eta")])
        client = MagicMock()
        client.consult = MagicMock(return_value=None)

        assess = make_canon_assessor(store, client)
        result = assess("Body text.", "e" * 64)

        assert result.verdict == "UNRELATED"

    # ── model exception ───────────────────────────────────────────────────────

    def test_model_exception_defaults_to_unrelated(self):
        """Model raises → UNRELATED for that neighbour, overall UNRELATED, no crash."""
        store = _make_store([_canon_row("canon-theta")])
        client = MagicMock()
        client.consult = MagicMock(side_effect=RuntimeError("Bifrost timeout"))

        assess = make_canon_assessor(store, client)
        result = assess("Body text.", "f" * 64)

        assert result.verdict == "UNRELATED"
        assert result.matched_canon_name is None

    # ── store exception ───────────────────────────────────────────────────────

    def test_search_exception_defaults_to_unrelated(self):
        """store.search_json raises → UNRELATED, no crash."""
        store = MagicMock()
        store.search_json = MagicMock(side_effect=Exception("DB gone"))
        client = _make_client([])

        assess = make_canon_assessor(store, client)
        result = assess("Body.", "g" * 64)

        assert result.verdict == "UNRELATED"
        client.consult.assert_not_called()

    # ── scan stops at first match ─────────────────────────────────────────────

    def test_first_match_wins_scan_stops(self):
        """First SUPERSEDES match stops the scan — later neighbours are not checked."""
        store = _make_store(
            [
                _canon_row("canon-first"),
                _canon_row("canon-second"),
                _canon_row("canon-third"),
            ]
        )
        # First neighbour → SUPERSEDES, should stop immediately.
        client = _make_client(["SUPERSEDES", "RELATED", "UNRELATED"])

        assess = make_canon_assessor(store, client)
        result = assess("Body.", "h" * 64)

        assert result.verdict == "SUPERSEDES"
        assert result.matched_canon_name == "canon-first"
        # Only one consult call should have been made.
        assert client.consult.call_count == 1

    def test_second_match_wins_when_first_unrelated(self):
        """Second RELATED neighbour wins when first is UNRELATED."""
        store = _make_store([_canon_row("canon-a"), _canon_row("canon-b")])
        client = _make_client(["UNRELATED", "RELATED"])

        assess = make_canon_assessor(store, client)
        result = assess("Body.", "i" * 64)

        assert result.verdict == "RELATED"
        assert result.matched_canon_name == "canon-b"
        assert client.consult.call_count == 2

    # ── tier filtering ────────────────────────────────────────────────────────

    def test_non_canonical_rows_filtered_out(self):
        """search_json rows with tier != 'canonical' are filtered before model call."""
        store = _make_store(
            [
                {"name": "working-mem", "title": "T", "body": "B", "tier": "working"},
                {"name": "ephemeral-mem", "title": "T", "body": "B", "tier": "ephemeral"},
            ]
        )
        client = _make_client([])

        assess = make_canon_assessor(store, client)
        result = assess("Body.", "j" * 64)

        # No canonical rows → UNRELATED without model call.
        assert result.verdict == "UNRELATED"
        client.consult.assert_not_called()

    # ── top_k parameter ───────────────────────────────────────────────────────

    def test_top_k_limits_model_calls(self):
        """top_k=2 → at most 2 model calls even when store returns more rows."""
        store = _make_store(
            [
                _canon_row("n1"),
                _canon_row("n2"),
                _canon_row("n3"),
                _canon_row("n4"),
            ]
        )
        client = _make_client(["UNRELATED", "UNRELATED"])

        assess = make_canon_assessor(store, client, top_k=2)
        result = assess("Body.", "k" * 64)

        assert result.verdict == "UNRELATED"
        assert client.consult.call_count == 2

    # ── content_hash argument is accepted (not used in logic) ─────────────────

    def test_content_hash_ignored(self):
        """The content_hash arg is accepted but not forwarded to the model."""
        store = _make_store([_canon_row("canon-x")])
        client = _make_client(["UNRELATED"])

        assess = make_canon_assessor(store, client)
        r1 = assess("Body.", "aaaa" * 16)
        r2 = assess("Body.", "bbbb" * 16)

        # Same verdict regardless of hash — hash is not part of the prompt.
        assert r1.verdict == r2.verdict == "UNRELATED"


# ── Live-Bifrost smoke test (always skipped in CI) ────────────────────────────


@pytest.mark.skipif(
    not __import__("os").environ.get("MORI_B2_LIVE_TEST"),
    reason="Live Bifrost smoke test — set MORI_B2_LIVE_TEST=1 to run manually.",
)
def test_live_bifrost_smoke(tmp_path):
    """Smoke-test the real assessor against a live Bifrost + SQLite store.

    NOT part of the automated suite.  Set ``MORI_B2_LIVE_TEST=1`` to run.
    The store is an empty SQLiteStore → expected verdict is UNRELATED.
    """
    from mori_advisor.bifrost_client import BifrostClient
    from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite
    from mori_advisor.store.sqlite_store import SQLiteStore

    db_path = tmp_path / "memories.db"
    store = SQLiteStore(db_path)
    apply_sqlite(db_path, tuple(m for m in MIGRATIONS if m.target == "memories"))

    client = BifrostClient()
    assess = make_canon_assessor(store, client)
    result = assess("The sky appears blue due to Rayleigh scattering.", "live" * 16)

    assert result.verdict in ("SUPERSEDES", "RELATED", "UNRELATED")
    assert result.matched_canon_name is None or isinstance(result.matched_canon_name, str)
