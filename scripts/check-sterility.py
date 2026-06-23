#!/usr/bin/env python3
"""Content-sterility gate for the public mori repo.

Fails (exit 1) if internal / homelab-specific markers leak into TRACKED content.
This closes the gap that produced past leaks: the only previous guard was a
PreToolUse filter on tool *output* — nothing scanned what actually got committed.

Run locally:  python scripts/check-sterility.py
CI/pre-commit invoke the same script, so a leak fails the build instead of shipping.

Allowed exceptions (deliberate references) live in ALLOW.
"""

from __future__ import annotations

import re
import subprocess
import sys

# (pattern, human-readable reason). Case-insensitive. Kept high-signal / low-false-positive:
# specific homelab hostnames, paths, the known prod IP, LAN range, and internal id forms.
MARKERS: list[tuple[str, str]] = [
    (r"/home/nucadmin", "absolute homelab path"),
    (r"\bnuc15pro\b", "homelab hostname"),
    (r"\buk-smr-", "homelab hostname"),
    (r"\bca-ws-raspi", "homelab hostname"),
    (r"\buk-ga-raspi", "homelab hostname"),
    (r"\bux3405\b", "homelab hostname"),
    (r"\btwiggy\b", "homelab hostname"),
    (r"\braspi[0-9]", "homelab hostname"),
    (r"\b10\.1\.2\.[0-9]{1,3}\b", "homelab LAN IP"),
    (r"\b100\.90\.219\.111\b", "prod GCE IP"),
    (r"consult-[0-9a-f]{10,}", "internal consult id"),
    (r"dotfiles/mori", "dotfiles path"),
]

# Files where an internal reference is intentional and reviewed.
ALLOW = {
    "DISCLOSURE.md",  # deliberately documents the relationship to the private homelab
    "scripts/check-sterility.py",  # this file necessarily contains the patterns
}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f]


def main() -> int:
    pats = [(re.compile(p, re.IGNORECASE), why) for p, why in MARKERS]
    violations: list[tuple[str, int, str, str]] = []
    for f in _tracked_files():
        if f in ALLOW:
            continue
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    for pat, why in pats:
                        if pat.search(line):
                            violations.append((f, i, why, line.strip()[:100]))
        except (IsADirectoryError, OSError):
            continue

    if violations:
        print("STERILITY GATE FAILED — internal/homelab markers in tracked content:\n")
        for f, i, why, txt in violations:
            print(f"  {f}:{i}  [{why}]  {txt}")
        print(
            f"\n{len(violations)} violation(s). Sanitise before committing; "
            "if a reference is intentional, add the file to ALLOW (and DISCLOSURE.md)."
        )
        return 1

    print("Sterility gate OK — no internal/homelab markers in tracked content.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
