"""#73: the CD gate. CD deploys only a tag whose own commit passed CI.

Fixtures in tests/fixtures/cd_gate/ are REAL GitHub API responses recorded on 2026-11-01 (trimmed to the fields the
gate reads): v2.3.13 (annotated, on main, one push CI run, all jobs green, but its ci.yml predates ``ci-ok``), v2.3.10
(lightweight; its merge commit never received a push CI run, the #73 incident) and the PR #93 head ``dd8c021`` (on
main as an ancestor, with only a pull_request run). Synthetic variations are derived from those, never hand-built
from nothing.
"""

from __future__ import annotations

import copy
import io
import json
import urllib.error
from pathlib import Path

import pytest

from scripts import cd_gate as g

FIX = Path(__file__).parent / "fixtures" / "cd_gate"
V13_SHA = "82658062a5ee2412811324a11c5d4d79d2738a20"
V13_RUN = 41325373639


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def _facts(fx: dict, *, tag: str | None = None, expected_sha: str | None = "auto") -> g.Facts:
    sha = fx["sha"]
    return g.Facts(
        tag=tag if tag is not None else fx["tag"],
        expected_sha=sha if expected_sha == "auto" else expected_sha,
        ref=fx.get("ref"),
        tag_object=fx.get("tag_object"),
        compare_status=fx["compare"]["status"],
        pyproject_version=g.parse_pyproject_version(fx["pyproject_toml"]),
        ci_yml_markers=fx.get("ci_yml_markers"),
        runs=copy.deepcopy(fx["runs"]),
        jobs={int(k): copy.deepcopy(v) for k, v in fx["jobs"].items()},
    )


def _with_ci_ok(f: g.Facts, conclusion: str = "success") -> g.Facts:
    """v2.3.13 as it would look with this PR's ci.yml: the same run plus a ``ci-ok`` job."""
    for jobs in f.jobs.values():
        jobs.append({"name": "ci-ok", "status": "completed", "conclusion": conclusion})
    f.ci_yml_markers = {"workflow_dispatch": True, "ci_ok_job": True}
    return f


def _v13_green() -> g.Facts:
    return _with_ci_ok(_facts(_load("v2_3_13.json")))


def _decide(f: g.Facts, waited: float = 0.0) -> g.Verdict:
    return g.decide(f, waited_s=waited)


# ── 1. Success, and the record it leaves ──────────────────────────────────────────────────────────────────────────


def test_success_cites_the_ci_run_it_relied_on():
    v = _decide(_v13_green())
    assert v.outcome == "pass", v.reasons
    assert v.record["ci_run_id"] == V13_RUN
    assert v.record["sha"] == V13_SHA
    assert v.record["event"] == "push"
    assert v.record["jobs"]["ci-ok"] == "success"


def test_real_v2_3_13_fails_only_because_its_ci_yml_predates_ci_ok():
    """The live dry-run expectation: every check passes on v2.3.13 except the ci-ok requirement (its ci.yml is older
    than ci-ok). The gate does not fall back to a weaker rule for old commits."""
    v = _decide(_facts(_load("v2_3_13.json")))
    assert v.outcome == "fail"
    assert len(v.reasons) == 1 and "ci-ok" in v.reasons[0], v.reasons


# ── 2/20. No run: wait for it to appear, then fail with the right remedy ─────────────────────────────────────────


def test_no_run_waits_then_fails_with_the_dispatch_remedy():
    f = _v13_green()
    f.runs, f.jobs = [], {}
    assert _decide(f, waited=10).outcome == "wait"
    v = _decide(f, waited=g.WAIT_APPEAR_S)
    assert v.outcome == "fail"
    assert any("gh workflow run ci.yml --ref v2.3.13" in r for r in v.reasons), v.reasons


def test_no_run_on_a_commit_whose_ci_yml_predates_dispatch_says_make_a_new_commit():
    f = _facts(_load("v2_3_13.json"))
    f.runs, f.jobs = [], {}
    v = _decide(f, waited=g.WAIT_APPEAR_S)
    assert any("new commit on main" in r for r in v.reasons), v.reasons
    assert not any("gh workflow run" in r for r in v.reasons)


def test_no_run_with_unknown_markers_prints_both_remedies():
    f = _v13_green()
    f.runs, f.jobs, f.ci_yml_markers = [], {}, None
    v = _decide(f, waited=g.WAIT_APPEAR_S)
    text = " ".join(v.reasons)
    assert "gh workflow run" in text and "new commit on main" in text


# ── 3/4/17. Run and job conclusions ───────────────────────────────────────────────────────────────────────────────


def test_a_failed_run_fails():
    f = _v13_green()
    f.runs[0]["conclusion"] = "failure"
    assert _decide(f).outcome == "fail"


@pytest.mark.parametrize("bad", ["skipped", "cancelled", "neutral", "failure", None])
def test_a_job_that_did_not_succeed_fails_even_if_the_run_says_success(bad):
    f = _v13_green()
    f.jobs[V13_RUN][0]["conclusion"] = bad
    assert _decide(f).outcome == "fail"


@pytest.mark.parametrize("bad", ["skipped", "failure", "cancelled", "missing"])
def test_ci_ok_missing_or_not_successful_fails(bad):
    f = _v13_green()
    if bad == "missing":
        f.jobs[V13_RUN] = [j for j in f.jobs[V13_RUN] if j["name"] != "ci-ok"]
    else:
        next(j for j in f.jobs[V13_RUN] if j["name"] == "ci-ok")["conclusion"] = bad
    v = _decide(f)
    assert v.outcome == "fail" and any("ci-ok" in r for r in v.reasons)


def test_an_empty_job_list_fails_and_says_so():
    f = _v13_green()
    f.jobs[V13_RUN] = []
    v = _decide(f)
    assert v.outcome == "fail" and any("lists no jobs" in r for r in v.reasons), v.reasons


def test_an_extra_successful_job_passes_and_an_extra_failing_job_fails():
    f = _v13_green()
    f.jobs[V13_RUN].append({"name": "new-job", "status": "completed", "conclusion": "success"})
    assert _decide(f).outcome == "pass"
    f.jobs[V13_RUN][-1]["conclusion"] = "failure"
    assert _decide(f).outcome == "fail"


# ── 5. In progress ────────────────────────────────────────────────────────────────────────────────────────────────


def test_an_in_progress_run_waits_then_fails_at_the_limit():
    f = _v13_green()
    f.runs[0].update(status="in_progress", conclusion=None)
    assert _decide(f, waited=60).outcome == "wait"
    assert _decide(f, waited=g.WAIT_RUN_S).outcome == "fail"


# ── 6/7/13. Which run counts ──────────────────────────────────────────────────────────────────────────────────────


def _add_run(f: g.Facts, **over) -> dict:
    r = dict(f.runs[0])
    r.update(over)
    f.runs.append(r)
    f.jobs[r["id"]] = copy.deepcopy(f.jobs[V13_RUN])
    return r


def test_the_newest_run_decides_a_newer_cancelled_run_blocks():
    f = _v13_green()
    _add_run(
        f,
        id=1,
        created_at="2026-11-01T17:00:00Z",
        conclusion="cancelled",
        event="workflow_dispatch",
    )
    v = _decide(f)
    assert v.outcome == "fail" and v.record.get("ci_run_id") == 1


def test_a_newer_successful_dispatch_run_decides():
    f = _v13_green()
    _add_run(
        f, id=2, created_at="2026-11-01T17:00:00Z", event="workflow_dispatch", head_branch="v2.3.13"
    )
    v = _decide(f)
    assert (
        v.outcome == "pass"
        and v.record["ci_run_id"] == 2
        and v.record["event"] == "workflow_dispatch"
    )


def test_a_dispatch_run_on_the_exact_sha_is_enough():
    f = _v13_green()
    f.runs[0].update(event="workflow_dispatch", head_branch="v2.3.13")
    assert _decide(f).outcome == "pass"


@pytest.mark.parametrize(
    "over",
    [
        {"event": "pull_request"},
        {"event": "push", "head_branch": "feature"},
        {"path": ".github/workflows/cd-gate-check.yml"},
        {"head_sha": "0" * 40},
        {"event": "schedule"},
    ],
)
def test_runs_that_do_not_count_are_ignored(over):
    f = _v13_green()
    f.runs[0].update(over)
    v = _decide(f, waited=g.WAIT_APPEAR_S)
    assert v.outcome == "fail" and any("no CI run" in r for r in v.reasons), v.reasons


def test_the_pr_head_is_on_main_but_its_pull_request_run_does_not_count():
    fx = _load("pr_head_dd8c021.json")
    f = _facts(fx, tag="v2.3.13")
    f.ref = {"object": {"type": "tag", "sha": "1" * 40}}
    f.tag_object = {"tag": "v2.3.13", "object": {"type": "commit", "sha": fx["sha"]}}
    assert f.compare_status == "ahead"  # R1 alone would accept it
    v = _decide(f, waited=g.WAIT_APPEAR_S)
    assert v.outcome == "fail" and any("no CI run" in r for r in v.reasons)


# ── 8/9/10/14/15. Tag, main and version ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["behind", "diverged", None])
def test_a_commit_not_on_main_fails(status):
    f = _v13_green()
    f.compare_status = status
    assert _decide(f).outcome == "fail"


def test_ahead_of_main_passes_with_a_warning():
    f = _v13_green()
    f.compare_status = "ahead"
    v = _decide(f)
    assert v.outcome == "pass" and any("tip of main" in w for w in v.warnings)


def test_a_lightweight_tag_fails_v2_3_10():
    v = _decide(_facts(_load("v2_3_10.json")), waited=g.WAIT_APPEAR_S)
    assert v.outcome == "fail"
    assert any("lightweight" in r for r in v.reasons)
    assert any("no CI run" in r for r in v.reasons), "the incident: no push run on the merge commit"


def test_a_tag_whose_name_differs_from_pyproject_fails():
    f = _v13_green()
    f.pyproject_version = "2.3.12"
    assert _decide(f).outcome == "fail"


def test_a_tag_pointing_at_a_different_commit_than_the_event_fails():
    f = _v13_green()
    f.expected_sha = "f" * 40
    assert _decide(f).outcome == "fail"


def test_a_nested_tag_object_fails():
    f = _v13_green()
    f.tag_object["object"]["type"] = "tag"
    assert _decide(f).outcome == "fail"


def test_a_missing_ref_fails():
    f = _v13_green()
    f.ref = None
    assert _decide(f).outcome == "fail"


@pytest.mark.parametrize(
    "tag",
    [
        "v1.02.3",
        "v01.2.3",
        "v1.2.3-rc1",
        "v1/2.3",
        "v9.9.9'$(id)'",
        "v1;${IFS}id",
        "1.2.3",
        "v1.2",
        "V1.2.3",
        "",
    ],
)
def test_the_tag_name_is_validated_before_anything_is_fetched(tag):
    assert not g.TAG_RE.fullmatch(tag)

    def opener(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("fetched before the tag name was validated")

    gh = g.GitHub("o/r", "t", opener=opener)
    v = g.run_gate(gh, tag, expected_sha=V13_SHA, clock=lambda: 0.0, sleep=lambda s: None)
    assert v.outcome == "fail" and "tag name" in v.reasons[0]


def test_strict_regex_accepts_real_versions():
    for t in ("v2.3.13", "v0.0.0", "v10.20.30"):
        assert g.TAG_RE.fullmatch(t)


# ── 11/20/6b. The fetch layer and the poll loop ───────────────────────────────────────────────────────────────────


class FakeAPI:
    """Serves recorded responses by path; ``runs_seq`` lets successive polls see different run lists."""

    def __init__(self, fx: dict, runs_seq=None, fail_on: str | None = None):
        self.fx, self.runs_seq, self.fail_on, self.calls = fx, list(runs_seq or []), fail_on, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        if self.fail_on and self.fail_on in url:
            raise urllib.error.URLError("boom")
        fx = self.fx
        if "/git/ref/tags/" in url:
            body = fx["ref"]
        elif "/git/tags/" in url:
            body = fx["tag_object"]
        elif "/compare/" in url:
            body = fx["compare"]
        elif "/contents/pyproject.toml" in url:
            body = {"encoding": "base64", "content": _b64(fx["pyproject_toml"])}
        elif "/contents/.github/workflows/ci.yml" in url:
            text = (
                "on:\n  workflow_dispatch:\njobs:\n  ci-ok:\n"
                if fx["ci_yml_markers"]["ci_ok_job"]
                else "jobs:\n"
            )
            body = {"encoding": "base64", "content": _b64(text)}
        elif "/actions/workflows/ci.yml/runs" in url:
            runs = self.runs_seq.pop(0) if self.runs_seq else fx["runs"]
            body = {"workflow_runs": runs}
        elif "/jobs" in url:
            rid = url.split("/actions/runs/")[1].split("/")[0]
            body = {"jobs": fx["jobs"][rid]}
        else:  # pragma: no cover
            raise AssertionError(url)
        return io.BytesIO(json.dumps(body).encode())


def _b64(s: str) -> str:
    import base64

    return base64.b64encode(s.encode()).decode()


def _green_fixture() -> dict:
    fx = _load("v2_3_13.json")
    fx["jobs"][str(V13_RUN)].append(
        {"name": "ci-ok", "status": "completed", "conclusion": "success"}
    )
    fx["ci_yml_markers"] = {"workflow_dispatch": True, "ci_ok_job": True}
    return fx


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_run_gate_end_to_end_on_recorded_responses_passes():
    fx = _green_fixture()
    v = g.run_gate(
        g.GitHub("fjwood69/mori", "t", opener=FakeAPI(fx)),
        "v2.3.13",
        V13_SHA,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    assert v.outcome == "pass", v.reasons


def test_an_api_error_fails_closed():
    fx = _green_fixture()
    for where in ("/git/ref/tags/", "/compare/", "/contents/pyproject.toml", "/runs", "/jobs"):
        gh = g.GitHub("fjwood69/mori", "t", opener=FakeAPI(fx, fail_on=where))
        v = g.run_gate(gh, "v2.3.13", V13_SHA, clock=lambda: 0.0, sleep=lambda s: None)
        assert v.outcome == "fail", where
        assert any("API" in r for r in v.reasons), (where, v.reasons)


@pytest.mark.parametrize("code", [500, 403, 502])
def test_an_http_error_fails_closed_and_names_the_status(code):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)

    v = g.run_gate(
        g.GitHub("o/r", "t", opener=opener),
        "v2.3.13",
        V13_SHA,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    assert v.outcome == "fail" and any(f"HTTP {code}" in r for r in v.reasons), v.reasons


def test_a_run_that_appears_within_the_window_passes():
    fx = _green_fixture()
    c = Clock()
    api = FakeAPI(fx, runs_seq=[[], [], fx["runs"]])
    v = g.run_gate(
        g.GitHub("fjwood69/mori", "t", opener=api), "v2.3.13", V13_SHA, clock=c, sleep=c.sleep
    )
    assert v.outcome == "pass" and 0 < c.t < g.WAIT_APPEAR_S


def test_the_newest_run_is_re_picked_on_every_poll():
    fx = _green_fixture()
    running = dict(fx["runs"][0], status="in_progress", conclusion=None)
    cancelled = dict(running, status="completed", conclusion="cancelled")
    newer = dict(fx["runs"][0], id=7, created_at="2026-11-01T17:00:00Z", event="workflow_dispatch")
    fx["jobs"]["7"] = fx["jobs"][str(V13_RUN)]
    c = Clock()
    api = FakeAPI(fx, runs_seq=[[running], [cancelled, newer]])
    v = g.run_gate(
        g.GitHub("fjwood69/mori", "t", opener=api), "v2.3.13", V13_SHA, clock=c, sleep=c.sleep
    )
    assert v.outcome == "pass" and v.record["ci_run_id"] == 7


def test_a_run_still_in_progress_at_the_limit_fails():
    fx = _green_fixture()
    running = dict(fx["runs"][0], status="in_progress", conclusion=None)
    c = Clock()
    api = FakeAPI(fx, runs_seq=[[running]] * 200)
    v = g.run_gate(
        g.GitHub("fjwood69/mori", "t", opener=api), "v2.3.13", V13_SHA, clock=c, sleep=c.sleep
    )
    assert v.outcome == "fail" and c.t >= g.WAIT_RUN_S


def test_jobs_are_paginated():
    pages = [
        {
            "jobs": [
                {"name": f"j{i}", "status": "completed", "conclusion": "success"}
                for i in range(100)
            ]
        },
        {"jobs": [{"name": "ci-ok", "status": "completed", "conclusion": "success"}]},
    ]

    def opener(req, timeout=None):
        return io.BytesIO(json.dumps(pages.pop(0)).encode())

    jobs = g.GitHub("o/r", "t", opener=opener).jobs(5)
    assert len(jobs) == 101 and jobs[-1]["name"] == "ci-ok"


# ── verify-tag: the release job re-checks the tag still points at the gated commit ───────────────────────────────


def test_verify_tag_passes_when_unmoved_and_fails_when_moved():
    fx = _green_fixture()
    gh = g.GitHub("fjwood69/mori", "t", opener=FakeAPI(fx))
    assert g.verify_tag(gh, "v2.3.13", V13_SHA) == []
    assert g.verify_tag(gh, "v2.3.13", "a" * 40)


def test_dry_run_without_an_expected_sha_resolves_it_from_the_tag():
    fx = _green_fixture()
    v = g.run_gate(
        g.GitHub("fjwood69/mori", "t", opener=FakeAPI(fx)),
        "v2.3.13",
        None,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    assert v.outcome == "pass" and v.record["sha"] == V13_SHA and v.record["mode"] == "dry-run"
