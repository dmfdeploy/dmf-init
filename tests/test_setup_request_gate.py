"""The setup-request gate is a setting that defaults off (dmfdeploy/dmfdeploy#844).

Off: configure runs without a setup request id and without the two pauses that
exist only to obtain one. On: the pauses, the id requirement and the server-side
checks behave as before.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_retry import _authenticate, _make_env_on_disk, _seed_current_download

from dmf_init import bootstrap_steps
from dmf_init.bootstrap_steps import build_bootstrap_steps
from dmf_init.main import create_app
from dmf_init.orchestrate import BootstrapRun, CommandStep, PauseStep, run_worker
from dmf_init.settings import Settings, load_settings

ENV = "sandbox-alpha"
GATE_IDS = {"initial-passkey", "setup-request"}


class RecordingExecutor:
    def __init__(self) -> None:
        self.steps: list[CommandStep] = []

    def run(self, step: CommandStep):
        self.steps.append(step)
        yield "", 0


def _fake_ctx() -> Any:
    return SimpleNamespace(
        env_id=ENV,
        repos_root=Path("/nonexistent"),
        command_env=lambda: {},
        run_playbook_argv=lambda name: ["ansible-playbook", name],
    )


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def _post_configure_steps(steps):
    ids = [s.id for s in steps]
    return ids[ids.index("post-seed") + 1 :]


@pytest.fixture
def stub_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "build_workstation_payload",
        "build_passkey_payload",
        "build_setup_request_payload",
    ):
        monkeypatch.setattr(bootstrap_steps, name, lambda ctx: {})


def test_setting_defaults_off_and_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().setup_request_gate is False
    monkeypatch.delenv("DMF_SETUP_REQUEST_GATE", raising=False)
    assert load_settings().setup_request_gate is False
    monkeypatch.setenv("DMF_SETUP_REQUEST_GATE", "true")
    assert load_settings().setup_request_gate is True
    monkeypatch.setenv("DMF_SETUP_REQUEST_GATE", "false")
    assert load_settings().setup_request_gate is False


def test_graph_without_the_gate_runs_configure_first(stub_payloads: None) -> None:
    steps = build_bootstrap_steps(_fake_ctx())
    tail = _post_configure_steps(steps)
    assert tail[:3] == ["configure", "workstation", "passkey"]
    assert not GATE_IDS & {s.id for s in steps}
    configure = next(s for s in steps if s.id == "configure")
    assert configure.setup_request_pause_id is None


def test_graph_with_the_gate_pauses_before_configure(stub_payloads: None) -> None:
    steps = build_bootstrap_steps(_fake_ctx(), setup_request_gate=True)
    assert _post_configure_steps(steps)[:5] == [
        "workstation",
        "initial-passkey",
        "setup-request",
        "configure",
        "passkey",
    ]
    configure = next(s for s in steps if s.id == "configure")
    assert configure.setup_request_pause_id == "setup-request"


def test_fresh_run_without_the_gate_reaches_configure_without_an_id(
    stub_payloads: None,
) -> None:
    steps = [
        s
        for s in build_bootstrap_steps(_fake_ctx())
        if s.id in {"configure", "workstation"}
    ]
    executor = RecordingExecutor()
    run = BootstrapRun(run_id="r1", steps=steps, executor=executor)
    threading.Thread(target=run_worker, args=(run,), daemon=True).start()
    _wait_for(lambda: any(e.get("pause_id") == "workstation" for e in run.events))

    assert [s.id for s in executor.steps] == ["configure"]
    assert "DMF_SETUP_REQUEST_ID" not in (executor.steps[0].env or {})
    assert not any(e["event"] == "error" for e in run.events)


def test_fresh_run_with_the_gate_waits_and_then_needs_an_id(stub_payloads: None) -> None:
    steps = [
        s
        for s in build_bootstrap_steps(_fake_ctx(), setup_request_gate=True)
        if s.id in {"initial-passkey", "setup-request", "configure"}
    ]
    executor = RecordingExecutor()
    run = BootstrapRun(run_id="r1", steps=steps, executor=executor)
    threading.Thread(target=run_worker, args=(run,), daemon=True).start()
    _wait_for(lambda: any(e.get("pause_id") == "initial-passkey" for e in run.events))
    assert executor.steps == []

    run.resume("initial-passkey")
    _wait_for(lambda: any(e.get("pause_id") == "setup-request" for e in run.events))
    assert executor.steps == []
    with pytest.raises(ValueError):
        run.resume("setup-request", {"request_id": "not-an-id"})
    run.resume("setup-request", {"request_id": "a" * 32})
    _wait_for(lambda: bool(executor.steps))
    assert executor.steps[0].env["DMF_SETUP_REQUEST_ID"] == "a" * 32


def _write_cursor(data_root: Path, failed_step_id: str, *, gate: bool) -> None:
    path = data_root / "runs" / ENV / "resume.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "env_id": ENV,
                "run_id": "old",
                "failed_step_id": failed_step_id,
                "setup_request_gate": gate,
                "final_status": "error",
                "finished_at": 1.0,
            }
        ),
        encoding="utf-8",
    )


def _retry_from_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, gate: bool):
    data_root = tmp_path / "data"
    env_dir, _ = _make_env_on_disk(data_root, ENV)
    (env_dir / "openbao-keys.json").write_text("{}", encoding="utf-8")
    _write_cursor(data_root, "configure", gate=gate)
    app = create_app(Settings(data_root=data_root, tls_enabled=False, setup_request_gate=gate))
    client = TestClient(app)
    _authenticate(client, app)
    _seed_current_download(app, ENV)
    monkeypatch.setattr("dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {})
    spawned: list[BootstrapRun] = []
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: spawned.append(run))
    response = client.post(
        "/api/bootstrap/retry",
        json={"env_id": ENV, "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text
    return spawned[0]


def test_disk_retry_without_the_gate_does_not_recreate_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _retry_from_disk(tmp_path, monkeypatch, gate=False)
    ids = [s.id for s in run.steps]
    assert not GATE_IDS & set(ids)
    configure = next(s for s in run.steps if s.id == "configure")
    assert configure.setup_request_pause_id is None


def test_disk_retry_with_the_gate_recreates_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _retry_from_disk(tmp_path, monkeypatch, gate=True)
    ids = [s.id for s in run.steps]
    assert ids[ids.index("workstation") :][:4] == [
        "workstation",
        "initial-passkey",
        "setup-request",
        "configure",
    ]
    configure = next(s for s in run.steps if s.id == "configure")
    assert configure.setup_request_pause_id == "setup-request"


def _client_with_run(tmp_path: Path, steps, *, gate: bool):
    data_root = tmp_path / "data"
    _make_env_on_disk(data_root, ENV)
    app = create_app(Settings(data_root=data_root, tls_enabled=False, setup_request_gate=gate))
    client = TestClient(app)
    _authenticate(client, app)
    run = BootstrapRun(run_id="r1", steps=steps, env_id=ENV, executor=RecordingExecutor())
    app.state.bootstrap_runs["r1"] = run
    return client, run


def _resume(client: TestClient, pause_id: str, payload: dict | None = None):
    body: dict[str, Any] = {"run_id": "r1", "pause_id": pause_id}
    if payload is not None:
        body["payload"] = payload
    return client.post("/api/bootstrap/resume", json=body)


def test_resume_route_rejects_gate_pauses_when_the_run_has_none(tmp_path: Path) -> None:
    client, run = _client_with_run(
        tmp_path, [CommandStep(id="configure", argv=["x"])], gate=False
    )
    assert _resume(client, "setup-request", {"request_id": "a" * 32}).status_code == 409
    assert _resume(client, "initial-passkey").status_code == 409
    assert run.pause_payloads == {}


def test_resume_route_with_the_gate_still_rejects_direct_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = [
        PauseStep(id="initial-passkey", title="Establish your first passkey"),
        PauseStep(id="setup-request", title="Authorize setup inventory"),
        CommandStep(id="configure", argv=["x"], setup_request_pause_id="setup-request"),
    ]
    client, run = _client_with_run(tmp_path, steps, gate=True)
    monkeypatch.setattr(
        "dmf_init.main.build_passkey_payload",
        lambda ctx: {"enrollment_url": "", "confirmed": 0, "required": 2},
    )
    threading.Thread(target=run_worker, args=(run,), daemon=True).start()
    _wait_for(lambda: any(e.get("pause_id") == "initial-passkey" for e in run.events))

    assert _resume(client, "initial-passkey").status_code == 409  # no passkey yet
    assert _resume(client, "setup-request", {"request_id": "a" * 32}).status_code == 409
    assert run.pause_payloads == {}
