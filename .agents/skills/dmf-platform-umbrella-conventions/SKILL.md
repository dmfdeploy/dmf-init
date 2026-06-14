---
name: dmf-platform-umbrella-conventions
description: DMF Platform umbrella workspace conventions, boot ritual, and cross-repo state rules
source: auto-skill
extracted_at: '2026-06-10T13:00:00.000Z'
---

# DMF Platform Umbrella Conventions

## Context

This repo (`dmf-init`) is a component of the **DMF Platform**, an umbrella workspace with multiple component repos checked out alongside it. Operators set `$DMFDEPLOY_UMBRELLA` to its local path.

## Before any non-trivial change

```bash
cd "$DMFDEPLOY_UMBRELLA"
git fetch && git pull
bin/generate-status.sh --no-fetch    # refreshes STATUS.md
```

Then read in order:
1. `dmfdeploy/STATUS.md` — what's happening across all repos right now
2. `dmfdeploy/QWEN.md` — full boot ritual + skills index + Qwen-specific rules
3. `dmfdeploy/docs/decisions/INDEX.md` — ADRs applicable to your task
4. The most recent file under `dmfdeploy/docs/handoffs/`

## Cross-repo state

If you change cross-repo state, update the `<!-- HUMAN-START -->` section of `dmfdeploy/STATUS.md` before ending the session.

## Plans-as-docs convention

Plans live in `docs/plans/` and follow the pattern: `<DMF Title> Plan YYYY-MM-DD.md`. They are treated as project docs, not ephemeral notes.

## Component repos

- `dmf-init` — Day-0 stateless init & bootstrap (this repo)
- `dmf-env` — environment management and wizard
- `dmf-cms` — content management
- `dmf-pipeline` — data pipeline
- Other component repos as enumerated in the umbrella

## Public-safety rules

- **No IPs, operator identity, or credentials** in tracked files
- **Loopback-only** web bind; single-use launch token
- Identity references use placeholders: `<lan-forgejo-host>`, `dmf.example.com`, `<handle>`
- `bin/scrub-public-repos.sh --tree . --strict` must be clean before any public export
