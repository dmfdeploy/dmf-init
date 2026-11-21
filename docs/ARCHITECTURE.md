# Architecture (scaffold)

> Canonical spec:
> [`DMF Init Bootstrap Container Plan 2026-06-02`](https://github.com/dmfdeploy/dmfdeploy/blob/45eb326e973b10e0b52deef290981ceac9bcf5bd/docs/plans/DMF%20Init%20Bootstrap%20Container%20Plan%202026-06-02.md)
> in the umbrella. This file is a short orientation; the plan is authoritative.

## One-paragraph summary

`dmf-init` is a **Day-0, stateless** Docker container. It exposes a
loopback-only web UI (React + FastAPI) that collects operator inputs, drives
`dmf-env`'s `init-wizard.sh --non-interactive` + `bin/` scripts to render an env
bundle and orchestrate the sandbox bootstrap, and seals a **passphrase-wrapped
backup that you download in the browser**. The container never uploads a backup anywhere. After commissioning,
the container is disposable. The downloaded encrypted backups + the operator
passphrase are the only durable artifacts.

## Layers

| Layer | Contents | Persistence |
|---|---|---|
| **Tool layer** (image, static) | OpenTofu, Ansible, sops, age, kubectl, helm, jq, openssh, Python/FastAPI, built React assets | image |
| **Repo layer** (runtime fetch) | Six platform repos (`dmf-env`, `dmf-infra`, `dmf-runbooks`, `dmf-cms`, `dmf-media`, `dmf-promsd`), shallow-cloned from the configured base URL at a ref you can set per repo (default `main`); never vendored into this repo or the image | tmpfs only |
| **Runtime state** | env dir (`bundle.sops.yaml`, inventory, manifest, ssh keys, `openbao-keys`), age key, passphrase | **tmpfs only**; dies with the container |
| **Durable output** | passphrase-wrapped `*.tar.age` backups | browser downloads you save yourself (checkpoint backups, recovery package) |

## Secrets model

For details, see the plan sections on the answers-file contract and the backup
format.

```
Outer:  backup tarball ──age --passphrase (scrypt)──▶ OPERATOR PASSPHRASE   (the one human-held secret)
Inner:  ├─ bundle.sops.yaml   (age-keypair encrypted)
        ├─ the age PRIVATE KEY
        ├─ the answers-file that produced the bundle
        └─ inventory / manifest / ssh keys / openbao-keys
```

`passphrase → unlock backup → recover age key → age key decrypts the inner sops
bundle`. The run seals a new backup at each checkpoint (1, 2, and 3), and the
latest one supersedes the earlier ones. The **passphrase** is the
confidentiality boundary, held in a different trust domain (password manager /
memorized).

## Phasing

- **Phase 0** (in `dmf-env`): `--non-interactive` answers mode for
  `init-wizard.sh` + answers-file schema; interactive command-line mode stays
  first-class.
- **Phase 1** (this repo): image + FastAPI/React skeleton → render + downloadable
  backup → full orchestration with streamed logs → Manage mode.
- **Phase 2**: cloud (Hetzner) profile + the OpenBao Shamir-distribution
  decision (new ADR).
- **Phase 3** (roadmap): hardware-key (YubiKey/FIDO2) recipient; native
  password-manager retrieval; multi-recipient backups.
