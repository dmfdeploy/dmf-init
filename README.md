# dmf-init

**Day-0 self-contained init & bootstrap container** for the DMF Platform.

A single container that puts a friendly, localhost-only web UI on the existing
`dmf-env` env-creation + `bin/` bootstrap toolchain, drives a cluster from zero
to verified, and offers a **passphrase-wrapped backup as a browser download** at
each checkpoint — so the operator can commission a cluster, save the backup
wherever they choose, and then delete the container with nothing left behind.
Reached as `http://localhost`, which is a browser **secure context** (clipboard
+ WebAuthn work) with no certificate warning; HTTPS is opt-in for non-localhost
access.

## What it does

| Mode | Purpose |
|---|---|
| **Create new** | Collect inputs, render the env bundle, seal a downloadable passphrase-wrapped backup (checkpoint #1), then orchestrate the full sandbox bootstrap (pre-seed → seed → post-seed → configure → verify) with live logs and per-checkpoint downloads. |
| **Manage** | Restore an env by uploading a previously downloaded encrypted backup (passphrase-unwrapped), then re-run playbooks, upgrade, rotate, or tear down — offering a fresh downloadable backup on any change. |

## Design principles

- **Stateless.** All env state lives in a tmpfs scratch root; nothing durable is
  written to the host. After commissioning, the downloaded encrypted backup(s) +
  the operator passphrase are the only artifacts that matter.
- **Passphrase wraps the age key.** The age private key rides inside the backup;
  the backup is encrypted by an operator passphrase (the single human-held
  secret). The operator keeps copies of the download for redundancy; the
  passphrase = confidentiality.
- **Wraps, does not reimplement.** The container drives `dmf-env`'s
  `init-wizard.sh --non-interactive` and `bin/` scripts; the bash remains the
  single source of truth and a first-class CLI path.
- **Sandbox profile first** (ADR-0031). Cloud (Hetzner) is a later phase.

## Run it (appliance)

The canonical image (`Dockerfile`) is public-safe and fetches the runtime repos
at startup. For a **clean host with no source checkout** (pre-release testing),
build a self-contained bundle that bakes the 6 runtime repos in, ships as a
tarball, and runs with one command:

```bash
# on a machine with Docker + the umbrella checked out:
bin/build-bundle.sh                 # → dmf-init-bundle-<version>.tar.gz

# copy that tarball to the clean host, then there:
docker load -i dmf-init-bundle-<version>.tar.gz
docker run --rm -p 127.0.0.1:8000:8000 dmf-init:bundle
# open the http://localhost:8000/?token=... printed in the logs
```

Loopback safety is the `-p 127.0.0.1:…` publish (never publish to a non-loopback
host interface). HTTPS is opt-in (`-e DMF_TLS_ENABLED=true`) for when you reach
it by a non-localhost address. Canonical spec:
[`DMF Init Bootstrap Container Plan 2026-06-02`](../docs/plans/DMF%20Init%20Bootstrap%20Container%20Plan%202026-06-02.md).

## Dependencies

- `dmf-env` — env tooling (`init-wizard.sh`, `bin/` scripts) the container wraps.
- `dmf-infra` — generic bootstrap playbooks/roles.
- `dmf-runbooks` — catalog launcher (post-seed).

## License

Apache License, Version 2.0 — see [LICENSE](LICENSE).
Third-party components are listed in [NOTICE](NOTICE).
