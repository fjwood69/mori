"""Tests for HermesEventNormalizer (v0.2.0 contract).

Covers:
  * action -> op mapping (add/replace/remove × memory/user).
  * name derivation: hermes-{target}-{stable_key}, deterministic, never random.
  * name sanitisation: dots/invalid stripped, hyphens collapsed, <= 128, stable.
  * user_id / memory_id stable keys; content+hash fallback for memory.
  * content_hash helper is sha256 of content.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from hermes_mori_provider.normalizer import (
    HermesEventNormalizer,
    action_to_op,
    content_hash,
    is_valid_name,
    sanitize_name,
)

_NAME_RE_DESC = "^[a-zA-Z0-9_-]{1,128}$"


@pytest.fixture()
def normalizer() -> HermesEventNormalizer:
    return HermesEventNormalizer()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── Action -> op mapping ──────────────────────────────────────────────────────


class TestActionMapping:
    def test_add_maps_to_propose(self) -> None:
        assert action_to_op("add") == "propose"

    def test_replace_maps_to_supersede(self) -> None:
        assert action_to_op("replace") == "supersede"

    def test_remove_maps_to_retract(self) -> None:
        assert action_to_op("remove") == "retract"

    def test_unknown_action_falls_back_to_propose(self) -> None:
        assert action_to_op("frobnicate") == "propose"

    def test_case_insensitive(self) -> None:
        assert action_to_op("ADD") == "propose"
        assert action_to_op("Replace") == "supersede"

    @pytest.mark.parametrize(
        "action,expected",
        [("add", "propose"), ("replace", "supersede"), ("remove", "retract")],
    )
    @pytest.mark.parametrize("target", ["memory", "user"])
    def test_op_in_normalize_for_both_targets(
        self,
        normalizer: HermesEventNormalizer,
        action: str,
        expected: str,
        target: str,
    ) -> None:
        desc = normalizer.normalize(
            action, target, "some content", {"memory_id": "m", "user_id": "u"}
        )
        assert desc["op"] == expected
        assert desc["target"] == target


# ── Name derivation ───────────────────────────────────────────────────────────


class TestNameDerivation:
    def test_memory_with_memory_id(self, normalizer: HermesEventNormalizer) -> None:
        name = normalizer.derive_name("memory", "body", {"memory_id": "my-learning"})
        assert name == "hermes-memory-my-learning"

    def test_user_with_user_id(self, normalizer: HermesEventNormalizer) -> None:
        name = normalizer.derive_name("user", "body", {"user_id": "alice"})
        assert name == "hermes-user-alice"

    def test_user_defaults_when_no_user_id(self, normalizer: HermesEventNormalizer) -> None:
        name = normalizer.derive_name("user", "body", {})
        assert name == "hermes-user-default"

    def test_memory_content_fallback_has_slug_and_hash(
        self, normalizer: HermesEventNormalizer
    ) -> None:
        content = "remember the deploy command for the staging cluster"
        name = normalizer.derive_name("memory", content, {})
        assert name.startswith("hermes-memory-")
        assert "remember" in name
        # 8-char content-hash suffix appended.
        assert name.endswith(content_hash(content)[:8])

    def test_memory_fallback_is_deterministic(self, normalizer: HermesEventNormalizer) -> None:
        content = "same content yields same name"
        a = normalizer.derive_name("memory", content, {})
        b = normalizer.derive_name("memory", content, {})
        assert a == b

    def test_memory_fallback_differs_by_content(self, normalizer: HermesEventNormalizer) -> None:
        a = normalizer.derive_name("memory", "first distinct memory body", {})
        b = normalizer.derive_name("memory", "second distinct memory body", {})
        assert a != b

    def test_namespaced_prefix_always_present(self, normalizer: HermesEventNormalizer) -> None:
        for target in ("memory", "user"):
            name = normalizer.derive_name(target, "x", {})
            assert name.startswith(f"hermes-{target}-")

    def test_unexpected_target_namespaced_as_memory(
        self, normalizer: HermesEventNormalizer
    ) -> None:
        name = normalizer.derive_name("weird", "x", {})
        assert name.startswith("hermes-memory-")


# ── Sanitisation ──────────────────────────────────────────────────────────────


class TestSanitization:
    def test_dots_stripped(self) -> None:
        out = sanitize_name("hermes-memory-my.dotted.id", prefix="hermes-memory-")
        assert "." not in out
        assert is_valid_name(out)

    def test_invalid_chars_stripped(self) -> None:
        out = sanitize_name("hermes-memory-a/b c:d!e", prefix="hermes-memory-")
        assert is_valid_name(out)
        assert out.startswith("hermes-memory-")

    def test_consecutive_hyphens_collapsed(self) -> None:
        out = sanitize_name("hermes-memory-a---b----c", prefix="hermes-memory-")
        assert "--" not in out

    def test_truncated_to_128_preserving_prefix(self) -> None:
        long_id = "z" * 500
        out = sanitize_name(f"hermes-memory-{long_id}", prefix="hermes-memory-")
        assert len(out) <= 128
        assert out.startswith("hermes-memory-")

    def test_derive_name_through_normalizer_is_valid_with_dots(
        self, normalizer: HermesEventNormalizer
    ) -> None:
        name = normalizer.derive_name("memory", "body", {"memory_id": "v1.2.3.config"})
        assert is_valid_name(name), f"{name!r} must match {_NAME_RE_DESC}"
        assert "." not in name

    def test_derive_name_long_id_le_128_and_stable(self, normalizer: HermesEventNormalizer) -> None:
        meta = {"memory_id": "x" * 300}
        a = normalizer.derive_name("memory", "body", meta)
        b = normalizer.derive_name("memory", "body", meta)
        assert a == b
        assert len(a) <= 128
        assert a.startswith("hermes-memory-")

    def test_empty_yields_unknown(self) -> None:
        out = sanitize_name("", prefix="")
        assert out == "unknown"

    def test_all_derived_names_are_valid(self, normalizer: HermesEventNormalizer) -> None:
        cases = [
            ("memory", "body", {"memory_id": "weird id with spaces & symbols!!"}),
            ("user", "b", {"user_id": "user@example.com"}),
            ("memory", "содержание на кириллице", {}),  # non-ascii content fallback
            ("user", "b", {}),
        ]
        for target, content, meta in cases:
            name = normalizer.derive_name(target, content, meta)
            assert is_valid_name(name), f"{name!r} invalid for {target}/{meta}"


# ── normalize() payload ───────────────────────────────────────────────────────


class TestNormalizePayload:
    def test_source_hermes_tag_present(self, normalizer: HermesEventNormalizer) -> None:
        desc = normalizer.normalize("add", "memory", "b", {"memory_id": "m"})
        assert "source:hermes" in desc["tags"]

    def test_target_tag_present(self, normalizer: HermesEventNormalizer) -> None:
        desc = normalizer.normalize("add", "user", "b", {"user_id": "u"})
        assert "target:user" in desc["tags"]

    def test_content_hash_is_sha256(self, normalizer: HermesEventNormalizer) -> None:
        desc = normalizer.normalize("add", "memory", "hash me", {"memory_id": "m"})
        assert desc["content_hash"] == _sha("hash me")

    def test_content_preserved_as_body_source(self, normalizer: HermesEventNormalizer) -> None:
        desc = normalizer.normalize("add", "memory", "the body text", {"memory_id": "m"})
        assert desc["content"] == "the body text"

    def test_extra_tags_from_metadata(self, normalizer: HermesEventNormalizer) -> None:
        desc = normalizer.normalize("add", "memory", "b", {"memory_id": "m", "tags": ["a", "b"]})
        assert "a" in desc["tags"] and "b" in desc["tags"]

    def test_type_defaults_by_target(self, normalizer: HermesEventNormalizer) -> None:
        assert normalizer.normalize("add", "memory", "b", {"memory_id": "m"})["type"] == "project"
        assert normalizer.normalize("add", "user", "b", {"user_id": "u"})["type"] == "user"

    def test_type_override_from_metadata(self, normalizer: HermesEventNormalizer) -> None:
        desc = normalizer.normalize("add", "memory", "b", {"memory_id": "m", "type": "pattern"})
        assert desc["type"] == "pattern"

    def test_never_returns_none(self, normalizer: HermesEventNormalizer) -> None:
        for action in ("add", "replace", "remove"):
            for target in ("memory", "user"):
                assert normalizer.normalize(action, target, "", {}) is not None


# ── content_hash helper ───────────────────────────────────────────────────────


class TestContentHash:
    def test_matches_hashlib(self) -> None:
        assert content_hash("abc") == _sha("abc")

    def test_empty_safe(self) -> None:
        assert content_hash("") == _sha("")
