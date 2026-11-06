"""A retry resumes only in the gate mode its run was built with."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_retry import _authenticate, _make_env_on_disk, _seed_current_download

import dmf_init.main as main
from dmf_init.orchestrate import BootstrapRun, run_worker
from dmf_init.settings import Settings

ENV = "sandbox-alpha"


class _FailEverything:
    def run(self, step):
        yield "", 1


class _Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path / "data"
        _, self.render_dir = _make_env_on_disk(self.root, ENV)
        (self.root / "envs" / ENV / "openbao-keys.json").write_text("{}")
        self.spawned: list = []
        monkeypatch.setattr(main, "run_worker", lambda run: self.spawned.append(run))

    def write_cursor(self, **fields) -> None:
        payload = {"final_status": "error", **fields}
        (self.render_dir / "resume.json").write_text(json.dumps(payload))

    def cursor(self) -> dict:
        return json.loads((self.render_dir / "resume.json").read_text())

    def retry(self, gate: bool):
        app = main.create_app(
            Settings(data_root=self.root, tls_enabled=False, setup_request_gate=gate)
        )
        client = TestClient(app)
        _authenticate(client, app)
        _seed_current_download(app, ENV)
        response = client.post(
            "/api/bootstrap/retry", json={"env_id": ENV, "passphrase": "passphrase"}
        )
        if response.status_code != 200:
            return response, None
        for _ in range(200):
            if self.spawned:
                return response, self.spawned.pop()
            time.sleep(0.01)
        raise AssertionError("worker did not spawn")


def _graph(h: _Harness, gate: bool) -> list[str]:
    ctx = main.BootstrapContext.from_data_root(h.root, ENV, remotes=[])
    return [s.id for s in main.build_bootstrap_steps(ctx, setup_request_gate=gate)]


# Spelled out so a change to either graph shows up as a test failure.
_GATE_ON = [
    "pre-seed",
    "checkpoint-2",
    "checkpoint-2-export-gate",
    "unseal",
    "seed-bao",
    "post-seed",
    "workstation",
    "initial-passkey",
    "setup-request",
    "configure",
    "passkey",
    "verify",
    "checkpoint-3",
]
_GATE_OFF = [
    "pre-seed",
    "checkpoint-2",
    "checkpoint-2-export-gate",
    "unseal",
    "seed-bao",
    "post-seed",
    "configure",
    "workstation",
    "passkey",
    "verify",
    "checkpoint-3",
]
_CASES = [(True, c) for c in _GATE_ON] + [(False, c) for c in _GATE_OFF]


def test_enumeration_matches_the_real_graphs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    assert _graph(h, True) == _GATE_ON
    assert _graph(h, False) == _GATE_OFF


@pytest.mark.parametrize(("mode", "cursor"), _CASES)
def test_same_mode_retry_resumes_at_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: bool, cursor: str
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    ids = _graph(h, mode)
    h.write_cursor(failed_step_id=cursor, setup_request_gate=mode)

    response, run = h.retry(mode)

    assert response.status_code == 200
    got = [s.id for s in run.steps if s.id != "bao-preflight"]
    expected = ids[ids.index(cursor) :]
    if mode and cursor == "configure":
        # The original gates sit behind the cursor, so the route recreates them.
        expected = ["workstation", "initial-passkey", "setup-request", *expected]
    assert got == expected
    assert ("configure" in got) == (ids.index(cursor) <= ids.index("configure"))


@pytest.mark.parametrize(("mode", "cursor"), _CASES)
def test_other_mode_retry_is_refused_and_schedules_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: bool, cursor: str
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    h.write_cursor(failed_step_id=cursor, setup_request_gate=mode)

    response, run = h.retry(not mode)

    assert response.status_code == 409
    assert run is None
    assert h.spawned == []


@pytest.mark.parametrize("recorded", ["missing", "true", "false", 1, 0, None, [], {}])
@pytest.mark.parametrize("mode", [True, False])
def test_unrecorded_or_non_boolean_mode_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: bool, recorded: object
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    fields: dict = {"failed_step_id": "configure"}
    if recorded != "missing":
        fields["setup_request_gate"] = recorded
    h.write_cursor(**fields)

    response, run = h.retry(mode)

    assert response.status_code == 409
    assert run is None
    assert h.spawned == []


def test_preflight_failure_then_mode_change_does_not_skip_configure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    h.write_cursor(failed_step_id="configure", setup_request_gate=True)

    _, run = h.retry(True)
    assert run.steps[0].id == "bao-preflight"
    run.executor = _FailEverything()
    run_worker(run)

    assert h.cursor()["setup_request_gate"] is True

    response, run2 = h.retry(False)
    assert response.status_code == 409
    assert run2 is None


@pytest.mark.parametrize("mode", [True, False])
def test_journal_records_the_mode_for_every_terminal_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: bool
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    ctx = main.BootstrapContext.from_data_root(h.root, ENV, remotes=[])
    for step in _graph(h, mode):
        run = BootstrapRun(
            run_id="r",
            env_id=ENV,
            steps=main.build_bootstrap_steps(ctx, setup_request_gate=mode),
            journal_fn=main.make_resume_journal_fn(ctx),
            setup_request_gate=mode,
        )
        run.failed_step_id = step
        run.emit({"event": "error", "step": step}, terminal=True)
        assert h.cursor()["setup_request_gate"] is mode
        assert h.cursor()["failed_step_id"] == step


def test_fresh_run_writes_its_own_cursor_and_inherits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    h.write_cursor(
        failed_step_id="verify",
        setup_request_gate=False,
        completed_step_ids=["configure"],
    )
    ctx = main.BootstrapContext.from_data_root(h.root, ENV, remotes=[])
    fresh = BootstrapRun(
        run_id="new",
        env_id=ENV,
        steps=main.build_bootstrap_steps(ctx, setup_request_gate=True),
        journal_fn=main.make_resume_journal_fn(ctx),
        setup_request_gate=True,
    )
    fresh.failed_step_id = "post-seed"
    fresh.emit({"event": "error", "step": "post-seed"}, terminal=True)

    cursor = h.cursor()
    assert cursor["failed_step_id"] == "post-seed"
    assert cursor["setup_request_gate"] is True
    assert cursor["run_id"] == "new"
    assert "completed_step_ids" not in cursor


@pytest.mark.parametrize(
    ("recorded", "setting", "code"),
    [
        (True, True, 200),
        (False, False, 200),
        (True, False, 409),
        (False, True, 409),
        (None, False, 409),
    ],
)
def test_in_memory_retry_checks_the_recorded_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded: bool | None,
    setting: bool,
    code: int,
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    ctx = main.BootstrapContext.from_data_root(h.root, ENV, remotes=[])
    run = BootstrapRun(
        run_id="mem",
        env_id=ENV,
        steps=main.build_bootstrap_steps(ctx, setup_request_gate=bool(recorded)),
        setup_request_gate=recorded,
    )
    run.failed_step_id = "configure"
    run.emit({"event": "error", "step": "configure"}, terminal=True)
    app = main.create_app(Settings(data_root=h.root, tls_enabled=False, setup_request_gate=setting))
    client = TestClient(app)
    _authenticate(client, app)
    _seed_current_download(app, ENV)
    app.state.bootstrap_runs["mem"] = run

    response = client.post(
        "/api/bootstrap/retry", json={"run_id": "mem", "passphrase": "passphrase"}
    )

    assert response.status_code == code
