from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from typing import Any, Literal

from .backup import BackupManifestMeta, backup
from .bootstrap_steps import CheckpointError, _iter_secret_strings
from .manage import ManageError, ManageSession
from .orchestrate import (
    BootstrapRun,
    CheckpointStep,
    CommandExecutor,
    CommandStep,
    SubprocessExecutor,
)

ManageAction = Literal["rerun-playbook", "upgrade-in-place", "rotate", "teardown"]


def _manage_bin(data_root: Path, script_name: str) -> Path:
    script_path = data_root / "repos" / "dmf-env" / "bin" / script_name
    if not script_path.is_file():
        raise ManageError("repos must be fetched first")
    return script_path


def _command_env(session: ManageSession, data_root: Path) -> dict[str, str]:
    return {
        "DMF_DATA_ROOT": str(data_root),
        "SOPS_AGE_KEY_FILE": str(session.age_key_path),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def _seed_run_redactions(run: BootstrapRun, data_root: Path, env_id: str) -> None:
    keys_path = data_root / "envs" / env_id / "openbao-keys.json"
    if not keys_path.is_file():
        return
    payload = json.loads(keys_path.read_text(encoding="utf-8"))
    for secret in _iter_secret_strings(payload):
        run.add_secret(secret)


def build_action_argv(
    session: ManageSession,
    data_root: Path,
    action: ManageAction,
    params: dict[str, Any] | None,
) -> list[str]:
    if action == "rerun-playbook":
        playbook = (params or {}).get("playbook")
        if not isinstance(playbook, str) or not playbook.strip():
            raise ManageError("playbook is required")
        repos_root = data_root / "repos"
        candidate = Path(playbook)
        if not candidate.is_absolute():
            candidate = repos_root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(repos_root.resolve())
        except ValueError as exc:
            raise ManageError("playbook must be under repos/") from exc
        if not candidate.is_file():
            raise ManageError("playbook not found")
        return [
            str(_manage_bin(data_root, "run-playbook.sh")),
            session.env_id,
            str(candidate),
        ]
    if action == "upgrade-in-place":
        return [str(_manage_bin(data_root, "upgrade-in-place.sh")), session.env_id]
    if action == "rotate":
        return [str(_manage_bin(data_root, "rotate-approle-secret-id.sh")), session.env_id]
    if action == "teardown":
        return [str(_manage_bin(data_root, "remove-env.sh")), "--yes", session.env_id]
    raise ManageError(f"unknown manage action: {action}")


def make_manage_checkpoint_fn(session: ManageSession, data_root: Path):
    def checkpoint_fn(run: BootstrapRun, n: int) -> dict[str, Any]:
        _seed_run_redactions(run, data_root, session.env_id)
        passphrase = run.passphrase
        if not passphrase:
            raise CheckpointError("passphrase unavailable for checkpoint backup")
        result = backup(
            env_dir=data_root / "envs" / session.env_id,
            age_key_path=session.age_key_path,
            answers_file=session.answers_file_path,
            passphrase=passphrase,
            manifest_meta=BackupManifestMeta(
                env_id=session.env_id,
                profile=session.manifest.profile,
                schema_version=session.manifest.schema_version,
                checkpoint=n,
            ),
        )
        # Persist artifact into data_root/artifacts/ for browser download
        artifacts_dir = data_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest = artifacts_dir / result.artifact_name
        shutil.copy2(result.artifact_path, dest)
        return {
            "artifact_name": result.artifact_name,
        }

    return checkpoint_fn


def build_action_run(
    session: ManageSession,
    data_root: Path,
    action: ManageAction,
    params: dict[str, Any] | None,
    *,
    checkpoint_n: int,
    executor: CommandExecutor | None = None,
) -> BootstrapRun:
    if session.passphrase is None:
        raise ManageError("manage session passphrase unavailable")

    argv = build_action_argv(session, data_root, action, params)
    env = _command_env(session, data_root)
    command_step = CommandStep(
        id=action,
        argv=argv,
        cwd=data_root / "repos" / "dmf-env",
        env=env,
    )
    steps: list[CommandStep | CheckpointStep]
    if action == "teardown":
        steps = [CheckpointStep(id="rebackup", n=checkpoint_n), command_step]
    else:
        steps = [command_step, CheckpointStep(id="rebackup", n=checkpoint_n)]

    run = BootstrapRun(
        run_id=secrets.token_urlsafe(8),
        steps=steps,
        passphrase=session.passphrase,
        remotes=[],
        executor=executor or SubprocessExecutor(),
        checkpoint_fn=make_manage_checkpoint_fn(session, data_root),
    )
    # Pre-seed from the restored OpenBao keys before returning.
    _seed_run_redactions(run, data_root, session.env_id)
    return run
