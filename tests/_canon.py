"""Test helper: the canon-authority token for tests that SEED canonical rows (v2.3.16).

Since the canon lock, a canonical row is created or changed only with canon authority. Tests that
seed canon pass ``_canon_authority=CANON_SEED`` explicitly, so every such seed is visible at the
call site. The token-creation pin (tests/test_canon_lock_token.py) covers production code only.
"""

from mori_advisor.canon_authority import CanonAuthority

CANON_SEED = CanonAuthority("test seed")
