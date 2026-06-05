"""Tests for HermesEventNormalizer.

Verifies:
  * Durable frontmatter → payload with correct name, idempotency key,
    frontmatter stripped from body.
  * Ephemeral frontmatter signal → None (drop).
  * Ephemeral target-regex fallback → None (drop).
  * action=="remove" → retraction proposal (new memory, NOT a delete).
  * Missing memory_id → degraded slug path, still namespaced hermes-*.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# Make the package importable without installing it.
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from hermes_mori_provider.normalizer import HermesEventNormalizer

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def normalizer() -> HermesEventNormalizer:
    return HermesEventNormalizer()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── Durable payload tests ────────────────────────────────────────────────────


class TestDurablePayload:
    """Durable frontmatter produces a well-formed payload."""

    def test_name_namespaced(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: my-learning\ndurability: durable\n---\nThe body."
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert result["name"] == "hermes-my-learning"

    def test_idempotency_is_sha256_of_original_content(
        self, normalizer: HermesEventNormalizer
    ) -> None:
        content = "---\nmemory_id: test-key\ndurability: durable\n---\nSome body text."
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert result["idempotency_key"] == _sha(content)

    def test_frontmatter_stripped_from_body(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: strip-test\ndurability: durable\n---\nBody text only."
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert "durability" not in result["body"]
        assert "memory_id" not in result["body"]
        assert "Body text only." in result["body"]

    def test_source_hermes_tag_always_present(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: tag-test\ndurability: durable\n---\nBody."
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert "source:hermes" in result["tags"]

    def test_extra_tags_from_frontmatter_included(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: multi-tag\ndurability: durable\ntags: [a, b]\n---\nBody."
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert "a" in result["tags"]
        assert "b" in result["tags"]

    def test_type_from_frontmatter(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: typed\ndurability: durable\ntype: pattern\n---\nBody."
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert result["type"] == "pattern"

    def test_replace_action_is_durable(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: upd\ndurability: durable\n---\nUpdated body."
        result = normalizer.normalize("replace", "MEMORY.md", content)
        assert result is not None
        assert result["name"] == "hermes-upd"


# ── Ephemeral drop tests ─────────────────────────────────────────────────────


class TestEphemeralSignal:
    """Events that should be dropped return None."""

    def test_explicit_ephemeral_durability(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: temp\ndurability: ephemeral\n---\nIgnore this."
        assert normalizer.normalize("add", "MEMORY.md", content) is None

    def test_ephemeral_case_insensitive(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: temp\ndurability: Ephemeral\n---\nIgnore."
        assert normalizer.normalize("add", "MEMORY.md", content) is None

    def test_ephemeral_target_regex_scratch(self, normalizer: HermesEventNormalizer) -> None:
        """Target 'scratch.md' → no frontmatter → dropped via regex."""
        content = "No frontmatter here. Just notes."
        assert normalizer.normalize("add", "scratch.md", content) is None

    def test_ephemeral_target_regex_temp(self, normalizer: HermesEventNormalizer) -> None:
        content = "Some temporary notes."
        assert normalizer.normalize("add", "temp_notes.md", content) is None

    def test_ephemeral_target_regex_wip(self, normalizer: HermesEventNormalizer) -> None:
        content = "WIP thoughts."
        assert normalizer.normalize("add", "wip-feature.md", content) is None

    def test_ephemeral_target_regex_draft(self, normalizer: HermesEventNormalizer) -> None:
        content = "Draft content."
        assert normalizer.normalize("add", "draft.md", content) is None

    def test_no_ephemeral_patterns_bypasses_regex(self) -> None:
        """With no patterns, a 'scratch' target without frontmatter is durable."""
        n = HermesEventNormalizer(ephemeral_target_patterns=[])
        content = "Should be durable even for scratch target."
        result = n.normalize("add", "scratch.md", content)
        # No frontmatter → degraded slug path, but NOT dropped.
        assert result is not None

    def test_durable_frontmatter_overrides_ephemeral_target(
        self, normalizer: HermesEventNormalizer
    ) -> None:
        """Explicit durability: durable beats ephemeral target pattern."""
        content = "---\nmemory_id: keep\ndurability: durable\n---\nKeep this."
        result = normalizer.normalize("add", "scratch.md", content)
        assert result is not None
        assert result["name"] == "hermes-keep"


# ── Retraction tests ──────────────────────────────────────────────────────────


class TestRetraction:
    """action=="remove" produces a retraction proposal, never a delete."""

    def test_retraction_name_has_retracted_suffix(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: old-fact\ndurability: durable\n---\nThe old fact."
        result = normalizer.normalize("remove", "MEMORY.md", content)
        assert result is not None
        assert result["name"] == "hermes-old-fact-retracted"

    def test_retraction_type_is_decision(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: old-fact\ndurability: durable\n---\nThe old fact."
        result = normalizer.normalize("remove", "MEMORY.md", content)
        assert result is not None
        assert result["type"] == "decision"

    def test_retraction_body_contains_original_name(
        self, normalizer: HermesEventNormalizer
    ) -> None:
        content = "---\nmemory_id: old-fact\ndurability: durable\n---\nThe old fact."
        result = normalizer.normalize("remove", "MEMORY.md", content)
        assert result is not None
        assert "hermes-old-fact" in result["body"]

    def test_retraction_body_embeds_original_content(
        self, normalizer: HermesEventNormalizer
    ) -> None:
        content = "---\nmemory_id: old-fact\ndurability: durable\n---\nThe old fact."
        result = normalizer.normalize("remove", "MEMORY.md", content)
        assert result is not None
        assert "The old fact." in result["body"]

    def test_retraction_tags_include_retraction(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: old-fact\ndurability: durable\n---\nBody."
        result = normalizer.normalize("remove", "MEMORY.md", content)
        assert result is not None
        assert "retraction" in result["tags"]
        assert "source:hermes" in result["tags"]

    def test_retraction_idempotency_from_content(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\nmemory_id: old-fact\ndurability: durable\n---\nBody."
        result = normalizer.normalize("remove", "MEMORY.md", content)
        assert result is not None
        assert result["idempotency_key"] == _sha(content)

    def test_ephemeral_remove_is_dropped(self, normalizer: HermesEventNormalizer) -> None:
        """Ephemeral events are dropped even for 'remove' action."""
        content = "---\nmemory_id: tmp\ndurability: ephemeral\n---\nEphemeral."
        assert normalizer.normalize("remove", "MEMORY.md", content) is None


# ── Degraded path (no memory_id) tests ───────────────────────────────────────


class TestDegradedSlug:
    """Missing memory_id → slug derived from content, still hermes-* namespaced."""

    def test_name_is_hermes_prefixed(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\ndurability: durable\n---\nThis is a durable memory without an id."
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert result["name"].startswith("hermes-")

    def test_name_slug_derived_from_body(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\ndurability: durable\n---\nhello world from the agent"
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        # Slug from "hello world from the agent"
        assert "hello" in result["name"]

    def test_idempotency_still_set(self, normalizer: HermesEventNormalizer) -> None:
        content = "---\ndurability: durable\n---\nDegradation test."
        result = normalizer.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert result["idempotency_key"] == _sha(content)

    def test_no_frontmatter_at_all_durable_target(self) -> None:
        """No frontmatter + non-ephemeral target → durable degraded path."""
        n = HermesEventNormalizer(ephemeral_target_patterns=[])
        content = "Plain memory with no frontmatter at all."
        result = n.normalize("add", "MEMORY.md", content)
        assert result is not None
        assert result["name"].startswith("hermes-")

    def test_degraded_retraction_has_retracted_suffix(
        self, normalizer: HermesEventNormalizer
    ) -> None:
        content = "---\ndurability: durable\n---\nSome old fact."
        result = normalizer.normalize("remove", "MEMORY.md", content)
        assert result is not None
        assert result["name"].endswith("-retracted")
        assert result["name"].startswith("hermes-")
