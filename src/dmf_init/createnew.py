from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from .backup import BackupManifestMeta, BackupResult, backup
from .redaction import redact_text
from .yaml_utils import parse_yaml_scalar_anywhere, rewrite_yaml_scalar, yaml_scalar

logger = logging.getLogger("dmf_init.createnew")

SANDBOX_PROFILE = "sandbox-single-node"
ANSWER_SCHEMA_VERSION = 1
WIZARD_NAME = "init-wizard.sh"


class OperatorInputs(BaseModel):
    username: str
    email: str
    display: str | None = None


class SandboxInputs(BaseModel):
    label: str = ""
    node_ip: str
    ansible_user: str
    iface: str
    ssh_private_key: str = Field(min_length=1)


class CreateNewRenderRequest(BaseModel):
    operator: OperatorInputs
    sandbox: SandboxInputs


class CreateNewBackupRequest(BaseModel):
    env_id: str
    passphrase: str
    passphrase_confirm: str


class CreateNewRenderResult(BaseModel):
    env_id: str
    render_dir: str
    age_key_path: str
    answers_file_path: str
    ssh_private_key_path: str
    metadata_path: str
    render_log_path: str
    profile: str = SANDBOX_PROFILE
    schema_version: int = ANSWER_SCHEMA_VERSION
    output_lines: list[str] = Field(default_factory=list)


class CreateNewBackupResponse(BaseModel):
    env_id: str
    backup: BackupResult


class CreateNewError(RuntimeError):
    pass


@dataclass
class _CreateNewState:
    data_root: Path
    work_dir: Path
    render_request: CreateNewRenderRequest
    env_id: str | None = None
    render_dir: Path | None = None
    age_key_path: Path | None = None
    answers_file_path: Path | None = None
    ssh_private_key_path: Path | None = None
    metadata_path: Path | None = None
    render_log_path: Path | None = None
    output_lines: list[str] = field(default_factory=list)
    error: str | None = None


_ENV_ID_PATTERN = re.compile(r"^\s*env_id(?:\s+\([^)]+\))?:\s*(\S+)\s*$")


def _require_runtime_repo(data_root: Path) -> Path:
    wizard_path = data_root / "repos" / "dmf-env" / "bin" / WIZARD_NAME
    if not wizard_path.is_file():
        raise CreateNewError("repos must be fetched first")
    return wizard_path


def _answers_file_contents(request: CreateNewRenderRequest, ssh_key_path: Path) -> str:
    operator_display = request.operator.display or request.operator.username
    sandbox = request.sandbox
    # The answers schema's ssh_private_key_path is a PATH: the operator-supplied
    # key CONTENTS were written to ssh_key_path (tmpfs 0600); the wizard reads that
    # file and owns the base64 encoding. (Passing the PEM here makes the wizard's
    # validate_absolute_path reject it — see the dmf-env answers-file schema.)
    lines = [
        "schema_version: 1",
        "provider: sandbox",
        "operator:",
        f"  username: {yaml_scalar(request.operator.username)}",
        f"  email: {yaml_scalar(request.operator.email)}",
        f"  display: {yaml_scalar(operator_display)}",
        "sandbox:",
        f"  label: {yaml_scalar(sandbox.label)}",
        f"  node_ip: {yaml_scalar(sandbox.node_ip)}",
        f"  ansible_user: {yaml_scalar(sandbox.ansible_user)}",
        f"  iface: {yaml_scalar(sandbox.iface)}",
        f"  ssh_private_key_path: {yaml_scalar(str(ssh_key_path))}",
    ]
    return "\n".join(lines) + "\n"


def _sandbox_env_dir(data_root: Path, env_id: str) -> Path:
    return data_root / "envs" / env_id


def _sandbox_ssh_private_key_path(data_root: Path, env_id: str) -> Path:
    return _sandbox_env_dir(data_root, env_id) / "ssh" / "sandbox-node.key"


def _sandbox_ssh_public_key_path(private_key_path: Path) -> Path:
    return private_key_path.with_name(private_key_path.name + ".pub")


def _render_base_domain(data_root: Path, env_id: str) -> str | None:
    group_vars = data_root / "envs" / env_id / "inventory" / "group_vars" / "all"
    return (
        parse_yaml_scalar_anywhere(group_vars, "dmf_sandbox_base_domain")
        or parse_yaml_scalar_anywhere(group_vars, "cert_manager_cluster_domain")
    )


def _materialize_sandbox_ssh_artifacts(data_root: Path, env_id: str, ssh_private_key: str) -> Path:
    private_key_path = _sandbox_ssh_private_key_path(data_root, env_id)
    public_key_path = _sandbox_ssh_public_key_path(private_key_path)
    _write_file(private_key_path, ssh_private_key)
    generated_pub = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(private_key_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_file(public_key_path, generated_pub.stdout, mode=0o644)
    return private_key_path


def _finalize_sandbox_render_artifacts(
    *,
    data_root: Path,
    env_id: str,
    request: CreateNewRenderRequest,
    work_dir: Path,
    answers_file_path: Path,
) -> Path:
    private_key_path = _materialize_sandbox_ssh_artifacts(
        data_root,
        env_id,
        request.sandbox.ssh_private_key,
    )
    _write_file(
        answers_file_path,
        _answers_file_contents(request, private_key_path),
    )
    rewrite_yaml_scalar(
        _sandbox_env_dir(data_root, env_id) / "inventory" / "group_vars" / "all" / "main.yml",
        "ansible_ssh_private_key_file",
        str(private_key_path),
    )
    shutil.rmtree(work_dir / "ssh", ignore_errors=True)
    return private_key_path


def _prepare_work_dir(data_root: Path) -> Path:
    runs_root = data_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="create-new-", dir=runs_root))


def _write_file(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if content and not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _generate_age_key(age_key_path: Path) -> None:
    age_key_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["age-keygen", "-o", str(age_key_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    age_key_path.chmod(0o600)


def _parse_env_id(line: str) -> str | None:
    match = _ENV_ID_PATTERN.match(line)
    return match.group(1) if match else None


def _write_render_metadata(state: _CreateNewState) -> Path:
    assert state.render_dir is not None
    assert state.env_id is not None
    assert state.age_key_path is not None
    assert state.answers_file_path is not None
    assert state.ssh_private_key_path is not None
    base_domain = _render_base_domain(state.data_root, state.env_id)
    metadata = {
        "env_id": state.env_id,
        "profile": SANDBOX_PROFILE,
        "schema_version": ANSWER_SCHEMA_VERSION,
        "render_dir": str(state.render_dir),
        "age_key_path": str(state.age_key_path),
        "answers_file_path": str(state.answers_file_path),
        "ssh_private_key_path": str(state.ssh_private_key_path),
        "node_ip": state.render_request.sandbox.node_ip,
        "base_domain": base_domain,
        "render_log_path": str(state.render_log_path) if state.render_log_path else "",
    }
    metadata_path = state.render_dir / "render.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    metadata_path.chmod(0o600)
    return metadata_path


def _finalise_render_state(state: _CreateNewState) -> CreateNewRenderResult:
    assert state.env_id is not None
    assert state.render_dir is not None
    state.metadata_path = _write_render_metadata(state)
    return CreateNewRenderResult(
        env_id=state.env_id,
        render_dir=str(state.render_dir),
        age_key_path=str(state.age_key_path),
        answers_file_path=str(state.answers_file_path),
        ssh_private_key_path=str(state.ssh_private_key_path),
        metadata_path=str(state.metadata_path),
        render_log_path=str(state.render_log_path),
        output_lines=state.output_lines,
    )


def run_render_create_new(
    data_root: Path, request: CreateNewRenderRequest
) -> CreateNewRenderResult:
    state = _CreateNewState(
        data_root=data_root,
        work_dir=_prepare_work_dir(data_root),
        render_request=request,
    )
    wizard_path = _require_runtime_repo(data_root)
    state.render_log_path = state.work_dir / "render.log"
    state.age_key_path = state.work_dir / "age" / "keys.txt"
    state.answers_file_path = state.work_dir / "answers.yaml"
    state.ssh_private_key_path = state.work_dir / "ssh" / "sandbox-node.key"
    _generate_age_key(state.age_key_path)
    _write_file(state.ssh_private_key_path, request.sandbox.ssh_private_key)
    key_error = _validate_ssh_private_key(state.ssh_private_key_path)
    if key_error:
        yield json.dumps({"event": "error", "error": key_error}, separators=(",", ":")) + "\n"
        return
    _write_file(
        state.answers_file_path,
        _answers_file_contents(request, state.ssh_private_key_path),
    )

    wizard_env = os.environ.copy()
    wizard_env["DMF_DATA_ROOT"] = str(data_root)
    wizard_env["SOPS_AGE_KEY_FILE"] = str(state.age_key_path)
    wizard_env["NO_COLOR"] = "1"
    wizard_env["TERM"] = "dumb"

    preexisting_envs = {
        entry.name
        for entry in (data_root / "envs").iterdir()
        if entry.is_dir()
    } if (data_root / "envs").exists() else set()

    proc = subprocess.Popen(
        [str(wizard_path), "--non-interactive", str(state.answers_file_path)],
        cwd=wizard_path.parent,
        env=wizard_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    with state.render_log_path.open("w", encoding="utf-8") as log_file:
        for raw_line in proc.stdout:
            log_file.write(raw_line)
            log_file.flush()
            redacted = redact_text(
                raw_line.rstrip("\n"),
                [
                    request.operator.username,
                    request.operator.email,
                    request.operator.display,
                    request.sandbox.ssh_private_key,
                ],
            )
            if redacted:
                state.output_lines.append(redacted)
            env_id = _parse_env_id(raw_line)
            if env_id and state.env_id is None:
                state.env_id = env_id
        return_code = proc.wait()

    if return_code != 0:
        state.error = f"init-wizard failed with exit code {return_code}"
        raise CreateNewError(state.error)

    if state.env_id is None:
        current_envs = {
            entry.name
            for entry in (data_root / "envs").iterdir()
            if entry.is_dir()
        } if (data_root / "envs").exists() else set()
        created_envs = sorted(current_envs - preexisting_envs)
        if len(created_envs) == 1:
            state.env_id = created_envs[0]
        else:
            raise CreateNewError("could not determine env_id from init-wizard output")

    # P1-2: validate env_id BEFORE any path construction
    from .backup import validate_env_id
    validate_env_id(state.env_id)

    final_render_dir = data_root / "runs" / state.env_id
    state.ssh_private_key_path = _finalize_sandbox_render_artifacts(
        data_root=data_root,
        env_id=state.env_id,
        request=request,
        work_dir=state.work_dir,
        answers_file_path=state.answers_file_path,
    )
    if final_render_dir.exists():
        shutil.rmtree(final_render_dir)
    shutil.move(str(state.work_dir), str(final_render_dir))
    state.render_dir = final_render_dir
    state.age_key_path = final_render_dir / "age" / "keys.txt"
    state.answers_file_path = final_render_dir / "answers.yaml"
    state.render_log_path = final_render_dir / "render.log"
    return _finalise_render_state(state)


def _validate_ssh_private_key(path: Path) -> str | None:
    """Cheap fail-fast parse check (ssh-keygen -y) before the wizard runs.

    Returns an operator-actionable error message, or None if the key is
    usable. A bad key otherwise only explodes minutes later when the public
    key is derived during finalize."""
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # ssh-keygen missing/hung — treat as unusable
        return f"could not validate the SSH private key: {exc}"
    if proc.returncode != 0:
        return (
            "The SSH private key could not be parsed (or it is "
            "passphrase-protected). Paste an unencrypted private key in "
            "OpenSSH or PEM format."
        )
    return None


def stream_render_create_new(
    data_root: Path,
    request: CreateNewRenderRequest,
) -> Iterator[str]:
    """Stream the render as NDJSON. Never lets an exception escape: any
    failure becomes an {"event": "error"} line, so the browser always gets a
    clean terminal event instead of a dropped connection."""
    try:
        yield from _stream_render_create_new_inner(data_root, request)
    except CreateNewError as exc:
        yield json.dumps({"event": "error", "error": str(exc)}, separators=(",", ":")) + "\n"
    except Exception:
        logger.exception("create-new render stream failed unexpectedly")
        yield json.dumps(
            {
                "event": "error",
                "error": "render failed unexpectedly — the container log has details",
            },
            separators=(",", ":"),
        ) + "\n"


def _stream_render_create_new_inner(
    data_root: Path,
    request: CreateNewRenderRequest,
) -> Iterator[str]:
    state = _CreateNewState(
        data_root=data_root,
        work_dir=_prepare_work_dir(data_root),
        render_request=request,
    )
    wizard_path = _require_runtime_repo(data_root)
    state.render_log_path = state.work_dir / "render.log"
    state.age_key_path = state.work_dir / "age" / "keys.txt"
    state.answers_file_path = state.work_dir / "answers.yaml"
    state.ssh_private_key_path = state.work_dir / "ssh" / "sandbox-node.key"
    _generate_age_key(state.age_key_path)
    _write_file(state.ssh_private_key_path, request.sandbox.ssh_private_key)
    key_error = _validate_ssh_private_key(state.ssh_private_key_path)
    if key_error:
        yield json.dumps({"event": "error", "error": key_error}, separators=(",", ":")) + "\n"
        return
    _write_file(
        state.answers_file_path,
        _answers_file_contents(request, state.ssh_private_key_path),
    )

    wizard_env = os.environ.copy()
    wizard_env["DMF_DATA_ROOT"] = str(data_root)
    wizard_env["SOPS_AGE_KEY_FILE"] = str(state.age_key_path)
    wizard_env["NO_COLOR"] = "1"
    wizard_env["TERM"] = "dumb"

    preexisting_envs = {
        entry.name
        for entry in (data_root / "envs").iterdir()
        if entry.is_dir()
    } if (data_root / "envs").exists() else set()

    proc = subprocess.Popen(
        [str(wizard_path), "--non-interactive", str(state.answers_file_path)],
        cwd=wizard_path.parent,
        env=wizard_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    with state.render_log_path.open("w", encoding="utf-8") as log_file:
        for raw_line in proc.stdout:
            log_file.write(raw_line)
            log_file.flush()
            redacted = redact_text(
                raw_line.rstrip("\n"),
                [
                    request.operator.username,
                    request.operator.email,
                    request.operator.display,
                    request.sandbox.ssh_private_key,
                ],
            )
            if redacted:
                state.output_lines.append(redacted)
            env_id = _parse_env_id(raw_line)
            if env_id and state.env_id is None:
                state.env_id = env_id
            yield json.dumps({"event": "log", "line": redacted}, separators=(",", ":")) + "\n"
        return_code = proc.wait()

    if return_code != 0:
        state.error = f"init-wizard failed with exit code {return_code}"
        yield json.dumps({"event": "error", "error": state.error}, separators=(",", ":")) + "\n"
        return

    if state.env_id is None:
        current_envs = {
            entry.name
            for entry in (data_root / "envs").iterdir()
            if entry.is_dir()
        } if (data_root / "envs").exists() else set()
        created_envs = sorted(current_envs - preexisting_envs)
        if len(created_envs) == 1:
            state.env_id = created_envs[0]
        else:
            state.error = "could not determine env_id from init-wizard output"
            yield json.dumps({"event": "error", "error": state.error}, separators=(",", ":")) + "\n"
            return

    # P1-2: validate env_id BEFORE any path construction
    from .backup import validate_env_id
    validate_env_id(state.env_id)

    final_render_dir = data_root / "runs" / state.env_id
    state.ssh_private_key_path = _finalize_sandbox_render_artifacts(
        data_root=data_root,
        env_id=state.env_id,
        request=request,
        work_dir=state.work_dir,
        answers_file_path=state.answers_file_path,
    )
    if final_render_dir.exists():
        shutil.rmtree(final_render_dir)
    shutil.move(str(state.work_dir), str(final_render_dir))
    state.render_dir = final_render_dir
    state.age_key_path = final_render_dir / "age" / "keys.txt"
    state.answers_file_path = final_render_dir / "answers.yaml"
    state.render_log_path = final_render_dir / "render.log"
    _finalise_render_state(state)
    yield json.dumps(
        {
            "event": "complete",
            "env_id": state.env_id,
            "render_dir": str(state.render_dir),
            "metadata_path": str(state.metadata_path),
            "age_key_path": str(state.age_key_path),
            "answers_file_path": str(state.answers_file_path),
        },
        separators=(",", ":"),
    ) + "\n"


def run_backup_create_new(
    data_root: Path,
    request: CreateNewBackupRequest,
) -> CreateNewBackupResponse:
    if request.passphrase != request.passphrase_confirm:
        raise CreateNewError("passphrase confirmation does not match")
    # P1-2: validate env_id BEFORE any path construction
    from .backup import validate_env_id
    validate_env_id(request.env_id)
    render_dir = data_root / "runs" / request.env_id
    if not render_dir.is_dir():
        raise CreateNewError(f"render state not found for env_id={request.env_id}")
    metadata_path = render_dir / "render.json"
    if not metadata_path.is_file():
        raise CreateNewError(f"render metadata not found for env_id={request.env_id}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    age_key_path = Path(metadata["age_key_path"])
    answers_file_path = Path(metadata["answers_file_path"])
    backup_result = backup(
        env_dir=data_root / "envs" / request.env_id,
        age_key_path=age_key_path,
        answers_file=answers_file_path,
        passphrase=request.passphrase,
        manifest_meta=BackupManifestMeta(
            env_id=request.env_id,
            profile=metadata.get("profile", SANDBOX_PROFILE),
            schema_version=metadata.get("schema_version", ANSWER_SCHEMA_VERSION),
            checkpoint=1,
        ),
    )
    return CreateNewBackupResponse(env_id=request.env_id, backup=backup_result)


def ensure_render_result(data_root: Path, request: CreateNewRenderRequest) -> CreateNewRenderResult:
    return run_render_create_new(data_root, request)
