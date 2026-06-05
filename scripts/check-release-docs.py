#!/usr/bin/env python3
"""Release-docs currency gate.

Fails the release if CHANGELOG.md / ROADMAP.md don't reflect the version being
shipped — so the docs can't silently lapse behind releases (as the ROADMAP did,
drifting from v2.1.16 while the code reached v2.2.8).

Enforces, for a release `vX.Y.Z`:
  1. CHANGELOG.md has a `## vX.Y.Z` section heading.
  2. ROADMAP.md footer `*Last updated: vX.Y.Z*` matches the release version
     (forces a per-release touch of the roadmap, so it can't go stale silently).

Usage:
    check-release-docs.py <version>     # e.g. v2.2.8 or 2.2.8

Wired into CD (.github/workflows/cd.yml) as the first step of build-and-push,
so a lapsed-docs release fails before any image is built or deployed.
"""

from __future__ import annotations

import pathlib
import re
import sys

_SEMVER = r"[0-9]+\.[0-9]+\.[0-9]+"


def check(version: str, root: pathlib.Path) -> list[str]:
    ver = version.strip().lstrip("v")
    if not re.fullmatch(_SEMVER, ver):
        return [f"version {version!r} is not a semver (X.Y.Z)"]

    errors: list[str] = []

    changelog = root / "CHANGELOG.md"
    if not changelog.exists():
        errors.append("CHANGELOG.md is missing")
    elif not re.search(rf"^##\s+v{re.escape(ver)}\b", changelog.read_text(encoding="utf-8"), re.M):
        errors.append(f"CHANGELOG.md has no '## v{ver}' section")

    roadmap = root / "ROADMAP.md"
    if not roadmap.exists():
        errors.append("ROADMAP.md is missing")
    else:
        m = re.search(rf"Last updated:\s*v?({_SEMVER})", roadmap.read_text(encoding="utf-8"))
        if not m:
            errors.append("ROADMAP.md has no '*Last updated: vX.Y.Z*' footer")
        elif m.group(1) != ver:
            errors.append(
                f"ROADMAP.md footer says v{m.group(1)} but releasing v{ver} "
                "— update the roadmap (and its footer) for this release"
            )

    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check-release-docs.py <version>", file=sys.stderr)
        return 2

    root = pathlib.Path(__file__).resolve().parent.parent
    errors = check(sys.argv[1], root)

    if errors:
        print("RELEASE DOCS GATE: FAILED", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(
            f"\nUpdate CHANGELOG.md and ROADMAP.md for {sys.argv[1]} before releasing.",
            file=sys.stderr,
        )
        return 1

    print(f"RELEASE DOCS GATE: OK ({sys.argv[1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
