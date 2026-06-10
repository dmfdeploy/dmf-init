from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field
from pyrage import passphrase as pyrage_passphrase

from .repos import RepoProvenance

logger = logging.getLogger("dmf_init.backup")

BACKUP_FORMAT_VERSION = 1

# P0-2: env_id must be safe for path construction and filenames
_ENV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_env_id(env_id: str) -> None:
    """Reject env_id values that could escape data_root or produce unsafe paths."""
    if not _ENV_ID_RE.match(env_id):
        raise ValueError(
            f"env_id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}, got {env_id!r}"
        )


class BackupManifestMeta(BaseModel):
    env_id: str
    profile: str
    schema_version: str | int
    checkpoint: int | None = None


class RcloneRemoteSpec(BaseModel):
    name: str
    type: str
    options: dict[str, str] = Field(default_factory=dict)
    destination_prefix: str = ""


class BackupManifest(BaseModel):
    backup_format_version: int
    env_id: str
    profile: str
    schema_version: str | int
    checkpoint: int | None = None
    created_at: str
    repos: list[RepoProvenance] = Field(default_factory=list)
    age_recipient: str
    inner_sha256: str


class BackupRemoteStatus(BaseModel):
    name: str
    destination: str
    validated: bool
    uploaded: bool


class BackupResult(BaseModel):
    artifact_name: str
    scratch_dir: str
    artifact_path: str
    rclone_config_path: str
    manifest_path: str
    inner_sha256: str
    manifest: BackupManifest
    remote_statuses: list[BackupRemoteStatus]
    remote_artifacts: list[str]


class RestoreResult(BaseModel):
    source: str
    restore_root: str
    env_dir: str
    age_key_path: str
    answers_file_path: str
    manifest_path: str
    manifest: BackupManifest
    inner_sha256: str
    verified: bool

    def cleanup(self) -> None:
        # restore_root holds plaintext secrets (decrypted outer tar, the extracted
        # age private key, answers file). The caller MUST call this once it has
        # consumed age_key_path (e.g. exported SOPS_AGE_KEY_FILE + run doctor) and
        # relocated env_dir — it is on tmpfs, but never rely on container teardown
        # alone (a host-mounted data root would otherwise leave secret residue).
        shutil.rmtree(self.restore_root, ignore_errors=True)


class BackupError(RuntimeError):
    pass


class BackupDecryptError(BackupError):
    pass


class BackupIntegrityError(BackupError):
    pass


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _coerce_passphrase(passphrase: str | bytes) -> str:
    if isinstance(passphrase, bytes):
        return passphrase.decode("utf-8")
    return passphrase


def _repo_provenance_path(env_dir: Path) -> Path | None:
    # Expect $DMF_DATA_ROOT/envs/<env_id>/; the envs-parent check below also
    # guards short paths (Path.parent never raises — for /env it is "/").
    if env_dir.parent.name != "envs":
        return None
    return env_dir.parent.parent / "provenance" / "repos.json"


def _load_repo_provenance(env_dir: Path) -> list[RepoProvenance]:
    provenance_path = _repo_provenance_path(env_dir)
    if provenance_path is None or not provenance_path.exists():
        return []
    payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    repos = payload.get("repos", [])
    return [RepoProvenance.model_validate(item) for item in repos]


def _tar_add_path(tar: tarfile.TarFile, path: Path, *, arcname: str) -> None:
    stat_result = path.lstat()
    tarinfo = tar.gettarinfo(str(path), arcname=arcname)
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = ""
    tarinfo.gname = ""
    tarinfo.mtime = 0
    if path.is_dir():
        tarinfo.mode = stat_result.st_mode & 0o777
        tarinfo.type = tarfile.DIRTYPE
        tarinfo.size = 0
        tar.addfile(tarinfo)
        return
    if path.is_symlink():
        tarinfo.mode = 0o777
        tarinfo.type = tarfile.SYMTYPE
        tarinfo.linkname = os.readlink(path)
        tarinfo.size = 0
        tar.addfile(tarinfo)
        return
    tarinfo.mode = stat_result.st_mode & 0o777
    tarinfo.size = stat_result.st_size
    with path.open("rb") as stream:
        tar.addfile(tarinfo, stream)


def _build_inner_tar_bytes(env_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, files in os.walk(env_dir):
            dirs.sort()
            files.sort()
            root_path = Path(root)
            if root_path != env_dir:
                _tar_add_path(tar, root_path, arcname=root_path.relative_to(env_dir).as_posix())
            for name in dirs:
                path = root_path / name
                _tar_add_path(tar, path, arcname=path.relative_to(env_dir).as_posix())
            for name in files:
                path = root_path / name
                _tar_add_path(tar, path, arcname=path.relative_to(env_dir).as_posix())
    return buffer.getvalue()


def _build_outer_tar_bytes(
    inner_tar_bytes: bytes,
    *,
    age_key_path: Path,
    answers_file: Path,
    manifest: BackupManifest,
) -> bytes:
    buffer = io.BytesIO()
    manifest_bytes = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    entries = [
        ("MANIFEST.json", manifest_bytes),
        ("age.key", age_key_path.read_bytes()),
        (answers_file.name, answers_file.read_bytes()),
        ("env.tar", inner_tar_bytes),
    ]
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, payload in sorted(entries, key=lambda item: item[0]):
            tarinfo = tarfile.TarInfo(name)
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ""
            tarinfo.gname = ""
            tarinfo.mtime = 0
            tarinfo.mode = 0o600 if name in {"age.key", answers_file.name} else 0o644
            tarinfo.size = len(payload)
            tar.addfile(tarinfo, io.BytesIO(payload))
    return buffer.getvalue()


def _age_secret_key_line(age_key_path: Path) -> str | None:
    # age-keygen writes comment lines (# created:, # public key:) plus the
    # AGE-SECRET-KEY-1... line; pyrage's Identity.from_str wants only the key.
    for line in age_key_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("AGE-SECRET-KEY-"):
            return stripped
    return None


def _derive_age_recipient(age_key_path: Path) -> str:
    try:
        from pyrage import x25519
    except ImportError:
        pass
    else:
        secret = _age_secret_key_line(age_key_path)
        if secret is not None:
            return str(x25519.Identity.from_str(secret).to_public())

    result = subprocess.run(
        ["age-keygen", "-y", str(age_key_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    public_key = result.stdout.strip()
    if not public_key:
        raise BackupError("failed to derive age recipient")
    return public_key


def _write_rclone_config(remotes: list[RcloneRemoteSpec], config_dir: Path) -> Path:
    config_path = config_dir / "rclone.conf"
    lines: list[str] = []
    for remote in remotes:
        lines.append(f"[{remote.name}]")
        lines.append(f"type = {remote.type}")
        for key in sorted(remote.options):
            lines.append(f"{key} = {remote.options[key]}")
        lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path


def _remote_destination(remote: RcloneRemoteSpec, artifact_name: str) -> str:
    if remote.destination_prefix:
        prefix = remote.destination_prefix.strip("/")
        return f"{remote.name}:{prefix}/{artifact_name}"
    return f"{remote.name}:{artifact_name}"


def _run_rclone(args: list[str], *, config_path: Path) -> None:
    subprocess.run(
        ["rclone", "--config", str(config_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _validate_remote(
    *,
    remote: RcloneRemoteSpec,
    config_path: Path,
    artifact_name: str,
    work_dir: Path,
) -> None:
    probe_source = work_dir / "probe"
    probe_source.write_bytes(b"")
    destination = _remote_destination(remote, f"probe/{artifact_name}.probe")
    logger.info(
        "validating remote",
        extra={"event": "backup_remote_validate", "remote": remote.name},
    )
    # Prove write+OVERWRITE (a bare create can pass on append-only buckets), and
    # always attempt to remove the probe — even if a copy raises — so a failed
    # validation never leaves an orphan probe on the remote.
    try:
        _run_rclone(["copyto", str(probe_source), destination], config_path=config_path)
        _run_rclone(["copyto", str(probe_source), destination], config_path=config_path)
    finally:
        try:
            _run_rclone(["deletefile", destination], config_path=config_path)
        except subprocess.CalledProcessError:
            pass  # probe may not exist if the first copyto failed


def _copy_to_remote(
    *,
    remote: RcloneRemoteSpec,
    config_path: Path,
    artifact_path: Path,
    artifact_name: str,
) -> str:
    destination = _remote_destination(remote, artifact_name)
    _run_rclone(["copyto", str(artifact_path), destination], config_path=config_path)
    return destination


# age scrypt passphrase wrapping via pyrage (in-memory; no plaintext to disk, no
# pty). pyrage is a hard dependency (prebuilt aarch64 wheel baked into the image),
# so there is no CLI/tty fallback — a missing pyrage is a hard import-time failure,
# not a silent degrade to a fragile pty path.
def _encrypt_with_passphrase(plaintext: bytes, passphrase: str) -> bytes:
    return pyrage_passphrase.encrypt(plaintext, passphrase)


def _decrypt_with_passphrase(ciphertext: bytes, passphrase: str) -> bytes:
    try:
        return pyrage_passphrase.decrypt(ciphertext, passphrase)
    except Exception as exc:  # wrong passphrase / corrupt artifact
        raise BackupDecryptError(f"age decryption failed: {type(exc).__name__}") from exc


def _extract_tar_bytes(tar_bytes: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        tar.extractall(destination, filter="data")


def backup(
    env_dir: Path,
    age_key_path: Path,
    answers_file: Path,
    passphrase: str | bytes,
    *,
    remotes: list[RcloneRemoteSpec] | None = None,
    manifest_meta: BackupManifestMeta,
) -> BackupResult:
    remotes = remotes or []
    if not env_dir.is_dir():
        raise ValueError("env_dir must be a directory")
    if not age_key_path.is_file():
        raise ValueError("age_key_path must be a file")
    if not answers_file.is_file():
        raise ValueError("answers_file must be a file")

    passphrase_text = _coerce_passphrase(passphrase)
    work_dir = Path(tempfile.mkdtemp(prefix="dmf-backup-"))
    config_path = _write_rclone_config(remotes, work_dir) if remotes else None
    env_tar_bytes = _build_inner_tar_bytes(env_dir)
    inner_sha256 = hashlib.sha256(env_tar_bytes).hexdigest()
    manifest = BackupManifest(
        backup_format_version=BACKUP_FORMAT_VERSION,
        env_id=manifest_meta.env_id,
        profile=manifest_meta.profile,
        schema_version=manifest_meta.schema_version,
        checkpoint=manifest_meta.checkpoint,
        created_at=_utc_iso(),
        repos=_load_repo_provenance(env_dir),
        age_recipient=_derive_age_recipient(age_key_path),
        inner_sha256=inner_sha256,
    )
    # P0-2: validate env_id before using in artifact name
    validate_env_id(manifest_meta.env_id)
    outer_tar_bytes = _build_outer_tar_bytes(
        env_tar_bytes,
        age_key_path=age_key_path,
        answers_file=answers_file,
        manifest=manifest,
    )
    artifact_name = f"dmf-backup-{manifest_meta.env_id}-{_utc_stamp()}.tar.age"
    artifact_path = work_dir / artifact_name
    artifact_path.write_bytes(_encrypt_with_passphrase(outer_tar_bytes, passphrase_text))

    remote_statuses: list[BackupRemoteStatus] = []
    remote_artifacts: list[str] = []
    if remotes and config_path:
        for remote in remotes:
            _validate_remote(
                remote=remote,
                config_path=config_path,
                artifact_name=artifact_name,
                work_dir=work_dir,
            )
        for remote in remotes:
            destination = _copy_to_remote(
                remote=remote,
                config_path=config_path,
                artifact_path=artifact_path,
                artifact_name=artifact_name,
            )
            remote_statuses.append(
                BackupRemoteStatus(
                    name=remote.name,
                    destination=destination,
                    validated=True,
                    uploaded=True,
                )
            )
            remote_artifacts.append(destination)

    manifest_path = work_dir / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = BackupResult(
        artifact_name=artifact_name,
        scratch_dir=str(work_dir),
        artifact_path=str(artifact_path),
        rclone_config_path=str(config_path),
        manifest_path=str(manifest_path),
        inner_sha256=inner_sha256,
        manifest=manifest,
        remote_statuses=remote_statuses,
        remote_artifacts=remote_artifacts,
    )
    logger.info(
        "backup complete",
        extra={
            "event": "backup_complete",
            "env_id": manifest_meta.env_id,
            "artifact_name": artifact_name,
            "remotes": [remote.name for remote in remotes],
        },
    )
    return result


def _download_remote_artifact(source: str, destination: Path, *, config_path: Path) -> None:
    _run_rclone(
        ["copyto", source, str(destination)],
        config_path=config_path,
    )


def restore(
    backup_artifact_or_remote: str | Path,
    passphrase: str | bytes,
    dest_dir: Path,
    *,
    rclone_config_path: Path | None = None,
) -> RestoreResult:
    passphrase_text = _coerce_passphrase(passphrase)
    dest_dir.mkdir(parents=True, exist_ok=True)
    restore_root = Path(tempfile.mkdtemp(prefix="dmf-restore-", dir=dest_dir.parent))
    encrypted_path = restore_root / "backup.tar.age"
    source_text = str(backup_artifact_or_remote)
    if Path(source_text).exists():
        encrypted_source = Path(source_text)
        encrypted_path.write_bytes(encrypted_source.read_bytes())
        source_label = str(encrypted_source)
    elif ":" in source_text:
        source_label = source_text
        if rclone_config_path is None:
            raise BackupError("remote restore requires rclone_config_path")
        _download_remote_artifact(source_label, encrypted_path, config_path=rclone_config_path)
    else:
        raise BackupError("backup_artifact_or_remote must be a file path or remote:path")

    outer_tar_path = restore_root / "backup.tar"
    outer_tar_path.write_bytes(
        _decrypt_with_passphrase(encrypted_path.read_bytes(), passphrase_text)
    )
    staging_dir = restore_root / "staging"
    _extract_tar_bytes(outer_tar_path.read_bytes(), staging_dir)
    manifest_path = staging_dir / "MANIFEST.json"
    env_tar_path = staging_dir / "env.tar"
    age_key_path = staging_dir / "age.key"
    extra = [
        name
        for name in os.listdir(staging_dir)
        if name not in {"MANIFEST.json", "env.tar", "age.key"}
    ]
    if len(extra) != 1:
        raise BackupIntegrityError(
            f"expected exactly one answers file in backup, found {len(extra)}: {extra!r}"
        )
    answers_file_path = staging_dir / extra[0]
    manifest = BackupManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    actual_inner_sha256 = hashlib.sha256(env_tar_path.read_bytes()).hexdigest()
    if actual_inner_sha256 != manifest.inner_sha256:
        raise BackupIntegrityError("inner payload sha256 mismatch")
    env_restore_dir = dest_dir / "env"
    if env_restore_dir.exists():
        shutil.rmtree(env_restore_dir)
    _extract_tar_bytes(env_tar_path.read_bytes(), env_restore_dir)
    result = RestoreResult(
        source=source_label,
        restore_root=str(restore_root),
        env_dir=str(env_restore_dir),
        age_key_path=str(age_key_path),
        answers_file_path=str(answers_file_path),
        manifest_path=str(manifest_path),
        manifest=manifest,
        inner_sha256=actual_inner_sha256,
        verified=True,
    )
    logger.info(
        "restore complete",
        extra={
            "event": "backup_restore_complete",
            "source": source_label,
            "env_id": manifest.env_id,
        },
    )
    return result
