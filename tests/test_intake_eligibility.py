"""Pure-logic tests for mori_intake.eligibility — no DB, always run.

Table-driven tests covering:
- Namespace gate: memory target (allowed + denied prefixes)
- Namespace gate: user target (allowed + hard-denied prefixes)
- Unknown target / unknown prefix → default-deny
- Action gate: remove → always denied
- Proposition classifier: empty, short, question, fragment, valid
"""

from __future__ import annotations

import pytest

from mori_intake.eligibility import evaluate

# Shorthand body that passes the proposition check.
_VALID_BODY = "The system has learned that caching improves latency significantly."

# Shorthand body that is a genuine proposition (>= 12 non-ws chars, >= 3 tokens, no '?').
_PROP_BODY = "Users prefer dark mode for night-time reading sessions."


# ── Namespace gate: memory target ─────────────────────────────────────────────


class TestMemoryNamespace:
    @pytest.mark.parametrize(
        "stable_key",
        [
            "learned-python-generics",
            "learned-",  # prefix alone (edge case)
            "fact-earth-is-round",
            "fact-",
        ],
    )
    def test_allowed_prefixes(self, stable_key):
        d = evaluate("memory", "add", stable_key, _VALID_BODY)
        assert d.eligible, f"Expected eligible for {stable_key!r}, got: {d.reason}"

    @pytest.mark.parametrize(
        "stable_key",
        [
            "session-abc-123",
            "scratch-temp-data",
            "temp-buffer",
        ],
    )
    def test_denied_prefixes(self, stable_key):
        d = evaluate("memory", "add", stable_key, _VALID_BODY)
        assert not d.eligible
        assert d.reason == "namespace-not-allowlisted"

    @pytest.mark.parametrize(
        "stable_key",
        [
            "unknown-key",
            "random",
            "hermes-internal",
        ],
    )
    def test_unknown_prefix_denied(self, stable_key):
        d = evaluate("memory", "add", stable_key, _VALID_BODY)
        assert not d.eligible
        assert d.reason == "namespace-not-allowlisted"

    def test_empty_key_denied_format(self):
        """Empty stable_key fails format check before namespace gate."""
        d = evaluate("memory", "add", "", _VALID_BODY)
        assert not d.eligible
        # Empty string fails the format regex.
        assert d.reason == "invalid-stable-key-format"


# ── Namespace gate: user target ───────────────────────────────────────────────


class TestUserNamespace:
    @pytest.mark.parametrize(
        "stable_key",
        [
            "preference-dark-mode",
            "preference-",
            "accessibility-font-size",
            "accessibility-",
        ],
    )
    def test_allowed_prefixes(self, stable_key):
        d = evaluate("user", "add", stable_key, _PROP_BODY)
        assert d.eligible, f"Expected eligible for {stable_key!r}, got: {d.reason}"

    @pytest.mark.parametrize(
        "stable_key",
        [
            "psychology-anxiety-level",
            "health-blood-pressure",
            "mood-current",
            "mood-",
        ],
    )
    def test_hard_denied_prefixes(self, stable_key):
        """Hard-deny list: psychology-*, health-*, mood-* — always rejected."""
        d = evaluate("user", "add", stable_key, _PROP_BODY)
        assert not d.eligible
        assert d.reason == "namespace-not-allowlisted"

    @pytest.mark.parametrize(
        "stable_key",
        [
            "learned-something",  # memory prefix, wrong target
            "fact-something",
            "random-key",
        ],
    )
    def test_non_allowlisted_prefix_denied(self, stable_key):
        d = evaluate("user", "add", stable_key, _PROP_BODY)
        assert not d.eligible
        assert d.reason == "namespace-not-allowlisted"

    def test_empty_key_denied_format(self):
        """Empty stable_key fails format check."""
        d = evaluate("user", "add", "", _PROP_BODY)
        assert not d.eligible
        assert d.reason == "invalid-stable-key-format"


# ── Unknown target ────────────────────────────────────────────────────────────


class TestUnknownTarget:
    @pytest.mark.parametrize(
        "target",
        ["agent", "system", "canon", "MEMORY", "USER", ""],
    )
    def test_unknown_target_denied(self, target):
        d = evaluate(target, "add", "learned-something", _VALID_BODY)
        assert not d.eligible
        assert d.reason == "namespace-not-allowlisted"


# ── Action gate ───────────────────────────────────────────────────────────────


class TestActionGate:
    def test_remove_always_denied(self):
        d = evaluate("memory", "remove", "learned-something", _VALID_BODY)
        assert not d.eligible
        assert d.reason == "retraction-requires-human"

    def test_remove_denied_even_for_valid_namespace(self):
        d = evaluate("user", "remove", "preference-dark-mode", _PROP_BODY)
        assert not d.eligible
        assert d.reason == "retraction-requires-human"

    def test_add_passes_action_gate(self):
        d = evaluate("memory", "add", "learned-something", _VALID_BODY)
        assert d.eligible

    def test_replace_passes_action_gate(self):
        d = evaluate("memory", "replace", "fact-earth-orbits-sun", _VALID_BODY)
        assert d.eligible


# ── Proposition classifier ────────────────────────────────────────────────────


class TestPropositionClassifier:
    def test_empty_body_denied(self):
        d = evaluate("memory", "add", "learned-x", "")
        assert not d.eligible
        assert d.reason == "not-a-proposition"

    def test_whitespace_only_denied(self):
        d = evaluate("memory", "add", "learned-x", "   \n  ")
        assert not d.eligible
        assert d.reason == "not-a-proposition"

    def test_too_short_non_whitespace_denied(self):
        # 11 non-whitespace chars — below the 12-char threshold.
        d = evaluate("memory", "add", "learned-x", "short text.")
        assert not d.eligible
        assert d.reason == "not-a-proposition"

    def test_exactly_12_non_whitespace_accepted(self):
        # "abc def ghij" = 10 non-ws; need 12.
        # "alpha beta gamma" = 15 non-ws, 3 tokens → should pass.
        d = evaluate("memory", "add", "learned-x", "alpha beta gamma")
        assert d.eligible

    def test_question_body_denied(self):
        d = evaluate("memory", "add", "learned-x", "Is the system working correctly?")
        assert not d.eligible
        assert d.reason == "not-a-proposition"

    def test_two_token_fragment_denied(self):
        # Only 2 whitespace-separated tokens → bare imperative fragment.
        d = evaluate("memory", "add", "learned-x", "Cache results")
        assert not d.eligible
        assert d.reason == "not-a-proposition"

    def test_three_token_body_accepted(self):
        # Exactly 3 tokens and >= 12 non-ws chars.
        d = evaluate("memory", "add", "learned-x", "Caching improves performance")
        assert d.eligible

    def test_valid_proposition_accepted(self):
        d = evaluate(
            "memory",
            "add",
            "learned-latency",
            "Caching database query results reduces latency by approximately 40%.",
        )
        assert d.eligible

    def test_valid_proposition_user_target(self):
        d = evaluate(
            "user",
            "add",
            "preference-theme",
            "The user consistently selects dark mode at night.",
        )
        assert d.eligible

    def test_reason_ok_on_accept(self):
        d = evaluate(
            "memory", "add", "fact-sky-colour", "The sky appears blue due to Rayleigh scattering."
        )
        assert d.eligible
        assert d.reason == "ok"
