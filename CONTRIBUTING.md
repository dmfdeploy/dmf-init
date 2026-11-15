# Contributing to dmf-init

The Day-0 stateless init/bootstrap container (React + FastAPI) that wraps the dmf-env wizard + toolchain behind a localhost web UI.

This repo is part of the **dmfdeploy platform**. GitHub is the canonical home and the
single source of truth: all changes land via **Pull Request** against `main`.

## Quick start

1. Read the platform overview in the [dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy) umbrella repo
   (`docs/architecture/dmfdeploy-platform-plan.md`) and apply any relevant ADRs from
   `docs/decisions/INDEX.md`.
2. Set up your machine and run the checks (see [Develop and test](#develop-and-test)).
3. Fork or branch, make your change on a topic branch, open a PR against `main`.
4. Ensure CI is green, your commits are **signed off** (see DCO below), and the
   PR body links its issue (see [What a pull request needs](#what-a-pull-request-needs)).

## Develop and test

This section is the one place for development and test commands. The README and
`AGENTS.md` point here. The commands match what CI runs in
`.github/workflows/ci.yml`.

### Prerequisites

- **Python 3.12 or later.** CI tests 3.12.
- **Node.js 22.** CI and the `Dockerfile` use Node 22, which includes npm 10.
  You need it only for the frontend.
- **`age` and `rclone`.** The backup and restore tests call `age-keygen` and
  `rclone`. Install them with your package manager, for example
  `brew install age rclone` or `sudo apt-get install age rclone`. CI installs
  both. See [Run ruff and pytest](#run-ruff-and-pytest) for what happens
  when they are missing.
- **Docker with BuildKit.** Docker 23.0 or later. You need it only for the
  image build. See [Checks that need more than a
  checkout](#checks-that-need-more-than-a-checkout).
- **Optional tools.** [gitleaks](https://github.com/gitleaks/gitleaks) powers
  the commit hook.

### Set up the Python environment

Run these commands from the repository root. Check first that `python3` is
version 3.12 or later. If it isn't, use the full name of a newer interpreter,
such as `python3.13`, in place of `python3`.

```bash
python3 --version
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

CI installs the package without `-e`. The editable install here makes `pytest`
test your edits in `src/`. A regular install tests a copy that you must
reinstall after each change.

### Run ruff and pytest

```bash
ruff check .
pytest
```

`pytest` runs about 220 tests and takes about a minute and a half. `ruff check`
takes a second. CI runs `ruff` first, then `pytest`.

Some tests skip, and one fails, when a tool or variable is missing:

- **Without `age` or `rclone`,** the backup and restore tests skip with
  `missing tools`. One test, `test_doctor_run_redacts_seeded_openbao_secrets`
  in `tests/test_manage.py`, fails instead of skipping when `age-keygen` or
  `rclone` is missing, because its fixture runs a real backup. A run with no
  failures needs both tools. dmfdeploy/dmfdeploy#916 tracks making it skip.
- **Without `DMF_TEST_DMF_ENV_REPO`,** three tests skip. They use scripts from a
  checkout of the [dmf-env](https://github.com/dmfdeploy/dmf-env) repo, and the
  variable holds the path of that checkout.

A plain `pytest` run on a machine with `age` and `rclone` therefore reports
three skipped tests. This is the normal result, and CI's first `pytest` step
skips the same three. CI then sets the variable for one of them, against a fresh
dmf-env clone, so a flag rename in dmf-env fails there. CI doesn't run the other
two. To run the one that CI runs:

```bash
git clone --depth 1 https://github.com/dmfdeploy/dmf-env.git ../dmf-env
DMF_TEST_DMF_ENV_REPO=../dmf-env pytest -rs tests/test_manage_actions.py -k upgrade_in_place_script
```

The output must say `1 passed`. A skip also exits with success, which is why CI
checks the output for `1 passed`.

### Run the launcher tests

The launcher script `bin/dmf-init` and the release scripts have shell tests.
They use a fake `docker` and a local HTTP fixture, so they need no Docker daemon
or network. Run them from the repository root:

```bash
REQUIRE_HTTP_FIXTURE=1 test/launcher/test-install-command.sh
test/launcher/test-stamp-launcher-image.sh
test/launcher/test-build-launcher-release.sh
```

Each script takes a few seconds. CI runs these three. A fourth script,
`test/launcher/test-dmf-init.sh`, covers the launcher's own commands and
helpers. CI doesn't run it, so run it when you change `bin/dmf-init`.

### Run the frontend checks

Run these commands from `frontend/`:

```bash
npm ci
npm run build
```

`npm ci` installs the locked dependencies and takes up to half a minute.
`npm run build` type-checks with `tsc`, then builds with Vite into
`src/dmf_init/static/app/`. It takes a few seconds. The frontend has no unit
tests or linter, so the build is the check.

### Checks that need more than a checkout

CI also runs these checks. Run them locally only if your change touches them:

- **`docker build -t dmf-init:ci .`** builds the image. CI runs it on an arm64
  runner after the Python and frontend jobs pass. Run it when you change the
  `Dockerfile`, `requirements-collections.yml` or anything the image copies in.
  It needs a Docker daemon.
- **`.umbrella/bin/check-working-model-sync.sh`** confirms that the working-model
  block in `AGENTS.md`, `CLAUDE.md` and `QWEN.md` matches the umbrella template.
  CI checks out the umbrella repo to run it. Edit the block only through the
  template in the umbrella repo.
- **The `guard` workflow** scans for secrets and vulnerabilities, lints the
  workflow files, and checks commit subjects and the issue link. The `DCO`
  workflow checks sign-offs.

The end-to-end harness in `test/e2e/` needs a Lima virtual machine and isn't
part of CI. See [its README](test/e2e/README.md).

### Run the service locally

After `npm run build`, start the service from the repository root. On Linux the
service refuses a data root that isn't tmpfs, so the command below allows one
and points it at a throwaway directory.

```bash
DMF_ALLOW_NON_TMPFS_DATA_ROOT=true DMF_DATA_ROOT="$(mktemp -d)" python -m dmf_init.main
```

The service prints a single-use launch link. Open it in your browser. For
operator use, the launcher in the [README](README.md#run-it-appliance) is the
supported way to run `dmf-init`.

### Install the hooks

Run this once in each clone:

```bash
bin/install-hooks.sh
```

The script sets `core.hooksPath` to `.githooks/`. The `pre-commit` and
`pre-push` hooks scan your changes with gitleaks. They skip the scan with a
warning when gitleaks isn't installed. CI runs the same scan.

## Branch & PR model

- **GitHub Pull Requests only.** Direct push to `main` is blocked; force-push is
  banned; linear history is required.
- Topic branches: **`<handle>/<short-slug>`** (e.g. `jdoe/fix-probe-path`). One
  logical change per branch; rebase onto `main` rather than long-lived branches.
- A reviewer (per `CODEOWNERS`) and green required checks are needed to merge.

## What a pull request needs

CI checks each of these.

- **An issue link in the PR body.** Write a fully qualified reference to an issue
  in the [dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy/issues)
  backlog, such as `Refs dmfdeploy/dmfdeploy#912`. Use `Closes dmfdeploy/dmfdeploy#N`
  if the pull request finishes the issue. A bare `#N` doesn't count, because it
  points at this repo. If your change has no issue, such as a typo fix, a
  maintainer can add the `no-issue` label. Only maintainers can add labels, so
  if you open the pull request from a fork, ask in the PR. The check runs again
  when you edit the body or push a commit. Adding the label does not rerun it,
  so after the label is on, edit the body or push to clear a failed check.
- **A DCO sign-off on every commit.** Use `git commit -s`. See the next section.
- **Conventional commit subjects.** Use `type(scope): summary`, with one
  optional scope in lowercase. The types are `feat`, `fix`, `docs`, `chore`,
  `refactor`, `test`, `build`, `ci`, `perf`, `revert` and `style`. Other types,
  such as `wip`, are rejected, and so is a scope list such as `(api,ui)`.
- **Passing CI.** The `ci` workflow runs the checks in
  [Develop and test](#develop-and-test). The `guard` workflow adds secret and
  vulnerability scans.

Also leave agent session links out of commit messages and the PR body. The
`guard` workflow rejects one known session-link format. It can't recognize
every tool's links, so reviewers look for them too.

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/),
not a CLA. **Every commit must be signed off:**

```bash
git commit -s -m "fix: correct the probe path"
```

This appends a `Signed-off-by: Your Name <you@example.com>` trailer certifying you
have the right to submit the work under the project license. A **DCO check** runs
on every PR and fails if any commit is missing the trailer. Amend with
`git commit --amend -s` or rebase with `git rebase --signoff main` to fix. PRs are
**rebase-merged** by default so your signed-off commits land on `main` unchanged.

## Versioning & releases

This repo carries a `VERSION` file (single semver line). Per **ADR-0005**,
`VERSION` is the single source of truth — any release-tagged change must update it
in the same commit. **No VERSION bump → no release.** Release tags are `v<VERSION>`,
created by release automation, never by hand.

## Secrets & public-safety posture

**Secrets stay in OpenBao.** Never commit, track, or reference credentials, tokens,
keys, kubeconfigs, or Terraform state — not even with a "remove later" TODO. Use
**placeholder syntax** for any IPs, DNS names, or operator identity in code, docs,
PR descriptions, or issues (`<control-node-public-ip>`, `dmf.example.com`,
`<handle>`). A local pre-commit gitleaks hook runs on commit, and CI runs
secret-scanning + scrub gates on every PR — but redaction is your responsibility
first. If you need a secret, ask a maintainer — do not improvise a transport.

## Must / Must not

### MUST
- Open changes as **GitHub PRs against `main`** with **signed-off** commits.
- Use Conventional Commit messages and `<handle>/<short-slug>` topic branches.
- Update `VERSION` in the same commit as any release-tagged change.
- Use **placeholder syntax** for all IPs / DNS / operator identity in every artifact.

### MUST NOT
- Commit secrets, tokens, keys, kubeconfigs, or Terraform state.
- Push directly to `main`, force-push, or use `--no-verify` / `--no-gpg-sign`.
- Paste secrets, real IPs/DNS, or operator identity into issues, PRs, or CI logs.

## AI agent contract

Much of this platform is built by AI agents. Agents contribute the same way:
**PRs against `main`, signed off, CI green.** Additionally, agents must run cluster
mutation only via `bin/run-playbook.sh` (ADR-0010), must not use
`--no-verify`/`--force`/`--no-gpg-sign`, and must stop and ask before modifying a
sub-repo with uncommitted state.

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). **Do not** open a public issue for a vulnerability.

## License & spec

Contributions are licensed under [Apache 2.0](LICENSE). The canonical governance
model is **ADR-0041 — DMF Release and Contribution Model** in the
[dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy)
umbrella repo (`docs/decisions/`).
