# dmf-init

<!-- WORKING-MODEL-BLOCK-START — generated from umbrella docs/templates/working-model-block.md; do not edit copies, edit the template and run bin/check-working-model-sync.sh -->
## Working model (mandatory)

Canonical: [docs/WORKING-MODEL.md](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/WORKING-MODEL.md)
in the umbrella repo. The three rules that matter mid-task:

1. **Work starts at an issue** in the canonical backlog
   ([dmfdeploy/dmfdeploy issues](https://github.com/dmfdeploy/dmfdeploy/issues);
   milestone + `component:*`/`workstream:*` labels). Non-trivial work gets a
   plan doc in umbrella `docs/plans/` with `tracking_issue` frontmatter.
2. **The completing PR closes the issue and flips the plan frontmatter in the
   same change.** From a component repo, reference umbrella issues **fully
   qualified** — `Closes dmfdeploy/dmfdeploy#N`; bare `#N` targets the wrong repo.
3. **Never invent a local backlog** (TODO files, ad-hoc trackers). Issues =
   liveness; plan frontmatter = design state; ADRs = decisions (RFC in
   Discussions first); STATUS.md = cross-repo now.
<!-- WORKING-MODEL-BLOCK-END -->

## DMF Platform context — read first

This repo is a component of the **DMF Platform**, an umbrella workspace
checked out alongside this repo. Operators set `$DMFDEPLOY_UMBRELLA` to its
local path. Cross-cutting state (status, decisions, plans, skills) lives
there, not here.

Before any non-trivial change in this repo:

```bash
cd "$DMFDEPLOY_UMBRELLA"
git fetch && git pull
bin/generate-status.sh --no-fetch    # refreshes STATUS.md
```

Then read in order:
1. `dmfdeploy/STATUS.md` — what's happening across all repos right now
2. `dmfdeploy/CLAUDE.md` — full boot ritual + workspace map
3. `dmfdeploy/docs/decisions/INDEX.md` — ADRs applicable to your task
4. The most recent file under `dmfdeploy/docs/handoffs/`

If you change cross-repo state, update the `<!-- HUMAN-START -->` section of
`dmfdeploy/STATUS.md` before ending the session.

---

**Day-0 stateless init & bootstrap container.** A localhost-only web UI
(React + FastAPI, served over HTTPS for a secure context) that wraps `dmf-env`'s
`init-wizard.sh --non-interactive` and `bin/` scripts to take a cluster from zero
to verified, plus a passphrase-wrapped backup/restore lifecycle delivered as
browser downloads (restore via file upload) so the container can be deleted after
commissioning.

Canonical spec: `dmfdeploy/docs/plans/DMF Init Bootstrap Container Plan 2026-06-02.md`.

Non-negotiables specific to this repo:
- **Wrap, don't reimplement** the `dmf-env` bash. The answers-file is the shared
  CLI/web contract; generated secrets stay wizard-internal.
- **Stateless**: tmpfs scratch only; never persist env state or secrets to the
  host or to this repo.
- **Public-safe**: no IPs, operator identity, or credentials in tracked files.
- **Loopback-only exposure**; single-use launch token. The container binds
  `0.0.0.0` *inside its namespace* (so `docker run -p 127.0.0.1:8000:8000` works
  with no flags) — loopback safety is the `-p 127.0.0.1:…` publish, never a
  non-loopback host interface. Served as **http://localhost** by default (already
  a secure context, no cert warning); HTTPS is opt-in (`DMF_TLS_ENABLED=true`)
  for non-localhost access.
