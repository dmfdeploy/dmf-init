# Sandbox E2E Harness

Phase 1 host launcher for a fresh `dmf-init` sandbox validation run.

Run from [`dmf-init/test/e2e`](./):

```bash
./e2e.sh --no-passkeys
```

What it does:

- `preflight` checks Lima, `uv`, the local `dmf-init` import, the 6 repo targets, and Playwright when passkeys are enabled.
- `reset` recreates the sandbox VM, captures the new bridged IP, wipes the tmpfs data roots, and rebuilds `/tmp/dmf-init-reposrc`.
- `bootstrap` launches `dmf-init`, drives `/api/repos/fetch` → `/api/render` → `/api/backup` → `/api/bootstrap/start`, and auto-resumes pauses.
- `verify` runs `bootstrap-sandbox-verify.yml` through `dmf-env/bin/run-playbook.sh`, then applies the monitoring SSH gate.

Flags:

- `--no-passkeys` skips the Phase 2 browser flow and tells verify to skip only the passkey-count assert (`verify-d8-passkeys`).
- `--keep` leaves the local `dmf-init` process up after bootstrap for manual inspection.
- `--only reset|bootstrap|verify` runs a single stage after preflight.

Artifacts:

- Run logs live under `/tmp/sandbox-e2e/runs/<timestamp>/`.
- State shared across stages is written to `state.env` in the run directory.
- `summary.txt` gives the one-screen result block for the run.
