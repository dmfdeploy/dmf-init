# Contributing to dmf-init

The Day-0 stateless init/bootstrap container (React + FastAPI) that wraps the dmf-env wizard + toolchain behind a localhost web UI.

This repo is part of the **DMF Platform**. GitHub is the canonical home and the
single source of truth: all changes land via **Pull Request** against `main`.
(The full pre-publish history is retained in a private read-only archive — it
is **not** an upstream and is never a contribution path.)

## Quick start

1. Read the platform overview in the **dmf-platform** umbrella repo
   (`docs/architecture/DMF Platform Plan.md`) and apply any relevant ADRs from
   `docs/decisions/INDEX.md`.
2. Fork or branch, make your change on a topic branch, open a PR against `main`.
3. Ensure CI is green and your commits are **signed off** (see DCO below).

## Branch & PR model

- **GitHub Pull Requests only.** Direct push to `main` is blocked; force-push is
  banned; linear history is required.
- Topic branches: **`<handle>/<short-slug>`** (e.g. `jdoe/fix-probe-path`). One
  logical change per branch; rebase onto `main` rather than long-lived branches.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`,
  `test:`, `build:`, `ci:`) are **required** on `main` and checked in CI. Other
  prefixes are rejected.
- A reviewer (per `CODEOWNERS`) and green required checks are needed to merge.

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
model is **ADR-0041 — DMF Release and Contribution Model** in the dmf-platform
umbrella repo (`docs/decisions/`).
