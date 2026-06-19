"""Tests for normalise_transcript_tail — pre-dream transcript normaliser.

Six required tests:
  1. Strip test     — no Tool:/Stopped:/Session: tokens survive in output
  2. Keep test      — reasoning prose is preserved verbatim
  3. No-narration   — all-scaffolding input yields empty/whitespace output
  4. Idempotency    — normalise(normalise(x)) == normalise(x)
  5. Token reduction — output word count < input word count on the example blob
  6. Mixed-interleave — interleaved Tool/Assistant labels preserve prose order
"""

from mori_advisor.dream import normalise_transcript_tail

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

EXAMPLE_BLOB = (
    "Tool: Bash Tool: Bash Tool: Bash Tool: Read "
    "Stopped: None "
    "Assistant: Now let me run the test: All 7 subtests pass. "
    "the external chroma dependency the task explicitly told me to skip "
    "go vet … Clean. "
    "Session: cd05be71-0000-0000-0000-000000000000 "
    "(2026-06-13T16:07:24, uk-smr-nuc15pro) "
    "Tool: Bash "
    "Assistant: Final verification complete."
)


# ---------------------------------------------------------------------------
# Test 1 — Strip test
# ---------------------------------------------------------------------------


def test_strip_structural_tokens():
    """Output must not contain Tool:, Stopped:, or Session: tokens."""
    result = normalise_transcript_tail(EXAMPLE_BLOB)
    assert "Tool:" not in result, f"'Tool:' survived normalisation: {result!r}"
    assert "Stopped:" not in result, f"'Stopped:' survived normalisation: {result!r}"
    assert "Session:" not in result, f"'Session:' survived normalisation: {result!r}"


# ---------------------------------------------------------------------------
# Test 2 — Keep test
# ---------------------------------------------------------------------------


def test_keep_reasoning_prose_verbatim():
    """Assistant reasoning prose must be preserved word-for-word."""
    result = normalise_transcript_tail(EXAMPLE_BLOB)
    assert "All 7 subtests pass" in result, f"Reasoning prose missing: {result!r}"
    assert "the external chroma dependency" in result, f"Prose fragment missing: {result!r}"
    assert "go vet" in result, f"'go vet' missing: {result!r}"
    assert "Clean" in result, f"'Clean' missing: {result!r}"


# ---------------------------------------------------------------------------
# Test 3 — No-narration-loss test
# ---------------------------------------------------------------------------


def test_all_scaffolding_yields_empty():
    """Input with no Assistant: prose must produce empty or whitespace-only output."""
    scaffolding_only = (
        "Tool: Bash Tool: Read Tool: Write "
        "Stopped: end_turn "
        "Session: abcd1234 (2026-01-01T00:00:00, some-host)"
    )
    result = normalise_transcript_tail(scaffolding_only)
    assert result.strip() == "", f"Expected empty output, got: {result!r}"


# ---------------------------------------------------------------------------
# Test 4 — Idempotency test
# ---------------------------------------------------------------------------


def test_idempotency():
    """Running the normaliser twice must equal running it once."""
    once = normalise_transcript_tail(EXAMPLE_BLOB)
    twice = normalise_transcript_tail(once)
    assert once == twice, f"normalise is not idempotent.\n  once:  {once!r}\n  twice: {twice!r}"


# ---------------------------------------------------------------------------
# Test 5 — Token-reduction assertion
# ---------------------------------------------------------------------------


def test_token_reduction():
    """Output word count must be strictly less than input word count."""
    input_words = len(EXAMPLE_BLOB.split())
    output_words = len(normalise_transcript_tail(EXAMPLE_BLOB).split())
    assert output_words < input_words, (
        f"Expected output ({output_words} words) < input ({input_words} words)"
    )


# ---------------------------------------------------------------------------
# Test 6 — Mixed-interleave test
# ---------------------------------------------------------------------------


def test_mixed_interleave_preserves_order():
    """Tool/Assistant/Tool/Assistant interleaving — prose only, order intact."""
    mixed = "Tool: Bash Assistant: foo Tool: Read Assistant: bar"
    result = normalise_transcript_tail(mixed)
    assert "foo" in result, f"'foo' missing: {result!r}"
    assert "bar" in result, f"'bar' missing: {result!r}"
    assert "Tool:" not in result, f"'Tool:' survived: {result!r}"
    # Order: foo must appear before bar
    assert result.index("foo") < result.index("bar"), f"Order not preserved: {result!r}"
    # The two prose segments should be contiguous (joined, no framing between)
    assert result.strip() == "foo bar", f"Expected 'foo bar', got: {result!r}"
