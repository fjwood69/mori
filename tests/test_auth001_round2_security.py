"""AUTH-001 Round-2 Security Tests.

Covers the three new attack surfaces identified in the security consult:

1. Skill loading path traversal (#1a)
   - sk="../../etc"  → rejected, no read
   - sk="../../../tmp/evil"  → rejected, no read
   - sk="valid-skill"  → passes _is_safe_skill_name (resolved path not checked here
     as SKILLS_DIR is not set, but the name-level guard fires first)

2. Standards directory containment (#1b)
   - import_standards(standards_dir=outside_allowed)  → rejected, no read
   - import_standards(standards_dir=real_standards_dir)  → allowed
   - Symlinked .md inside standards dir pointing outside  → skipped via
     _is_sensitive_path (for sensitive targets) or contained by rglob scan

3. _is_sensitive_path evasion (#3)
   - Case-insensitive: PASSWD, Passwd → rejected
   - Prefix coverage: .env.local, .env.production → rejected
   - Substring coverage: Secrets.txt, my-credentials.json → rejected
   - Legitimate files not blocked: README.md, config.yaml → allowed

4. O_NOFOLLOW confirmation (#3)
   - _read_files with a live symlink (pointing outside root) → denied
   - Confirmed by checking that the allowlist check fires before O_NOFOLLOW
     (symlink resolved outside root → allowlist denial, not an open error)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────


def _patch_roots(monkeypatch, roots: list[Path]) -> None:
    import mori_advisor.main as m

    monkeypatch.setattr(m, "CONSULT_FILE_ROOTS", roots)


def _patch_standards_roots(monkeypatch, roots: list[Path]) -> None:
    import mori_advisor.main as m

    monkeypatch.setattr(m, "STANDARDS_ROOTS", roots)


# ══════════════════════════════════════════════════════════════════════════
# #1a — Skill name sanitisation
# ══════════════════════════════════════════════════════════════════════════


def test_skill_name_dotdot_rejected():
    """sk='../../etc' must be rejected by _is_safe_skill_name."""
    from mori_advisor.main import _is_safe_skill_name

    assert not _is_safe_skill_name("../../etc")


def test_skill_name_deep_traversal_rejected():
    """sk='../../../tmp/evil' must be rejected."""
    from mori_advisor.main import _is_safe_skill_name

    assert not _is_safe_skill_name("../../../tmp/evil")


def test_skill_name_with_slash_rejected():
    """sk='foo/bar' must be rejected (contains path separator)."""
    from mori_advisor.main import _is_safe_skill_name

    assert not _is_safe_skill_name("foo/bar")


def test_skill_name_with_backslash_rejected():
    """sk='foo\\\\bar' must be rejected (backslash path separator)."""
    from mori_advisor.main import _is_safe_skill_name

    assert not _is_safe_skill_name("foo\\bar")


def test_skill_name_leading_dot_rejected():
    """sk='.hidden' must be rejected."""
    from mori_advisor.main import _is_safe_skill_name

    assert not _is_safe_skill_name(".hidden")


def test_skill_name_absolute_rejected():
    """sk='/etc/passwd' must be rejected (absolute path)."""
    from mori_advisor.main import _is_safe_skill_name

    assert not _is_safe_skill_name("/etc/passwd")


def test_skill_name_valid_passes():
    """Normal skill names must pass _is_safe_skill_name."""
    from mori_advisor.main import _is_safe_skill_name

    for name in ("brief", "dream", "consult", "my-skill", "skill_2", "pensieve"):
        assert _is_safe_skill_name(name), f"Expected {name!r} to be safe"


def test_safe_skill_path_traversal_returns_none(tmp_path):
    """_safe_skill_path with a traversal sk returns None, never a path."""
    import mori_advisor.main as m

    original_skills_dir = m.SKILLS_DIR
    try:
        m.SKILLS_DIR = str(tmp_path)
        assert m._safe_skill_path("../../etc") is None
        assert m._safe_skill_path("../../../tmp/evil") is None
    finally:
        m.SKILLS_DIR = original_skills_dir


def test_safe_skill_path_valid_returns_path(tmp_path):
    """_safe_skill_path with a valid sk returns the expected path."""
    import mori_advisor.main as m

    original_skills_dir = m.SKILLS_DIR
    try:
        m.SKILLS_DIR = str(tmp_path)
        skill_dir = tmp_path / "brief"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Brief\n")
        result = m._safe_skill_path("brief")
        assert result is not None
        assert result.exists()
        assert result.name == "SKILL.md"
    finally:
        m.SKILLS_DIR = original_skills_dir


# ══════════════════════════════════════════════════════════════════════════
# #1b — Standards directory containment
# ══════════════════════════════════════════════════════════════════════════


def test_standards_dir_outside_roots_rejected(monkeypatch, tmp_path):
    """import_standards(standards_dir=outside_allowed) must be rejected."""
    import asyncio

    import mori_advisor.main as m

    # Allowed root is tmp_path/allowed; standards_dir is tmp_path/evil (sibling).
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "baseline.md").write_text("# Evil standard\n")

    _patch_standards_roots(monkeypatch, [allowed])

    result = asyncio.run(m.import_standards(standards_dir=str(evil)))
    assert "Access denied" in result or "outside" in result.lower(), (
        f"Expected access-denied error but got: {result!r}"
    )


def test_standards_dir_inside_roots_allowed(monkeypatch, tmp_path):
    """import_standards(standards_dir=inside_allowed) must succeed."""
    import asyncio

    import mori_advisor.main as m

    standards = tmp_path / "standards"
    standards.mkdir()
    (standards / "baseline.md").write_text("# Baseline\nSome content.\n")

    _patch_standards_roots(monkeypatch, [standards])

    result = asyncio.run(m.import_standards(standards_dir=str(standards)))
    assert "Imported 1 standards" in result, f"Unexpected result: {result!r}"


def test_standards_real_standards_dir_allowed(monkeypatch, tmp_path):
    """When MORI_STANDARDS_DIR is within allowed roots, import should work."""
    import asyncio

    import mori_advisor.main as m

    standards = tmp_path / "std"
    standards.mkdir()
    (standards / "security-baseline.md").write_text("# Security\nAll writes need auth.\n")

    _patch_standards_roots(monkeypatch, [standards])

    result = asyncio.run(m.import_standards(standards_dir=str(standards)))
    assert "Imported" in result


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks not supported")
def test_standards_sensitive_symlink_skipped(monkeypatch, tmp_path):
    """A .md symlink inside the standards dir pointing at a sensitive target is skipped."""
    import asyncio

    import mori_advisor.main as m

    standards = tmp_path / "std"
    standards.mkdir()
    # Normal standard (should import fine).
    (standards / "ok.md").write_text("# OK standard\n")
    # Symlink to /etc/hostname as a .md file — _is_sensitive_path won't catch
    # /etc/hostname itself (not sensitive by name), but the path escapes the
    # standards dir so the real guard is containment.  We test a symlink to a
    # genuinely sensitive-named target instead.
    secret_outside = tmp_path / ".secrets"
    secret_outside.write_text("SECRET=hunter2\n")
    link = standards / "evil.md"
    link.symlink_to(secret_outside)

    _patch_standards_roots(monkeypatch, [standards])

    result = asyncio.run(m.import_standards(standards_dir=str(standards)))
    # The .secrets symlink target name is sensitive → skipped (counted as error).
    # The ok.md should still import.
    assert "Imported" in result
    # errors > 0 because the symlink was skipped
    assert "error" in result.lower() or "(1 errors)" in result


# ══════════════════════════════════════════════════════════════════════════
# #3 — _is_sensitive_path filter evasion
# ══════════════════════════════════════════════════════════════════════════


def test_sensitive_path_case_insensitive_PASSWD(tmp_path):
    """PASSWD (uppercase) must be rejected."""
    from mori_advisor.main import _is_sensitive_path

    assert _is_sensitive_path(tmp_path / "PASSWD")


def test_sensitive_path_case_insensitive_Passwd(tmp_path):
    """Passwd (mixed case) must be rejected."""
    from mori_advisor.main import _is_sensitive_path

    assert _is_sensitive_path(tmp_path / "Passwd")


def test_sensitive_path_env_local(tmp_path):
    """.env.local must be rejected (prefix match)."""
    from mori_advisor.main import _is_sensitive_path

    assert _is_sensitive_path(tmp_path / ".env.local")


def test_sensitive_path_env_production(tmp_path):
    """.env.production must be rejected (prefix match)."""
    from mori_advisor.main import _is_sensitive_path

    assert _is_sensitive_path(tmp_path / ".env.production")


def test_sensitive_path_Secrets_txt(tmp_path):
    """Secrets.txt must be rejected (substring match)."""
    from mori_advisor.main import _is_sensitive_path

    assert _is_sensitive_path(tmp_path / "Secrets.txt")


def test_sensitive_path_my_credentials_json(tmp_path):
    """my-credentials.json must be rejected (substring match on 'credential')."""
    from mori_advisor.main import _is_sensitive_path

    assert _is_sensitive_path(tmp_path / "my-credentials.json")


def test_sensitive_path_SECRET_env(tmp_path):
    """SECRET.env must be rejected (substring 'secret' in lowercased basename)."""
    from mori_advisor.main import _is_sensitive_path

    assert _is_sensitive_path(tmp_path / "SECRET.env")


def test_sensitive_path_readme_not_blocked(tmp_path):
    """README.md must NOT be flagged as sensitive."""
    from mori_advisor.main import _is_sensitive_path

    assert not _is_sensitive_path(tmp_path / "README.md")


def test_sensitive_path_config_yaml_not_blocked(tmp_path):
    """config.yaml must NOT be flagged as sensitive."""
    from mori_advisor.main import _is_sensitive_path

    assert not _is_sensitive_path(tmp_path / "config.yaml")


def test_sensitive_path_main_py_not_blocked(tmp_path):
    """main.py must NOT be flagged as sensitive."""
    from mori_advisor.main import _is_sensitive_path

    assert not _is_sensitive_path(tmp_path / "main.py")


# ══════════════════════════════════════════════════════════════════════════
# #3 — read_files evasion via sensitive-named files inside allowed root
# ══════════════════════════════════════════════════════════════════════════


def test_read_files_env_local_inside_root_denied(monkeypatch, tmp_path):
    """.env.local inside the allowed root must be rejected by _read_files."""
    _patch_roots(monkeypatch, [tmp_path])
    f = tmp_path / ".env.local"
    f.write_text("DB_PASS=secret\n")
    from mori_advisor.main import _read_files

    blocks, errors = _read_files([str(f)])
    assert not blocks
    assert any("Access denied" in e for e in errors), f"Expected denial but got: {errors}"


def test_read_files_env_production_inside_root_denied(monkeypatch, tmp_path):
    """.env.production inside the allowed root must be rejected by _read_files."""
    _patch_roots(monkeypatch, [tmp_path])
    f = tmp_path / ".env.production"
    f.write_text("API_KEY=secret\n")
    from mori_advisor.main import _read_files

    blocks, errors = _read_files([str(f)])
    assert not blocks
    assert any("Access denied" in e for e in errors)


def test_read_files_secrets_txt_inside_root_denied(monkeypatch, tmp_path):
    """Secrets.txt inside the allowed root must be rejected."""
    _patch_roots(monkeypatch, [tmp_path])
    f = tmp_path / "Secrets.txt"
    f.write_text("password=hunter2\n")
    from mori_advisor.main import _read_files

    blocks, errors = _read_files([str(f)])
    assert not blocks
    assert any("Access denied" in e for e in errors)


def test_read_files_PASSWD_inside_root_denied(monkeypatch, tmp_path):
    """PASSWD inside the allowed root must be rejected (case-insensitive match)."""
    _patch_roots(monkeypatch, [tmp_path])
    f = tmp_path / "PASSWD"
    f.write_text("root:x:0:0:root:/root:/bin/bash\n")
    from mori_advisor.main import _read_files

    blocks, errors = _read_files([str(f)])
    assert not blocks
    assert any("Access denied" in e for e in errors)


# ══════════════════════════════════════════════════════════════════════════
# #3 — O_NOFOLLOW (symlink that resolves outside root is caught by allowlist)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks not supported")
def test_symlink_outside_root_denied_by_allowlist(monkeypatch, tmp_path):
    """Symlink inside root pointing outside → allowlist catches it (path resolves outside).

    O_NOFOLLOW closes a race window AFTER the allowlist check; the allowlist
    itself handles the common case.  We verify the allowlist fires first.
    """
    _patch_roots(monkeypatch, [tmp_path])
    target = Path("/etc/hostname")
    if not target.exists():
        pytest.skip("/etc/hostname not present on this system")
    link = tmp_path / "escape.txt"
    link.symlink_to(target)
    from mori_advisor.main import _read_files

    blocks, errors = _read_files([str(link)])
    assert not blocks
    # Allowlist denial fires because resolve() returns /etc/hostname which is
    # outside tmp_path.
    assert any("Access denied" in e or "outside allowed" in e for e in errors), errors
