"""Tests for normalise_events_text — pre-dream events normaliser.

Six required tests (reformulated against the real _format_events output format):
  1. Strip test      — Tool: and Stopped: lines are removed; nothing else is
  2. Keep test       — Session headers, Prompt, FAILURE, CWD, Assistant prose survive
  3. No-signal test  — all-scaffolding input (no Assistant:) yields no Tool:/Stopped: lines
  4. Idempotency     — normalise(normalise(x)) == normalise(x)
  5. Token reduction — output word count < input word count on a real-format batch
  6. Mixed-interleave — Tool/Assistant/Tool/Assistant order: prose kept, framing gone

Additional:
  7. FAILURE lines are never stripped (critical signal)
  8. Prose containing "Tool: X" inside an Assistant: segment is not stripped
     (line-anchor means only lines *starting* with "  Tool:" are removed)
"""

from mori_advisor.dream import normalise_events_text

# ---------------------------------------------------------------------------
# Shared fixtures — format matches real _format_events output
# ---------------------------------------------------------------------------

# Matches the real example shown in the spec (10+ tool calls, one assistant block)
SESSION_A = """\
Session: a50e2377-968e-4442-b8bc-dca49cdebc1f (2026-06-13T16:07:24, uk-smr-nuc15pro)
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Read
  Tool: Read
  Stopped: None
  Assistant: Now let me run the test: All 7 subtests pass. the external chroma dependency the task explicitly told me to skip go vet Clean.
"""

# Session with no Assistant: block — pure tool scaffolding
SESSION_B = """\
Session: 636c51c4-9120-4a43-ba5a-8e54c372c01f (2026-06-13T16:07:28, uk-smr-nuc15pro)
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: Bash
  Tool: TodoWrite
  Tool: TodoWrite
  Tool: Edit
  Tool: Bash
  Stopped: None
"""

# Session with FAILURE and CWD (must survive)
SESSION_C = """\
Session: cd05be71-0cec-4e54-8f4a-3a0b9a5ecc11 (2026-06-13T16:07:24, uk-smr-nuc15pro)
  CWD: /home/nucadmin/mori
  Prompt: You are a software engineer working in the current directory.
  Tool: Bash
  Tool: Read
  FAILURE (Edit): cannot apply patch — context mismatch
  Tool: Bash
  Stopped: end_turn
  Assistant: Almost there. The expected output has an extra newline.
"""

EXAMPLE_BATCH = SESSION_A + "\n" + SESSION_B + "\n" + SESSION_C


# ---------------------------------------------------------------------------
# Test 1 — Strip test
# ---------------------------------------------------------------------------


def test_strip_tool_and_stopped_lines():
    """Tool: and Stopped: lines must be absent from the output."""
    result = normalise_events_text(EXAMPLE_BATCH)
    for line in result.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("Tool:"), f"Tool: line survived: {line!r}"
        assert not stripped.startswith("Stopped:"), f"Stopped: line survived: {line!r}"


# ---------------------------------------------------------------------------
# Test 2 — Keep test
# ---------------------------------------------------------------------------


def test_keep_session_headers_and_signal():
    """Session headers, Prompt, CWD, FAILURE, and Assistant prose must survive."""
    result = normalise_events_text(EXAMPLE_BATCH)
    assert "Session: a50e2377" in result, "Session header A missing"
    assert "Session: 636c51c4" in result, "Session header B missing"
    assert "Session: cd05be71" in result, "Session header C missing"
    assert "Prompt: You are a software engineer" in result, "Prompt line missing"
    assert "CWD: /home/nucadmin/mori" in result, "CWD line missing"
    assert "All 7 subtests pass" in result, "Assistant prose missing"
    assert "the external chroma dependency" in result, "Prose fragment missing"
    assert "Almost there" in result, "Session C assistant prose missing"


# ---------------------------------------------------------------------------
# Test 3 — No-signal test
# ---------------------------------------------------------------------------


def test_all_scaffolding_removes_tool_stopped():
    """An all-scaffolding session (no Assistant:) loses only Tool:/Stopped: lines."""
    result = normalise_events_text(SESSION_B)
    # The Session: header must still be present
    assert "Session: 636c51c4" in result, "Session header stripped — should be kept"
    # No Tool: or Stopped: lines
    for line in result.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("Tool:"), f"Tool: line survived: {line!r}"
        assert not stripped.startswith("Stopped:"), f"Stopped: line survived: {line!r}"


# ---------------------------------------------------------------------------
# Test 4 — Idempotency test
# ---------------------------------------------------------------------------


def test_idempotency():
    """Running the normaliser twice must equal running it once."""
    once = normalise_events_text(EXAMPLE_BATCH)
    twice = normalise_events_text(once)
    assert once == twice, (
        f"normalise_events_text is not idempotent.\n  once:  {once!r}\n  twice: {twice!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Token-reduction assertion
# ---------------------------------------------------------------------------


def test_token_reduction():
    """Output word count must be strictly less than input word count."""
    input_words = len(EXAMPLE_BATCH.split())
    output_words = len(normalise_events_text(EXAMPLE_BATCH).split())
    assert output_words < input_words, (
        f"Expected output ({output_words} words) < input ({input_words} words)"
    )


# ---------------------------------------------------------------------------
# Test 6 — Mixed-interleave test
# ---------------------------------------------------------------------------


def test_mixed_interleave_preserves_prose_order():
    """Tool/Assistant/Tool/Assistant interleaving: prose kept, Tools gone, order intact."""
    mixed = (
        "Session: aaa (2026-01-01, host)\n"
        "  Tool: Bash\n"
        "  Assistant: foo reasoning here\n"
        "  Tool: Read\n"
        "  Assistant: bar reasoning there\n"
    )
    result = normalise_events_text(mixed)
    assert "foo reasoning here" in result, f"'foo' missing: {result!r}"
    assert "bar reasoning there" in result, f"'bar' missing: {result!r}"
    assert "Tool:" not in result, f"Tool: survived: {result!r}"
    assert result.index("foo") < result.index("bar"), f"Order not preserved: {result!r}"


# ---------------------------------------------------------------------------
# Test 7 — FAILURE lines are never stripped
# ---------------------------------------------------------------------------


def test_failure_lines_preserved():
    """FAILURE (Tool): lines are critical signal and must never be stripped."""
    result = normalise_events_text(SESSION_C)
    assert "FAILURE (Edit): cannot apply patch" in result, (
        f"FAILURE line was stripped — must be kept: {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 8 — Prose containing "Tool: X" inside Assistant: is not stripped
# ---------------------------------------------------------------------------


def test_prose_tool_reference_not_stripped():
    """A line starting with '  Assistant:' that contains 'Tool: Read' in prose is kept."""
    events = (
        "Session: bbb (2026-01-01, host)\n"
        "  Tool: Bash\n"
        "  Assistant: I used Tool: Read to verify the file contents. Result was correct.\n"
    )
    result = normalise_events_text(events)
    # The Tool: inside the Assistant prose must survive (it's part of the prose line,
    # not a standalone "  Tool: <name>" line at the start).
    assert "I used Tool: Read to verify" in result, (
        f"Prose reference to Tool: was incorrectly stripped: {result!r}"
    )
    # The standalone Tool: Bash line must still be stripped
    lines = [ln.strip() for ln in result.splitlines()]
    assert "Tool: Bash" not in lines, "Standalone Tool: Bash survived"
