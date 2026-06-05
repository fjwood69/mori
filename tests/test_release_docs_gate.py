"""Tests for the release-docs currency gate (scripts/check-release-docs.py).

The gate keeps CHANGELOG/ROADMAP from silently lapsing behind releases.
"""

from __future__ import annotations

import importlib.util
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_release_docs", _ROOT / "scripts" / "check-release-docs.py"
)
crd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crd)


def _setup(tmp_path, changelog: str, footer_ver: str):
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (tmp_path / "ROADMAP.md").write_text(
        f"# Roadmap\n\nstuff\n\n---\n\n*Last updated: v{footer_ver}*\n", encoding="utf-8"
    )
    return tmp_path


def test_passes_when_docs_current(tmp_path):
    _setup(tmp_path, "# Changelog\n\n## v2.2.8 — release\n", "2.2.8")
    assert crd.check("v2.2.8", tmp_path) == []
    assert crd.check("2.2.8", tmp_path) == []  # bare version too


def test_fails_when_changelog_entry_missing(tmp_path):
    _setup(tmp_path, "# Changelog\n\n## v2.2.7 — older\n", "2.2.8")
    errors = crd.check("v2.2.8", tmp_path)
    assert any("CHANGELOG" in e for e in errors)


def test_fails_when_roadmap_footer_stale(tmp_path):
    _setup(tmp_path, "# Changelog\n\n## v2.2.8 — release\n", "2.1.16")
    errors = crd.check("v2.2.8", tmp_path)
    assert any("ROADMAP" in e and "footer" in e for e in errors)


def test_fails_when_footer_absent(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("## v2.2.8\n", encoding="utf-8")
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n\nno footer here\n", encoding="utf-8")
    errors = crd.check("v2.2.8", tmp_path)
    assert any("footer" in e for e in errors)


def test_rejects_non_semver(tmp_path):
    _setup(tmp_path, "## v2.2.8\n", "2.2.8")
    assert crd.check("not-a-version", tmp_path)


def test_changelog_heading_must_be_exact_version(tmp_path):
    # '## v2.2.8' must not be satisfied by a longer version like v2.2.80
    _setup(tmp_path, "# Changelog\n\n## v2.2.80 — different\n", "2.2.8")
    errors = crd.check("v2.2.8", tmp_path)
    assert any("CHANGELOG" in e for e in errors)
