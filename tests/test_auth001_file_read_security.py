"""AUTH-001 — _read_files path security tests.

Every attack path listed in the assessment MUST be rejected; a legitimate
.py/.md file inside an allowed root MUST be readable.  No real LLM is called.

Design:
- All tests operate on ``_read_files`` and the helper predicates directly.
- ``tmp_path`` (pytest fixture) provides an allowed root; tests pass it via
  ``monkeypatch`` on ``mori_advisor.main.CONSULT_FILE_ROOTS``.
- A symlink-escape test creates a symlink inside the allowed root that points
  outside — resolved path must be caught by the allowlist check.

Async tests use asyncio.run() to match repo style (no asyncio_mode set).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Ensure mori_advisor.main is importable without a live store.
# (conftest.py sets MORI_ADVISOR_DATA to a temp dir before imports.)


def _read_files_fn():
    """Lazy import to avoid triggering main's module-level side effects."""
    from mori_advisor.main import _read_files  # noqa: PLC0415

    return _read_files


# ── Helpers ───────────────────────────────────────────────────────────────


def _assert_denied(errors: list[str], path_str: str) -> None:
    """Assert that *path_str* appears in errors as a denial, not a read."""
    denied = [e for e in errors if path_str in e and "Access denied" in e]
    assert denied, f"Expected 'Access denied' error for {path_str!r} but got errors: {errors}"


def _assert_not_read(blocks: list[str], path_str: str) -> None:
    """Assert the file content did NOT appear in blocks."""
    assert not any(path_str in b for b in blocks), (
        f"File {path_str!r} content should not appear in blocks but it did"
    )


def _patch_roots(monkeypatch, roots: list[Path]) -> None:
    import mori_advisor.main as m

    monkeypatch.setattr(m, "CONSULT_FILE_ROOTS", roots)


# ── Tests: absolute paths outside roots ──────────────────────────────────


def test_etc_passwd_denied(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, [tmp_path])
    read_files = _read_files_fn()
    blocks, errors, _ = read_files(["/etc/passwd"])
    assert not blocks
    _assert_denied(errors, "/etc/passwd")


def test_proc_environ_denied(monkeypatch, tmp_path):
    _patch_roots(monkeypatch, [tmp_path])
    read_files = _read_files_fn()
    blocks, errors, _ = read_files(["/proc/self/environ"])
    assert not blocks
    _assert_denied(errors, "/proc/self/environ")


def test_absolute_outside_root_denied(monkeypatch, tmp_path):
    # /tmp itself is not inside tmp_path
    _patch_roots(monkeypatch, [tmp_path])
    read_files = _read_files_fn()
    blocks, errors, _ = read_files(["/tmp"])
    assert not blocks
    # Either "not a file" or "outside allowed roots" — either is a denial
    assert errors


# ── Tests: .. traversal ───────────────────────────────────────────────────


def test_dotdot_traversal_denied(monkeypatch, tmp_path):
    """../../../etc/passwd must be caught after resolve()."""
    _patch_roots(monkeypatch, [tmp_path])
    read_files = _read_files_fn()
    traversal = str(tmp_path / "../../../etc/passwd")
    blocks, errors, _ = read_files([traversal])
    assert not blocks
    _assert_not_read(blocks, traversal)
    # Must have an error of some kind (denied or not-found)
    assert errors


# ── Tests: sensitive basenames inside allowed root ────────────────────────


def test_dotenv_inside_root_denied(monkeypatch, tmp_path):
    """.env placed INSIDE the allowed root must still be rejected."""
    _patch_roots(monkeypatch, [tmp_path])
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=hunter2\n")
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(env_file)])
    assert not blocks
    _assert_denied(errors, str(env_file))


def test_dotsecrets_inside_root_denied(monkeypatch, tmp_path):
    """.secrets placed INSIDE the allowed root must still be rejected."""
    _patch_roots(monkeypatch, [tmp_path])
    sec_file = tmp_path / ".secrets"
    sec_file.write_text("API_KEY=abcdef\n")
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(sec_file)])
    assert not blocks
    _assert_denied(errors, str(sec_file))


def test_pem_inside_root_denied(monkeypatch, tmp_path):
    """*.pem inside the allowed root must be rejected by suffix check."""
    _patch_roots(monkeypatch, [tmp_path])
    pem_file = tmp_path / "server.pem"
    pem_file.write_text("-----BEGIN CERTIFICATE-----\n")
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(pem_file)])
    assert not blocks
    _assert_denied(errors, str(pem_file))


def test_key_inside_root_denied(monkeypatch, tmp_path):
    """*.key inside the allowed root must be rejected."""
    _patch_roots(monkeypatch, [tmp_path])
    key_file = tmp_path / "private.key"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\n")
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(key_file)])
    assert not blocks
    _assert_denied(errors, str(key_file))


def test_sqlite_inside_root_denied(monkeypatch, tmp_path):
    """*.sqlite inside the allowed root must be rejected."""
    _patch_roots(monkeypatch, [tmp_path])
    db_file = tmp_path / "memories.sqlite"
    db_file.write_bytes(b"SQLite format 3\x00")
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(db_file)])
    assert not blocks
    _assert_denied(errors, str(db_file))


# ── Tests: symlink escape ──────────────────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks not supported on this platform")
def test_symlink_escape_denied(monkeypatch, tmp_path):
    """Symlink inside allowed root pointing outside it → denied.

    resolve() follows symlinks, so the resolved path will be outside
    tmp_path and the allowlist check will reject it.
    """
    _patch_roots(monkeypatch, [tmp_path])
    # Create a symlink inside the root pointing at /etc/hostname (exists on Linux)
    target = Path("/etc/hostname")
    if not target.exists():
        pytest.skip("/etc/hostname does not exist on this system")
    link = tmp_path / "escape_link"
    link.symlink_to(target)
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(link)])
    assert not blocks
    # Must be denied (either allowlist or sensitive path)
    assert any("Access denied" in e or "outside allowed" in e for e in errors), errors


# ── Tests: legitimate files are allowed ───────────────────────────────────


def test_py_file_inside_root_allowed(monkeypatch, tmp_path):
    """A normal .py file inside the allowed root must be readable."""
    _patch_roots(monkeypatch, [tmp_path])
    py_file = tmp_path / "hello.py"
    py_file.write_text("print('hello')\n")
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(py_file)])
    assert blocks, f"Expected blocks but got errors: {errors}"
    assert "hello.py" in blocks[0]
    assert "print" in blocks[0]


def test_md_file_inside_root_allowed(monkeypatch, tmp_path):
    """A .md file inside the allowed root must be readable."""
    _patch_roots(monkeypatch, [tmp_path])
    md_file = tmp_path / "README.md"
    md_file.write_text("# Title\nSome content.\n")
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(md_file)])
    assert blocks, f"Expected blocks but got errors: {errors}"
    assert "README.md" in blocks[0]


def test_file_in_subdirectory_of_root_allowed(monkeypatch, tmp_path):
    """A file in a subdirectory of the allowed root must be accessible."""
    _patch_roots(monkeypatch, [tmp_path])
    subdir = tmp_path / "src" / "utils"
    subdir.mkdir(parents=True)
    src_file = subdir / "helpers.py"
    src_file.write_text("def noop(): pass\n")
    read_files = _read_files_fn()
    blocks, errors, _ = read_files([str(src_file)])
    assert blocks, f"Expected blocks but got errors: {errors}"


# ── Tests: consult_advisor returns errors, not content ────────────────────


def test_consult_advisor_denied_paths_return_errors(monkeypatch, tmp_path):
    """consult_advisor propagates _read_files errors without reading content.

    We verify that _read_files produces NO content blocks for known-dangerous
    paths — the integration with consult_advisor is covered by the fact that
    _read_files is called unmodified by consult_advisor (verified by reading
    the source; no additional wrapper).
    """
    import mori_advisor.main as m

    _patch_roots(monkeypatch, [tmp_path])

    # Verify _read_files directly: no blocks for denied paths.
    blocks, errors, _ = m._read_files(["/etc/passwd", "/proc/self/environ"])
    assert not blocks, "No content blocks should be produced for denied paths"
    assert len(errors) == 2
    assert all("Access denied" in e for e in errors)

    # Also verify an absolute path outside the root is denied.
    blocks2, errors2, _ = m._read_files(["/etc/shadow"])
    assert not blocks2
    assert errors2 and "Access denied" in errors2[0]
