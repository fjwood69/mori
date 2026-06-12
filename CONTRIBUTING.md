# Contributing to mori

Thanks for your interest in contributing! A few things to know before you open a pull request.

## Contributor License Agreement (one-time)

**mori is dual-licensed (AGPL-3.0 + a commercial licence) — the CLA is what makes both licences
possible, while you keep ownership of your work.** Before your first contribution can be merged,
you'll need to sign the CLA. It's quick and one-time:

1. Open your pull request as normal.
2. The **CLA Assistant** bot will comment with a link to the agreement and ask you to sign.
3. Reply to the PR with exactly:

   > I have read the CLA Document and I hereby sign the CLA

4. The check goes green and your PR is unblocked. You won't be asked again — one signature covers
   all your past and future contributions to mori.

- **Individual contributor?** → [`.github/CLA.md`](.github/CLA.md)
- **Contributing as part of your job / your employer owns the work?** → [`.github/CLA-ENTITY.md`](.github/CLA-ENTITY.md)

You keep ownership of your contribution — the CLA is a *licence grant*, not an assignment. See the
plain-English summary at the top of each document, and [COMMERCIAL.md](COMMERCIAL.md) for what the
commercial licence enables.

## AI-assisted contributions are welcome

Using AI coding tools under your direction is completely fine. The expectation is simple: **you
reviewed the result and you take responsibility for it as your own submission.** Right-to-submit and
responsibility attach to you, the human contributor, regardless of the tools used (this mirrors the
clause in the CLA).

## Before you open a PR

- **Discuss large changes first** — open an issue so we can agree on the approach before you invest
  the work.
- **Match the surrounding code** — style, naming, and test conventions. See
  [`standards/agent-working-practices.md`](standards/agent-working-practices.md).
- **Tests + lint pass locally**: `ruff check . && ruff format --check .` and `pytest -q`. CI runs the
  full suite (SQLite + Postgres), plugin-manifest validation, and the plugin-skills mirror check.
- **Editing a skill?** `skills/<x>/SKILL.md` is mirrored into `plugins/mori/skills/` — run
  `bash scripts/sync-plugin-skills.sh` and commit, or CI will fail the mirror check.
- **Keep docs honest** — update `CHANGELOG.md` for user-facing changes; the release-docs gate enforces it.

## Getting set up

mori is a self-hosted memory server for AI coding agents. For running it locally and connecting a
client, see the [quickstart in the README](README.md#quickstart) and the per-client guides in
[`docs/getting-started/`](docs/getting-started/). Architecture and concepts live in
[`docs/concepts/`](docs/concepts/) and [`docs/reference/`](docs/reference/).

Questions? Open an issue. Thanks for helping make mori better.

<!-- CLA flow verification PR — safe to close. -->
