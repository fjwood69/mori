"""#73: pin the workflow safeguards the CD gate depends on, so a later edit cannot remove one quietly.

ci.yml: the ``ci-ok`` aggregator needs every other job (new jobs are required by default), runs with
``if: always()`` and fails unless every result is ``success``; ``workflow_dispatch`` exists and the version guard
gets a real base on it. cd.yml: the gate exists and every job chains from it; the gate has no environment; every
checkout is pinned to the gated commit; no untrusted expression is pasted into a script; host-key checking is on.
cd-gate-check.yml: a dry run with no route to production.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import cd_gate

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
# Jobs deliberately NOT required by ci-ok. Adding one here is the only way to opt a job out; it shows in the diff.
CI_OK_EXEMPT: set[str] = set()


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text())


def _on(wf: dict) -> dict:
    return wf.get("on", wf.get(True))  # PyYAML (YAML 1.1) reads the bare key `on` as True


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _needs(job: dict) -> set[str]:
    n = job.get("needs") or []
    return {n} if isinstance(n, str) else set(n)


def _step(job: dict, name: str) -> dict:
    return next(s for s in _steps(job) if s.get("name") == name)


# ── ci.yml ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_ci_ok_needs_every_other_job():
    jobs = _load("ci.yml")["jobs"]
    assert "ci-ok" in jobs, "ci-ok is the one list of required jobs; the CD gate requires it"
    assert _needs(jobs["ci-ok"]) == set(jobs) - {"ci-ok"} - CI_OK_EXEMPT


def test_ci_ok_runs_always_and_is_named_as_the_gate_expects():
    job = _load("ci.yml")["jobs"]["ci-ok"]
    assert str(job.get("if")).replace(" ", "") == "always()", (
        "without it a failed job leaves ci-ok skipped"
    )
    assert job.get("name") == cd_gate.CI_OK_JOB, "the gate matches the job's display name"


def _ci_ok_script() -> str:
    run = _step(_load("ci.yml")["jobs"]["ci-ok"], "Every required job succeeded")["run"]
    return run.split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0]


@pytest.mark.parametrize(
    "needs,ok",
    [
        ({"a": {"result": "success"}, "b": {"result": "success"}}, True),
        ({"a": {"result": "success"}, "b": {"result": "skipped"}}, False),
        ({"a": {"result": "failure"}}, False),
        ({"a": {"result": "cancelled"}}, False),
        ({}, False),
    ],
)
def test_ci_ok_fails_unless_every_result_is_success(needs, ok, tmp_path):
    import json
    import sys

    p = subprocess.run(
        [sys.executable, "-c", _ci_ok_script()],
        env={**os.environ, "NEEDS": json.dumps(needs)},
        capture_output=True,
        text=True,
    )
    assert (p.returncode == 0) is ok, p.stdout + p.stderr


def test_ci_can_be_dispatched():
    assert "workflow_dispatch" in _on(_load("ci.yml"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("event", ["workflow_dispatch", "pull_request", "push"])
def test_version_guard_base_per_event(event, tmp_path):
    """Runs the real step script. Dispatch must use HEAD^1 (an empty base would skip the bump rule)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for i in range(3):
        (repo / "f").write_text(str(i))
        _git(repo, "add", "f")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", str(i))
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    out = tmp_path / "args"
    (fakebin / "python3").write_text(f'#!/bin/sh\necho "$@" > "{out}"\n')
    (fakebin / "python3").chmod(0o755)
    step = _step(
        _load("ci.yml")["jobs"]["version-guard"], "Changed artifacts raised their versions"
    )
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "EVENT": event,
        "PR_BASE": "pr-base-sha",
        "BEFORE": "before-sha",
    }
    subprocess.run(["bash", "-c", step["run"]], cwd=repo, env=env, check=True)
    base = out.read_text().split("--base ")[1].strip()
    expected = {
        "workflow_dispatch": _git(repo, "rev-parse", "HEAD^1"),
        "pull_request": "pr-base-sha",
        "push": "before-sha",
    }
    assert base == expected[event]


# ── cd.yml ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_cd_runs_only_on_release_tags():
    assert _on(_load("cd.yml")) == {"push": {"tags": ["v*"]}}


def test_cd_deploys_one_at_a_time_and_never_cancels():
    assert _load("cd.yml")["concurrency"] == {"group": "cd-production", "cancel-in-progress": False}


def test_gate_exists_reads_only_and_never_sees_production_secrets():
    gate = _load("cd.yml")["jobs"]["gate"]
    assert "environment" not in gate
    assert gate["permissions"] == {"actions": "read", "contents": "read"}
    assert "scripts/cd_gate.py check" in _step(gate, "Gate")["run"]
    assert set(gate["outputs"]) >= {"sha", "ci_run_id", "ci_run_url"}


def test_every_job_chains_from_the_gate():
    jobs = _load("cd.yml")["jobs"]
    assert _needs(jobs["build-and-push"]) == {"gate"}
    assert _needs(jobs["deploy"]) == {"gate", "build-and-push"}
    assert _needs(jobs["create-release"]) == {"gate", "deploy"}
    assert set(jobs) == {"gate", "build-and-push", "deploy", "create-release"}


def test_every_checkout_after_the_gate_is_pinned_to_the_gated_commit():
    jobs = _load("cd.yml")["jobs"]
    for name, job in jobs.items():
        for s in _steps(job):
            if str(s.get("uses", "")).startswith("actions/checkout"):
                want = "${{ github.sha }}" if name == "gate" else "${{ needs.gate.outputs.sha }}"
                assert (s.get("with") or {}).get("ref") == want, name


@pytest.mark.parametrize("wf", ["cd.yml", "cd-gate-check.yml", "ci.yml"])
def test_no_expression_is_pasted_into_a_script(wf):
    """Stricter than a deny-list of contexts (which `${{ github['head_ref'] }}` would evade): NO `${{` at all in a
    `run:` script. Every value reaches a script through `env:`."""
    for name, job in _load(wf)["jobs"].items():
        for s in _steps(job):
            assert "${{" not in str(s.get("run", "")), (wf, name, s.get("name"))


def test_ssh_host_key_checking_is_on_and_the_key_comes_from_the_secret():
    deploy = _load("cd.yml")["jobs"]["deploy"]
    step = _step(deploy, "Deploy to GCE via SSH")
    run = step["run"]
    assert "StrictHostKeyChecking=yes" in run
    assert "StrictHostKeyChecking=no" not in run and "UserKnownHostsFile=/dev/null" not in run
    assert 'UserKnownHostsFile="$KNOWN"' in run
    assert "ssh-keyscan" not in str(deploy), "no trust-on-first-use"
    assert step["env"]["GCE_HOST_KEY"] == "${{ secrets.GCE_HOST_KEY }}"


def test_the_deploy_step_validates_the_tag_with_the_gates_pattern():
    run = _step(_load("cd.yml")["jobs"]["deploy"], "Deploy to GCE via SSH")["run"]
    assert cd_gate.TAG_RE.pattern in run


def test_deploy_is_least_privilege_and_bounded():
    deploy = _load("cd.yml")["jobs"]["deploy"]
    assert deploy["permissions"] == {} and deploy["timeout-minutes"] <= 30


def test_no_checkout_in_cd_persists_credentials():
    for name, job in _load("cd.yml")["jobs"].items():
        for s in _steps(job):
            if str(s.get("uses", "")).startswith("actions/checkout"):
                assert (s.get("with") or {}).get("persist-credentials") is False, name


def test_the_deploy_pulls_the_build_by_digest():
    jobs = _load("cd.yml")["jobs"]
    build = jobs["build-and-push"]
    assert build["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"
    assert _step(build, "Build and push")["id"] == "build"
    step = _step(jobs["deploy"], "Deploy to GCE via SSH")
    assert step["env"]["DIGEST"] == "${{ needs.build-and-push.outputs.digest }}"


# ── The deploy step, RUN (with a fake ssh that records what it was given) ────────────────────────────────────────

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyNotARealKeyxxxxxxxxxxxxxxxxxxxx"
DIGEST = "sha256:" + "ab" * 32
FAKE_SSH = """#!/bin/bash
printf '%s\\n' "$@" > "$REC/args"
for a in "$@"; do
  case "$a" in UserKnownHostsFile=*) cp "${a#UserKnownHostsFile=}" "$REC/known" ;; esac
done
cat > "$REC/stdin"
"""


def _run_deploy(tmp_path, **over):
    step = _step(_load("cd.yml")["jobs"]["deploy"], "Deploy to GCE via SSH")
    rec = tmp_path / "rec"
    rec.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "ssh").write_text(FAKE_SSH)
    (fakebin / "ssh").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "REC": str(rec),
        "GCE_SSH_KEY": "dummy-private-key",
        "GCE_USER": "deploy",
        "GCE_HOST": "198.51.100.7",
        "GCE_HOST_KEY": HOST_KEY,
        "REF_NAME": "v2.3.14",
        "DIGEST": DIGEST,
        **over,
    }
    p = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True)
    return p, rec


def test_the_deploy_step_runs_with_host_key_checking_and_a_digest(tmp_path):
    p, rec = _run_deploy(tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    args = (rec / "args").read_text().splitlines()
    assert "StrictHostKeyChecking=yes" in args
    assert (rec / "known").read_text() == f"198.51.100.7 {HOST_KEY}\n"
    # v2.3.17 (G3/G6): the remote command is exactly the forced command's verb — no shell, no
    # here-document; the pull-by-digest lives in deploy/gcp/cd/mori-cd-deploy-as-mori.
    assert args[-1] == f"deploy v2.3.14 {DIGEST}"
    assert "-T" in args
    remote = (rec / "stdin").read_text() if (rec / "stdin").exists() else ""
    assert remote.strip() == ""
    inner = (ROOT / "deploy" / "gcp" / "cd" / "mori-cd-deploy-as-mori").read_text()
    assert 'IMAGE_PINNED="ghcr.io/fjwood69/mori@${DIGEST}"' in inner
    assert 'podman pull "$IMAGE_PINNED"' in inner and 'podman pull "$IMAGE_VERSION"' not in inner


@pytest.mark.parametrize("key", [HOST_KEY + "\n", HOST_KEY + "\r\n", "  " + HOST_KEY + "  "])
def test_the_host_key_tolerates_surrounding_whitespace(tmp_path, key):
    p, rec = _run_deploy(tmp_path, GCE_HOST_KEY=key)
    assert p.returncode == 0, p.stdout + p.stderr
    assert (rec / "known").read_text() == f"198.51.100.7 {HOST_KEY}\n"


@pytest.mark.parametrize(
    "over",
    [
        {"GCE_HOST_KEY": ""},
        {"GCE_HOST_KEY": "198.51.100.7 " + HOST_KEY},  # a full known_hosts line, host included
        {"GCE_HOST_KEY": "ssh-ed25519"},
        {"GCE_HOST_KEY": HOST_KEY + " trailing-comment"},  # the secret is exactly "<type> <base64>"
        {"GCE_HOST_KEY": "ssh-dss AAAAB3NzaC1kc3MAAACBAexample"},  # DSA is not accepted
        {"REF_NAME": "v2.3.14';id;'"},
        {"REF_NAME": "v2.03.14"},
        {"DIGEST": ""},
        {"DIGEST": "sha256:xyz"},
        {"DIGEST": "latest"},
    ],
)
def test_the_deploy_step_refuses_before_contacting_the_host(tmp_path, over):
    p, rec = _run_deploy(tmp_path, **over)
    assert p.returncode != 0
    assert not (rec / "args").exists(), "ssh must not be reached"


def test_registry_latest_moves_only_for_the_highest_version():
    jobs = _load("cd.yml")["jobs"]
    assert jobs["gate"]["outputs"]["is_highest"] == "${{ steps.gate.outputs.is_highest }}"
    tags = next(s for s in _steps(jobs["build-and-push"]) if s.get("id") == "meta")["with"]["tags"]
    latest = [line.strip() for line in tags.splitlines() if "value=latest" in line]
    assert latest == ["type=raw,value=latest,enable=${{ needs.gate.outputs.is_highest == 'true' }}"]


def test_release_rechecks_the_tag_immediately_before_and_after_publishing():
    steps = _load("cd.yml")["jobs"]["create-release"]["steps"]
    names = [s.get("name") for s in steps]
    i = names.index("Create or update release")
    assert names[i - 1] == "Tag unmoved since the gate"
    assert names[i + 1] == "Tag still unmoved after the release"
    for n in ("Tag unmoved since the gate", "Tag still unmoved after the release"):
        assert "cd_gate.py verify-tag" in _step({"steps": steps}, n)["run"]


def test_ci_token_is_read_only_and_lint_covers_scripts():
    wf = _load("ci.yml")
    assert wf["permissions"] == {"contents": "read"}
    lint = wf["jobs"]["lint"]
    assert "scripts/" in _step(lint, "Ruff lint")["run"]
    assert "scripts/" in _step(lint, "Ruff format check")["run"]


# ── cd-gate-check.yml ─────────────────────────────────────────────────────────────────────────────────────────────


def test_dry_run_workflow_has_no_route_to_production():
    wf = _load("cd-gate-check.yml")
    text = (WF / "cd-gate-check.yml").read_text()
    assert set(_on(wf)) == {"workflow_dispatch"}
    assert wf["permissions"] == {"actions": "read", "contents": "read"}
    assert set(re.findall(r"secrets\.([A-Z_]+)", text)) == {"GITHUB_TOKEN"}
    for job in wf["jobs"].values():
        assert "environment" not in job
        for s in _steps(job):
            assert "ssh" not in str(s.get("run", "")) and "docker" not in str(s.get("uses", ""))
            if str(s.get("uses", "")).startswith("actions/checkout"):
                assert "ref" not in (s.get("with") or {}), (
                    "runs the gate from the dispatcher's ref, not the tag"
                )


# ── v2.3.17: the deploy never reads the env file; the contract runs from the runner ──────────


def test_the_deploy_never_reads_the_key_table_or_uses_sudo():
    deploy = _step(_load("cd.yml")["jobs"]["deploy"], "Deploy to GCE via SSH")["run"]
    assert "MORI_API_KEYS" not in deploy and "sudo" not in deploy and ".env" not in deploy


def test_the_contract_runs_from_the_runner_with_cds_own_key():
    step = _step(
        _load("cd.yml")["jobs"]["deploy"],
        "Deployment contract (from the runner, CD's own write key)",
    )
    env = step["env"]
    assert env["MORI_CD_KEY"] == "${{ secrets.MORI_CD_KEY }}"
    assert env["MORI_CD_KEY_NAME"] == "${{ vars.MORI_CD_KEY_NAME }}"
    assert env["GATED_SHA"] == "${{ needs.gate.outputs.sha }}"
    run = step["run"]
    assert "verify-deployment.py" in run and "${GATED_SHA}" in run and "MORI_API_KEYS" not in run
