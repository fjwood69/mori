"""Pure-logic tests for mori_intake.normalize — no DB, always run.

Covers:
- NFKC normalisation (ligature folding, compatibility characters)
- Whitespace collapse (leading/trailing/internal)
- Hash stability across differently-spaced equivalent inputs
- Hash equality for NFKC-equivalent inputs
"""

from __future__ import annotations

import hashlib

from mori_intake.normalize import canonical_body, content_hash

# ── canonical_body ────────────────────────────────────────────────────────────


class TestCanonicalBody:
    def test_nfkc_ligature_fi(self):
        """Latin small ligature 'ﬁ' (U+FB01) must fold to 'fi'."""
        result = canonical_body("ﬁle")
        assert result == "file"

    def test_nfkc_ligature_fl(self):
        """Latin small ligature 'ﬂ' (U+FB02) must fold to 'fl'."""
        result = canonical_body("ﬂoor")
        assert result == "floor"

    def test_nfkc_superscript_digits(self):
        """Superscript digit '²' (U+00B2) folds to '2'."""
        result = canonical_body("x² + y²")
        assert result == "x2 + y2"

    def test_nfkc_fullwidth_chars(self):
        """Full-width ASCII letter folds to ASCII equivalent."""
        # U+FF21 = full-width 'A'
        result = canonical_body("Ａ test")
        assert result == "A test"

    def test_strips_leading_trailing_whitespace(self):
        assert canonical_body("  hello world  ") == "hello world"

    def test_collapses_internal_whitespace(self):
        assert canonical_body("hello   world\t\there") == "hello world here"

    def test_newlines_collapsed(self):
        assert canonical_body("line one\nline two\n") == "line one line two"

    def test_already_canonical(self):
        text = "already canonical body"
        assert canonical_body(text) == text

    def test_empty_string(self):
        assert canonical_body("") == ""

    def test_whitespace_only(self):
        assert canonical_body("   \t\n  ") == ""


# ── content_hash ──────────────────────────────────────────────────────────────


class TestContentHash:
    def test_returns_64_char_hex(self):
        h = content_hash("The agent learned something important today.")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_stability(self):
        """Same input must yield the same hash on repeated calls."""
        text = "The agent learned something important today."
        assert content_hash(text) == content_hash(text)

    def test_hash_equality_for_equivalent_spacing(self):
        """Differently-spaced but semantically-identical texts must hash the same."""
        a = "The agent   learned    something important."
        b = "The agent learned something important."
        assert content_hash(a) == content_hash(b)

    def test_hash_equality_for_nfkc_equivalent(self):
        """NFKC-equivalent inputs must hash identically."""
        # U+FB01 = ﬁ → 'fi'
        a = "The agent ﬁled a report about something important."
        b = "The agent filed a report about something important."
        assert content_hash(a) == content_hash(b)

    def test_hash_differs_for_different_content(self):
        a = "The agent learned X is true."
        b = "The agent learned X is false."
        assert content_hash(a) != content_hash(b)

    def test_matches_manual_sha256(self):
        """Verify the hash is SHA-256 of the canonical UTF-8 bytes."""
        text = "the quick brown fox"
        expected = hashlib.sha256(canonical_body(text).encode("utf-8")).hexdigest()
        assert content_hash(text) == expected

    def test_hash_of_whitespace_only(self):
        """Whitespace-only collapses to '' — hash of empty string is stable."""
        h_empty = content_hash("")
        h_spaces = content_hash("   \n\t  ")
        assert h_empty == h_spaces
