from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from install_capabilities import write_install_capabilities

from dmf_init.backup import (
    BackupManifestMeta,
    backup,
    restore,
)
from dmf_init.main import create_app
from dmf_init.manage import ManageError, ManageRestoreRequest, run_manage_restore
from dmf_init.manage_actions import build_action_argv, build_action_run
from dmf_init.orchestrate import CheckpointStep, CommandStep, run_worker
from dmf_init.repos import INSTALL_CAPABILITIES_FILE
from dmf_init.settings import Settings


def _require_tools() -> None:
    missing = [tool for tool in ("age", "age-keygen") if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


def _run_age_keygen(key_path: Path) -> None:
    subprocess.run(
        ["age-keygen", "-o", str(key_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_env_tree(env_dir: Path) -> None:
    (env_dir / "bundle.sops.yaml").write_text("sops: {}\n", encoding="utf-8")
    (env_dir / "manifest.yaml").write_text("manifest: {}\n", encoding="utf-8")
    (env_dir / "inventory").mkdir(parents=True)
    (env_dir / "inventory" / "hosts.yml").write_text("all: {}\n", encoding="utf-8")
    (env_dir / "openbao-keys").mkdir()
    (env_dir / "openbao-keys" / "tier3.key").write_text("unseal-key\n", encoding="utf-8")
    (env_dir / "ssh").mkdir()
    (env_dir / "ssh" / "id_ed25519").write_text("ssh-private-key\n", encoding="utf-8")


def _seed_repos_bin(data_root: Path) -> None:
    write_install_capabilities(data_root)
    bin_dir = data_root / "repos" / "dmf-env" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    scripts = {
        "run-playbook.sh": "#!/bin/sh\nset -eu\nexit 0\n",
        "upgrade-in-place.sh": "#!/bin/sh\nset -eu\nexit 0\n",
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
) -> tuple[Path, str, str, Path]:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir = data_root / "envs" / env_id
    env_dir.mkdir(parents=True)
    _seed_env_tree(env_dir)
    answers_file = data_root / "answers.yaml"
    answers_file.write_text("operator: alice\n", encoding="utf-8")
    age_key_path = data_root / "backup.age.key"
    _run_age_keygen(age_key_path)

    backup_result = backup(
        env_dir=env_dir,
        age_key_path=age_key_path,
        answers_file=answers_file,
        passphrase=passphrase,
        manifest_meta=BackupManifestMeta(
            env_id=env_id,
            profile="sandbox-single-node",
            schema_version="2026-06-02",
            checkpoint=3,
        ),
    )
    artifact_path = Path(backup_result.artifact_path)
    return data_root, env_id, backup_result.artifact_name, artifact_path


def test_mutating_action_graph_order(tmp_path: Path) -> None:
    _require_tools()
    data_root, _env_id, _, artifact_path = _build_backup_fixture(tmp_path)
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
    data_root, _env_id, _, artifact_path = _build_backup_fixture(tmp_path)
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
    data_root, _env_id, _, artifact_path = _build_backup_fixture(tmp_path)
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
    data_root, env_id, _, artifact_path = _build_backup_fixture(tmp_path)
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
    data_root, env_id, _, artifact_path = _build_backup_fixture(tmp_path)
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
        "upgrade-in-place",
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
    data_root, _env_id, _, artifact_path = _build_backup_fixture(tmp_path)
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
    data_root, _env_id, _, artifact_path = _build_backup_fixture(tmp_path)
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


def _restore_session(tmp_path: Path):
    data_root, _env_id, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    session, _ = run_manage_restore(
        data_root,
        ManageRestoreRequest(
            artifact_path=artifact_path,
            passphrase="correct horse battery staple",
        ),
    )
    return data_root, session, artifact_path


def test_upgrade_in_place_argv_passes_the_confirmation_flag(tmp_path: Path) -> None:
    _require_tools()
    data_root, session, _ = _restore_session(tmp_path)

    argv = build_action_argv(session, data_root, "upgrade-in-place", None)

    assert argv[1:] == [session.env_id, "--yes"]


def test_retired_rotate_action_is_refused_by_name(tmp_path: Path) -> None:
    _require_tools()
    data_root, session, artifact_path = _restore_session(tmp_path)

    with pytest.raises(ManageError, match="rotate action was retired"):
        build_action_argv(session, data_root, "rotate", None)
    with pytest.raises(ManageError, match="unknown manage action: nonsense"):
        build_action_argv(session, data_root, "nonsense", None)

    # The same refusal reaches an API client as a 400 with the message, not a
    # 422 schema error or a 500.
    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    client.get(f"/?token={token}", follow_redirects=False)
    with open(artifact_path, "rb") as f:
        restore_response = client.post(
            "/api/manage/restore",
            files={"file": ("backup.tar.age", f, "application/octet-stream")},
            data={"passphrase": "correct horse battery staple"},
        )
    assert restore_response.status_code == 200
    session_id = restore_response.json()["session_id"]

    refused = client.post(
        "/api/manage/action/start",
        json={"session_id": session_id, "action": "rotate"},
    )
    assert refused.status_code == 400
    assert "rotate action was retired" in refused.json()["detail"]

    # Validation runs before the reservation: with the env already busy, a
    # retired or unknown name still gets its own 400, not the 409 busy reply,
    # and the existing reservation is left alone.
    app.state.active_runs[session.env_id] = "__pending__"
    refusals = (
        ("rotate", "rotate action was retired"),
        ("nonsense", "unknown manage action"),
    )
    for action, message in refusals:
        busy = client.post(
            "/api/manage/action/start",
            json={"session_id": session_id, "action": action},
        )
        assert busy.status_code == 400
        assert message in busy.json()["detail"]
        assert app.state.active_runs[session.env_id] == "__pending__"
    del app.state.active_runs[session.env_id]

    # The refusal leaves the env free for the next action.
    accepted = client.post(
        "/api/manage/action/start",
        json={"session_id": session_id, "action": "upgrade-in-place"},
    )
    assert accepted.status_code == 200


def test_upgrade_in_place_script_passes_its_own_confirmation(tmp_path: Path) -> None:
    """Run the real dmf-env script with the argv Manage builds and no TTY.

    Every script it calls next is a stub, so nothing reaches a cluster. A
    dmf-env change to the flag or to the prompt makes the script exit before
    the first stub runs.
    """
    _require_tools()
    source_checkout = os.getenv("DMF_TEST_DMF_ENV_REPO")
    if not source_checkout:
        pytest.skip("DMF_TEST_DMF_ENV_REPO is not set")
    source_bin = Path(source_checkout) / "bin"
    if not (source_bin / "upgrade-in-place.sh").is_file():
        pytest.skip(f"DMF_TEST_DMF_ENV_REPO has no bin/upgrade-in-place.sh: {source_checkout}")

    data_root, session, _ = _restore_session(tmp_path)
    bin_dir = data_root / "repos" / "dmf-env" / "bin"
    shutil.copy2(source_bin / "upgrade-in-place.sh", bin_dir / "upgrade-in-place.sh")
    shutil.copytree(source_bin / "lib", bin_dir / "lib", dirs_exist_ok=True)

    calls = tmp_path / "stub-calls.log"
    stub = (
        '#!/bin/sh\n'
        f'echo "$(basename "$0") $*" >> "{calls}"\n'
        # The unseal precheck reads this status line; every other stub just succeeds.
        'case "$*" in *--status*) echo \'"sealed": false\' ;; esac\n'
        'exit 0\n'
    )
    for name in ("unseal-openbao.sh", "bootstrap-secrets.sh", "run-playbook.sh"):
        (bin_dir / name).write_text(stub, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    env_dir = data_root / "envs" / session.env_id
    group_vars = env_dir / "inventory" / "group_vars" / "all"
    group_vars.mkdir(parents=True, exist_ok=True)
    # The script reads the lane from the inventory and refuses to guess it.
    (group_vars / "main.yml").write_text("dmf_provider: sandbox\n", encoding="utf-8")

    argv = build_action_argv(session, data_root, "upgrade-in-place", None)
    result = subprocess.run(
        argv,
        cwd=data_root / "repos" / "dmf-env",
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            "DMF_DATA_ROOT": str(data_root),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert argv[1:] == [session.env_id, "--yes"]
    assert "bootstrap-sandbox-provision-pre-seed.yml" in calls.read_text(encoding="utf-8")


def _endpoint_session(tmp_path: Path, *, stale: bool):
    """Restore a Manage session through the API; optionally leave a stale dmf-infra."""
    data_root, _env_id, _, artifact_path = _build_backup_fixture(tmp_path)
    _seed_repos_bin(data_root)
    if stale:
        (data_root / "repos" / INSTALL_CAPABILITIES_FILE).unlink()
    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    client.get(f"/?token={token}", follow_redirects=False)
    with open(artifact_path, "rb") as f:
        restored = client.post(
            "/api/manage/restore",
            files={"file": ("backup.tar.age", f, "application/octet-stream")},
            data={"passphrase": "correct horse battery staple"},
        )
    assert restored.status_code == 200
    return client, app, data_root, restored.json()["session_id"]


@pytest.mark.parametrize(
    "action, params",
    [
        ("upgrade-in-place", None),
        ("rerun-playbook", {"playbook": "dmf-infra/k3s-lab-bootstrap/example.yml"}),
    ],
)
def test_playbook_actions_refuse_a_stale_checkout(
    tmp_path: Path, action: str, params: dict | None
) -> None:
    _require_tools()
    client, app, data_root, session_id = _endpoint_session(tmp_path, stale=True)
    playbook = data_root / "repos" / "dmf-infra" / "k3s-lab-bootstrap" / "example.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n", encoding="utf-8")

    response = client.post(
        "/api/manage/action/start",
        json={"session_id": session_id, "action": action, "params": params},
    )

    assert response.status_code == 409
    assert "openbao-seed-in-install" in response.json()["detail"]
    assert not app.state.active_runs


def test_teardown_and_doctor_run_with_a_stale_checkout(tmp_path: Path) -> None:
    _require_tools()
    client, app, _data_root, session_id = _endpoint_session(tmp_path, stale=True)

    doctor = client.post("/api/manage/doctor", json={"session_id": session_id})
    assert "openbao-seed-in-install" not in str(doctor.json())

    _wait_for_runs(app)  # the doctor run frees the env for the next action

    teardown = client.post(
        "/api/manage/action/start",
        json={"session_id": session_id, "action": "teardown"},
    )
    assert teardown.status_code == 200
    # The run takes a checkpoint backup, which encrypts with a passphrase and
    # keeps a CPU busy. Wait for it, or it slows the tests that run next.
    _wait_for_runs(app, timeout=60)


def _wait_for_runs(app, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not app.state.active_runs:
            return
        time.sleep(0.05)
    raise AssertionError("a Manage run did not finish")
