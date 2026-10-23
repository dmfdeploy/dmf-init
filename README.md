# dmf-init

[![CI](https://github.com/dmfdeploy/dmf-init/actions/workflows/ci.yml/badge.svg)](https://github.com/dmfdeploy/dmf-init/actions/workflows/ci.yml)

**Day-0 installer container** that commissions a dmfdeploy platform cluster on a node you bring, through a localhost-only web UI.

Part of [dmfdeploy](https://github.com/dmfdeploy/dmfdeploy), an independent open prototype that explores the ideas of the EBU Dynamic Media Facility on commodity infrastructure. Status: prototype, single-node sandbox.

> **New to the project vocabulary?** See the [DMF Glossary](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/GLOSSARY.md) for project-coined terms (appliance, checkpoint, age key, answers-file, …).

A single container puts a friendly, localhost-only web UI on the existing
[`dmf-env`](https://github.com/dmfdeploy/dmf-env) env-creation and `bin/`
bootstrap toolchain. It drives a cluster from zero to verified. At each
checkpoint it offers a **passphrase-wrapped backup as a browser download**. You
can commission a cluster, save the backup wherever you choose, and then delete
the container with nothing left behind.
Reached as `http://localhost`, which is a browser **secure context** (clipboard
+ WebAuthn work) with no certificate warning; HTTPS is opt-in for non-localhost
access.

To see what a commissioned cluster does next, an 81-second recording of one
media workload being provisioned through the console, with a plain account of
what it does and does not show, is on the
[organization page](https://github.com/dmfdeploy).

> **What you get (the deliverable):** a **commissioned, verified cluster** plus
> one or more **passphrase-wrapped encrypted backups** (browser downloads). The
> container is **disposable / run-once**; delete it when you're done and the
> backups + your passphrase are all that remain. It does **not** build or emit a
> deployable image; it *commissions a cluster*.

Architecture orientation: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## What it does

| Mode | Purpose |
|---|---|
| **Create new** | Collect inputs, render the env bundle, seal a downloadable passphrase-wrapped backup (checkpoint #1), then orchestrate the full sandbox bootstrap (pre-seed → seed → post-seed → configure → verify) with live logs and per-checkpoint downloads. |
| **Manage** | Restore an env by uploading a previously downloaded encrypted backup (passphrase-unwrapped), then re-run playbooks, upgrade, rotate, or tear down, offering a fresh downloadable backup on any change. |

## Design principles

- **Stateless.** All env state lives in a tmpfs scratch root; nothing durable is
  written to the host. After commissioning, the downloaded encrypted backups and
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

### Quick start (launcher)

To get started, the `dmf-init` launcher wraps Docker for you; it
starts the container with the correct flags, waits until it's ready, extracts the
one-time launch link, and opens your browser. No clone required:

Read the launcher before you run it. Download it, read it, then run it:

```bash
curl -fsSL https://raw.githubusercontent.com/dmfdeploy/dmf-init/main/bin/dmf-init -o dmf-init
less dmf-init
bash dmf-init up
```

If you trust the source, you can pipe the launcher into your shell instead. This runs remote code without a chance to read it first:

```bash
curl -fsSL https://raw.githubusercontent.com/dmfdeploy/dmf-init/main/bin/dmf-init | bash -s -- up
```

From a clone, run `bin/dmf-init`:

```bash
bin/dmf-init up        # start + open the browser (prints the link too)
bin/dmf-init link      # re-mint the single-use link and reopen it
bin/dmf-init status    # is it running? show the current link
bin/dmf-init down      # stop and remove the container
```

Options for `up`: `--port N` (default 8000), `--tls` (HTTPS), `--image REF`,
`--repo-base-url URL` (private mirror). Run `bin/dmf-init --help` for details. The
launcher never weakens the security posture; it always publishes on `127.0.0.1`
only and mounts the tmpfs data root. The explicit `docker run` invocations below
remain the advanced/reference path (and are what the launcher runs under the hood).

### Prerequisites

A **BuildKit-capable Docker** is mandatory; the `Dockerfile` pins
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

**Networked** (canonical ghcr image: simplest path):

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  --tmpfs /tmp/dmf-init-data:exec \
  ghcr.io/dmfdeploy/dmf-init:latest
# open the http://localhost:8000/?token=... printed in the logs
```

> **`--tmpfs /tmp/dmf-init-data:exec` is required** (ADR-0044): env secrets (age
> key, OpenBao keys) must stay in RAM and never touch host disk. The `:exec`
> mount option is needed because the data root holds and runs the fetched wizard
> toolchain; without it Docker's default `noexec` mount flag blocks script
> execution (rc 126). The mount is still tmpfs, so ADR-0044 (RAM-only secrets)
> holds. dmf-init **refuses to start** if its data root isn't tmpfs-backed or is
> mounted `noexec`. For a throwaway dev run on a non-tmpfs root, override with
> `-e DMF_ALLOW_NON_TMPFS_DATA_ROOT=true` (dev/test only). `docker rm` stays
> safe because nothing durable is written to host disk.

Repos are fetched from the public org `https://github.com/dmfdeploy` by default;
no extra env needed. To pull from a private mirror, override the base URL (and
optionally supply credentials):

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  --tmpfs /tmp/dmf-init-data:exec \
  -e DMF_REPO_BASE_URL=https://git.example.com/dmf \
  ghcr.io/dmfdeploy/dmf-init:latest
```

Multi-arch (amd64 + arm64), published by
[`.github/workflows/publish-image.yml`](.github/workflows/publish-image.yml).

**Air-gapped** (bundle: repos baked in via `bin/build-bundle.sh`):

```bash
# on a machine with Docker + the umbrella checked out:
bin/build-bundle.sh                 # → dmf-init-bundle-<version>.tar.gz

# copy that tarball to the clean host, then there:
docker load -i dmf-init-bundle-<version>.tar.gz
docker run --rm -p 127.0.0.1:8000:8000 --tmpfs /tmp/dmf-init-data:exec dmf-init:bundle
# open the http://localhost:8000/?token=... printed in the logs
```

Loopback safety is the `-p 127.0.0.1:…` publish (never publish to a non-loopback
host interface). HTTPS is opt-in (`-e DMF_TLS_ENABLED=true`) for when you reach
it by a non-localhost address.

**Lost your session / launch link?** The session refreshes while you're active
and lasts well beyond a single run idle, but if it lapses (or you closed the tab)
the one-time launch link is spent. You don't need to restart; re-mint a fresh
link on the *running* container (preserving the in-progress run) by sending it
`SIGHUP`, then open the new link printed in the logs:

```bash
docker kill --signal=HUP <container>   # e.g. the name from `docker ps`
docker logs --tail 3 <container>       # copy the fresh open http://localhost:8000/?token=... line
```

Canonical spec:
[`DMF Init Bootstrap Container Plan 2026-06-02`](https://github.com/dmfdeploy/dmfdeploy/blob/45eb326e973b10e0b52deef290981ceac9bcf5bd/docs/plans/DMF%20Init%20Bootstrap%20Container%20Plan%202026-06-02.md).

### Target node

The installer provisions a **node you bring**: an SSH-reachable **Debian, ARM64**
host (a cloud VM, a spare box, a Raspberry Pi). The installer does not check the
Debian version itself. v0.1 is tested on **Debian 13 (Trixie)**, as
[`dmf-infra`](https://github.com/dmfdeploy/dmf-infra) documents. Its baseline
playbook also handles Debian 12 (Bookworm). Other distros and architectures are
untested for now. The node needs 4 vCPU, 16 GB RAM, and 60 GB disk at minimum.
The full stack uses about 7 GB RAM and 30 GB disk at idle. Your own workstation OS doesn't matter; the
installer runs in a browser. On the wizard's *Target node* step you supply:

- **Node IP**: an address reachable *from this installer container*. Do not use
  `localhost` or `127.0.0.1`, which resolve to the container itself.
- **Ansible user**: the SSH login on that node (`root`, or the image's default
  user such as `debian`).
- **Interface**: the node's primary network interface (`eth0`, `ens3`, …).

These fields are intentionally blank by default; fill them for *your* node.

> **macOS convenience (not the paved path):** `dmf-env/bin/recreate-sandbox-vm.sh`
> spins up a local Lima VM (whose user / interface are `lima` / `lima0`). It
> requires macOS + Lima; the bring-a-Debian-node path above works from any
> workstation.

## Dependencies

- [`dmf-env`](https://github.com/dmfdeploy/dmf-env): env tooling (`init-wizard.sh`, `bin/` scripts) the container wraps.
- [`dmf-infra`](https://github.com/dmfdeploy/dmf-infra): generic bootstrap playbooks/roles.
- [`dmf-runbooks`](https://github.com/dmfdeploy/dmf-runbooks): catalog launcher (post-seed).

## For contributors

`AGENTS.md`, `CLAUDE.md` and `QWEN.md` hold instructions for AI coding agents that help maintain this repository. Human contributors can skip them and start with [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License, Version 2.0. See see [LICENSE](LICENSE).
Third-party components are listed in [NOTICE](NOTICE).
