from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dmf_init.main import create_app
from dmf_init.orchestrate import (
    BootstrapRun,
    CheckpointStep,
    CommandStep,
    PauseStep,
    Step,
)
from dmf_init.settings import Settings


def _real_build_steps() -> list[Step]:
    """Subset of build_bootstrap_steps matching the real graph order.

    Real order: pre-seed, checkpoint-2, unseal, seed-bao, post-seed, configure,
    workstation (pause), passkey (pause), verify, checkpoint-3.
    """
    return [
        CommandStep(id="pre-seed", argv=["ignored"]),
        CheckpointStep(id="checkpoint-2", n=2),
        CommandStep(id="unseal", argv=["ignored"]),
        CommandStep(id="seed-bao", argv=["ignored"]),
        CommandStep(id="post-seed", argv=["ignored"]),
        CommandStep(id="configure", argv=["ignored"]),
        PauseStep(id="workstation", title="Prepare your workstation"),
        PauseStep(id="passkey", title="Complete passkey enrollment"),
        CommandStep(id="verify", argv=["ignored"]),
        CheckpointStep(id="checkpoint-3", n=3),
    ]


def _make_failed_run(
    app: Any,
    *,
    env_id: str,
    failed_step_id: str = "post-seed",
) -> str:
    all_steps = _real_build_steps()
    run_id = "failed-run-1"
    run = BootstrapRun(run_id=run_id, steps=all_steps, env_id=env_id)
    run.emit({"event": "run_start", "run_id": run_id, "steps": [s.id for s in all_steps]})
    run.emit({"event": "step_start", "step": "pre-seed", "index": 0, "kind": "command"})
    run.emit({"event": "step_complete", "step": "pre-seed", "status": "ok"})
    run.emit({"event": "step_start", "step": "checkpoint-2", "index": 1, "kind": "checkpoint"})
    run.emit({"event": "checkpoint", "n": 2, "artifact_name": "cp2.tar.age"})
    run.emit({"event": "step_complete", "step": "checkpoint-2", "status": "ok"})
    run.emit({"event": "step_start", "step": failed_step_id, "index": 4, "kind": "command"})
    run.emit({"event": "step_complete", "step": failed_step_id, "status": "error"})
    run.failed_step_id = failed_step_id
    run.emit(
        {
            "event": "error",
            "step": failed_step_id,
            "error": "command exited 1",
            "hint": "some error output",
        },
        terminal=True,
    )
    with app.state.bootstrap_lock:
        app.state.bootstrap_runs[run_id] = run
    return run_id


def _make_env_on_disk(data_root: Path, env_id: str) -> tuple[Path, Path]:
    env_dir = data_root / "envs" / env_id
    render_dir = data_root / "runs" / env_id
    env_dir.mkdir(parents=True, exist_ok=True)
    render_dir.mkdir(parents=True, exist_ok=True)
    age_key_path = render_dir / "age" / "keys.txt"
    age_key_path.parent.mkdir(parents=True, exist_ok=True)
    age_key_path.write_text("AGE-SECRET-KEY-1TEST\n", encoding="utf-8")
    answers_file_path = render_dir / "answers.yaml"
    answers_file_path.write_text("operator: test\n", encoding="utf-8")
    (render_dir / "render.json").write_text(
        json.dumps(
            {
                "env_id": env_id,
                "profile": "sandbox-single-node",
                "schema_version": 1,
                "render_dir": str(render_dir),
                "age_key_path": str(age_key_path),
                "answers_file_path": str(answers_file_path),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return env_dir, render_dir


def _authenticate(client: TestClient, app: Any) -> None:
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}


def test_retry_404_unknown_run_id(tmp_path: Path) -> None:
    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    response = client.post("/api/bootstrap/retry", json={"run_id": "no-such-run"})
    assert response.status_code == 404


def test_retry_409_not_failed(tmp_path: Path) -> None:
    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    run_id = "complete-run"
    steps: list[Step] = [CommandStep(id="one", argv=["ignored"])]
    run = BootstrapRun(run_id=run_id, steps=steps, env_id="test-env")
    run.emit({"event": "complete", "run_id": run_id, "checkpoints": []}, terminal=True)
    with app.state.bootstrap_lock:
        app.state.bootstrap_runs[run_id] = run

    response = client.post("/api/bootstrap/retry", json={"run_id": run_id})
    assert response.status_code == 409
    assert "not in a failed state" in response.json()["detail"]


def test_retry_409_active_run(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    _make_env_on_disk(data_root, "test-env")
    run_id = _make_failed_run(app, env_id="test-env")
    with app.state.bootstrap_lock:
        app.state.active_runs["test-env"] = "some-other-run"

    response = client.post("/api/bootstrap/retry", json={"run_id": run_id})
    assert response.status_code == 409
    assert "already in progress" in response.json()["detail"]


def test_retry_422_passphrase_required_when_checkpoint_in_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    _make_env_on_disk(data_root, env_id)

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    monkeypatch.setattr("dmf_init.main.build_bootstrap_steps", lambda ctx: _real_build_steps())
    monkeypatch.setattr("dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {})
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: None)

    # Fail at pre-seed — slice is the full graph, which includes checkpoint-2
    # AND checkpoint-3. Create openbao-keys.json so the A1 fail-closed guard
    # doesn't fire (it only triggers when CP2 is absent from the slice).
    env_dir = data_root / "envs" / env_id
    (env_dir / "openbao-keys.json").write_text("{}", encoding="utf-8")

    run_id = _make_failed_run(app, env_id=env_id, failed_step_id="pre-seed")

    response = client.post(
        "/api/bootstrap/retry",
        json={"run_id": run_id},  # no passphrase
    )
    assert response.status_code == 422
    assert "passphrase required" in response.json()["detail"]


def test_retry_passes_passphrase_and_slices_from_failed_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir, _ = _make_env_on_disk(data_root, env_id)
    # A1 guard: slice from post-seed is past CP2, so openbao-keys.json must exist.
    (env_dir / "openbao-keys.json").write_text("{}", encoding="utf-8")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    monkeypatch.setattr("dmf_init.main.build_bootstrap_steps", lambda ctx: _real_build_steps())
    monkeypatch.setattr("dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {})

    spawned_runs: list[BootstrapRun] = []
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: spawned_runs.append(run))

    run_id = _make_failed_run(app, env_id=env_id, failed_step_id="post-seed")

    response = client.post(
        "/api/bootstrap/retry",
        json={"run_id": run_id, "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 200
    new_run_id = response.json()["run_id"]
    assert new_run_id != run_id

    assert len(spawned_runs) == 1
    retry_run = spawned_runs[0]
    step_ids = [s.id for s in retry_run.steps]
    assert step_ids == [
        "post-seed",
        "configure",
        "workstation",
        "passkey",
        "verify",
        "checkpoint-3",
    ]
    assert retry_run.env_id == env_id
    assert retry_run.passphrase == "correct horse battery staple"

    with app.state.bootstrap_lock:
        assert new_run_id in app.state.bootstrap_runs


def test_retry_seeds_openbao_redactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir, _ = _make_env_on_disk(data_root, env_id)

    fake_openbao_keys = {
        "unseal_keys_b64": ["VERY_LONG_UNSEAL_KEY_SECRET_VALUE_1234567890"],
        "root_token": "VERY_LONG_ROOT_TOKEN_SECRET_VALUE_1234567890",
    }
    (env_dir / "openbao-keys.json").write_text(
        json.dumps(fake_openbao_keys), encoding="utf-8"
    )

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    monkeypatch.setattr("dmf_init.main.build_bootstrap_steps", lambda ctx: _real_build_steps())
    monkeypatch.setattr("dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {})

    spawned_runs: list[BootstrapRun] = []
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: spawned_runs.append(run))

    run_id = _make_failed_run(app, env_id=env_id, failed_step_id="post-seed")

    response = client.post(
        "/api/bootstrap/retry",
        json={"run_id": run_id, "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 200

    assert len(spawned_runs) == 1
    retry_run = spawned_runs[0]
    assert "VERY_LONG_UNSEAL_KEY_SECRET_VALUE_1234567890" in retry_run.secrets
    assert "VERY_LONG_ROOT_TOKEN_SECRET_VALUE_1234567890" in retry_run.secrets


def test_retry_409_past_checkpoint2_without_openbao_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice starts past CP2 AND openbao-keys.json is absent -> fail closed."""
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    _make_env_on_disk(data_root, env_id)
    # Do NOT create openbao-keys.json — the file is absent.

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    monkeypatch.setattr("dmf_init.main.build_bootstrap_steps", lambda ctx: _real_build_steps())
    monkeypatch.setattr("dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {})
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: None)

    # Fail at configure — slice is configure..checkpoint-3, which is past CP2.
    run_id = _make_failed_run(app, env_id=env_id, failed_step_id="configure")

    response = client.post(
        "/api/bootstrap/retry",
        json={"run_id": run_id, "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 409
    assert "openbao-keys.json" in response.json()["detail"]
