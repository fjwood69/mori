"""Memory-name rules (v2.3.12, D7).

A memory name becomes a file name on export and a path segment in tooling, so both write
chokepoints reject anything that is not a plain, single path segment. Prod names were measured
against this rule before it shipped (4,870 active names; none would be rejected).

``normalise_name`` is for the two places that DERIVE a name from free text (the dream's
``_path_to_name`` and ingestion's ``_derive_name``): it returns a valid name unchanged, byte for
byte, so existing memories keep matching their names, and maps anything else to a valid one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

NAME_MAX_LEN = 200
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}"
_NAME_RE = re.compile(NAME_PATTERN)


def invalid_name_reason(name: object) -> str | None:
    """Return why *name* is not a valid memory name, or None if it is valid."""
    if not isinstance(name, str) or not name:
        return "memory name is empty"
    if not _NAME_RE.fullmatch(name):
        return f"memory name {name!r} must match ^{NAME_PATTERN}$"
    if ".." in name:
        return f"memory name {name!r} must not contain '..'"
    return None


def normalise_name(raw: object) -> str:
    """Map derived text to a valid memory name. A valid name is returned unchanged."""
    if not isinstance(raw, str):
        raw = "" if raw is None else str(raw)
    if invalid_name_reason(raw) is None:
        return raw
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.lstrip("._-")[:NAME_MAX_LEN].rstrip("._-")
    if invalid_name_reason(s) is None:
        return s
    # Nothing usable survived (e.g. an all-symbol or all-CJK title): a stable name, so the same
    # input always maps to the same memory.
    return "memory-" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]


def confined_export_path(out_dir: Path, name: str) -> Path | None:
    """``<out_dir>/<name>.md`` if it resolves directly inside *out_dir*, else None.

    Defence in depth behind the write-time name rule (D7): a row stored before v2.3.12 with a
    path-like name can never write outside the export directory.
    """
    if invalid_name_reason(name):
        return None
    out = Path(out_dir).resolve()
    target = (out / f"{name}.md").resolve()
    return target if target.parent == out else None
