"""A retry never skips a step that hasn't completed, even across a gate-mode change."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_retry import _authenticate, _make_env_on_disk, _seed_current_download

import dmf_init.main as main
from dmf_init.orchestrate import run_worker
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


@pytest.mark.parametrize("first_gate", [True, False])
def test_preflight_failure_keeps_configure_across_mode_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_gate: bool
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    h.write_cursor(failed_step_id="configure")

    _, run = h.retry(first_gate)
    assert run.steps[0].id == "bao-preflight"
    run.executor = _FailEverything()
    run_worker(run)

    assert h.cursor()["failed_step_id"] == "configure"

    _, run2 = h.retry(not first_gate)
    assert "configure" in [s.id for s in run2.steps]


def test_gate_pause_failure_keeps_configure_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    h.write_cursor(failed_step_id="configure")
    _, run = h.retry(True)
    # An inserted step failing must not move the target past configure.
    run.failed_step_id = "workstation"
    run.final_status = "error"
    main.make_resume_journal_fn(
        main.BootstrapContext.from_data_root(h.root, ENV, remotes=[])
    )(run)

    assert h.cursor()["failed_step_id"] == "configure"
    _, run2 = h.retry(False)
    assert "configure" in [s.id for s in run2.steps]


@pytest.mark.parametrize("completed", [None, [], ["unseal"]])
def test_workstation_cursor_without_configure_proof_never_skips_configure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed: list[str] | None
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    fields: dict = {"failed_step_id": "workstation"}
    if completed is not None:
        fields["completed_step_ids"] = completed
    h.write_cursor(**fields)

    response, run = h.retry(False)

    if run is None:
        assert response.status_code == 409
    else:
        assert "configure" in [s.id for s in run.steps]


def test_workstation_cursor_with_configure_completed_resumes_at_workstation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    h.write_cursor(failed_step_id="workstation", completed_step_ids=["configure"])

    _, run = h.retry(False)

    ids = [s.id for s in run.steps]
    assert "workstation" in ids
    assert "configure" not in ids
