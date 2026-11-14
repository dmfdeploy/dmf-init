<!--
⚠️ NEVER paste secrets, credentials, real IPs/DNS, kubeconfigs, Terraform state,
or operator identity into this PR. Use placeholder syntax (<control-node-public-ip>,
dmf.example.com, <handle>). CI gitleaks/scrub will block a leak — but it is your
responsibility first.
-->

## What & why

<!-- One paragraph: what this changes and the motivation. -->

Refs dmfdeploy/dmfdeploy#NNN

<!-- Replace NNN with the issue number. Keep the full dmfdeploy/dmfdeploy prefix: CI rejects a bare #NNN. For a change with no issue, ask a maintainer for the `no-issue` label. -->

## Checklist

- [ ] Commits are **signed off** (`git commit -s`) — DCO check will verify.
- [ ] Commit messages follow **Conventional Commits** (`feat:`/`fix:`/`docs:`/…).
- [ ] `VERSION` bumped if this is a release-tagged change (ADR-0005); otherwise N/A.
- [ ] No secrets / real IPs / DNS / operator identity anywhere in the diff or this PR.
- [ ] CI is green (gitleaks, scrub, lint, commitlint where applicable).
- [ ] Docs/ADRs updated if behavior or decisions changed.
