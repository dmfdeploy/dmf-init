# Security Policy

## Reporting a vulnerability

**Do not open a public issue, PR, or discussion for a security vulnerability.**

Report privately via GitHub's **[Report a vulnerability](https://github.com/dmfdeploy/dmf-init/security/advisories/new)**
(Security → Advisories → Report a vulnerability) on the `dmf-init` repository.
This opens a private advisory visible only to maintainers.

If you cannot use the GitHub flow, contact the maintainer listed in `CODEOWNERS`
through their GitHub profile and request a private channel — never include the
vulnerability details in a public message.

## What to include

- Affected repo, version (`VERSION` file), and commit/branch.
- A description of the issue and its impact.
- Steps to reproduce (a minimal PoC if possible).
- **Never include real secrets, credentials, cluster IPs/DNS, or operator
  identity** — use placeholders, exactly as in the rest of the project.

## Our commitment

- We acknowledge reports within **5 business days**.
- We work with you on a coordinated-disclosure timeline and credit you (if you wish)
  in the advisory and release notes.
- Fixes ship as a normal `vX.Y.Z` release; the advisory is published once a fix is
  available.

## Supported versions

This project is pre-1.0 (experiment phase). Only the latest `vX.Y.Z` release on
`main` is supported; please reproduce against the current tip before reporting.
