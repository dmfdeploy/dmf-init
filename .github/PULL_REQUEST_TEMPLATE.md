<!--
⚠️ NEVER paste secrets, credentials, real IPs/DNS, kubeconfigs, Terraform state,
or operator identity into this PR. Use placeholder syntax (<control-node-public-ip>,
dmf.example.com, <handle>). CI gitleaks/scrub will block a leak — but it is your
responsibility first.
-->

## What & why

<!-- One paragraph: what this changes and the motivation. Link issues with #NNN. -->

## Checklist

- [ ] Commits are **signed off** (`git commit -s`) — DCO check will verify.
- [ ] Commit messages follow **Conventional Commits** (`feat:`/`fix:`/`docs:`/…).
- [ ] `VERSION` bumped if this is a release-tagged change (ADR-0005); otherwise N/A.
- [ ] No secrets / real IPs / DNS / operator identity anywhere in the diff or this PR.
- [ ] CI is green (gitleaks, scrub, lint, commitlint where applicable).
- [ ] Docs/ADRs updated if behavior or decisions changed.
