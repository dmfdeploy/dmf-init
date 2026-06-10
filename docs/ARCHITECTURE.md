# dmf-init — architecture (scaffold)

> Canonical spec:
> [`DMF Init Bootstrap Container Plan 2026-06-02`](../../docs/plans/DMF%20Init%20Bootstrap%20Container%20Plan%202026-06-02.md)
> in the umbrella. This file is a short orientation; the plan is authoritative.

## One-paragraph summary

`dmf-init` is a **Day-0, stateless** Docker container. It exposes a
loopback-only web UI (React + FastAPI) that collects operator inputs, drives
`dmf-env`'s `init-wizard.sh --non-interactive` + `bin/` scripts to render an env
bundle and orchestrate the sandbox bootstrap, and pushes a **passphrase-wrapped
backup to two external (rclone) remotes**. After commissioning, the container is
disposable — the two encrypted backups + the operator passphrase are the only
durable artifacts.

## Layers

| Layer | Contents | Persistence |
|---|---|---|
| **Tool layer** (image, static) | OpenTofu, Ansible, sops, age, rclone, kubectl, jq, openssh, Python/FastAPI, built React assets | image |
| **Repo layer** (image, pinned) | `dmf-env` + `dmf-infra` + `dmf-runbooks` at a release tag, pulled at build time (never vendored into this repo) | image (tag == platform release) |
| **Runtime state** | env dir (`bundle.sops.yaml`, inventory, manifest, ssh keys, `openbao-keys`), age key, passphrase | **tmpfs only** — dies with the container |
| **Durable output** | passphrase-wrapped `*.tar.age` backups | two operator-chosen rclone remotes |

## Secrets model (see plan §"Answers-file contract" + §"Backup format")

```
Outer:  backup tarball ──age --passphrase (scrypt)──▶ OPERATOR PASSPHRASE   (the one human-held secret)
Inner:  ├─ bundle.sops.yaml   (age-keypair encrypted)
        ├─ the age PRIVATE KEY
        ├─ the answers-file that produced the bundle
        └─ inventory / manifest / ssh keys / openbao-keys
```

`passphrase → unlock backup → recover age key → age key decrypts the inner sops
bundle`. Two backups are for **redundancy/durability**; the **passphrase** is the
confidentiality boundary, held in a different trust domain (password manager /
memorized).

## Phasing

- **Phase 0** (in `dmf-env`): `--non-interactive` answers mode for
  `init-wizard.sh` + answers-file schema; interactive CLI stays first-class.
- **Phase 1** (this repo): image + FastAPI/React skeleton → render + dual-remote
  backup → full orchestration with streamed logs → Manage mode.
- **Phase 2**: cloud (Hetzner) profile + the OpenBao Shamir-distribution
  decision (new ADR).
- **Phase 3** (roadmap): hardware-key (YubiKey/FIDO2) recipient; native
  password-manager retrieval; multi-recipient backups.
