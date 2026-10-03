#!/usr/bin/env python3
"""CI guard: a shipped artifact that changes must change its version (issue #88).

The Claude Code plugin gained HTTP status checks in June, but its version stayed 0.3.3, so the
plugin manager never offered the update and every installed client kept running the old hooks:
a release-process failure, not a code one. This guard makes that state fail CI.

Each artifact is a directory plus the files that state its version:

* **claude-plugin** -- ``plugins/mori/``: ``plugins/mori/.claude-plugin/plugin.json`` and the
  ``mori`` entry of ``.claude-plugin/marketplace.json``;
* **hermes-provider** -- ``integrations/hermes-memory-provider/``: ``pyproject.toml``,
  ``plugin.yaml``, ``hermes_mori_provider/__init__.py`` and the ``plugin_version`` the outbox
  sends.

Two rules per artifact:

1. its version sources agree with each other (always checked);
2. if any file under its directory differs from the base, its version is greater than the
   base's (semver X.Y.Z).

Usage: ``check-artifact-versions.py --base <ref>`` -- in CI, the pull request's base SHA, or the
push's ``before`` SHA. An empty or all-zero base (a new branch) skips rule 2. Changes are read
from the working tree against the merge base, so a local run sees uncommitted edits too. Needs
full history (``fetch-depth: 0``): a merge base that can't be found fails, never passes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _json_version(text: str) -> str:
    return str(json.loads(text)["version"])


def _marketplace_version(text: str) -> str:
    for plugin in json.loads(text).get("plugins", []):
        if plugin.get("name") == "mori":
            return str(plugin["version"])
    raise KeyError("no plugin named 'mori'")


def _pyproject_version(text: str) -> str:
    return str(tomllib.loads(text)["project"]["version"])


def _regex(pattern: str) -> Callable[[str], str]:
    rx = re.compile(pattern, re.MULTILINE)

    def extract(text: str) -> str:
        found = rx.findall(text)
        if len(found) != 1:
            raise KeyError(f"expected exactly one match for {pattern!r}, found {len(found)}")
        return str(found[0])

    return extract


@dataclass(frozen=True)
class Source:
    path: str
    extract: Callable[[str], str]


@dataclass(frozen=True)
class Artifact:
    name: str
    directory: str
    sources: tuple[Source, ...]  # the first one is compared against the base


HERMES = "integrations/hermes-memory-provider"
ARTIFACTS = (
    Artifact(
        "claude-plugin",
        "plugins/mori",
        (
            Source("plugins/mori/.claude-plugin/plugin.json", _json_version),
            Source(".claude-plugin/marketplace.json", _marketplace_version),
        ),
    ),
    Artifact(
        "hermes-provider",
        HERMES,
        (
            Source(f"{HERMES}/pyproject.toml", _pyproject_version),
            Source(f"{HERMES}/plugin.yaml", _regex(r'^version:\s*["\']?([^"\'\s]+)["\']?\s*$')),
            Source(
                f"{HERMES}/hermes_mori_provider/__init__.py",
                _regex(r'^__version__\s*=\s*["\']([^"\']+)["\']'),
            ),
            Source(
                f"{HERMES}/hermes_mori_provider/outbox.py", _regex(r'"plugin_version":\s*"([^"]+)"')
            ),
        ),
    ),
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def semver(v: str) -> tuple[int, int, int] | None:
    m = SEMVER.match(v)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def changed_files(repo: Path, base: str) -> list[str]:
    tracked = git(repo, "diff", "--name-only", base).splitlines()
    untracked = git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    return [p for p in tracked + untracked if p]


def check(repo: Path, base: str | None) -> list[str]:
    errors: list[str] = []
    merge_base: str | None = None
    changed: list[str] = []
    if base and set(base) != {"0"}:
        try:
            merge_base = git(repo, "merge-base", base, "HEAD").strip()
        except subprocess.CalledProcessError:
            return [
                f"cannot find a merge base with {base!r}; CI needs full history (fetch-depth: 0)"
            ]
        changed = changed_files(repo, merge_base)

    for art in ARTIFACTS:
        versions: dict[str, str] = {}
        for src in art.sources:
            try:
                versions[src.path] = src.extract((repo / src.path).read_text(encoding="utf-8"))
            except (OSError, KeyError, ValueError) as exc:
                errors.append(f"{art.name}: cannot read the version from {src.path} ({exc})")
        if len(versions) != len(art.sources):
            continue
        head = versions[art.sources[0].path]
        if len(set(versions.values())) != 1:
            listing = "; ".join(f"{p} = {v}" for p, v in versions.items())
            errors.append(f"{art.name}: version sources disagree ({listing}); make them all equal")
            continue
        if semver(head) is None:
            errors.append(f"{art.name}: version {head!r} in {art.sources[0].path} is not X.Y.Z")
            continue

        if merge_base is None:
            print(f"OK  {art.name} {head} (sources agree; no base, bump rule skipped)")
            continue
        touched = [p for p in changed if p.startswith(art.directory + "/")]
        if not touched:
            print(f"OK  {art.name} {head} (unchanged)")
            continue
        primary = art.sources[0].path
        try:
            base_version = art.sources[0].extract(git(repo, "show", f"{merge_base}:{primary}"))
        except subprocess.CalledProcessError:
            print(f"OK  {art.name} {head} (new artifact: {primary} not in the base)")
            continue
        old, new = semver(base_version), semver(head)
        if old is None or new is None or new <= old:
            errors.append(
                f"{art.name}: {len(touched)} file(s) under {art.directory}/ changed "
                f"(e.g. {touched[0]}) but the version is {head}, base {base_version}. "
                f"Raise it in {', '.join(s.path for s in art.sources)}: installed clients only "
                "update when the version goes up."
            )
            continue
        print(f"OK  {art.name} {base_version} -> {head} ({len(touched)} file(s) changed)")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--base", default="", help="base ref/SHA; empty or all zeros skips the bump rule"
    )
    ap.add_argument("--repo", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    errors = check(args.repo.resolve(), args.base.strip() or None)
    for e in errors:
        print(f"::error::{e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
