"""v2.3.17 — the CD deploy user can deploy and nothing else (ruling G3/G6, P1-2; design §4.3).

``deploy/gcp/cd/mori-cd-deploy`` is the sshd ForceCommand: it accepts exactly
``deploy <vX.Y.Z> <sha256:64hex>`` from SSH_ORIGINAL_COMMAND and refuses anything else.
``mori-cd-deploy-as-mori`` converts the key table only for tags >= v2.3.17, runs the store check
only for those, and refuses to deploy an older release over a hashed table (v2.3.16 can't read
hashes). sshd and sudoers files hold the ruled options. The canary script refuses under Claude Code.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

CD = Path(__file__).resolve().parent.parent / "deploy" / "gcp" / "cd"
DIGEST = "sha256:" + "ab" * 32


def _validate(cmd: str | None):
    env = {k: v for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"}
    if cmd is not None:
        env["SSH_ORIGINAL_COMMAND"] = cmd
    return subprocess.run(
        ["bash", str(CD / "mori-cd-deploy"), "--validate"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_forced_command_accepts_exactly_deploy_tag_digest():
    r = _validate(f"deploy v2.3.17 {DIGEST}")
    assert r.returncode == 0 and r.stdout.strip() == f"ok v2.3.17 {DIGEST}", r.stderr


@pytest.mark.parametrize(
    "cmd",
    [
        None,  # an interactive login: no command at all
        "",
        "bash",
        "sh -c id",
        "deploy",
        "deploy v2.3.17",
        "deploy v2.3.17 latest",
        f"deploy v2.3.17 {DIGEST[:-1]}",  # 63 hex
        f"deploy v2.3.17 {DIGEST.upper()}",
        f"deploy v2.3.17;id {DIGEST}",
        f"deploy v2.3.17 {DIGEST};id",
        f"deploy v2.3.17 {DIGEST} extra",
        f"deploy  v2.3.17 {DIGEST}",  # two spaces: not exactly the three words
        f"deploy v2.3.17 {DIGEST}\nid",
        f"deploy v02.3.17 {DIGEST}",
        f"deploy 2.3.17 {DIGEST}",
        f"verify v2.3.17 {DIGEST}",
        f"deploy v2.3.17 $(id) {DIGEST}",
    ],
)
def test_the_forced_command_refuses_everything_else(cmd):
    r = _validate(cmd)
    assert r.returncode == 2 and "refused" in r.stderr, (cmd, r.stdout, r.stderr)
    assert "ok " not in r.stdout


def _plan(tmp_path: Path, tag: str, table: str):
    env_file = tmp_path / ".env"
    env_file.write_text(f"MORI_TD_MODE=api\nMORI_API_KEYS={table}\n")
    return subprocess.run(
        ["bash", str(CD / "mori-cd-deploy-as-mori"), tag, DIGEST, "--plan"],
        env={**os.environ, "MORI_CD_ENVFILE_FOR_TESTS": str(env_file)},
        capture_output=True,
        text=True,
        check=False,
    )


PLAIN = "a:" + "1" * 64
HASHED = "a:sha256:" + "2" * 64


@pytest.mark.parametrize("tag", ["v2.3.17", "v2.3.18", "v2.4.0", "v3.0.0", "v2.3.100"])
@pytest.mark.parametrize("table", [PLAIN, HASHED])
def test_conversion_and_the_store_check_run_from_v2317(tmp_path, tag, table):
    r = _plan(tmp_path, tag, table)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith(f"plan tag={tag} convert=yes store_check=yes ")


@pytest.mark.parametrize("tag", ["v2.3.16", "v2.3.9", "v2.2.99", "v1.9.0"])
def test_an_older_release_neither_converts_nor_runs_the_store_check(tmp_path, tag):
    r = _plan(tmp_path, tag, PLAIN)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith(f"plan tag={tag} convert=no store_check=no ")


def test_an_older_release_is_refused_over_a_hashed_table(tmp_path):
    """P1-2's trap made impossible: v2.3.16 would read every hash as a literal plain secret."""
    r = _plan(tmp_path, "v2.3.16", HASHED)
    assert r.returncode == 3 and "cannot read hashes" in r.stderr


@pytest.mark.parametrize("tag", ["2.3.17", "v2.3", "latest", "v2.3.17;id"])
def test_the_inner_script_revalidates_its_arguments(tmp_path, tag):
    assert _plan(tmp_path, tag, PLAIN).returncode == 2


def test_the_inner_script_ignores_the_test_override_outside_plan_mode():
    src = (CD / "mori-cd-deploy-as-mori").read_text()
    assert '[[ "$MODE" == "--plan" ]] || ENVFILE=/data/mori-advisor/.env' in src


def test_sshd_and_sudoers_hold_the_ruled_options():
    conf = (CD / "mori-deploy.conf").read_text()
    for opt in (
        "Match User mori-deploy",
        "AuthorizedKeysFile /etc/ssh/authorized_keys.d/mori-deploy",
        "PermitTTY no",
        "PermitUserRC no",
        "AllowTcpForwarding no",
        "X11Forwarding no",
        "PasswordAuthentication no",
        "AuthenticationMethods publickey",
        "ForceCommand /usr/local/sbin/mori-cd-deploy",
    ):
        assert re.search(rf"^\s*{re.escape(opt)}\s*$", conf, re.M), opt
    rules = [
        ln
        for ln in (CD / "mori-deploy.sudoers").read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert rules == ["mori-deploy ALL=(mori) NOPASSWD: /usr/local/sbin/mori-cd-deploy-as-mori"]


def test_the_canary_script_refuses_under_claude_code():
    r = subprocess.run(
        ["bash", str(CD / "mori-cd-canaries.sh"), "http://x", "phase1"],
        env={**os.environ, "CLAUDECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 1 and "Claude Code" in r.stderr


def test_the_canary_core_needs_the_key_on_stdin():
    r = subprocess.run(
        ["python3", str(CD / "mori-cd-canaries.py"), "http://127.0.0.1:9", "setup"],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2 and "no key" in r.stderr


def test_the_inner_script_runs_from_tmp_with_its_own_home(tmp_path):
    """Board, build ruling (Cursor): `sudo -n -u mori` keeps the caller's directory and doesn't set
    HOME; rootless podman needs mori's home. HOME comes from the passwd entry, the cwd is /tmp, and
    both are set before the first podman command."""
    import pwd

    r = _plan(tmp_path, "v2.3.17", PLAIN)
    assert r.returncode == 0, r.stderr
    me = pwd.getpwuid(os.getuid())
    assert f"home={me.pw_dir} " in r.stdout and r.stdout.strip().endswith("cwd=/tmp")
    src = (CD / "mori-cd-deploy-as-mori").read_text()
    first_podman = re.search(
        r"^\s*(?:if\s+)?podman\s", src, re.M
    ).start()  # a command, not a comment
    assert src.index('export HOME="$ME_HOME"') < first_podman
    assert re.search(r"^cd /tmp$", src, re.M).start() < first_podman


def test_an_unreadable_env_file_is_a_failure_not_unhashed(tmp_path):
    """Consult C17, P3-4: unreadable must not read as 'not hashed' (which would let an older
    release deploy over a hashed table)."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"MORI_API_KEYS={HASHED}\n")
    env_file.chmod(0o000)
    try:
        r = subprocess.run(
            ["bash", str(CD / "mori-cd-deploy-as-mori"), "v2.3.16", DIGEST, "--plan"],
            env={**os.environ, "MORI_CD_ENVFILE_FOR_TESTS": str(env_file)},
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        env_file.chmod(0o600)
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    assert r.returncode == 1 and "unreadable" in r.stderr


def test_an_exported_hashed_table_still_refuses_an_older_release(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"export MORI_API_KEYS={HASHED}\n")
    r = subprocess.run(
        ["bash", str(CD / "mori-cd-deploy-as-mori"), "v2.3.16", DIGEST, "--plan"],
        env={**os.environ, "MORI_CD_ENVFILE_FOR_TESTS": str(env_file)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 3, (r.stdout, r.stderr)


def test_the_inner_script_checks_the_user_runtime_before_podman():
    src = (CD / "mori-cd-deploy-as-mori").read_text()
    first_podman = re.search(r"^\s*(?:if\s+)?podman\s", src, re.M).start()
    assert src.index('[[ -d "$XDG_RUNTIME_DIR" ]]') < first_podman
