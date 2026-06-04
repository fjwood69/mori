"""Pytest bootstrap for mori_advisor.

`mori_advisor.main` constructs its store at import time from MORI_ADVISOR_DATA,
so point that at a throwaway temp dir before any test imports it.
"""

import os
import tempfile

os.environ.setdefault("MORI_ADVISOR_DATA", tempfile.mkdtemp(prefix="mori-test-"))
# Force the SQLite backend regardless of the developer's shell env.
os.environ.pop("MORI_DATABASE_URL", None)
