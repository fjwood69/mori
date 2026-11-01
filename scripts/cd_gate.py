#!/usr/bin/env python3
"""CD gate (#73): CD deploys only a tag whose own commit passed CI.

Runs as the first job of ``cd.yml``; every other job ``needs`` it. Every fact it decides on is read from the GitHub
REST API (never from the checkout), so each one can be recorded as a test fixture. Standard library only.

The rules (board-approved design, 2026-11-01):

* **Tag name.** Strict ``vX.Y.Z`` with no leading zeros, validated before anything else (the tag name reaches
  shells later in CD). Pre-release tags do not deploy.
* **Annotated tag.** The ref must point at a tag object, which must point at a commit, which must equal the commit
  the workflow was triggered for (``GITHUB_SHA``).
* **On main.** ``compare/<sha>...main`` must be ``identical`` (or ``ahead``, with a warning: tag the tip of main).
* **Version.** The tag name must equal ``version`` in ``pyproject.toml`` at that commit.
* **CI on this exact commit.** A run of ``.github/workflows/ci.yml`` with ``head_sha`` equal to the commit, from a
  ``push`` to ``main`` or a ``workflow_dispatch``. Pull-request runs and runs on other commits never count, even
  with an identical tree. The NEWEST matching run decides (re-picked on every poll); it must have completed with
  ``success``, its ``ci-ok`` job must have succeeded, and every job in its latest attempt must have succeeded.
* **Fail closed.** No run: wait up to WAIT_APPEAR_S for one to appear (push events can lag), then fail with the
  remedy. Run in progress: wait up to WAIT_RUN_S. Any API error or unexpected shape fails.

The gate runs from the tagged commit's own ``cd.yml`` (in-band): a tag on a commit older than the gate runs no gate.
The repository settings (production environment limited to ``v*`` tags, a tag ruleset, ``ci-ok`` required on main)
are the out-of-band complement; see docs/RELEASING.md.

Usage::

    cd_gate.py check --repo OWNER/NAME --tag vX.Y.Z [--sha SHA]   # no --sha: dry run, commit taken from the tag
    cd_gate.py verify-tag --repo OWNER/NAME --tag vX.Y.Z --sha SHA

Reads the token from GH_TOKEN (or GITHUB_TOKEN). Writes the record to GITHUB_STEP_SUMMARY and the gated commit and
CI run to GITHUB_OUTPUT when those are set.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CI_OK_JOB = "ci-ok"
MAIN = "main"
WAIT_RUN_S = 30 * 60  # board ruling: wait up to 30 min for a run in progress
WAIT_APPEAR_S = 5 * 60  # and up to 5 min for a run that has not appeared yet (push events can lag)
POLL_S = 30
API_TIMEOUT_S = 20
PER_PAGE = 100


class GateError(Exception):
    """A fact could not be read. Always ends the gate with a failure."""


@dataclass
class Facts:
    tag: str
    expected_sha: str | None  # GITHUB_SHA; None in a dry run
    ref: dict[str, Any] | None
    tag_object: dict[str, Any] | None
    compare_status: str | None
    pyproject_version: str | None
    ci_yml_markers: (
        dict[str, bool] | None
    )  # {"workflow_dispatch": .., "ci_ok_job": ..}; None = unknown
    runs: list[dict[str, Any]]
    jobs: dict[int, list[dict[str, Any]]]  # run id -> jobs of its latest attempt


@dataclass
class Verdict:
    outcome: str  # "pass" | "fail" | "wait"
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    record: dict[str, Any] = field(default_factory=dict)


# ── The decision (pure) ───────────────────────────────────────────────────────────────────────────────────────────


def resolve_commit(f: Facts) -> tuple[str | None, list[str]]:
    """The commit the tag points at, and every tag-level failure."""
    reasons: list[str] = []
    obj = (f.ref or {}).get("object") or {}
    if not obj.get("sha"):
        return None, [f"tag {f.tag}: ref not found"]
    sha: str | None = obj["sha"]
    if obj.get("type") == "commit":
        reasons.append(f"tag {f.tag} is lightweight; release tags must be annotated (git tag -a)")
    elif obj.get("type") == "tag":
        inner = (f.tag_object or {}).get("object") or {}
        if inner.get("type") != "commit" or not inner.get("sha"):
            return None, reasons + [
                f"tag {f.tag}: the tag object does not point at a commit (nested or malformed)"
            ]
        sha = inner["sha"]
    else:
        return None, [f"tag {f.tag}: unexpected ref type {obj.get('type')!r}"]
    if f.expected_sha is not None and sha != f.expected_sha:
        reasons.append(
            f"tag {f.tag} points at {sha}, but this workflow was triggered for {f.expected_sha}"
        )
    return sha, reasons


def counts(run: dict[str, Any], sha: str) -> bool:
    """Does this run count as CI for ``sha``? ci.yml only; exact commit; push to main or a manual dispatch."""
    if run.get("path") != CI_WORKFLOW_PATH or run.get("head_sha") != sha:
        return False
    if run.get("event") == "push":
        return run.get("head_branch") == MAIN
    return run.get("event") == "workflow_dispatch"


def newest(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return max(runs, key=lambda r: (str(r.get("created_at") or ""), int(r.get("id") or 0)))


def remedy(f: Facts) -> str:
    dispatch = f"request a run on this commit: gh workflow run ci.yml --ref {f.tag}"
    commit = "this commit's ci.yml predates workflow_dispatch/ci-ok: make a new commit on main and tag that"
    m = f.ci_yml_markers
    if m is None:
        return f"either {dispatch} (if its ci.yml has workflow_dispatch and ci-ok), or {commit}"
    return dispatch if m.get("workflow_dispatch") and m.get("ci_ok_job") else commit


def decide(
    f: Facts, waited_s: float, wait_run_s: float = WAIT_RUN_S, wait_appear_s: float = WAIT_APPEAR_S
) -> Verdict:
    v = Verdict(outcome="fail")
    rec = v.record
    rec.update(tag=f.tag, mode="check" if f.expected_sha else "dry-run")
    if not TAG_RE.fullmatch(f.tag):
        v.reasons.append(f"tag name {f.tag!r} is not a strict vX.Y.Z release tag")
        return v

    sha, tag_reasons = resolve_commit(f)
    v.reasons += tag_reasons
    rec["sha"] = sha
    if sha is None:
        return v

    rec["compare"] = f.compare_status
    if f.compare_status == "ahead":
        v.warnings.append(
            "the tagged commit is on main but is not the tip of main (tag the current tip of main)"
        )
    elif f.compare_status != "identical":
        v.reasons.append(f"commit {sha} is not on main (compare status {f.compare_status!r})")

    rec["pyproject_version"] = f.pyproject_version
    if f.pyproject_version != f.tag[1:]:
        v.reasons.append(
            f"tag {f.tag} does not match pyproject.toml version {f.pyproject_version!r}"
        )

    matching = [r for r in f.runs if counts(r, sha)]
    if not matching:
        if waited_s < wait_appear_s and not v.reasons:
            v.outcome = "wait"
            return v
        v.reasons.append(
            f"no CI run of {CI_WORKFLOW_PATH} on {sha} (push to main or workflow_dispatch); {remedy(f)}"
        )
        return v

    run = newest(matching)
    rid = int(run["id"])
    rec.update(
        ci_run_id=rid,
        ci_run_url=run.get("html_url"),
        event=run.get("event"),
        attempt=run.get("run_attempt"),
    )
    if run.get("status") != "completed":
        if waited_s < wait_run_s and not v.reasons:
            v.outcome = "wait"
            return v
        v.reasons.append(f"CI run {rid} is still {run.get('status')!r} after {int(waited_s)}s")
        return v
    if run.get("conclusion") != "success":
        v.reasons.append(
            f"CI run {rid} concluded {run.get('conclusion')!r} (the newest matching run decides)"
        )

    jobs = f.jobs.get(rid) or []
    rec["jobs"] = {str(j.get("name")): j.get("conclusion") for j in jobs}
    if not jobs:
        v.reasons.append(f"CI run {rid} lists no jobs")
    elif CI_OK_JOB not in rec["jobs"]:
        v.reasons.append(
            f"CI run {rid} has no {CI_OK_JOB} job (its ci.yml predates ci-ok); the gate requires ci-ok"
        )
    else:
        if rec["jobs"][CI_OK_JOB] != "success":
            v.reasons.append(f"CI run {rid}: {CI_OK_JOB} concluded {rec['jobs'][CI_OK_JOB]!r}")
        bad = {n: c for n, c in rec["jobs"].items() if c != "success" and n != CI_OK_JOB}
        if bad:
            v.reasons.append(f"CI run {rid}: jobs that did not succeed: {bad}")

    if not v.reasons:
        v.outcome = "pass"
    return v


# ── Reading the facts ─────────────────────────────────────────────────────────────────────────────────────────────


Opener = Callable[..., Any]


class GitHub:
    def __init__(self, repo: str, token: str, opener: Opener | None = None) -> None:
        self.repo, self.token = repo, token
        self.opener: Opener = opener or urllib.request.urlopen

    def get(self, path: str) -> Any:
        url = f"https://api.github.com/repos/{self.repo}/{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener(req, timeout=API_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise GateError(f"GitHub API {path}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise GateError(f"GitHub API {path}: {e}") from e

    def contents(self, path: str, sha: str) -> str | None:
        body = self.get(f"contents/{path}?ref={sha}")
        if body is None:
            return None
        if not isinstance(body, dict) or body.get("encoding") != "base64":
            raise GateError(f"GitHub API contents/{path}: unexpected shape")
        return base64.b64decode(body["content"]).decode()

    def runs(self, sha: str) -> list[dict[str, Any]]:
        body = self.get(f"actions/workflows/ci.yml/runs?head_sha={sha}&per_page={PER_PAGE}")
        if not isinstance(body, dict) or not isinstance(body.get("workflow_runs"), list):
            raise GateError("GitHub API workflow runs: unexpected shape")
        return list(body["workflow_runs"])

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            body = self.get(
                f"actions/runs/{run_id}/jobs?filter=latest&per_page={PER_PAGE}&page={page}"
            )
            if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
                raise GateError(f"GitHub API jobs for run {run_id}: unexpected shape")
            out += body["jobs"]
            if len(body["jobs"]) < PER_PAGE:
                return out
            page += 1


def parse_pyproject_version(text: str | None) -> str | None:
    if not text:
        return None
    try:
        v = tomllib.loads(text).get("project", {}).get("version")
    except tomllib.TOMLDecodeError:
        return None
    return v if isinstance(v, str) else None


def ci_markers(text: str | None) -> dict[str, bool] | None:
    """Textual (standard library: no YAML parser). Only ever chooses the remedy text, never pass/fail."""
    if text is None:
        return None
    return {
        "workflow_dispatch": re.search(r"^\s+workflow_dispatch\s*:", text, re.M) is not None,
        "ci_ok_job": re.search(rf"^  {re.escape(CI_OK_JOB)}:\s*$", text, re.M) is not None,
    }


def static_facts(gh: GitHub, tag: str, expected_sha: str | None) -> Facts:
    ref = gh.get(f"git/ref/tags/{tag}")
    f = Facts(tag, expected_sha, ref, None, None, None, None, [], {})
    obj = (ref or {}).get("object") or {}
    if obj.get("type") == "tag":
        f.tag_object = gh.get(f"git/tags/{obj['sha']}")
    sha, _ = resolve_commit(f)
    if sha is None:
        return f
    cmp = gh.get(f"compare/{sha}...{MAIN}")
    f.compare_status = (cmp or {}).get("status")
    f.pyproject_version = parse_pyproject_version(gh.contents("pyproject.toml", sha))
    f.ci_yml_markers = ci_markers(gh.contents(CI_WORKFLOW_PATH, sha))
    return f


def refresh_runs(gh: GitHub, f: Facts, sha: str) -> None:
    f.runs = gh.runs(sha)
    matching = [r for r in f.runs if counts(r, sha)]
    f.jobs = {}
    if matching:
        run = newest(matching)
        if run.get("status") == "completed":
            f.jobs[int(run["id"])] = gh.jobs(int(run["id"]))


def run_gate(
    gh: GitHub,
    tag: str,
    expected_sha: str | None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = POLL_S,
) -> Verdict:
    if not TAG_RE.fullmatch(tag):  # before any use of the name, including API paths
        return decide(Facts(tag, expected_sha, None, None, None, None, None, [], {}), 0)
    start = clock()
    try:
        f = static_facts(gh, tag, expected_sha)
        sha, _ = resolve_commit(f)
        while True:
            if sha is not None:
                refresh_runs(gh, f, sha)  # re-picked on every poll
            v = decide(f, waited_s=clock() - start)
            if v.outcome != "wait":
                return v
            sleep(poll_s)
    except GateError as e:
        return Verdict(outcome="fail", reasons=[str(e)], record={"tag": tag})


def verify_tag(gh: GitHub, tag: str, sha: str) -> list[str]:
    """For the release job: the tag still points at the commit the gate approved."""
    if not TAG_RE.fullmatch(tag):
        return [f"tag name {tag!r} is not a strict vX.Y.Z release tag"]
    try:
        f = static_facts(gh, tag, sha)
    except GateError as e:
        return [str(e)]
    _, reasons = resolve_commit(f)
    return reasons


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def render(v: Verdict) -> str:
    lines = [f"## CD gate: {v.outcome.upper()}", ""]
    lines += [
        f"- **{k}**: {v.record[k]}"
        for k in ("tag", "mode", "sha", "compare", "pyproject_version")
        if k in v.record
    ]
    if "ci_run_id" in v.record:
        r = v.record
        lines.append(
            f"- **CI run**: {r['ci_run_id']} ({r.get('ci_run_url')}), event {r.get('event')}, attempt {r.get('attempt')}"
        )
    for name, concl in sorted((v.record.get("jobs") or {}).items()):
        lines.append(f"  - {name}: {concl}")
    lines += [f"- WARNING: {w}" for w in v.warnings]
    lines += [f"- FAIL: {r}" for r in v.reasons]
    return "\n".join(lines) + "\n"


def _append(var: str, text: str) -> None:
    path = os.environ.get(var)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "verify-tag"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--tag", required=True)
        p.add_argument("--sha", required=(name == "verify-tag"))
    a = ap.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print("FAIL: no GH_TOKEN/GITHUB_TOKEN")
        return 1
    gh = GitHub(a.repo, token)
    if a.cmd == "verify-tag":
        reasons = verify_tag(gh, a.tag, a.sha)
        for r in reasons:
            print(f"FAIL: {r}")
        print("OK: tag unmoved" if not reasons else "")
        return 1 if reasons else 0
    v = run_gate(gh, a.tag, a.sha or None)
    text = render(v)
    print(text)
    _append("GITHUB_STEP_SUMMARY", text)
    if v.outcome == "pass":
        rec = v.record
        _append(
            "GITHUB_OUTPUT",
            f"sha={rec['sha']}\nci_run_id={rec['ci_run_id']}\nci_run_url={rec['ci_run_url']}\n",
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
