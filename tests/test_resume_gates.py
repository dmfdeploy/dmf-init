"""Server-side enforcement for the first-passkey and setup-request pauses.

The browser polls the passkey status before it resumes, but the server must not
rely on that: an authenticated caller can post to /api/bootstrap/resume directly.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dmf_init.main import create_app
from dmf_init.orchestrate import (
    BootstrapRun,
    CommandStep,
    PauseStep,
    Step,
    run_worker,
)
from dmf_init.settings import Settings

ENV = "sandbox-alpha"
REQUEST_ID = "c" * 32


class OkExecutor:
    def run(self, step: CommandStep):
        yield "", 0


def _make_env_on_disk(data_root: Path) -> Path:
    env_dir = data_root / "envs" / ENV
    render_dir = data_root / "runs" / ENV
    env_dir.mkdir(parents=True, exist_ok=True)
    (render_dir / "age").mkdir(parents=True, exist_ok=True)
    (render_dir / "age" / "keys.txt").write_text("AGE-SECRET-KEY-1TEST\n", encoding="utf-8")
    (render_dir / "answers.yaml").write_text("operator: test\n", encoding="utf-8")
    (render_dir / "render.json").write_text(
        json.dumps(
            {
                "env_id": ENV,
                "profile": "sandbox-single-node",
                "schema_version": 1,
                "render_dir": str(render_dir),
                "age_key_path": str(render_dir / "age" / "keys.txt"),
                "answers_file_path": str(render_dir / "answers.yaml"),
            }
        ),
        encoding="utf-8",
    )
    return env_dir


def _client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    _make_env_on_disk(app.state.settings.data_root)
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}
    return client, app


def _passkey_status(monkeypatch: pytest.MonkeyPatch, **payload: Any) -> list[int]:
    calls: list[int] = []

    def fake(ctx):
        calls.append(1)
        return {"enrollment_url": "", "confirmed": 0, "required": 0, **payload}

    monkeypatch.setattr("dmf_init.main.build_passkey_payload", fake)
    return calls


def _gate_steps() -> list[Step]:
    return [
        PauseStep(id="initial-passkey", title="Establish your first passkey"),
        PauseStep(id="setup-request", title="Authorize setup inventory"),
        CommandStep(id="configure", argv=["x"], setup_request_pause_id="setup-request"),
    ]


def _start(app: FastAPI, steps: list[Step], *, spawn: bool) -> BootstrapRun:
    run = BootstrapRun(run_id="r1", steps=steps, env_id=ENV, executor=OkExecutor())
    app.state.bootstrap_runs["r1"] = run
    if spawn:
        threading.Thread(target=run_worker, args=(run,), daemon=True).start()
        _wait_for(lambda: any(e.get("pause_id") == steps[0].id for e in run.events))
    return run


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def _resume(client: TestClient, pause_id: str, payload: dict | None = None):
    body: dict[str, Any] = {"run_id": "r1", "pause_id": pause_id}
    if payload is not None:
        body["payload"] = payload
    return client.post("/api/bootstrap/resume", json=body)


@pytest.mark.parametrize(
    "status",
    [
        {"confirmed": 0, "required": 2},
        {"confirmed": 1, "required": 2},
        {"confirmed": 0, "required": 0},
        {"confirmed": 3, "required": 0},
        {"confirmed": 2, "required": 2, "note": "could not fetch live passkey status"},
    ],
)
def test_initial_passkey_refused_until_required_passkeys_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: dict[str, Any]
) -> None:
    client, app = _client(tmp_path)
    _passkey_status(monkeypatch, **status)
    run = _start(app, _gate_steps(), spawn=True)

    resp = _resume(client, "initial-passkey")

    assert resp.status_code == 409
    assert not run.pauses["initial-passkey"].is_set()


def test_initial_passkey_resumes_when_confirmed_meets_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _client(tmp_path)
    _passkey_status(monkeypatch, confirmed=2, required=2)
    run = _start(app, _gate_steps(), spawn=True)

    assert _resume(client, "initial-passkey").status_code == 200
    assert run.pauses["initial-passkey"].is_set()


def test_initial_passkey_refused_before_the_worker_reaches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _client(tmp_path)
    _passkey_status(monkeypatch, confirmed=2, required=2)
    steps: list[Step] = [
        PauseStep(id="workstation", title="Prepare your workstation"),
        *_gate_steps(),
    ]
    run = _start(app, steps, spawn=True)  # worker waits on workstation

    resp = _resume(client, "initial-passkey")

    assert resp.status_code == 409
    assert not run.pauses["initial-passkey"].is_set()


def test_setup_request_refused_before_the_worker_reaches_it(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    run = _start(app, _gate_steps(), spawn=True)  # worker waits on initial-passkey

    resp = _resume(client, "setup-request", {"request_id": REQUEST_ID})

    assert resp.status_code == 409
    assert not run.pauses["setup-request"].is_set()
    assert run.pause_payloads == {}


def test_setup_request_resumes_while_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _client(tmp_path)
    _passkey_status(monkeypatch, confirmed=1, required=1)
    run = _start(app, _gate_steps(), spawn=True)
    assert _resume(client, "initial-passkey").status_code == 200
    _wait_for(lambda: any(e.get("pause_id") == "setup-request" for e in run.events))

    assert _resume(client, "setup-request", {"request_id": REQUEST_ID}).status_code == 200


def test_retry_recreated_gates_enforce_the_same_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = _client(tmp_path)
    data_root = app.state.settings.data_root
    (data_root / "envs" / ENV / "openbao-keys.json").write_text("{}", encoding="utf-8")
    real_steps: list[Step] = [
        CommandStep(id="post-seed", argv=["x"]),
        PauseStep(id="workstation", title="Prepare your workstation"),
        PauseStep(id="initial-passkey", title="Establish your first passkey"),
        PauseStep(id="setup-request", title="Authorize setup inventory"),
        CommandStep(id="configure", argv=["x"], setup_request_pause_id="setup-request"),
    ]
    # Retry from configure: the original gates are behind the cursor, so the
    # route recreates them.
    monkeypatch.setattr("dmf_init.main.build_bootstrap_steps", lambda ctx: list(real_steps))
    monkeypatch.setattr("dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {})
    spawned: list[BootstrapRun] = []
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: spawned.append(run))
    failed = BootstrapRun(run_id="failed", steps=list(real_steps), env_id=ENV)
    failed.failed_step_id = "configure"
    failed.emit({"event": "error", "step": "configure", "error": "x", "hint": ""}, terminal=True)
    app.state.bootstrap_runs["failed"] = failed

    resp = client.post("/api/bootstrap/retry", json={"run_id": "failed"})
    assert resp.status_code == 200
    retry = spawned[0]
    assert [s.id for s in retry.steps][:3] == ["workstation", "initial-passkey", "setup-request"]
    retry.executor = OkExecutor()
    app.state.bootstrap_runs["r1"] = retry
    threading.Thread(target=run_worker, args=(retry,), daemon=True).start()
    _wait_for(lambda: any(e.get("pause_id") == "workstation" for e in retry.events))

    assert _resume(client, "initial-passkey").status_code == 409  # not active yet
    assert _resume(client, "workstation").status_code == 200
    _wait_for(lambda: any(e.get("pause_id") == "initial-passkey" for e in retry.events))

    _passkey_status(monkeypatch, confirmed=0, required=2)
    assert _resume(client, "initial-passkey").status_code == 409
    _passkey_status(monkeypatch, confirmed=2, required=2)
    assert _resume(client, "initial-passkey").status_code == 200

