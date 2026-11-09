# Releasing mori

A release is an annotated `vX.Y.Z` tag on the tip of `main`. Pushing it runs `.github/workflows/cd.yml`, whose first
job (the **CD gate**, `scripts/cd_gate.py`, issue #73) refuses to build anything unless that exact commit passed CI.

## Steps

1. **Merge** the release PR. It carries the version bump (`pyproject.toml`), a `## vX.Y.Z` section in
   `CHANGELOG.md` and the `*Last updated: vX.Y.Z*` footer in `ROADMAP.md`.
2. **CI on the merge commit.** The gate checks this, but until the gate has carried a real release, also confirm
   by hand that the newest `ci.yml` run for the merge commit is green (`headSha` equals the merge commit, not
   just a green run).
3. **Pre-tag UAT** on the deploy host (SQLite and Postgres instances); see the comment at the top of `cd.yml`.
4. **Tag the current tip of `main`**, annotated:

   ```bash
   git fetch origin && git tag -a vX.Y.Z origin/main -m "vX.Y.Z: <one line>" && git push origin vX.Y.Z
   ```

5. **Watch CD.** The gate's record (tag, commit, CI run, every job) is in the run summary; the release notes end
   with the CI run the gate relied on.
6. **Check production** read-only after deploy.

## What the gate requires

- The tag name is strictly `vX.Y.Z` (no leading zeros). **Pre-release tags such as `v1.2.3-rc1` do not deploy.**
- The tag is **annotated** and points at the commit the workflow was triggered for.
- The commit is on `main`: the compare API returns `identical` (the tip of main), or `ahead` (main is ahead of
  the tagged commit), which passes with a warning. `behind` or `diverged` fails.
- The tag name equals `version` in `pyproject.toml` at that commit.
- The **newest** run of `ci.yml` on that exact commit, from a push to `main` or a manual dispatch, completed with
  `success`, its `ci-ok` job succeeded, and every job in its latest attempt succeeded. A pull-request run never
  counts, nor does a run on another commit with the same tree.
- It waits up to 5 minutes for a run to appear and up to 30 minutes for one in progress, then fails.

`ci-ok` (in `ci.yml`) is the one list of required jobs. It needs every other job, so a new job is required by
default; `tests/test_cd_workflows.py` enforces that.

## When the gate fails

- **No CI run on the commit** (GitHub sometimes never delivers the push event): request one for the exact ref,
  `gh workflow run ci.yml --ref vX.Y.Z`, then **re-run the failed CD workflow**. Do not delete and re-push the tag.
  For a commit whose `ci.yml` predates `workflow_dispatch`, make a new commit on `main` and tag that.
- **CI still running:** re-run the CD workflow once CI finishes.
- **Anything else:** fix the cause on `main` and release a new version.

To check a tag without deploying anything: `gh workflow run cd-gate-check.yml -f tag=vX.Y.Z` (no build, deploy or
release job; read-only token).

## Rules

- Never tag while a deploy is pending. CD runs one deploy at a time; a third tag pushed while one is pending
  replaces the pending one, and that version is never deployed.
- There is **no override** in CD. In an emergency, deploy by hand on the production host (pull the image, retag,
  restart the units, as the deploy step does) and record who did it and why alongside the release decision.
- The production host pulls the image from GHCR **anonymously**, which works because the package is public. Its
  stored registry login was removed on 2026-11-01 after it started returning `403` on every pull (a `403` while
  "requesting bearer token" means a stale credential, not a wrong image reference). **Before making the package
  private, add a read-only pull token for the host** (`read:packages`), or every deploy will fail at the pull.
- SSH host-key checking is on. The production host's public key is the `GCE_HOST_KEY` secret of the `production`
  environment. If the host is recreated, deploys fail until that secret is updated with the new key, verified out
  of band.

## Evidence runs are never deleted

A workflow run cited as evidence (a gate verdict, a dry run, the CI run a release relied on) is part of the audit
trail: **never delete it.** If a cited run turns out to be wrong, supersede it with a new run, and record which run it
replaces and why. GitHub lets anyone with write access delete runs, so this is a rule, not a control. It exists because
the first two #73 negative-proof runs (41354577460 and 41354583075, 2026-11-01) were deleted after being cited, leaving
only a 404 behind. The proof of record is their re-runs, 41358366883 and 41358374029.

## Known gaps (disclosed)

- The gate runs from the tagged commit's own `cd.yml`. A tag on a commit older than the gate runs no gate. The
  repository settings below are the out-of-band complement.
- The ingestion service is exercised by no gate, before or after deploy (the smoke check only probes its health
  endpoint, and UAT runs no ingestion service).
- CD rebuilds the image from the gated commit; the deployed image is not the one CI built. The gate attests the
  source, not the artifact.
- The deploy SSH key is a repository secret, so any branch's workflow can read it; only the `production`
  environment's secrets (the Tailscale key) are limited to release tags. Only the host being reachable solely over
  the tailnet keeps that from mattering. Moving the key into the environment is the next item.

## The registry's `latest`

The registry's `latest` moves only when the release is the highest version (the gate's `is_highest` output), so
re-running CD for an older tag never moves it backwards. On the host, the deploy still retags the local `latest` to
the image it just pulled, including on a rollback, because the running units follow that local tag.

## Repository settings (out of band; required before tagging)

1. The `production` environment accepts deployments from `v*` tags only.
2. A tag ruleset on `refs/tags/v*`: creation restricted to admins; no update (moving a tag) and no deletion.
3. `ci-ok` is a required status check on `main`.

These complement the gate; they do not replace it. A required check on the pull request would not have caught
v2.3.10, whose merge commit never received a push CI run.
