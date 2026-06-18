# dmf-init

**Day-0 self-contained init & bootstrap container** for the DMF Platform.

> **New to the project vocabulary?** See the [DMF Glossary](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/GLOSSARY.md) for project-coined terms (appliance, checkpoint, age key, answers-file, …).

A single container that puts a friendly, localhost-only web UI on the existing
[`dmf-env`](https://github.com/dmfdeploy/dmf-env) env-creation + `bin/`
bootstrap toolchain, drives a cluster from zero to verified, and offers a
**passphrase-wrapped backup as a browser download** at each checkpoint — so the
operator can commission a cluster, save the backup wherever they choose, and
then delete the container with nothing left behind.
Reached as `http://localhost`, which is a browser **secure context** (clipboard
+ WebAuthn work) with no certificate warning; HTTPS is opt-in for non-localhost
access.

> **What you get (the deliverable):** a **commissioned, verified cluster** plus
> one or more **passphrase-wrapped encrypted backups** (browser downloads). The
> container is **disposable / run-once** — delete it when you're done and the
> backups + your passphrase are all that remain. It does **not** build or emit a
> deployable image; it *commissions a cluster*.

Architecture orientation: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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

### Prerequisites

A **BuildKit-capable Docker** is mandatory — the `Dockerfile` pins
`# syntax=docker/dockerfile:1.7` and `bin/build-bundle.sh` forces
`DOCKER_BUILDKIT=1`, so classic builders will fail. **Docker 23.0+** is a
sensible minimum.

### Choose your path

Pick based on whether the host has **runtime network access**:

| | Networked host | Air-gapped host |
|---|---|---|
| **When** | Host can reach the internet at runtime | Host has no runtime network |
| **Image** | `ghcr.io/dmfdeploy/dmf-init:latest` | `dmf-init:bundle` |
| **Repos** | Fetched at startup | Baked into the tarball |

**Networked** (canonical ghcr image — simplest path):

```bash
docker run --rm -p 127.0.0.1:8000:8000 ghcr.io/dmfdeploy/dmf-init:latest
# open the http://localhost:8000/?token=... printed in the logs
```

Repos are fetched from the public org `https://github.com/dmfdeploy` by default —
no extra env needed. To pull from a private mirror, override the base URL (and
optionally supply credentials):

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  -e DMF_REPO_BASE_URL=https://git.example.com/dmf \
  ghcr.io/dmfdeploy/dmf-init:latest
```

Multi-arch (amd64 + arm64), published by
[`.github/workflows/publish-image.yml`](.github/workflows/publish-image.yml).

**Air-gapped** (bundle — repos baked in via `bin/build-bundle.sh`):

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
[`DMF Init Bootstrap Container Plan 2026-06-02`](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/plans/DMF%20Init%20Bootstrap%20Container%20Plan%202026-06-02.md).

## Dependencies

- [`dmf-env`](https://github.com/dmfdeploy/dmf-env) — env tooling (`init-wizard.sh`, `bin/` scripts) the container wraps.
- [`dmf-infra`](https://github.com/dmfdeploy/dmf-infra) — generic bootstrap playbooks/roles.
- [`dmf-runbooks`](https://github.com/dmfdeploy/dmf-runbooks) — catalog launcher (post-seed).

## License

Apache License, Version 2.0 — see [LICENSE](LICENSE).
Third-party components are listed in [NOTICE](NOTICE).
