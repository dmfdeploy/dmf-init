from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backup import BackupManifestMeta, backup
from .orchestrate import BootstrapRun, CheckpointStep, CommandStep, PauseStep, Step
from .repos import (
    DEFAULT_REFS,
    REPO_NAMES,
    RepoFetchRequest,
    RepoFetchResult,
    RepoProvenance,
    fetch_runtime_repos,
)
from .yaml_utils import parse_yaml_scalar_anywhere

logger = logging.getLogger("dmf_init.bootstrap_steps")


def ensure_runtime_repos(
    data_root: Path,
    request: RepoFetchRequest,
    *,
    fallback_username: str | None = None,
    fallback_password: str | None = None,
    fallback_base_url: str | None = None,
) -> RepoFetchResult:
    """P1-6: If ALL required repos are already present in data_root/repos,
    short-circuit by synthesizing provenance from their git HEAD shas.
    Otherwise, fall back to a full fetch from the provided base URL."""
    repos_root = data_root / "repos"
    # Check every required repo
    all_present = all(
        (repos_root / name / ".git").is_dir() or (repos_root / name / ".git").is_file()
        for name in REPO_NAMES
    )
    if all_present:
        provenance: list[RepoProvenance] = []
        for name in REPO_NAMES:
            repo_path = repos_root / name
            sha_result = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
            sha = sha_result.stdout.strip()
            # Best-effort source_url from git remote
            source_url = ""
            try:
                origin = subprocess.run(
                    ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                source_url = origin.stdout.strip()
            except subprocess.CalledProcessError:
                pass
            # P1: detect dirty/untracked state
            dirty = ""
            try:
                status = subprocess.run(
                    ["git", "-C", str(repo_path), "status", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                dirty = status.stdout.strip()
            except subprocess.CalledProcessError:
                pass
            provenance.append(
                RepoProvenance(
                    name=name,
                    ref=DEFAULT_REFS.get(name, "main"),
                    sha=sha,
                    source_url=source_url,
                    destination=str(repo_path),
                    dirty=dirty,
                )
            )
        provenance_path = data_root / "provenance" / "repos.json"
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {"repos": [item.model_dump() for item in provenance]},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        logger.info(
            "runtime repos already present — skipping fetch",
            extra={"event": "repo_fetch_skipped", "repo_count": len(provenance)},
        )
        return RepoFetchResult(
            provenance_path=str(provenance_path),
            repos=provenance,
        )

    # Fall back to full fetch
    base_url = (request.base_url or "").strip() or (fallback_base_url or "")
    if not base_url:
        raise ValueError("repo base URL is required")
    fetch_request = RepoFetchRequest(
        base_url=base_url,
        username=request.username or fallback_username,
        password=request.password or fallback_password,
        refs=request.refs,
    )
    return fetch_runtime_repos(data_root, fetch_request)


@dataclass(frozen=True)
class BootstrapRenderMeta:
    profile: str
    schema_version: str | int
    node_ip: str | None = None
    base_domain: str | None = None


@dataclass(frozen=True)
class BootstrapContext:
    data_root: Path
    env_id: str
    render_dir: Path
    env_dir: Path
    age_key_path: Path
    answers_file_path: Path
    repos_root: Path
    render_meta: BootstrapRenderMeta
    bootstrap_skip_tags: tuple[str, ...] = ()

    @classmethod
    def from_data_root(
        cls,
        data_root: Path,
        env_id: str,
        remotes: list | None = None,  # accepted for backward compat, ignored
    ) -> BootstrapContext:
        # P1-2: validate env_id BEFORE any path construction
        from .backup import validate_env_id
        validate_env_id(env_id)
        render_dir = data_root / "runs" / env_id
        render_json = render_dir / "render.json"
        env_dir = data_root / "envs" / env_id
        if not env_dir.is_dir():
            raise FileNotFoundError(f"env_dir not found for env_id={env_id}")
        if not render_json.is_file():
            raise FileNotFoundError(f"render metadata not found for env_id={env_id}")

        payload = json.loads(render_json.read_text(encoding="utf-8"))
        profile = payload.get("profile")
        schema_version = payload.get("schema_version")
        if profile is None or schema_version is None:
            raise ValueError(f"render metadata missing profile/schema_version for env_id={env_id}")

        age_key_path = Path(payload.get("age_key_path") or (render_dir / "age" / "keys.txt"))
        answers_file_path = Path(
            payload.get("answers_file_path") or (render_dir / "answers.yaml")
        )
        if not age_key_path.is_file():
            raise FileNotFoundError(f"age key not found for env_id={env_id}")
        if not answers_file_path.is_file():
            raise FileNotFoundError(f"answers file not found for env_id={env_id}")
        render_meta = BootstrapRenderMeta(
            profile=str(profile),
            schema_version=schema_version,
            node_ip=_nested_string(payload, "node_ip"),
            base_domain=_nested_string(payload, "base_domain"),
        )
        bootstrap_skip_tags = tuple(
            tag
            for tag in (
                os.getenv("DMF_BOOTSTRAP_SKIP_TAGS", "")
                .replace(",", " ")
                .split()
            )
            if tag
        )
        return cls(
            data_root=data_root,
            env_id=env_id,
            render_dir=render_dir,
            env_dir=env_dir,
            age_key_path=age_key_path,
            answers_file_path=answers_file_path,
            repos_root=data_root / "repos",
            render_meta=render_meta,
            bootstrap_skip_tags=bootstrap_skip_tags,
        )

    def command_env(self) -> dict[str, str]:
        env = {
            "DMF_DATA_ROOT": str(self.data_root),
            "SOPS_AGE_KEY_FILE": str(self.age_key_path),
            "NO_COLOR": "1",
            "TERM": "dumb",
            # run-playbook.sh defaults to a 900s cap, but the sandbox post-seed
            # (full app stack incl. AWX, which alone exceeds 900s to become
            # ready) needs longer. The constrained Pi 4 repeatedly exceeded 5400s
            # on configure — and configure is now longer with AWX scale-to-zero
            # default-on (a Phase-C wake adds ~7 min of operator reconcile plus
            # the Phase-A/E sleeps). The proven full-bootstrap run used 7200s, so
            # match it. (A ceiling — finishes when done; harmless for fast lanes
            # and for the non-playbook steps.)
            "RUNBOOK_TIMEOUT": "7200",
        }
        if self.bootstrap_skip_tags:
            env["DMF_BOOTSTRAP_SKIP_TAGS"] = ",".join(self.bootstrap_skip_tags)
        return env

    def run_playbook_argv(self, playbook_rel: str) -> list[str]:
        argv = [
            str(self.repos_root / "dmf-env" / "bin" / "run-playbook.sh"),
            self.env_id,
            str(self.repos_root / "dmf-infra" / "k3s-lab-bootstrap" / playbook_rel),
        ]
        if self.bootstrap_skip_tags:
            argv.extend(["--skip-tags", ",".join(self.bootstrap_skip_tags)])
        return argv


def _nested_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value
    for container_key in ("metadata", "sandbox"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            nested = container.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested
    return None


def _parse_inventory_ssh_target(hosts_ini: Path) -> str | None:
    if not hosts_ini.is_file():
        return None
    lines = hosts_ini.read_text(encoding="utf-8").splitlines()

    current_section: str | None = None
    control_host: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            continue
        if current_section == "k3s_control" and control_host is None:
            control_host = line.split()[0]
            break

    if not control_host:
        return None

    current_section = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            continue
        if current_section == "k3s" and line.startswith(control_host):
            user = ""
            ip = ""
            for token in line.split()[1:]:
                if token.startswith("ansible_host="):
                    ip = token.removeprefix("ansible_host=")
                elif token.startswith("ansible_user="):
                    user = token.removeprefix("ansible_user=")
            if user and ip:
                return f"{user}@{ip}"
    return None


def _expand_local_path(value: str) -> str:
    if value.startswith("~/"):
        return str(Path.home() / value[2:])
    return value


def _inventory_group_vars_dir(ctx: BootstrapContext) -> Path:
    return ctx.env_dir / "inventory" / "group_vars" / "all"


def _resolve_openbao_ssh(ctx: BootstrapContext) -> tuple[str | None, str | None]:
    group_vars_dir = _inventory_group_vars_dir(ctx)
    derived_key = parse_yaml_scalar_anywhere(group_vars_dir, "ansible_ssh_private_key_file")
    if derived_key:
        derived_key = _expand_local_path(derived_key)
    derived_target = _parse_inventory_ssh_target(ctx.env_dir / "inventory" / "hosts.ini")
    ssh_target = (os.getenv("OPENBAO_SSH_TARGET") or derived_target or "").strip() or None
    ssh_key = (os.getenv("OPENBAO_SSH_KEY") or derived_key or "").strip() or None
    return ssh_target, ssh_key


def _ssh_args(ctx: BootstrapContext) -> list[str]:
    ssh_target, ssh_key = _resolve_openbao_ssh(ctx)
    if not ssh_target:
        return []
    args = ["-o", "LogLevel=ERROR"]
    if ssh_key:
        args.extend(["-i", ssh_key])
    return args


def _run_openbao_ssh(
    ctx: BootstrapContext, remote_argv: list[str]
) -> subprocess.CompletedProcess[str]:
    ssh_target, _ssh_key = _resolve_openbao_ssh(ctx)
    if not ssh_target:
        raise RuntimeError("SSH target not resolvable from inventory")
    ssh_cmd = ["ssh", *_ssh_args(ctx), ssh_target, *remote_argv]
    return subprocess.run(
        ssh_cmd,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
    )


def _run_passkey_helper(ctx: BootstrapContext) -> subprocess.CompletedProcess[str]:
    script = ctx.repos_root / "dmf-env" / "bin" / "get-passkey-enrollment-url.sh"
    if not script.is_file():
        raise RuntimeError("passkey helper not found")
    env = os.environ.copy()
    env["DMF_DATA_ROOT"] = str(ctx.data_root)
    return subprocess.run(
        [str(script), ctx.env_id],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _command_step(ctx: BootstrapContext, step_id: str, argv: list[str]) -> CommandStep:
    return CommandStep(id=step_id, argv=argv, cwd=ctx.repos_root / "dmf-env", env=ctx.command_env())


# Only this profile is auto-unsealable: it uses a 1-of-1 Shamir key held in the
# env bundle (openbao-keys.json), so dmf-init can re-unseal unattended. A multi-
# share (e.g. Hetzner 3-of-5) env must be unsealed manually — never auto-unseal
# there. (ADR-0044 facet (d).)
SANDBOX_SINGLE_NODE_PROFILE = "sandbox-single-node"
BAO_PREFLIGHT_ID = "bao-preflight"


def supports_auto_unseal(profile: str) -> bool:
    return profile == SANDBOX_SINGLE_NODE_PROFILE


def _idempotent_unseal_argv(ctx: BootstrapContext, banner: str) -> list[str]:
    # unseal-openbao.sh exits 2 for "already unsealed" — map it to 0 so the step
    # is a safe no-op when OpenBao is already up. Any other non-zero stays fatal.
    return [
        "sh",
        "-c",
        f'echo "{banner}"; '
        'rc=0; "$0" "$@" || rc=$?; '
        'if [ "$rc" -eq 2 ]; then '
        'echo "[dmf-init] OpenBao already unsealed (exit 2 = ok)"; '
        'exit 0; fi; '
        'exit "$rc"',
        str(ctx.repos_root / "dmf-env" / "bin" / "unseal-openbao.sh"),
        ctx.env_id,
        "--yes",
    ]


def build_bao_preflight_step(ctx: BootstrapContext) -> CommandStep:
    """A loud, idempotent OpenBao unseal preflight (ADR-0044 facet (d)).

    Injected at the head of a retry slice that resumes *past* the unseal step
    (the env node may have rebooted, which re-seals OpenBao), so the Bao-dependent
    phases don't fail opaquely against a sealed OpenBao. Idempotent: no-ops when
    already unsealed. If it can't unseal (key absent / Bao uninitialised), the
    step errors legibly instead of letting `configure` fail mysteriously.
    """
    return CommandStep(
        id=BAO_PREFLIGHT_ID,
        argv=_idempotent_unseal_argv(
            ctx,
            "[dmf-init] bao-preflight: ensuring OpenBao is unsealed before "
            "resuming Bao-dependent phases (the env node may have rebooted)",
        ),
        cwd=ctx.repos_root / "dmf-env",
        env=ctx.command_env(),
    )


# OpenBao unseal keys (>=44 chars b64 / 64 hex) and root tokens (~30 chars) are
# all high-entropy. Guarding on a minimum length keeps every real secret while
# avoiding redacting short, low-entropy metadata (e.g. a "1" or a version tag)
# that could otherwise corrupt unrelated log lines.
_MIN_SECRET_LEN = 8


def _iter_secret_strings(value: Any, *, min_len: int = _MIN_SECRET_LEN) -> list[str]:
    secrets: list[str] = []
    if isinstance(value, str):
        return [value] if len(value) >= min_len else []
    if isinstance(value, dict):
        for item in value.values():
            secrets.extend(_iter_secret_strings(item, min_len=min_len))
        return secrets
    if isinstance(value, list | tuple | set):
        for item in value:
            secrets.extend(_iter_secret_strings(item, min_len=min_len))
    return secrets


def seed_openbao_redactions(run: BootstrapRun, ctx: BootstrapContext) -> None:
    """Seed run.secrets from on-disk openbao-keys.json if present.

    Called at checkpoint-2 (normal path) and by the retry endpoint before
    spawning a run that may slice past checkpoint-2. Without this, a retry
    starting after checkpoint-2 would stream OpenBao unseal key / root token
    unredacted in log events and the failure hint.
    """
    keys_path = ctx.env_dir / "openbao-keys.json"
    if not keys_path.is_file():
        return
    data = json.loads(keys_path.read_text(encoding="utf-8"))
    for secret in _iter_secret_strings(data):
        run.add_secret(secret)


def build_ca_cert_payload(ctx: BootstrapContext) -> dict[str, Any]:
    payload = {
        "filename": "dmf-ca.crt",
        "pem": "",
        "present": False,
        "note": "",
        "requirement_note": (
            "Trusting the DMF Local CA is strongly recommended: without it every "
            "cluster service shows certificate warnings, and on desktop "
            "Chrome/Chromium passkey enrollment fails silently — 'Registration "
            "cancelled or timed out' there means non-secure-context, NOT a real "
            "cancel. Other browsers (e.g. Safari/iOS) may allow enrollment after "
            "a certificate warning. To limit blast radius, trust the CA only in "
            "Firefox's own certificate store (Preferences > Privacy & Security > "
            "Certificates > View Certificates > Authorities > Import) or in a "
            "dedicated browser profile. To remove later: delete the 'DMF Local "
            "CA' entry from your certificate store."
        ),
    }
    try:
        proc = _run_openbao_ssh(
            ctx,
            [
                "sudo",
                "kubectl",
                "--kubeconfig",
                "/etc/rancher/k3s/k3s.yaml",
                "get",
                "secret",
                "-n",
                "cert-manager",
                "dmf-local-ca",
                "-o",
                "json",
            ],
        )
    except subprocess.TimeoutExpired:
        payload["note"] = "timed out fetching live CA cert from cert-manager/dmf-local-ca"
        return payload
    except Exception as exc:
        payload["note"] = f"could not fetch live CA cert: {exc}"
        return payload
    if proc.returncode != 0:
        payload["note"] = "could not fetch live CA cert from cert-manager/dmf-local-ca"
        return payload
    try:
        secret = json.loads(proc.stdout)
        encoded = secret["data"]["tls.crt"]
        pem = base64.b64decode(encoded).decode("utf-8").strip()
    except Exception as exc:
        payload["note"] = f"could not decode live CA cert: {exc}"
        return payload
    if not pem:
        payload["note"] = "empty CA cert returned from cert-manager/dmf-local-ca"
        return payload
    payload["pem"] = pem
    payload["present"] = True
    return payload


def build_hosts_map_payload(ctx: BootstrapContext) -> dict[str, Any]:
    node_ip = ctx.render_meta.node_ip
    base_domain = ctx.render_meta.base_domain
    if not node_ip or not base_domain:
        return {
            "entries": [],
            "node_ip": node_ip or "",
            "base_domain": base_domain or "",
            "note": "node_ip/base_domain unavailable in render metadata",
            "dns_note": "node_ip/base_domain unavailable in render metadata",
        }
    live_hosts: list[str] = []
    try:
        proc = _run_openbao_ssh(
            ctx,
            [
                "sudo",
                "kubectl",
                "--kubeconfig",
                "/etc/rancher/k3s/k3s.yaml",
                "get",
                "ingress,ingressroute",
                "-A",
                "-o",
                "json",
            ],
        )
    except subprocess.TimeoutExpired:
        proc = None
    except Exception:
        proc = None
    if proc and proc.returncode == 0 and proc.stdout.strip():
        host_pattern = re.compile(
            rf"(?<![A-Za-z0-9-])(?:[A-Za-z0-9-]+\.)*{re.escape(base_domain)}(?![A-Za-z0-9-])"
        )
        discovered = list(dict.fromkeys(host_pattern.findall(proc.stdout)))
        live_hosts = [host for host in discovered if host]
    if not live_hosts:
        live_hosts = [
            base_domain,
            f"console.{base_domain}",
            f"auth.{base_domain}",
            f"forgejo.{base_domain}",
            f"awx.{base_domain}",
            f"grafana.{base_domain}",
            f"netbox.{base_domain}",
            f"registry.{base_domain}",
        ]
    entries = [f"{node_ip} {host}" for host in live_hosts]
    return {
        "entries": entries,
        "node_ip": node_ip,
        "base_domain": base_domain,
        "note": "WebAuthn RP IDs and cluster hostnames must resolve to the node IP.",
        "dns_note": "WebAuthn RP IDs are bound to hostnames; add these entries before continuing.",
    }


def build_workstation_payload(ctx: BootstrapContext) -> dict[str, Any]:
    """Combined payload for the single 'prepare your workstation' pause.

    CA trust and hosts mapping are both local-workstation tasks with no
    server-side gate, so they pause the run once, not twice. The sub-payloads
    keep their existing shapes (the /api/ca-cert endpoint reuses the CA one).
    """
    return {
        "ca": build_ca_cert_payload(ctx),
        "hosts": build_hosts_map_payload(ctx),
    }


def build_passkey_payload(ctx: BootstrapContext) -> dict[str, Any]:
    payload = {
        "enrollment_url": "",
        "confirmed": 0,
        "required": 0,
        "hint": f"run get-passkey-enrollment-url.sh {ctx.env_id} on the host",
        "host_hint": f"run get-passkey-enrollment-url.sh {ctx.env_id} on the host",
    }
    try:
        proc = _run_passkey_helper(ctx)
    except subprocess.TimeoutExpired:
        payload["note"] = "timed out fetching live passkey status from helper script"
        return payload
    except Exception as exc:
        payload["note"] = f"could not fetch live passkey status: {exc}"
        return payload
    if proc.returncode != 0:
        payload["note"] = "could not fetch live passkey status from helper script"
        return payload

    enrollment_url = ""
    confirmed = 0
    required = 0
    for line in proc.stdout.splitlines():
        if line.startswith("enrollment_url:"):
            enrollment_url = line.split(":", 1)[1].strip()
        elif line.startswith("confirmed passkeys:"):
            match = re.search(r"confirmed passkeys:\s*(\d+)/(\d+)", line)
            if match:
                confirmed = int(match.group(1))
                required = int(match.group(2))
    payload["enrollment_url"] = enrollment_url
    payload["confirmed"] = confirmed
    payload["required"] = required
    return payload


# ADR-0044: the recovery bundle is the only cross-lifetime artifact, and it only
# counts once it has left tmpfs (been downloaded). Everything after checkpoint-2
# (unseal → seed-bao → post-seed → the long, unattended `configure`) can run for
# a long time on a constrained node; a crash before the operator has saved the
# checkpoint-2 bundle is unrecoverable. So we gate continuation past checkpoint-2
# on a *proven, current* download (enforced server-side in /api/bootstrap/resume).
CHECKPOINT_EXPORT_GATE_ID = "checkpoint-2-export-gate"


def build_checkpoint_export_gate_payload(ctx: BootstrapContext) -> dict[str, Any]:
    return {
        "gate": "checkpoint-export",
        "checkpoint": 2,
        "env_id": ctx.env_id,
        "message": (
            "Save your recovery bundle before continuing. The next phases run "
            "unattended and can take a long time on a constrained node — if this "
            "container is lost before you've downloaded the bundle, the run can't "
            "be recovered. Continue is enabled once the current bundle has saved."
        ),
    }


def build_bootstrap_steps(ctx: BootstrapContext) -> list[Step]:
    return [
        _command_step(
            ctx,
            "pre-seed",
            ctx.run_playbook_argv("bootstrap-sandbox-provision-pre-seed.yml"),
        ),
        CheckpointStep(id="checkpoint-2", n=2),
        # Forced export gate (ADR-0044): blocks unseal/seed-bao/post-seed/configure
        # until the checkpoint-2 bundle is proven downloaded off tmpfs.
        PauseStep(
            id=CHECKPOINT_EXPORT_GATE_ID,
            title="Save your recovery bundle",
            payload_fn=lambda run, ctx=ctx: build_checkpoint_export_gate_payload(ctx),
        ),
        _command_step(
            ctx,
            "unseal",
            # The sandbox profile's Tier-3 self-recovering unseal already unseals
            # OpenBao during pre-seed, so "already unsealed" (exit 2) is the NORMAL
            # path here; the shared idempotent wrapper maps it to 0 so the graph
            # doesn't halt. Any other non-zero stays fatal.
            _idempotent_unseal_argv(ctx, "[dmf-init] unseal"),
        ),
        _command_step(
            ctx,
            "seed-bao",
            [
                str(ctx.repos_root / "dmf-env" / "bin" / "bootstrap-secrets.sh"),
                "seed-bao",
                ctx.env_id,
            ],
        ),
        _command_step(
            ctx,
            "post-seed",
            ctx.run_playbook_argv("bootstrap-sandbox-provision-post-seed.yml"),
        ),
        _command_step(
            ctx,
            "configure",
            ctx.run_playbook_argv("bootstrap-sandbox-configure.yml"),
        ),
        PauseStep(
            id="workstation",
            title="Prepare your workstation",
            payload_fn=lambda run, ctx=ctx: build_workstation_payload(ctx),
        ),
        PauseStep(
            id="passkey",
            title="Complete passkey enrollment",
            payload_fn=lambda run, ctx=ctx: build_passkey_payload(ctx),
        ),
        _command_step(
            ctx,
            "verify",
            ctx.run_playbook_argv("bootstrap-sandbox-verify.yml"),
        ),
        CheckpointStep(id="checkpoint-3", n=3),
    ]


class CheckpointError(RuntimeError):
    pass


def make_checkpoint_fn(ctx: BootstrapContext):
    def checkpoint_fn(run: BootstrapRun, n: int) -> dict[str, Any]:
        if n == 2:
            # HARD FAIL if the keys file is absent. Proceeding would run `unseal`
            # / `seed-bao` with an empty redaction set and stream the OpenBao
            # unseal key + root token in clear. run_worker turns this into a
            # terminal `error` (the message names no secret), halting before
            # unseal. (qwen P1.)
            if not (ctx.env_dir / "openbao-keys.json").is_file():
                raise CheckpointError(
                    "openbao-keys.json missing at checkpoint #2 — refusing to "
                    "continue (would stream unredacted OpenBao secrets)"
                )
            seed_openbao_redactions(run, ctx)
        # The run owns the passphrase (wiped after #3); never fall back to a
        # second copy — fail loudly if it is unexpectedly unset. (qwen P2-1.)
        passphrase = run.passphrase
        if not passphrase:
            raise CheckpointError("passphrase unavailable for checkpoint backup")
        result = backup(
            env_dir=ctx.env_dir,
            age_key_path=ctx.age_key_path,
            answers_file=ctx.answers_file_path,
            passphrase=passphrase,
            manifest_meta=BackupManifestMeta(
                env_id=ctx.env_id,
                profile=ctx.render_meta.profile,
                schema_version=ctx.render_meta.schema_version,
                checkpoint=n,
            ),
        )
        # Persist artifact into data_root/artifacts/ for browser download
        artifacts_dir = ctx.data_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest = artifacts_dir / result.artifact_name
        shutil.copy2(result.artifact_path, dest)
        return {
            "artifact_name": result.artifact_name,
        }

    return checkpoint_fn
