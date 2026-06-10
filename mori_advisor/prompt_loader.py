"""Externalised prompt loading.

Distillation prompts (dreamer, archivist) live as editable text files so they can
be tuned without code changes — edit the file, restart mori, no `.py` edit. The
packaged ``mori_advisor/prompts/`` directory holds the shipped defaults; set
``MORI_PROMPTS_DIR`` (e.g. a bind-mounted host dir in the container) to override.

Resolution is done once at import: a prompt change is picked up on the next
restart, which is the intended operational model. A missing or unreadable file
falls back to the caller's compact in-code default (logged), so a typo or a bad
mount can never hard-fail distillation.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGED_DIR = Path(__file__).parent / "prompts"

# Appended to the BOTTOM of the user payload (and the system tail) so the output
# contract sits in the recency-most position the model reads last — combats the
# recency bias that buries instructions under transcript/chunk text.
OUTPUT_REMINDER = (
    "Now output the JSON array of memory objects described above. "
    "Raw JSON only: the first character must be [ and the last must be ]. "
    "No markdown code fences, no commentary before or after."
)


def prompts_dir() -> Path:
    """The active prompts directory: ``MORI_PROMPTS_DIR`` if set, else packaged."""
    override = os.getenv("MORI_PROMPTS_DIR")
    return Path(override) if override else _PACKAGED_DIR


def load_prompt(name: str, default: str) -> str:
    """Return the text of ``{prompts_dir()}/{name}.txt``, or ``default`` on any miss.

    ``default`` is an emergency fallback only — the shipped packaged file is the
    real default and the operator-editable source of truth.
    """
    path = prompts_dir() / f"{name}.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            logger.info("Loaded prompt '%s' from %s (%d chars)", name, path, len(text))
            return text
        logger.warning("Prompt file %s is empty; using built-in fallback for '%s'", path, name)
    except FileNotFoundError:
        logger.warning("No prompt file %s; using built-in fallback for '%s'", path, name)
    except Exception as e:  # unreadable, permission, decode — never hard-fail
        logger.warning("Failed reading prompt %s (%s); using built-in fallback", path, e)
    return default
