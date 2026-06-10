from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dmf_init.backup import (
    BackupManifestMeta,
    RcloneRemoteSpec,
    backup,
    restore,
)
from dmf_init.main import create_app
from dmf_init.manage import ManageError, ManageRestoreRequest, run_manage_restore
from dmf_init.manage_actions import build_action_argv, build_action_run
from dmf_init.orchestrate import CheckpointStep, CommandStep, run_worker
from dmf_init.settings import Settings


def _require_tools() -> None:
    missing = [tool for tool in ("rclone", "age", "age-keygen") if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


def _run_age_keygen(key_path: Path) -> None:
    subprocess.run(
        ["age-keygen", "-o", str(key_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_rclone_alias_config(config_path: Path, remote_a: Path, remote_b: Path) -> None:
    config_path.write_text(
        "\n".join(
            [
                "[remote-a]",
                "type = alias",
                f"remote = {remote_a}",
                "",
                "[remote-b]",
                "type = alias",
                f"remote = {remote_b}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)


def _seed_env_tree(env_dir: Path) -> None:
    (env_dir / "bundle.sops.yaml").write_text("sops: {}\n", encoding="utf-8")
    (env_dir / "manifest.yaml").write_text("manifest: {}\n", encoding="utf-8")
    (env_dir / "inventory").mkdir(parents=True)
    (env_dir / "inventory" / "hosts.yml").write_text("all: {}\n", encoding="utf-8")
    (env_dir / "openbao-keys").mkdir()
    (env_dir / "openbao-keys" / "tier3.key").write_text("unseal-key\n", encoding="utf-8")
    (env_dir / "ssh").mkdir()
    (env_dir / "ssh" / "id_ed25519").write_text("ssh-private-key\n", encoding="utf-8")


def _build_remote_specs(tmp_path: Path) -> tuple[list[RcloneRemoteSpec], Path]:
    remote_a = tmp_path / "remote-a"
    remote_b = tmp_path / "remote-b"
    remote_a.mkdir()
    remote_b.mkdir()
    config_path = tmp_path / "rclone.conf"
    _write_rclone_alias_config(config_path, remote_a, remote_b)
    remotes = [
        RcloneRemoteSpec(
            name="remote-a",
            type="alias",
            options={"remote": str(remote_a)},
            destination_prefix="backups",
        ),
        RcloneRemoteSpec(
            name="remote-b",
            type="alias",
            options={"remote": str(remote_b)},
            destination_prefix="backups",
        ),
    ]
    return remotes, config_path


def _seed_repos_bin(data_root: Path) -> None:
    bin_dir = data_root / "repos" / "dmf-env" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    scripts = {
        "run-playbook.sh": "#!/bin/sh\nset -eu\nexit 0\n",
        "upgrade-in-place.sh": "#!/bin/sh\nset -eu\nexit 0\n",
        "rotate-approle-secret-id.sh": "#!/bin/sh\nset -eu\nexit 0\n",
        "remove-env.sh": "#!/bin/sh\nset -eu\nexit 0\n",
    }
    for name, content in scripts.items():
        path = bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)


def _build_backup_fixture(
    tmp_path: Path,
    *,
    passphrase: str = "correct horse battery staple",
) -> tuple[Path, str, list[RcloneRemoteSpec], str]:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir = data_root / "envs" / env_id
    env_dir.mkdir(parents=True)
    _seed_env_tree(env_dir)
    answers_file = data_root / "answers.yaml"
    answers_file.write_text("operator: alice\n", encoding="utf-8")
    age_key_path = data_root / "backup.age.key"
    _run_age_keygen(age_key_path)

    remotes, _ = _build_remote_specs(tmp_path)
    backup_result = backup(
        env_dir=env_dir,
        age_key_path=age_key_path,
        answers_file=answers_file,
        passphrase=passphrase,
        remotes=remotes,
        manifest_meta=BackupManifestMeta(
            env_id=env_id,
            profile="sandbox-single-node",
            schema_version="2026-06-02",
            checkpoint=3,
        ),
    )
    artifact_path = Path(backup_result.artifact_path)
    return (data_root, env_id, remotes, backup_result.artifact_name,
            artifact_path)


def test_mutating_action_graph_order(tmp_path: Path) -> None:
    _require_tools()
    data_root, _env_id, _, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    session, _ = run_manage_restore(
        data_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )

    run = build_action_run(
        session,
        data_root,
        "upgrade-in-place",
        None,
        checkpoint_n=session.last_checkpoint + 1,
    )

    assert [type(step) for step in run.steps] == [CommandStep, CheckpointStep]
    assert isinstance(run.steps[0], CommandStep)
    assert isinstance(run.steps[1], CheckpointStep)
    assert run.steps[0].id == "upgrade-in-place"
    assert run.steps[1].id == "rebackup"


def test_teardown_graph_order(tmp_path: Path) -> None:
    _require_tools()
    data_root, _env_id, _, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    session, _ = run_manage_restore(
        data_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )

    run = build_action_run(
        session,
        data_root,
        "teardown",
        None,
        checkpoint_n=session.last_checkpoint + 1,
    )

    assert [type(step) for step in run.steps] == [CheckpointStep, CommandStep]
    assert isinstance(run.steps[0], CheckpointStep)
    assert isinstance(run.steps[1], CommandStep)
    assert run.steps[0].id == "rebackup"
    assert run.steps[1].id == "teardown"


def test_rerun_playbook_missing_param_raises(tmp_path: Path) -> None:
    _require_tools()
    data_root, _env_id, _, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    session, _ = run_manage_restore(
        data_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )

    with pytest.raises(ManageError):
        build_action_argv(session, data_root, "rerun-playbook", None)


def test_action_run_rebackups_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_tools()
    stamps = iter(["20260101T000001Z", "20260101T000002Z"])
    monkeypatch.setattr("dmf_init.backup._utc_stamp", lambda: next(stamps))
    data_root, env_id, remotes, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    openbao_keys_path = data_root / "envs" / env_id / "openbao-keys.json"
    openbao_keys_path.write_text(json.dumps({"root_token": "ultrasecret"}), encoding="utf-8")

    session, _ = run_manage_restore(
        data_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )

    class FakeExecutor:
        def run(self, step):
            del step
            yield ("action log line", None)
            yield ("", 0)

    run = build_action_run(
        session,
        data_root,
        "upgrade-in-place",
        None,
        checkpoint_n=session.last_checkpoint + 1,
        executor=FakeExecutor(),
    )
    run_worker(run)

    checkpoint_event = next(ev for ev in run.events if ev["event"] == "checkpoint")
    new_artifact = checkpoint_event["artifact_name"]
    assert checkpoint_event["n"] == session.last_checkpoint + 1
    assert (data_root / "artifacts" / new_artifact).exists()

    restore_result = restore(
        data_root / "artifacts" / new_artifact,
        "correct horse battery staple",
        data_root / "verify",
    )
    try:
        assert restore_result.manifest.checkpoint == session.last_checkpoint + 1
    finally:
        restore_result.cleanup()


def test_action_redaction(tmp_path: Path) -> None:
    _require_tools()
    data_root, env_id, _, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    secret = "ultrasecret"

    session, _ = run_manage_restore(
        data_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )
    (data_root / "envs" / env_id / "openbao-keys.json").write_text(
        json.dumps({"root_token": secret}),
        encoding="utf-8",
    )

    class FakeExecutor:
        def run(self, step):
            del step
            yield (f"echo {secret}", None)
            yield ("", 0)

    run = build_action_run(
        session,
        data_root,
        "rotate",
        None,
        checkpoint_n=session.last_checkpoint + 1,
        executor=FakeExecutor(),
    )
    run_worker(run)

    rendered = json.dumps(run.events)
    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_endpoint_flow(tmp_path: Path) -> None:
    _require_tools()
    data_root, _env_id, _, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    with open(artifact_path, "rb") as f:
        restore_response = client.post(
            "/api/manage/restore",
            files={"file": ("backup.tar.age", f, "application/octet-stream")},
            data={"passphrase": "correct horse battery staple"},
        )
    assert restore_response.status_code == 200
    session_id = restore_response.json()["session_id"]

    # Direct action start works now (no lock required)
    action = client.post(
        "/api/manage/action/start",
        json={"session_id": session_id, "action": "upgrade-in-place"},
    )
    assert action.status_code == 200
    assert "run_id" in action.json()


def test_rerun_playbook_builds_under_repos(tmp_path: Path) -> None:
    _require_tools()
    data_root, _env_id, _, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    session, _ = run_manage_restore(
        data_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )
    playbook = data_root / "repos" / "dmf-infra" / "k3s-lab-bootstrap" / "example.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n", encoding="utf-8")

    argv = build_action_argv(
        session,
        data_root,
        "rerun-playbook",
        {"playbook": "dmf-infra/k3s-lab-bootstrap/example.yml"},
    )
    assert argv[-1] == str(playbook)
