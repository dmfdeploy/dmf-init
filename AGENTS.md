# AGENTS.md — dmf-init

The Day-0, stateless init and bootstrap container of the dmfdeploy platform
(React, FastAPI). A localhost-only web UI wraps `dmf-env`'s `init-wizard.sh
--non-interactive` and `bin/` scripts to take a cluster from zero to verified,
with passphrase-wrapped backup and restore, so you can delete the container
afterward. Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Canonical spec: the [DMF Init Bootstrap Container Plan](https://github.com/dmfdeploy/dmfdeploy/blob/45eb326e973b10e0b52deef290981ceac9bcf5bd/docs/plans/DMF%20Init%20Bootstrap%20Container%20Plan%202026-06-02.md).

## Boot steps

In the umbrella repo ([dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy),
a sibling checkout), run `git pull` and `bin/generate-status.sh`, then read
`STATUS.digest.local.md` and skim `docs/decisions/INDEX.md`. Before you stop,
record shared-state changes in the pinned [Operator notes issue](https://github.com/dmfdeploy/dmfdeploy/issues/618).

## Build and test

- Python 3.12+: `pip install .[dev]`, then `ruff check .` and `pytest`. Backup
  and restore tests need `age` and `rclone`.
- Frontend: `cd frontend && npm ci && npm run build`.
- Image: `docker build -t dmf-init:ci .` (run modes: `README.md`).

## Rules that are easy to miss

- Wrap the `dmf-env` bash; never reimplement it. The answers file is the shared
  CLI and web contract, and generated secrets stay inside the wizard.
- Keep the container stateless: tmpfs scratch only; never persist env state or
  secrets to the host or to this repo. Keep tracked files public-safe: no IPs,
  operator identity or credentials.
- Expose the web UI on loopback only, and keep the single-use launch token.
  The container binds `0.0.0.0` inside its own namespace, so loopback safety
  comes from publishing with `-p 127.0.0.1:…`. Never publish on a non-loopback
  host interface.
- The UI serves `http://localhost` by default, already a secure context. Enable
  HTTPS (`DMF_TLS_ENABLED=true`) only for non-localhost access.

<!-- WORKING-MODEL-BLOCK-START — generated from umbrella docs/templates/working-model-block.md; do not edit copies, edit the template and run bin/check-working-model-sync.sh -->
## Working model (mandatory)

Canonical: [docs/WORKING-MODEL.md](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/WORKING-MODEL.md)
in the umbrella repo. The three rules that matter mid-task:

1. **Work starts at an issue** in the canonical backlog
   ([dmfdeploy/dmfdeploy issues](https://github.com/dmfdeploy/dmfdeploy/issues)):
   `component:*`/`workstream:*` labels **always**, plus a milestone **only if
   the work is scheduled** — unscheduled work gets `platform-debt` and no
   milestone (§2). Non-trivial work gets a plan doc in umbrella `docs/plans/`
   with `tracking_issue` frontmatter.
2. **The completing PR auto-closes its issue; you still flip the plan
   frontmatter by hand in that PR.** Reference umbrella issues **fully
   qualified** — `Closes dmfdeploy/dmfdeploy#N` (bare `#N` targets the wrong
   repo); the daily issue-close reconciler honors that ref, cross-repo
   included. Manual close is a fallback.
3. **Never invent a local backlog** (TODO files, ad-hoc trackers). Issues =
   liveness; plan frontmatter = design state; ADRs = decisions (RFC in
   Discussions first); STATUS.md = pointer file (operator notes live in pinned issue dmfdeploy/dmfdeploy#618);
   STATUS.local.md = live repo snapshot.
<!-- WORKING-MODEL-BLOCK-END -->
