from __future__ import annotations

import json
import logging
import secrets
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from .backup import (
    BackupManifest,
    RestoreResult,
    restore,
    validate_env_id,
)
from .bootstrap_steps import _iter_secret_strings
from .orchestrate import BootstrapRun, CommandExecutor, CommandStep, SubprocessExecutor
from .repos import RepoProvenance
from .yaml_utils import rewrite_yaml_scalar

logger = logging.getLogger("dmf_init.manage")


class ManageError(RuntimeError):
    pass


class ManageRestoreRequest(BaseModel):
    """Restore from a local artifact path (uploaded by the operator)."""
    artifact_path: Path
    passphrase: str = Field(min_length=1)


class ManageRestoreResult(BaseModel):
    session_id: str
    env_id: str
    profile: str
    schema_version: str | int
    checkpoint: int | None
    repos: list[RepoProvenance]
    age_key_path: str
    answers_file_path: str
    render_dir: str
    verified: bool


class ManageDoctorRequest(BaseModel):
    session_id: str = Field(min_length=1)


@dataclass
class ManageSession:
    session_id: str
    env_id: str
    manifest: BackupManifest
    restored_artifact_name: str
    age_key_path: Path
    answers_file_path: Path
    render_dir: Path
    passphrase: str | None
    last_checkpoint: int
    created_at: float
    finished_at: float | None = None
    terminal: bool = False

    def __repr__(self) -> str:
        return (
            f"ManageSession(session_id={self.session_id!r}, env_id={self.env_id!r}, "
            f"terminal={self.terminal!r})"
        )

    __str__ = __repr__

    def wipe_secrets(self) -> None:
        self.passphrase = None


class DataRootNotTmpfsError(RuntimeError):
    """Raised when DMF_DATA_ROOT is not tmpfs-backed and enforcement is on."""


def data_root_fs_type(data_root: Path) -> str | None:
    """Return the backing filesystem type for ``data_root``.

    ``None`` when it can't be determined (non-Linux, no ``/proc/mounts``, or no
    matching mount) — callers treat that as "can't prove non-tmpfs", so they
    neither warn nor enforce. On Linux it returns the fs type of the longest
    matching mount point (e.g. ``tmpfs``, ``overlay``, ``ext4``).
    """
    if not sys.platform.startswith("linux"):
        return None
    mounts_path = Path("/proc/mounts")
    if not mounts_path.exists():
        return None

    mounts: list[tuple[Path, str]] = []
    try:
        for raw_line in mounts_path.read_text(encoding="utf-8").splitlines():
            fields = raw_line.split()
            if len(fields) < 3:
                continue
            mount_point = Path(fields[1].replace("\\040", " "))
            mounts.append((mount_point, fields[2]))
    except OSError:
        return None

    resolved = data_root.resolve()
    for mount_point, fs_type in sorted(mounts, key=lambda item: len(str(item[0])), reverse=True):
        try:
            resolved.relative_to(mount_point.resolve())
        except ValueError:
            continue
        return fs_type
    return None


def assert_data_root_tmpfs(data_root: Path, *, enforce: bool) -> None:
    """Check that ``data_root`` is RAM-backed (tmpfs/ramfs).

    ADR-0044: env secrets must never touch host disk. Policy:

    - tmpfs/ramfs → OK.
    - a determinable non-tmpfs fs (overlay/ext4/…) → fail.
    - **Linux but undeterminable** (no ``/proc/mounts``, read error, no matching
      mount) → **fail** when enforcing: on the Linux appliance we must not
      silently proceed when we can't prove RAM-backing (fail-closed, codex P2.1).
    - **non-Linux** → allow: we can't introspect mounts on dev machines, and the
      appliance is always Linux.

    On a fail, ``enforce`` raises ``DataRootNotTmpfsError`` (used at startup so the
    appliance refuses to run); otherwise it warns.
    """
    fs_type = data_root_fs_type(data_root)
    if fs_type in {"tmpfs", "ramfs"}:
        return
    if fs_type is None:
        if not sys.platform.startswith("linux"):
            return  # can't introspect off-Linux; the real appliance is Linux
        reason = f"could not determine the filesystem backing DMF_DATA_ROOT ({data_root})"
    else:
        reason = f"DMF_DATA_ROOT ({data_root}) is on '{fs_type}', not tmpfs"

    if enforce:
        raise DataRootNotTmpfsError(
            f"{reason} — refusing to start so env secrets never touch host disk "
            "(ADR-0044). Mount it as tmpfs (docker: --tmpfs /tmp/dmf-init-data), or "
            "set DMF_ALLOW_NON_TMPFS_DATA_ROOT=true to override (dev/test only)."
        )
    logger.warning(
        "data root is not tmpfs-backed",
        extra={
            "event": "data_root_not_tmpfs",
            "data_root": str(data_root),
            "filesystem": fs_type or "unknown",
        },
    )


def _canonical_manage_paths(data_root: Path, env_id: str) -> tuple[Path, Path, Path]:
    env_dir = data_root / "envs" / env_id
    render_dir = data_root / "runs" / env_id
    age_key_path = render_dir / "age" / "keys.txt"
    answers_file_path = render_dir / "answers.yaml"
    return env_dir, age_key_path, answers_file_path


def _write_manage_render_json(
    *,
    render_dir: Path,
    manifest: BackupManifest,
    age_key_path: Path,
    answers_file_path: Path,
) -> None:
    render_dir.mkdir(parents=True, exist_ok=True)
    render_json = render_dir / "render.json"
    render_json.write_text(
        json.dumps(
            {
                "env_id": manifest.env_id,
                "profile": manifest.profile,
                "schema_version": manifest.schema_version,
                "age_key_path": str(age_key_path),
                "answers_file_path": str(answers_file_path),
                "node_ip": None,
                "base_domain": None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    render_json.chmod(0o600)


def run_manage_restore(
    data_root: Path, request: ManageRestoreRequest, *, enforce_tmpfs: bool = False
) -> tuple[ManageSession, ManageRestoreResult]:
    data_root.mkdir(parents=True, exist_ok=True)
    # Defense in depth: startup already fail-closes in the real runtime, so a
    # restore would only reach here on tmpfs. Restore is where secrets are
    # written, so re-check (enforced when the caller passes the runtime setting).
    assert_data_root_tmpfs(data_root, enforce=enforce_tmpfs)

    dest_dir = Path(tempfile.mkdtemp(prefix="manage-restore-", dir=data_root))
    restore_result: RestoreResult | None = None
    try:
        restore_result = restore(
            request.artifact_path,
            request.passphrase,
            dest_dir,
        )
        env_id = restore_result.manifest.env_id
        # P0-2: validate env_id from uploaded artifact BEFORE any path construction
        validate_env_id(env_id)
        env_dir, age_key_path, answers_file_path = _canonical_manage_paths(data_root, env_id)
        render_dir = data_root / "runs" / env_id

        if env_dir.exists():
            shutil.rmtree(env_dir)
        if render_dir.exists():
            shutil.rmtree(render_dir)
        shutil.copytree(Path(restore_result.env_dir), env_dir)

        age_key_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(restore_result.age_key_path), age_key_path)
        age_key_path.chmod(0o600)

        answers_file_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(restore_result.answers_file_path), answers_file_path)
        answers_file_path.chmod(0o600)
        env_ssh_key_path = env_dir / "ssh" / "sandbox-node.key"
        rewrite_yaml_scalar(
            env_dir / "inventory" / "group_vars" / "all" / "main.yml",
            "ansible_ssh_private_key_file",
            str(env_ssh_key_path),
        )
        rewrite_yaml_scalar(
            answers_file_path,
            "ssh_private_key_path",
            str(env_ssh_key_path),
        )

        _write_manage_render_json(
            render_dir=render_dir,
            manifest=restore_result.manifest,
            age_key_path=age_key_path,
            answers_file_path=answers_file_path,
        )

        session_id = secrets.token_urlsafe(8)
        session = ManageSession(
            session_id=session_id,
            env_id=env_id,
            manifest=restore_result.manifest,
            restored_artifact_name=request.artifact_path.name,
            age_key_path=age_key_path,
            answers_file_path=answers_file_path,
            render_dir=render_dir,
            passphrase=request.passphrase,
            last_checkpoint=restore_result.manifest.checkpoint or 0,
            created_at=time.time(),
        )
        result = ManageRestoreResult(
            session_id=session_id,
            env_id=restore_result.manifest.env_id,
            profile=restore_result.manifest.profile,
            schema_version=restore_result.manifest.schema_version,
            checkpoint=restore_result.manifest.checkpoint,
            repos=restore_result.manifest.repos,
            age_key_path=str(age_key_path),
            answers_file_path=str(answers_file_path),
            render_dir=str(render_dir),
            verified=restore_result.verified,
        )
        return session, result
    finally:
        if restore_result is not None:
            restore_result.cleanup()
        shutil.rmtree(dest_dir, ignore_errors=True)


def build_env_doctor_run(
    env_id: str,
    age_key_path: Path,
    data_root: Path,
    *,
    executor: CommandExecutor | None = None,
) -> BootstrapRun:
    """Build a read-only `doctor` run for an env that already lives in tmpfs.

    Shared by the Manage flow (restored session) and the create flow (the
    env rendered in this same container). Only needs env_id + its age key.
    """
    bin_path = data_root / "repos" / "dmf-env" / "bin" / "bootstrap-secrets.sh"
    if not bin_path.is_file():
        raise ManageError("repos must be fetched first")

    doctor_step = CommandStep(
        id="doctor",
        argv=[str(bin_path), "doctor", env_id],
        cwd=data_root / "repos" / "dmf-env",
        env={
            "DMF_DATA_ROOT": str(data_root),
            "SOPS_AGE_KEY_FILE": str(age_key_path),
            "NO_COLOR": "1",
            "TERM": "dumb",
        },
    )
    run = BootstrapRun(
        run_id=secrets.token_urlsafe(8),
        steps=[doctor_step],
        executor=executor or SubprocessExecutor(),
    )

    openbao_keys_path = data_root / "envs" / env_id / "openbao-keys.json"
    if openbao_keys_path.is_file():
        with openbao_keys_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        for secret in _iter_secret_strings(payload):
            run.add_secret(secret)
    return run


def build_doctor_run(
    session: ManageSession,
    data_root: Path,
    *,
    executor: CommandExecutor | None = None,
) -> BootstrapRun:
    return build_env_doctor_run(
        session.env_id,
        session.age_key_path,
        data_root,
        executor=executor,
    )
