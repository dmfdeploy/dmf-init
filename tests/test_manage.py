from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dmf_init.backup import (
    BackupDecryptError,
    BackupIntegrityError,
    BackupManifest,
    BackupManifestMeta,
    RcloneRemoteSpec,
    backup,
)
from dmf_init.main import create_app
from dmf_init.manage import (
    ManageRestoreRequest,
    ManageSession,
    build_doctor_run,
    run_manage_restore,
)
from dmf_init.orchestrate import run_worker
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


def _build_backup_fixture(
    tmp_path: Path,
    *,
    passphrase: str = "correct horse battery staple",
    with_sandbox_ssh_key: bool = False,
) -> tuple[Path, str, list[RcloneRemoteSpec], str, BackupManifest]:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir = data_root / "envs" / env_id
    env_dir.mkdir(parents=True)
    _seed_env_tree(env_dir)

    if with_sandbox_ssh_key:
        ssh_dir = env_dir / "ssh"
        ssh_key_path = ssh_dir / "sandbox-node.key"
        ssh_pub_path = ssh_dir / "sandbox-node.key.pub"
        ssh_key_path.write_text("sandbox-node-private-key\n", encoding="utf-8")
        ssh_pub_path.write_text("ssh-ed25519 AAAATEST sandbox-node\n", encoding="utf-8")
        inventory_main = env_dir / "inventory" / "group_vars" / "all"
        inventory_main.mkdir(parents=True, exist_ok=True)
        inventory_main.joinpath("main.yml").write_text(
            f"ansible_ssh_private_key_file: {ssh_key_path}\n",
            encoding="utf-8",
        )

    answers_file = data_root / "answers.yaml"
    answers_file.write_text(
        "operator: alice\n"
        + (
            f"ssh_private_key_path: {env_dir / 'ssh' / 'sandbox-node.key'}\n"
            if with_sandbox_ssh_key
            else ""
        ),
        encoding="utf-8",
    )

    age_key_path = data_root / "backup.age.key"
    _run_age_keygen(age_key_path)

    remote_a = tmp_path / "remote-a"
    remote_b = tmp_path / "remote-b"
    remote_a.mkdir()
    remote_b.mkdir()
    rclone_config_path = tmp_path / "rclone.conf"
    _write_rclone_alias_config(rclone_config_path, remote_a, remote_b)

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
    assert Path(backup_result.artifact_path).exists()
    assert Path(backup_result.rclone_config_path).exists()
    assert Path(backup_result.manifest_path).exists()
    artifact_path = Path(backup_result.artifact_path)
    return (data_root, env_id, remotes, backup_result.artifact_name,
            backup_result.manifest, artifact_path)


def test_manage_restore_relocates_env_and_cleans_restore_root(tmp_path: Path) -> None:
    _require_tools()
    _source_root, env_id, remotes, artifact_name, _, artifact_path = _build_backup_fixture(
        tmp_path,
        with_sandbox_ssh_key=True,
    )
    target_root = tmp_path / "restored-data"

    session, result = run_manage_restore(
        target_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )

    restored_env = target_root / "envs" / env_id
    assert restored_env.is_dir()
    assert (restored_env / "bundle.sops.yaml").read_text(encoding="utf-8") == "sops: {}\n"
    assert (restored_env / "inventory" / "hosts.yml").read_text(encoding="utf-8") == "all: {}\n"
    restored_key = restored_env / "ssh" / "sandbox-node.key"
    restored_pub = restored_env / "ssh" / "sandbox-node.key.pub"
    assert restored_key.read_text(encoding="utf-8") == "sandbox-node-private-key\n"
    assert restored_pub.read_text(encoding="utf-8") == "ssh-ed25519 AAAATEST sandbox-node\n"

    inventory_main = restored_env / "inventory" / "group_vars" / "all" / "main.yml"
    answers_file_path = target_root / "runs" / env_id / "answers.yaml"
    assert str(restored_key) in inventory_main.read_text(encoding="utf-8")
    assert str(restored_key) in answers_file_path.read_text(encoding="utf-8")

    render_json_path = target_root / "runs" / env_id / "render.json"
    render_json = json.loads(render_json_path.read_text(encoding="utf-8"))
    assert render_json["env_id"] == env_id
    assert render_json["profile"] == "sandbox-single-node"
    assert render_json["schema_version"] == "2026-06-02"
    assert render_json["node_ip"] is None
    assert render_json["base_domain"] is None
    assert Path(render_json["age_key_path"]).read_text(encoding="utf-8")
    assert Path(render_json["answers_file_path"]).read_text(encoding="utf-8") == (
        "operator: alice\n"
        + f"ssh_private_key_path: '{restored_key}'\n"
    )

    age_key_path = target_root / "runs" / env_id / "age" / "keys.txt"
    assert age_key_path.exists()
    assert answers_file_path.exists()
    assert stat_mode(age_key_path) == 0o600
    assert stat_mode(answers_file_path) == 0o600

    assert result.verified is True
    assert session.passphrase == "correct horse battery staple"
    assert not list(target_root.glob("dmf-restore-*"))


def test_manage_restore_wrong_passphrase_raises(tmp_path: Path) -> None:
    _require_tools()
    data_root, _, _, _, _, artifact_path = _build_backup_fixture(tmp_path)

    with pytest.raises(BackupDecryptError):
        run_manage_restore(
            data_root,
            ManageRestoreRequest(
                artifact_path=artifact_path,
                passphrase="wrong passphrase",
            ),
        )


def test_manage_restore_integrity_mismatch_raises(tmp_path: Path) -> None:
    _require_tools()
    data_root, _, _, _, _, artifact_path = _build_backup_fixture(tmp_path)
    corrupt_path = artifact_path
    # Bit-flip the last byte: writing a fixed literal could coincide with the
    # existing byte (~1/256 per run, age output is randomized) and leave the
    # artifact unmodified — the corruption must be guaranteed.
    body = corrupt_path.read_bytes()
    corrupt_path.write_bytes(body[:-1] + bytes([body[-1] ^ 0xFF]))

    with pytest.raises((BackupDecryptError, BackupIntegrityError)):
        run_manage_restore(
            data_root,
            ManageRestoreRequest(
                artifact_path=artifact_path,
                passphrase="correct horse battery staple",
            ),
        )


def test_doctor_run_redacts_seeded_openbao_secrets(tmp_path: Path) -> None:
    data_root, env_id, _, _, manifest, _ = _build_backup_fixture(tmp_path)
    repos_bin = data_root / "repos" / "dmf-env" / "bin"
    repos_bin.mkdir(parents=True, exist_ok=True)
    (repos_bin / "bootstrap-secrets.sh").write_text("#!/bin/sh\nset -eu\n", encoding="utf-8")

    secret = "ultrasecret"
    openbao_keys_path = data_root / "envs" / env_id / "openbao-keys.json"
    openbao_keys_path.write_text(json.dumps({"root_token": secret}), encoding="utf-8")

    session = ManageSession(
        session_id="sess-1",
        env_id=env_id,
        manifest=manifest,
        restored_artifact_name="artifact.tar.age",
        age_key_path=data_root / "runs" / env_id / "age" / "keys.txt",
        answers_file_path=data_root / "runs" / env_id / "answers.yaml",
        render_dir=data_root / "runs" / env_id,
        passphrase="passphrase",
        last_checkpoint=0,
        created_at=0.0,
    )
    session.age_key_path.parent.mkdir(parents=True, exist_ok=True)
    session.age_key_path.write_text("AGE-SECRET-KEY-1TEST\n", encoding="utf-8")

    class FakeExecutor:
        def run(self, step):
            yield (f"doctor saw {secret}", None)
            yield ("", 0)

    run = build_doctor_run(session, data_root, executor=FakeExecutor())
    run_worker(run)

    events = json.dumps(run.events)
    assert secret not in events
    assert "[REDACTED]" in events
    assert any(event["event"] == "complete" for event in run.events)


def test_manage_endpoints_map_errors(tmp_path: Path) -> None:
    _require_tools()
    data_root, env_id, _, _, _, artifact_path = _build_backup_fixture(tmp_path)
    repos_bin = data_root / "repos" / "dmf-env" / "bin"
    repos_bin.mkdir(parents=True, exist_ok=True)
    (repos_bin / "bootstrap-secrets.sh").write_text("#!/bin/sh\nset -eu\n", encoding="utf-8")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    # Wrong passphrase via file upload
    with open(artifact_path, "rb") as f:
        restore_response = client.post(
            "/api/manage/restore",
            files={"file": ("backup.tar.age", f, "application/octet-stream")},
            data={"passphrase": "wrong passphrase"},
        )
    assert restore_response.status_code == 422
    assert "restore decryption failed" in restore_response.text

    doctor_response = client.post("/api/manage/doctor", json={"session_id": "missing"})
    assert doctor_response.status_code == 404
    assert "manage session not found" in doctor_response.text


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
