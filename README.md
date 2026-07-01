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

### Quick start (launcher)

If you just want to get going, the `dmf-init` launcher wraps Docker for you — it
starts the container with the correct flags, waits until it's ready, extracts the
one-time launch link, and opens your browser. No clone required:

```bash
curl -fsSL https://raw.githubusercontent.com/dmfdeploy/dmf-init/main/bin/dmf-init | bash -s -- up
```

> Piping a script into your shell runs remote code — if you'd rather read it
> first (recommended), download then run:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/dmfdeploy/dmf-init/main/bin/dmf-init -o dmf-init
> less dmf-init && bash dmf-init up
> ```

From a clone, it's just `bin/dmf-init`:

```bash
bin/dmf-init up        # start + open the browser (prints the link too)
bin/dmf-init link      # re-mint the single-use link and reopen it
bin/dmf-init status    # is it running? show the current link
bin/dmf-init down      # stop and remove the container
```

Options for `up`: `--port N` (default 8000), `--tls` (HTTPS), `--image REF`,
`--repo-base-url URL` (private mirror). Run `bin/dmf-init --help` for details. The
launcher never weakens the security posture — it always publishes on `127.0.0.1`
only and mounts the tmpfs data root. The explicit `docker run` invocations below
remain the advanced/reference path (and are what the launcher runs under the hood).

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
docker run --rm -p 127.0.0.1:8000:8000 \
  --tmpfs /tmp/dmf-init-data \
  ghcr.io/dmfdeploy/dmf-init:latest
# open the http://localhost:8000/?token=... printed in the logs
```

> **`--tmpfs /tmp/dmf-init-data` is required** (ADR-0044): env secrets (age key,
> OpenBao keys) must stay in RAM and never touch host disk. dmf-init **refuses to
> start** if its data root isn't tmpfs-backed. For a throwaway dev run on a
> non-tmpfs root, override with `-e DMF_ALLOW_NON_TMPFS_DATA_ROOT=true` (dev/test
> only). `docker rm` stays safe because nothing durable is written to host disk.

Repos are fetched from the public org `https://github.com/dmfdeploy` by default —
no extra env needed. To pull from a private mirror, override the base URL (and
optionally supply credentials):

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  --tmpfs /tmp/dmf-init-data \
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
docker run --rm -p 127.0.0.1:8000:8000 --tmpfs /tmp/dmf-init-data dmf-init:bundle
# open the http://localhost:8000/?token=... printed in the logs
```

Loopback safety is the `-p 127.0.0.1:…` publish (never publish to a non-loopback
host interface). HTTPS is opt-in (`-e DMF_TLS_ENABLED=true`) for when you reach
it by a non-localhost address.

**Lost your session / launch link?** The session refreshes while you're active
and lasts well beyond a single run idle, but if it lapses (or you closed the tab)
the one-time launch link is spent. You don't need to restart — re-mint a fresh
link on the *running* container (preserving the in-progress run) by sending it
`SIGHUP`, then open the new link printed in the logs:

```bash
docker kill --signal=HUP <container>   # e.g. the name from `docker ps`
docker logs --tail 3 <container>       # copy the fresh open http://localhost:8000/?token=... line
```

Canonical spec:
[`DMF Init Bootstrap Container Plan 2026-06-02`](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/plans/DMF%20Init%20Bootstrap%20Container%20Plan%202026-06-02.md).

### Target node

The installer provisions a **node you bring** — an SSH-reachable **Debian 12 or
13, ARM64** host (a cloud VM, a spare box, a Raspberry Pi). Other distros and
architectures are untested for now. Your own workstation OS doesn't matter; the
installer runs in a browser. On the wizard's *Target node* step you supply:

- **Node IP** — an address reachable *from this installer container*. Not
  `localhost` / `127.0.0.1`: that resolves to the container itself, not your node.
- **Ansible user** — the SSH login on that node (`root`, or the image's default
  user such as `debian`).
- **Interface** — the node's primary network interface (`eth0`, `ens3`, …).

These fields are intentionally blank by default — fill them for *your* node.

> **macOS convenience (not the paved path):** `dmf-env/bin/recreate-sandbox-vm.sh`
> spins up a local Lima VM (whose user / interface are `lima` / `lima0`). It
> requires macOS + Lima; the bring-a-Debian-node path above works from any
> workstation.

## Dependencies

- [`dmf-env`](https://github.com/dmfdeploy/dmf-env) — env tooling (`init-wizard.sh`, `bin/` scripts) the container wraps.
- [`dmf-infra`](https://github.com/dmfdeploy/dmf-infra) — generic bootstrap playbooks/roles.
- [`dmf-runbooks`](https://github.com/dmfdeploy/dmf-runbooks) — catalog launcher (post-seed).

## License

Apache License, Version 2.0 — see [LICENSE](LICENSE).
Third-party components are listed in [NOTICE](NOTICE).
