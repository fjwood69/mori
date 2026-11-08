"""v2.3.17 — API keys MAY be stored as SHA-256 hashes; key_generate is retired.

Hashing is optional: hashed entries work, plain entries are accepted with a warning, startup is never refused over
the key format, and nothing in the deploy converts the table. The unchanged deploy borrows the FIRST entry's plain
secret, so the conversion script leaves that entry plain by default. The conversion script and mori-key-add.sh are
opt-in operator tools.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SECRET_A = "a" * 64
SECRET_B = "b" * 64


def _h(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()


@pytest.fixture
def fresh_auth(monkeypatch):
    import mori_advisor.auth as auth
    import mori_advisor.policy as pol

    monkeypatch.setattr(auth, "_KEYS", {})
    monkeypatch.delenv("MORI_ADVISOR_API_KEY", raising=False)
    monkeypatch.delenv("MORI_LOCAL_FULL_ACCESS", raising=False)
    return auth, pol


def _init(monkeypatch, keys: str, roles: str, mode: str | None):
    import mori_advisor.auth as auth
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", mode or "host")  # read once at import in production
    monkeypatch.setenv("MORI_API_KEYS", keys)
    monkeypatch.setenv("MORI_API_KEY_ROLES", roles)
    if mode is None:
        monkeypatch.delenv("MORI_TD_MODE", raising=False)
    else:
        monkeypatch.setenv("MORI_TD_MODE", mode)
    auth.init_auth()
    return auth


# ── H2: the format and the comparison ────────────────────────────────────────────────


def test_a_hashed_entry_authenticates_and_wrong_or_empty_is_refused(monkeypatch, fresh_auth):
    auth = _init(monkeypatch, f"alpha:{_h(SECRET_A)},beta:{_h(SECRET_B)}", "alpha:write", "api")
    assert auth.check_key(SECRET_A) == "alpha"
    assert auth.check_key(SECRET_B) == "beta"
    assert auth.check_key("c" * 64) is None
    assert auth.check_key("") is None
    assert auth.check_key(None) is None
    # the stored hash itself is not a secret anyone can present
    assert auth.check_key(_h(SECRET_A)) is None


def test_api_mode_accepts_a_plain_entry_with_a_warning(monkeypatch, fresh_auth, caplog):
    """Hashing is optional (Fred, 8 Nov): a plain entry starts, with a warning that names the entry, never its
    value. Startup is never refused over the key format."""
    import logging

    with caplog.at_level(logging.WARNING):
        auth = _init(
            monkeypatch, f"alpha:{_h(SECRET_A)},beta:{SECRET_B}", "alpha:write,beta:write", "api"
        )
    assert auth.check_key(SECRET_B) == "beta" and auth.check_key(SECRET_A) == "alpha"
    warned = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "beta" in warned and "plain" in warned and SECRET_B not in warned


def test_api_mode_accepts_the_legacy_single_key(monkeypatch, fresh_auth, caplog):
    monkeypatch.delenv("MORI_API_KEYS", raising=False)
    monkeypatch.setenv("MORI_ADVISOR_API_KEY", SECRET_A)
    monkeypatch.setenv("MORI_TD_MODE", "api")
    import mori_advisor.auth as auth
    import mori_advisor.policy as pol

    monkeypatch.setattr(pol, "_TD_MODE", "api")
    import logging

    with caplog.at_level(logging.WARNING):
        auth.init_auth()  # no SystemExit
    assert auth.check_key(SECRET_A) == "legacy"
    warned = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "legacy" in warned and SECRET_A not in warned


def _cd_borrowed_key(tmp_path, table: str) -> str:
    """Run cd.yml's own VERIFY_KEY= pipeline (grep | cut) on an env file holding *table*."""
    import re

    cd = (ROOT / ".github" / "workflows" / "cd.yml").read_text()
    line = re.search(r"VERIFY_KEY=\$\((.*)\)\n", cd).group(1)  # the exact pipeline CD runs
    env_file = tmp_path / ".env"
    env_file.write_text(f"MORI_TD_MODE=api\nMORI_API_KEYS={table}\n")
    pipeline = line.replace("sudo ", "").replace('"$ENVFILE"', str(env_file))
    return subprocess.run(
        ["bash", "-c", pipeline], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_deploy_contract_finds_a_plain_first_key(monkeypatch, fresh_auth, tmp_path):
    """The deploy path is unchanged: cd.yml borrows the FIRST entry of MORI_API_KEYS. A plain first
    entry authenticates; hashed LATER entries don't disturb it."""
    table = f"nuc:{SECRET_A},cd-x:{_h(SECRET_B)}"
    key = _cd_borrowed_key(tmp_path, table)
    auth = _init(monkeypatch, table, "nuc:write,cd-x:write", "api")
    assert key == SECRET_A and auth.check_key(key) == "nuc"


def test_a_hashed_first_key_breaks_the_deploy_contract(monkeypatch, fresh_auth, tmp_path):
    """The other direction (consult F17, P1): a hashed first entry makes the cut yield the literal
    'sha256', which authenticates as nothing — every contract probe would 401."""
    table = f"nuc:{_h(SECRET_A)},other:{SECRET_B}"
    key = _cd_borrowed_key(tmp_path, table)
    auth = _init(monkeypatch, table, "nuc:write,other:write", "api")
    assert key == "sha256" and auth.check_key(key) is None


def test_host_mode_still_accepts_plain_entries(monkeypatch, fresh_auth):
    """Intake's case: it runs host mode with one plain key (its own release item)."""
    auth = _init(monkeypatch, f"intake-x:{SECRET_A}", "intake-x:write", None)
    assert auth.check_key(SECRET_A) == "intake-x"


def test_every_entry_is_compared_no_early_return(monkeypatch, fresh_auth):
    auth = _init(
        monkeypatch,
        f"k1:{_h(SECRET_A)},k2:{_h(SECRET_B)},k3:{_h('c' * 64)}",
        "k1:write",
        "api",
    )
    calls = []
    real = auth.hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", counting)
    assert auth.check_key(SECRET_A) == "k1"  # the FIRST entry matches…
    assert len(calls) == 3  # …and every entry is still compared


def test_the_ingestion_server_starts_with_a_plain_entry_in_api_mode(monkeypatch, fresh_auth):
    import mori_advisor.ingestion_server as ing

    monkeypatch.setenv("MORI_API_KEYS", f"upload-key:{SECRET_A}")
    monkeypatch.setenv("MORI_API_KEY_ROLES", "upload-key:write")
    monkeypatch.setenv("MORI_TD_MODE", "api")
    monkeypatch.setattr(fresh_auth[1], "_TD_MODE", "api")
    ing.startup_check()  # no SystemExit
    assert fresh_auth[0].check_key(SECRET_A) == "upload-key"


# ── the conversion script ─────────────────────────────────────────────────────────────

CONVERT = ROOT / "scripts" / "mori-hash-keys.py"


def _env(tmp_path: Path, text: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(text)
    p.chmod(0o640)
    return p


def _convert(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CONVERT), str(path)], capture_output=True, text=True, check=False
    )


def test_conversion_hashes_plain_entries_and_keeps_everything_else(tmp_path):
    original = (
        "# header comment\n"
        "MORI_TD_MODE=api\n"
        f"MORI_API_KEYS=first:{SECRET_B},alpha:{SECRET_A},beta:{_h(SECRET_B)}\n"
        "MORI_API_KEY_ROLES=first:write,alpha:write,beta:dreamer\n"
        "OTHER=value with = signs\n"
    )
    p = _env(tmp_path, original)
    r = _convert(p)
    assert r.returncode == 0, r.stderr
    out = p.read_text()
    lines, before = out.split("\n"), original.split("\n")
    # names kept; the FIRST entry (the deploy contract's identity) stays plain by default
    assert lines[2] == f"MORI_API_KEYS=first:{SECRET_B},alpha:{_h(SECRET_A)},beta:{_h(SECRET_B)}"
    assert "kept plain" in r.stdout and "first" in r.stdout
    assert [ln for i, ln in enumerate(lines) if i != 2] == [
        ln for i, ln in enumerate(before) if i != 2
    ]
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert sorted(x.name for x in tmp_path.iterdir()) == [".env"]  # no backup, no temp left
    assert SECRET_A not in out
    assert SECRET_A not in r.stdout + r.stderr  # it never prints a secret


def test_conversion_is_idempotent_and_never_double_hashes(tmp_path):
    p = _env(tmp_path, f"MORI_API_KEYS=first:{SECRET_B},alpha:{SECRET_A}\n")
    assert _convert(p).returncode == 0
    once = p.read_text()
    assert _convert(p).returncode == 0
    assert p.read_text() == once
    assert once.strip() == f"MORI_API_KEYS=first:{SECRET_B},alpha:{_h(SECRET_A)}"


def test_include_first_converts_the_first_entry_and_warns(tmp_path):
    p = _env(tmp_path, f"MORI_API_KEYS=first:{SECRET_B},alpha:{SECRET_A}\n")
    r = subprocess.run(
        [sys.executable, str(CONVERT), "--include-first", str(p)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert p.read_text().strip() == f"MORI_API_KEYS=first:{_h(SECRET_B)},alpha:{_h(SECRET_A)}"
    assert "will now fail" in r.stderr and SECRET_B not in r.stderr


def test_conversion_keeps_the_files_owner_and_group(tmp_path, monkeypatch):
    """Consult F17, P3: run under sudo, a root-owned file would stop rootless units reading it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mori_hash_keys", CONVERT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    p = _env(tmp_path, f"MORI_API_KEYS=first:{SECRET_B},alpha:{SECRET_A}\n")
    st = os.stat(p)
    calls = []
    real = os.fchown
    monkeypatch.setattr(
        mod.os, "fchown", lambda fd, uid, gid: (calls.append((uid, gid)), real(fd, uid, gid))
    )
    assert mod.main(["mori-hash-keys.py", str(p)]) == 0
    assert calls == [(st.st_uid, st.st_gid)]
    new = os.stat(p)
    assert (new.st_uid, new.st_gid) == (st.st_uid, st.st_gid)
    assert stat.S_IMODE(new.st_mode) == 0o600


@pytest.mark.parametrize(
    "stored", ["sha256:" + "A" * 64, "SHA256:" + "a" * 64, "sha256:" + "a" * 63]
)
def test_a_lookalike_hash_entry_is_warned_about(monkeypatch, fresh_auth, caplog, stored):
    """Consult F17, P3: an entry that starts with sha256: but isn't 64 lowercase hex is a dead key."""
    import logging

    with caplog.at_level(logging.WARNING):
        auth = _init(
            monkeypatch, f"good:{_h(SECRET_A)},typo:{stored}", "good:write,typo:write", "api"
        )
    warned = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "typo" in warned and "64 lowercase hex" in warned
    assert auth.check_key(SECRET_A) == "good"


def test_conversion_leaves_the_llm_provider_key_alone(tmp_path):
    """MORI_API_KEY is the LLM provider's key in direct mode (bifrost_client.py), not a mori
    client key: the conversion must not refuse on it, nor touch it."""
    p = _env(tmp_path, f"MORI_API_KEYS=alpha:{SECRET_A}\nMORI_API_KEY=provider-key-value\n")
    r = _convert(p)
    assert r.returncode == 0, r.stderr
    assert "MORI_API_KEY=provider-key-value" in p.read_text().split("\n")


@pytest.mark.parametrize("legacy", ["MORI_ADVISOR_API_KEY"])
@pytest.mark.parametrize("value", [SECRET_B, "legacy-key-not-hex-shaped"])
def test_conversion_fails_loudly_on_a_legacy_key_variable(tmp_path, legacy, value):
    """Any value, hex-shaped or not: the legacy check (not only the stray-table check) must fire."""
    p = _env(tmp_path, f"MORI_API_KEYS=alpha:{SECRET_A}\n{legacy}={value}\n")
    before = p.read_bytes()
    r = _convert(p)
    assert r.returncode == 2
    assert "legacy mori key variable set" in r.stderr and value not in r.stderr
    assert p.read_bytes() == before  # nothing changed


# ── G8: key_generate is retired; the local script replaces it ─────────────────────────


def test_key_generate_is_no_longer_an_mcp_tool():
    import asyncio

    from mori_advisor.main import mcp

    async def names():
        tools = await mcp.list_tools() if hasattr(mcp, "list_tools") else await mcp.get_tools()
        return {t.name for t in tools} if not isinstance(tools, dict) else set(tools)

    tools = asyncio.run(names())
    assert "memory_write" in tools  # the listing works
    assert "key_generate" not in tools


KEY_ADD = ROOT / "scripts" / "mori-key-add.sh"


def test_the_key_add_script_refuses_under_claude_code():
    r = subprocess.run(
        ["bash", str(KEY_ADD), "cd-verify", "write"],
        env={**os.environ, "CLAUDECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode != 0 and "Claude Code" in r.stderr


def test_the_key_add_script_sends_only_the_hash(tmp_path):
    """--dry-run prints the remote payload (as it would go over ssh) and no plain secret."""
    import re

    r = subprocess.run(
        ["bash", str(KEY_ADD), "--dry-run", "cd-verify", "write"],
        env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    payload = r.stdout
    assert re.search(r"cd-verify:sha256:[0-9a-f]{64}", payload)
    assert "cd-verify:write" in payload
    stripped = re.sub(r"sha256:[0-9a-f]{64}", "", payload)
    assert not re.search(r"[0-9a-f]{64}", stripped), "a plain 64-hex secret is in the payload"


@pytest.mark.parametrize(
    "stray",
    [
        "# MORI_API_KEYS=old:{plain}",
        "MORI_API_KEYS_OLD=old:{plain}",
        "#MORI_ADVISOR_API_KEY={plain}",
        "export MORI_API_KEYS_BACKUP=a:{plain},b:{plain}",
    ],
)
def test_conversion_refuses_a_commented_or_renamed_plain_table(tmp_path, stray):
    """Consult C17, P2-1: a stray copy would stay a usable key in the file after conversion."""
    p = _env(tmp_path, f"MORI_API_KEYS=alpha:{SECRET_A}\n{stray.format(plain=SECRET_B)}\n")
    before = p.read_bytes()
    r = _convert(p)
    assert r.returncode == 2 and "another line holds a plain key table" in r.stderr, r.stderr
    assert SECRET_B not in r.stderr and p.read_bytes() == before


def test_conversion_allows_a_hashed_stray_and_the_provider_key(tmp_path):
    p = _env(
        tmp_path,
        f"MORI_API_KEYS=alpha:{SECRET_A}\n# MORI_API_KEYS=old:{_h(SECRET_B)}\nMORI_API_KEY={SECRET_B}\n",
    )
    assert _convert(p).returncode == 0


def test_generate_key_is_gone():
    import mori_advisor.auth as auth

    assert not hasattr(auth, "generate_key")


def test_the_key_add_script_refuses_a_path_with_shell_characters():
    r = subprocess.run(
        ["bash", str(KEY_ADD), "--dry-run", "--env-file", "/data/x';id;'", "cd-verify", "write"],
        env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2 and "plain path" in r.stderr
