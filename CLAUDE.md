# CLAUDE.md — dmf-init

Project guidance for every agent lives in [AGENTS.md](AGENTS.md). Read it first.

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
