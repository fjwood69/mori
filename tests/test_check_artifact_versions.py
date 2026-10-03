"""#88: scripts/check-artifact-versions.py -- a changed artifact must raise its version.

Each test builds a throwaway git repo holding the version sources of both artifacts, commits a
base, edits the working tree and runs the guard against that base.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_artifact_versions",
    Path(__file__).resolve().parent.parent / "scripts" / "check-artifact-versions.py",
)
assert _SPEC and _SPEC.loader
cav = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cav  # dataclasses resolve their module through sys.modules
_SPEC.loader.exec_module(cav)

HERMES = "integrations/hermes-memory-provider"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _set_plugin(repo: Path, plugin: str, market: str | None = None) -> None:
    _write(
        repo,
        "plugins/mori/.claude-plugin/plugin.json",
        json.dumps({"name": "mori", "version": plugin}),
    )
    _write(
        repo,
        ".claude-plugin/marketplace.json",
        json.dumps(
            {
                "name": "mori",
                "plugins": [
                    {"name": "other", "version": "9.9.9"},
                    {"name": "mori", "version": market or plugin},
                ],
            }
        ),
    )


def _set_hermes(repo: Path, v: str, outbox: str | None = None) -> None:
    _write(repo, f"{HERMES}/pyproject.toml", f'[project]\nname = "x"\nversion = "{v}"\n')
    _write(repo, f"{HERMES}/plugin.yaml", f'name: mori\nversion: "{v}"\n')
    _write(repo, f"{HERMES}/hermes_mori_provider/__init__.py", f'__version__ = "{v}"\n')
    _write(
        repo,
        f"{HERMES}/hermes_mori_provider/outbox.py",
        f'payload = {{\n    "plugin_version": "{outbox or v}",\n}}\n',
    )


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q", "-b", "main")
    _set_plugin(tmp_path, "0.3.3")
    _set_hermes(tmp_path, "0.3.0")
    _write(tmp_path, "plugins/mori/scripts/hook.mjs", "// v1\n")
    _write(tmp_path, "README.md", "root\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD").strip()


def _run(repo: Path, base: str) -> list[str]:
    return cav.check(repo, base)


def test_unchanged_passes(repo: tuple[Path, str]) -> None:
    path, base = repo
    assert _run(path, base) == []


def test_change_outside_artifacts_needs_no_bump(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, "README.md", "edited\n")
    assert _run(path, base) == []


def test_plugin_change_without_bump_fails(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, "plugins/mori/scripts/hook.mjs", "// v2\n")
    errs = _run(path, base)
    assert len(errs) == 1 and "claude-plugin" in errs[0]
    assert "plugins/mori/.claude-plugin/plugin.json" in errs[0]


def test_new_untracked_file_counts_as_a_change(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, "plugins/mori/scripts/lib/new.mjs", "// new\n")
    assert any("claude-plugin" in e for e in _run(path, base))


def test_committed_change_without_bump_fails(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, "plugins/mori/scripts/hook.mjs", "// v2\n")
    _git(path, "commit", "-qam", "change")
    assert any("claude-plugin" in e for e in _run(path, base))


def test_plugin_change_with_bump_passes(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, "plugins/mori/scripts/hook.mjs", "// v2\n")
    _set_plugin(path, "0.4.0")
    assert _run(path, base) == []


def test_version_going_down_fails(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, "plugins/mori/scripts/hook.mjs", "// v2\n")
    _set_plugin(path, "0.3.2")
    assert any("claude-plugin" in e for e in _run(path, base))


def test_semver_compares_numerically(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, "plugins/mori/scripts/hook.mjs", "// v2\n")
    _set_plugin(path, "0.3.10")  # a string compare would call this lower than 0.3.3
    assert _run(path, base) == []


def test_plugin_and_marketplace_disagree_fails(repo: tuple[Path, str]) -> None:
    path, base = repo
    _set_plugin(path, "0.4.0", market="0.3.3")
    errs = _run(path, base)
    assert len(errs) == 1 and "disagree" in errs[0] and ".claude-plugin/marketplace.json" in errs[0]


def test_disagreement_fails_even_without_a_base(repo: tuple[Path, str]) -> None:
    path, _ = repo
    _set_plugin(path, "0.4.0", market="0.3.3")
    assert any("disagree" in e for e in cav.check(path, None))


def test_hermes_sources_disagree_fails(repo: tuple[Path, str]) -> None:
    path, base = repo
    _set_hermes(path, "0.4.0", outbox="0.3.0")
    errs = _run(path, base)
    assert len(errs) == 1 and "hermes-provider" in errs[0] and "outbox.py" in errs[0]


def test_hermes_change_without_bump_fails(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, f"{HERMES}/hermes_mori_provider/provider.py", "x = 1\n")
    assert any("hermes-provider" in e for e in _run(path, base))


def test_hermes_change_with_full_bump_passes(repo: tuple[Path, str]) -> None:
    path, base = repo
    _write(path, f"{HERMES}/hermes_mori_provider/provider.py", "x = 1\n")
    _set_hermes(path, "0.4.0")
    assert _run(path, base) == []


def test_all_zero_base_skips_the_bump_rule(repo: tuple[Path, str]) -> None:
    path, _ = repo
    _write(path, "plugins/mori/scripts/hook.mjs", "// v2\n")
    assert _run(path, "0" * 40) == []


def test_unknown_base_fails_closed(repo: tuple[Path, str]) -> None:
    path, _ = repo
    errs = _run(path, "deadbeef" * 5)
    assert len(errs) == 1 and "fetch-depth" in errs[0]


def test_unreadable_source_fails(repo: tuple[Path, str]) -> None:
    path, base = repo
    (path / f"{HERMES}/plugin.yaml").unlink()
    assert any("plugin.yaml" in e for e in _run(path, base))


def test_main_exit_codes(repo: tuple[Path, str]) -> None:
    path, base = repo
    assert cav.main(["--base", base, "--repo", str(path)]) == 0
    _write(path, "plugins/mori/scripts/hook.mjs", "// v2\n")
    assert cav.main(["--base", base, "--repo", str(path)]) == 1


def test_real_repo_sources_are_readable() -> None:
    """The extractors match the real files (catches a renamed field or a moved file)."""
    assert [e for e in cav.check(cav.ROOT, None) if "cannot read" in e] == []
