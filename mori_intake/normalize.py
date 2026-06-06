"""Hash contract for mori-intake — the cross-system invariant.

This module is the **single source of truth** for content normalisation and
hashing.  Any future provider-side hash MUST import or faithfully replicate
these two functions.

No I/O, no side effects — pure functions, safe to import anywhere.
"""

from __future__ import annotations

import hashlib
import unicodedata


def canonical_body(text: str) -> str:
    """Deterministic canonical form for dedup.

    Applies Unicode NFKC normalisation, strips leading/trailing whitespace,
    then collapses all internal whitespace sequences to a single space.
    The result is stable across processes and programming languages provided
    the input encoding is UTF-8.
    """
    return " ".join(unicodedata.normalize("NFKC", text).split())


def content_hash(text: str) -> str:
    """Full 64-character hex SHA-256 of the canonical body.

    Stable across processes and languages.  Stored as TEXT (not bytea) in
    ``intake_candidates.content_hash`` for debuggability and encoding
    consistency.
    """
    return hashlib.sha256(canonical_body(text).encode("utf-8")).hexdigest()
