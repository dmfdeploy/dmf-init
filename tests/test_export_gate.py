"""Tests for the ADR-0044 forced checkpoint-export gate (facet c, part 2).

The gate blocks /api/bootstrap/resume past checkpoint-2 until the current recovery
bundle is proven downloaded off tmpfs. Proof = a completed download record whose
artifact matches the latest artifact on disk (download_is_current).
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dmf_init.bootstrap_steps import CHECKPOINT_EXPORT_GATE_ID
from dmf_init.main import create_app, download_is_current
from dmf_init.settings import Settings

ARTIFACT = "dmf-backup-envtest-20260101T000000Z.tar.age"
ENV = "envtest"


# --- pure proof logic ---------------------------------------------------------


def test_download_is_current_truth_table() -> None:
    assert download_is_current(None, ARTIFACT) is False
    assert download_is_current({}, ARTIFACT) is False
    # downloaded but no artifact name recorded
    assert download_is_current({"downloaded_at": 1.0}, ARTIFACT) is False
    # stale: downloaded an older artifact than the latest on disk
    assert download_is_current(
        {"downloaded_at": 1.0, "artifact": "dmf-backup-envtest-20250101T000000Z.tar.age"},
        ARTIFACT,
    ) is False
    # no latest artifact on disk
    assert download_is_current({"downloaded_at": 1.0, "artifact": ARTIFACT}, None) is False
    # current: completed download of the latest artifact
    assert download_is_current({"downloaded_at": 1.0, "artifact": ARTIFACT}, ARTIFACT) is True


# --- server-enforced gate -----------------------------------------------------


class _StubRun:
    def __init__(self, env_id: str) -> None:
        self.env_id = env_id
        self.resumed: str | None = None

    def resume(self, pause_id: str, payload=None) -> None:
        self.resumed = pause_id


def _client_with_session(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}
    return client, app


def _seed_artifact(app) -> None:
    artifacts = app.state.settings.data_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / ARTIFACT).write_bytes(b"x")


def test_gate_refuses_resume_without_a_saved_bundle(tmp_path: Path) -> None:
    client, app = _client_with_session(tmp_path)
    _seed_artifact(app)  # bundle exists on tmpfs but was never downloaded
    run = _StubRun(ENV)
    app.state.bootstrap_runs["r1"] = run

    resp = client.post(
        "/api/bootstrap/resume", json={"run_id": "r1", "pause_id": CHECKPOINT_EXPORT_GATE_ID}
    )
    assert resp.status_code == 409
    assert "recovery bundle not saved" in resp.json()["detail"]
    assert run.resumed is None  # did not advance


def test_gate_allows_resume_once_current_bundle_downloaded(tmp_path: Path) -> None:
    client, app = _client_with_session(tmp_path)
    _seed_artifact(app)
    app.state.package_downloads[ENV] = {"downloaded_at": time.time(), "artifact": ARTIFACT}
    run = _StubRun(ENV)
    app.state.bootstrap_runs["r1"] = run

    resp = client.post(
        "/api/bootstrap/resume", json={"run_id": "r1", "pause_id": CHECKPOINT_EXPORT_GATE_ID}
    )
    assert resp.status_code == 200
    assert run.resumed == CHECKPOINT_EXPORT_GATE_ID


def test_gate_refuses_stale_download(tmp_path: Path) -> None:
    client, app = _client_with_session(tmp_path)
    _seed_artifact(app)
    # downloaded an OLDER artifact than the one now on disk → stale → refused
    app.state.package_downloads[ENV] = {
        "downloaded_at": time.time(),
        "artifact": "dmf-backup-envtest-20250101T000000Z.tar.age",
    }
    app.state.bootstrap_runs["r1"] = _StubRun(ENV)
    resp = client.post(
        "/api/bootstrap/resume", json={"run_id": "r1", "pause_id": CHECKPOINT_EXPORT_GATE_ID}
    )
    assert resp.status_code == 409


def test_other_pauses_are_not_gated(tmp_path: Path) -> None:
    """The gate applies only to the export-gate pause — other pauses resume freely
    even with no download recorded."""
    client, app = _client_with_session(tmp_path)
    run = _StubRun(ENV)
    app.state.bootstrap_runs["r1"] = run
    resp = client.post(
        "/api/bootstrap/resume", json={"run_id": "r1", "pause_id": "workstation"}
    )
    assert resp.status_code == 200
    assert run.resumed == "workstation"
