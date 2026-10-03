#!/usr/bin/env python3
"""CI ratchet for strict mypy: the error count may only go down (issue #78).

Runs ``mypy --strict --ignore-missing-imports mori_advisor/`` and compares the total with the
committed baseline in ``scripts/mypy-baseline.txt``:

* more errors than the baseline -> fail (new code must not add type errors);
* fewer -> pass, with a notice asking for the baseline to be lowered in the same PR;
* mypy unable to report a count (crash, missing install) -> fail. An empty or unparseable
  result is never read as zero errors.

Two real bugs fixed in v2.3.11 were already in this output, unread: a missing keyword argument
that made SQLite rollback raise, and a ``write()`` signature mismatch that stopped the SQLite
dream writing anything since v2.3.0.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "mypy-baseline.txt"
CMD = [sys.executable, "-m", "mypy", "--strict", "--ignore-missing-imports", "mori_advisor/"]


def count_errors(output: str) -> int | None:
    m = re.search(r"^Found (\d+) errors? in \d+ files?", output, re.MULTILINE)
    if m:
        return int(m.group(1))
    if re.search(r"^Success: no issues found", output, re.MULTILINE):
        return 0
    return None


def main() -> int:
    baseline = int(BASELINE.read_text().strip())
    proc = subprocess.run(CMD, cwd=ROOT, capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    count = count_errors(output)
    if count is None:
        print(output[-4000:])
        print("::error::mypy did not report an error count — the ratchet cannot pass on no result")
        return 1
    print(f"strict mypy: {count} errors (baseline {baseline})")
    if count > baseline:
        print(output[-8000:])
        print(
            f"::error::strict mypy errors rose from {baseline} to {count}. Fix the new errors "
            "(they are listed above) rather than raising the baseline."
        )
        return 1
    if count < baseline:
        print(
            f"::notice::strict mypy errors fell from {baseline} to {count} — lower "
            f"scripts/mypy-baseline.txt to {count} in this PR so the ratchet holds the gain."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
