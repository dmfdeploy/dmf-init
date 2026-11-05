from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
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
    ManageError,
    ManageRestoreRequest,
    ManageSession,
    _relocate_restored_env_paths,
    build_doctor_run,
    run_manage_restore,
)
from dmf_init.orchestrate import run_worker
from dmf_init.settings import Settings
from dmf_init.yaml_utils import relocate_yaml_paths, rewrite_yaml_scalar


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
    extra_group_vars: str = "",
    ssh_key_value: str | None = None,
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
            f"ansible_ssh_private_key_file: {ssh_key_value or ssh_key_path}\n" + extra_group_vars,
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


def _env_path_group_vars(old_env: Path) -> str:
    return (
        "# packaged inventory\n"
        f"openbao_key_path: {old_env}/openbao-keys/tier3.key\n"
        f"eso_openbao_breakglass_file: '{old_env}/openbao-keys/breakglass.json'\n"
        f'awx_control_node_ssh_privkey_path: "{old_env}/ssh/id_ed25519"\n'
        f"unrelated_note: /elsewhere{old_env}/keep\n"
        f"sibling_env: {old_env}-other/ssh/key\n"
        "block_note: |\n"
        f"  openbao_key_path: {old_env}/not-a-yaml-path\n"
        f"  see {old_env}/ssh\n"
        f"# comment mentioning {old_env}/ssh/id_ed25519\n"
    )


def test_manage_restore_rewrites_only_the_five_env_path_keys(tmp_path: Path) -> None:
    _require_tools()
    old_env = tmp_path / "data" / "envs" / "sandbox-alpha"
    group_vars = _env_path_group_vars(old_env)
    _, env_id, _, _, _, artifact_path = _build_backup_fixture(
        tmp_path, with_sandbox_ssh_key=True, extra_group_vars=group_vars
    )
    target_root = tmp_path / "restored-data"

    run_manage_restore(
        target_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )

    new_env = target_root / "envs" / env_id
    text = (new_env / "inventory" / "group_vars" / "all" / "main.yml").read_text(
        encoding="utf-8"
    )
    loaded = yaml.safe_load(text)
    assert loaded["ansible_ssh_private_key_file"] == f"{new_env}/ssh/sandbox-node.key"
    assert loaded["openbao_key_path"] == f"{new_env}/openbao-keys/tier3.key"
    assert loaded["eso_openbao_breakglass_file"] == f"{new_env}/openbao-keys/breakglass.json"
    assert loaded["awx_control_node_ssh_privkey_path"] == f"{new_env}/ssh/id_ed25519"
    expected = (
        f"ansible_ssh_private_key_file: '{new_env}/ssh/sandbox-node.key'\n"
        + group_vars.replace(
            f"openbao_key_path: {old_env}/openbao-keys/tier3.key\n",
            f"openbao_key_path: '{new_env}/openbao-keys/tier3.key'\n",
            1,
        )
        .replace(
            f"eso_openbao_breakglass_file: '{old_env}/openbao-keys/breakglass.json'\n",
            f"eso_openbao_breakglass_file: '{new_env}/openbao-keys/breakglass.json'\n",
            1,
        )
        .replace(
            f'awx_control_node_ssh_privkey_path: "{old_env}/ssh/id_ed25519"\n',
            f"awx_control_node_ssh_privkey_path: '{new_env}/ssh/id_ed25519'\n",
            1,
        )
    )
    assert text == expected


def test_manage_restore_fails_and_keeps_existing_env_when_old_root_is_unknown(
    tmp_path: Path,
) -> None:
    _require_tools()
    old_env = tmp_path / "data" / "envs" / "sandbox-alpha"
    _, env_id, _, _, _, artifact_path = _build_backup_fixture(
        tmp_path,
        with_sandbox_ssh_key=True,
        ssh_key_value="/somewhere/else/key",
        extra_group_vars=f"openbao_key_path: {old_env}/openbao-keys/tier3.key\n",
    )
    target_root = tmp_path / "restored-data"
    existing = target_root / "envs" / env_id
    existing.mkdir(parents=True)
    (existing / "marker").write_text("keep", encoding="utf-8")

    with pytest.raises(ManageError, match="ansible_ssh_private_key_file.*/somewhere/else/key"):
        run_manage_restore(
            target_root,
            ManageRestoreRequest(
                artifact_path=artifact_path,
                passphrase="correct horse battery staple",
            ),
        )

    assert (existing / "marker").read_text(encoding="utf-8") == "keep"
    assert not (existing / "inventory").exists()


# The shape init-wizard.sh writes for a sandbox env (dmf-env
# bin/init-wizard.sh:1794-1800 and 1915-1918), with the SSH key line as
# dmf-init rewrites it (src/dmf_init/createnew.py:176-180), which the fixture
# writes as ssh_key_value: a single-quoted path.
def _wizard_group_vars(old_env: Path) -> str:
    return (
        'ansible_ssh_common_args: "-o StrictHostKeyChecking=no"\n'
        "# AWX control-node key, generated per env.\n"
        f"awx_control_node_ssh_privkey_path: {old_env}/ssh/awx-control-node.key\n"
        "\n"
        "# --- OpenBao: sandbox Tier-3 self-recovering unseal ---\n"
        f'openbao_key_path: "{old_env}/openbao-keys"\n'
        "# ESO loads the AppRole creds from the JSON the openbao role writes.\n"
        f'eso_openbao_breakglass_file: "{old_env}/openbao-keys.json"\n'
    )


def test_manage_restore_rewrites_the_shape_the_wizard_writes(tmp_path: Path) -> None:
    _require_tools()
    old_env = tmp_path / "data" / "envs" / "sandbox-alpha"
    _, env_id, _, _, _, artifact_path = _build_backup_fixture(
        tmp_path,
        with_sandbox_ssh_key=True,
        ssh_key_value=f"'{old_env}/ssh/sandbox-node.key'",
        extra_group_vars=_wizard_group_vars(old_env),
    )
    target_root = tmp_path / "restored-data"

    run_manage_restore(
        target_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )

    new_env = target_root / "envs" / env_id
    loaded = yaml.safe_load(
        (new_env / "inventory" / "group_vars" / "all" / "main.yml").read_text(encoding="utf-8")
    )
    assert loaded["ansible_ssh_private_key_file"] == f"{new_env}/ssh/sandbox-node.key"
    assert loaded["awx_control_node_ssh_privkey_path"] == f"{new_env}/ssh/awx-control-node.key"
    assert loaded["openbao_key_path"] == f"{new_env}/openbao-keys"
    assert loaded["eso_openbao_breakglass_file"] == f"{new_env}/openbao-keys.json"
    assert loaded["ansible_ssh_common_args"] == "-o StrictHostKeyChecking=no"


# The cloud-lane shape (dmf-env bin/init-wizard.sh:1470, 1528 and 1580-1596):
# the SSH key is operator.key under the env, the AWX key sits under the env,
# and the OpenBao paths live under a separate break-glass directory.
def test_manage_restore_rewrites_the_cloud_lane_shape(tmp_path: Path) -> None:
    _require_tools()
    old_env = tmp_path / "data" / "envs" / "sandbox-alpha"
    breakglass = "/breakglass/sandbox-alpha"
    _, env_id, _, _, _, artifact_path = _build_backup_fixture(
        tmp_path,
        with_sandbox_ssh_key=True,
        ssh_key_value=f'"{old_env}/ssh/operator.key"',
        extra_group_vars=(
            f"awx_control_node_ssh_privkey_path: {old_env}/ssh/awx-control-node.key\n"
            f"openbao_key_path: {breakglass}/openbao-keys-automation\n"
            f"eso_openbao_breakglass_file: {breakglass}/openbao-keys-automation.json\n"
        ),
    )
    target_root = tmp_path / "restored-data"

    run_manage_restore(
        target_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )

    new_env = target_root / "envs" / env_id
    loaded = yaml.safe_load(
        (new_env / "inventory" / "group_vars" / "all" / "main.yml").read_text(encoding="utf-8")
    )
    assert loaded["ansible_ssh_private_key_file"] == f"{new_env}/ssh/operator.key"
    assert loaded["awx_control_node_ssh_privkey_path"] == f"{new_env}/ssh/awx-control-node.key"
    assert loaded["openbao_key_path"] == f"{breakglass}/openbao-keys-automation"
    assert loaded["eso_openbao_breakglass_file"] == f"{breakglass}/openbao-keys-automation.json"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "ansible_ssh_private_key_file: /somewhere/else/key\n"
            "openbao_key_path: /old/envs/x/openbao-keys\n",
            "ansible_ssh_private_key_file",
        ),
        (
            "ansible_ssh_private_key_file: /old/envs/x/ssh/sandbox-node.key\n"
            "awx_control_node_ssh_privkey_path: /old/envs/x/ssh/awx.key\n"
            "openbao_key_path: /old/envs/x/openbao-keys # operator note\n",
            "openbao_key_path",
        ),
        (
            "ansible_ssh_private_key_file: /old/envs/x/ssh/sandbox-node.key\n"
            "awx_control_node_ssh_privkey_path: /old/envs/x/ssh/awx.key\n"
            'eso_openbao_breakglass_file: "/old/envs/x/openbao\\x2djson"\n',
            "eso_openbao_breakglass_file",
        ),
        (
            "ansible_ssh_private_key_file: /old/envs/x/ssh/sandbox-node.key\n"
            "openbao_key_path: '/old/envs/x/openbao-keys' # operator note\n",
            "openbao_key_path",
        ),
    ],
)
def test_relocation_fails_closed_and_leaves_the_file_unchanged(
    tmp_path: Path, body: str, message: str
) -> None:
    group_vars = tmp_path / "main.yml"
    group_vars.write_text(body, encoding="utf-8")

    with pytest.raises(ManageError, match=message):
        _relocate_restored_env_paths(group_vars, tmp_path / "new" / "envs" / "x")

    assert group_vars.read_text(encoding="utf-8") == body


def test_relocation_ignores_unsupported_forms_outside_the_old_root(tmp_path: Path) -> None:
    group_vars = tmp_path / "main.yml"
    note = "openbao_key_path: /var/keys/openbao # operator note\n"
    group_vars.write_text(
        "ansible_ssh_private_key_file: /old/envs/x/ssh/sandbox-node.key\n" + note,
        encoding="utf-8",
    )
    new_env = tmp_path / "new" / "envs" / "x"

    _relocate_restored_env_paths(group_vars, new_env)

    assert group_vars.read_text(encoding="utf-8") == (
        f"ansible_ssh_private_key_file: '{new_env}/ssh/sandbox-node.key'\n" + note
    )


@pytest.mark.parametrize(
    "new_root",
    [
        "/data root/envs/x",
        '/data"root/envs/x',
        "/data\\root/envs/x",
        "/it's/envs/x",
        "/a\\n\"b c'd/envs/x",
    ],
)
def test_relocate_yaml_path_emits_valid_yaml_for_any_destination(
    tmp_path: Path, new_root: str
) -> None:
    old_root = "/old root/envs/x"
    path = tmp_path / "main.yml"
    path.write_text(
        f'openbao_key_path: "{old_root}/openbao-keys/tier3.key"\n'
        f"eso_openbao_breakglass_file: '{old_root}/openbao-keys/breakglass.json'\n"
        f"awx_control_node_ssh_privkey_path: {old_root}\n"
        f"unrelated: {old_root}-other/key\n",
        encoding="utf-8",
    )

    relocate_yaml_paths(
        path,
        ("openbao_key_path", "eso_openbao_breakglass_file", "awx_control_node_ssh_privkey_path"),
        old_root,
        new_root,
    )

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["openbao_key_path"] == f"{new_root}/openbao-keys/tier3.key"
    assert loaded["eso_openbao_breakglass_file"] == f"{new_root}/openbao-keys/breakglass.json"
    assert loaded["awx_control_node_ssh_privkey_path"] == new_root
    assert loaded["unrelated"] == f"{old_root}-other/key"


@pytest.mark.parametrize(
    "new_value",
    [
        "/data root/ssh/k",
        '/data"root/ssh/k',
        "/data\\root/ssh/k",
        "/it's/ssh/k",
        "/a\\1\"b/ssh/k",
    ],
)
def test_rewrite_yaml_scalar_emits_valid_yaml_for_any_path(tmp_path: Path, new_value: str) -> None:
    path = tmp_path / "main.yml"
    path.write_text("ansible_ssh_private_key_file: /old/key\nother: 1\n", encoding="utf-8")

    rewrite_yaml_scalar(path, "ansible_ssh_private_key_file", new_value)

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "ansible_ssh_private_key_file": new_value,
        "other": 1,
    }


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
