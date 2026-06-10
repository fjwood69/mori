"""Unit tests for externalised prompt loading.

Always runs — no database, no network. Covers:

* ``load_prompt`` reads from ``MORI_PROMPTS_DIR`` when set.
* ``load_prompt`` falls back to the in-code default when the file is
  missing or empty (never hard-fails).
* The packaged default prompts load by default (not the emergency fallback),
  carry the new ``UNIT OF OUTPUT`` directive, and the dreamer prompt has
  dropped the ``action`` field.
* The JSON examples embedded in the packaged prompts are valid JSON
  (a typo there would teach the model malformed output).
"""

from __future__ import annotations

import json
import re

from mori_advisor import prompt_loader
from mori_advisor.prompt_loader import _PACKAGED_DIR, load_prompt


def _embedded_json(text: str):
    """Parse the example JSON array embedded in a prompt.

    The prose contains a literal ``[]`` ("return an empty array []"), so anchor
    on the array-of-objects (``[`` followed by ``{``) rather than the first ``[``.
    """
    m = re.search(r"\[\s*\{", text)
    assert m, "prompt has no embedded JSON example array"
    end = text.rfind("]")
    return json.loads(text[m.start() : end + 1])


def test_load_prompt_reads_override_dir(tmp_path, monkeypatch):
    (tmp_path / "dreamer.txt").write_text("OVERRIDE PROMPT", encoding="utf-8")
    monkeypatch.setenv("MORI_PROMPTS_DIR", str(tmp_path))
    assert load_prompt("dreamer", "fallback") == "OVERRIDE PROMPT"


def test_load_prompt_falls_back_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MORI_PROMPTS_DIR", str(tmp_path))  # empty dir
    assert load_prompt("does-not-exist", "FALLBACK") == "FALLBACK"


def test_load_prompt_falls_back_when_empty(tmp_path, monkeypatch):
    (tmp_path / "archivist.txt").write_text("   \n  ", encoding="utf-8")
    monkeypatch.setenv("MORI_PROMPTS_DIR", str(tmp_path))
    assert load_prompt("archivist", "FALLBACK") == "FALLBACK"


def test_load_prompt_strips_trailing_whitespace(tmp_path, monkeypatch):
    (tmp_path / "p.txt").write_text("hello\n\n", encoding="utf-8")
    monkeypatch.setenv("MORI_PROMPTS_DIR", str(tmp_path))
    assert load_prompt("p", "x") == "hello"


def test_packaged_prompts_exist():
    assert (_PACKAGED_DIR / "dreamer.txt").is_file()
    assert (_PACKAGED_DIR / "archivist.txt").is_file()


def test_packaged_dreamer_is_v2(monkeypatch):
    monkeypatch.delenv("MORI_PROMPTS_DIR", raising=False)
    text = load_prompt("dreamer", "FALLBACK")
    assert text != "FALLBACK"  # loaded the packaged file, not the emergency default
    assert "UNIT OF OUTPUT" in text
    assert "evidence" in text
    # the action field was dropped (epistemic-access rule)
    assert '"action"' not in text and "- action:" not in text
    _embedded_json(text)  # examples parse as JSON


def test_packaged_archivist_is_v2(monkeypatch):
    monkeypatch.delenv("MORI_PROMPTS_DIR", raising=False)
    text = load_prompt("archivist", "FALLBACK")
    assert text != "FALLBACK"
    assert "UNIT OF OUTPUT" in text
    _embedded_json(text)


def test_module_constants_use_packaged_prompts():
    # The wired-in constants should resolve to the packaged files, not the fallback.
    from mori_advisor.dream import DREAM_SYSTEM_PROMPT
    from mori_advisor.ingestion import INGESTION_SYSTEM_PROMPT

    assert "UNIT OF OUTPUT" in DREAM_SYSTEM_PROMPT
    assert "UNIT OF OUTPUT" in INGESTION_SYSTEM_PROMPT


def test_output_reminder_is_a_closer():
    # The reminder must demand raw JSON bounded by [ ].
    r = prompt_loader.OUTPUT_REMINDER
    assert "[" in r and "]" in r and "JSON" in r


def test_distill_batch_output_contract_is_last():
    """Regression for the assembly-order bug: the dynamic focus/tier/tags lines
    must NOT be the tail of the assembled prompt (it ended on 'Add these tags to
    every memory: g'). The output contract is the closer — in both the system
    tail and the bottom of the user payload (the recency-most position).
    """
    from mori_advisor.ingestion import IngestionPipeline
    from mori_advisor.parsers import Chunk
    from mori_advisor.prompt_loader import OUTPUT_REMINDER

    captured: dict[str, str] = {}

    class StubClient:
        def consult(self, system, user, vk, max_tokens, temperature):
            captured["system"] = system
            captured["user"] = user
            return "[]"

    pipe = IngestionPipeline(
        db_path="/tmp/unused-mori-test.db",
        bifrost_client=StubClient(),
        memory_store=None,
        store=object(),  # avoid SQLiteStore (and its nats import)
    )
    chunk = Chunk(content="some source code", metadata={"source_path": "x.py"})
    pipe._distill_batch([chunk], focus_guidance="Focus on X.", tier="canonical", tags=["g"])

    system = captured["system"].rstrip()
    assert system.endswith(OUTPUT_REMINDER)
    assert not system.endswith("Add these tags to every memory: g")
    assert captured["user"].rstrip().endswith(OUTPUT_REMINDER)
    # the housekeeping is still present — just no longer the last thing the model reads
    assert 'Set tier to "canonical" for all memories.' in system
    assert "Add these tags to every memory: g" in system
