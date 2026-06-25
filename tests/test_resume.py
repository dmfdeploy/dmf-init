"""Disk-backed resume (#143): persist the resume cursor + resume after GC.

A bootstrap run is garbage-collected `run_ttl_seconds` after it goes terminal
(default 30 min), and is also lost on any `docker run --rm` restart. Before this
change the resume cursor (`failed_step_id`) lived only in the in-memory run, so
`POST /api/bootstrap/retry` 404'd once the run was gone even though the env still
sat intact on disk. These tests cover persisting the cursor next to render.json
and resuming from it by env_id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dmf_init.bootstrap_steps import BootstrapContext
from dmf_init.main import create_app, make_resume_journal_fn
from dmf_init.orchestrate import (
    BootstrapRun,
    CheckpointStep,
    CommandStep,
    PauseStep,
    Step,
)
from dmf_init.settings import Settings


def _real_build_steps() -> list[Step]:
    """Subset of build_bootstrap_steps matching the real graph order."""
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


def _write_resume_cursor(
    data_root: Path,
    env_id: str,
    *,
    failed_step_id: str | None,
    final_status: str = "error",
) -> Path:
    path = data_root / "runs" / env_id / "resume.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "env_id": env_id,
                "run_id": "gc-d-run",
                "failed_step_id": failed_step_id,
                "final_status": final_status,
                "finished_at": 123.0,
            }
        ),
        encoding="utf-8",
    )
    return path


def _authenticate(client: TestClient, app: Any) -> None:
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}


# ─── journal_fn: cursor persistence ──────────────────────────────────────────


def test_resume_journal_writes_cursor_on_terminal_error(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    _make_env_on_disk(data_root, env_id)
    ctx = BootstrapContext.from_data_root(data_root, env_id)

    run = BootstrapRun(
        run_id="r1",
        steps=[CommandStep(id="post-seed", argv=["x"])],
        env_id=env_id,
        journal_fn=make_resume_journal_fn(ctx),
    )
    run.failed_step_id = "post-seed"
    run.emit(
        {"event": "error", "step": "post-seed", "error": "boom"}, terminal=True
    )

    cursor = json.loads((data_root / "runs" / env_id / "resume.json").read_text())
    assert cursor["failed_step_id"] == "post-seed"
    assert cursor["final_status"] == "error"
    assert cursor["env_id"] == env_id
    assert cursor["run_id"] == "r1"


def test_resume_journal_removes_cursor_on_complete(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    _make_env_on_disk(data_root, env_id)
    ctx = BootstrapContext.from_data_root(data_root, env_id)
    # A stale cursor from a prior failed attempt must be cleared on success.
    cursor_path = _write_resume_cursor(data_root, env_id, failed_step_id="post-seed")

    run = BootstrapRun(
        run_id="r2",
        steps=[CommandStep(id="x", argv=["x"])],
        env_id=env_id,
        journal_fn=make_resume_journal_fn(ctx),
    )
    run.emit(
        {"event": "complete", "run_id": "r2", "checkpoints": []}, terminal=True
    )

    assert not cursor_path.exists()


# ─── disk-backed retry (the #143 fix) ────────────────────────────────────────


def test_retry_disk_resume_after_gc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISCRIMINATING: with no in-memory run, retry by env_id resumes from disk.

    Old code requires an in-memory run keyed by run_id and 404s once the run is
    GC'd; the env_id path reconstructs the failed-step cursor from disk and
    spawns a sliced resume run. Fails on origin/main (no env_id path), passes here.
    """
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir, _ = _make_env_on_disk(data_root, env_id)
    # Slice from post-seed is past CP2, so openbao-keys.json must exist (A1 guard).
    (env_dir / "openbao-keys.json").write_text("{}", encoding="utf-8")
    _write_resume_cursor(data_root, env_id, failed_step_id="post-seed")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    monkeypatch.setattr(
        "dmf_init.main.build_bootstrap_steps", lambda ctx: _real_build_steps()
    )
    monkeypatch.setattr(
        "dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {}
    )
    spawned: list[BootstrapRun] = []
    monkeypatch.setattr(
        "dmf_init.main.run_worker", lambda run: spawned.append(run)
    )

    # No in-memory run exists (it was GC'd / lost to --rm). Resume by env_id.
    response = client.post(
        "/api/bootstrap/retry",
        json={"env_id": env_id, "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 200
    assert len(spawned) == 1
    assert [s.id for s in spawned[0].steps] == [
        "post-seed",
        "configure",
        "workstation",
        "passkey",
        "verify",
        "checkpoint-3",
    ]
    assert spawned[0].env_id == env_id


def test_retry_stale_run_id_with_env_id_falls_back_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-tab Retry after GC: the frontend still holds the GC'd run_id but now
    also sends env_id, so the backend falls back to the disk cursor (Path B).

    This is the contract the in-session Retry button relies on (it sends both).
    """
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir, _ = _make_env_on_disk(data_root, env_id)
    (env_dir / "openbao-keys.json").write_text("{}", encoding="utf-8")
    _write_resume_cursor(data_root, env_id, failed_step_id="post-seed")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    monkeypatch.setattr(
        "dmf_init.main.build_bootstrap_steps", lambda ctx: _real_build_steps()
    )
    monkeypatch.setattr(
        "dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {}
    )
    spawned: list[BootstrapRun] = []
    monkeypatch.setattr(
        "dmf_init.main.run_worker", lambda run: spawned.append(run)
    )

    # run_id is gone from memory (GC'd); env_id rescues it via the disk cursor.
    response = client.post(
        "/api/bootstrap/retry",
        json={"run_id": "gc-d-run", "env_id": env_id, "passphrase": "pw"},
    )
    assert response.status_code == 200
    assert len(spawned) == 1
    assert spawned[0].steps[0].id == "post-seed"


def test_retry_run_id_only_still_404_after_gc(tmp_path: Path) -> None:
    """A caller that only knows the GC'd run_id (no env_id) still 404s.

    Confirms the env_id path — not some accidental widening — is what enables
    resume; the in-memory run_id alone cannot find a GC'd run.
    """
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    _make_env_on_disk(data_root, env_id)
    _write_resume_cursor(data_root, env_id, failed_step_id="post-seed")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    response = client.post(
        "/api/bootstrap/retry",
        json={"run_id": "gc-d-run", "passphrase": "pw"},
    )
    assert response.status_code == 404


def test_retry_env_id_409_past_cp2_without_openbao_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk-backed path must not bypass the openbao fail-closed guard.

    Cursor points at `configure` (slice is past CP2) and openbao-keys.json is
    absent → resuming would stream unredacted secrets → 409, same as the
    in-memory path.
    """
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    _make_env_on_disk(data_root, env_id)  # no openbao-keys.json
    _write_resume_cursor(data_root, env_id, failed_step_id="configure")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    monkeypatch.setattr(
        "dmf_init.main.build_bootstrap_steps", lambda ctx: _real_build_steps()
    )
    monkeypatch.setattr(
        "dmf_init.main.make_checkpoint_fn", lambda ctx: lambda run, n: {}
    )
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: None)

    response = client.post(
        "/api/bootstrap/retry",
        json={"env_id": env_id, "passphrase": "pw"},
    )
    assert response.status_code == 409
    assert "openbao-keys.json" in response.json()["detail"]


def test_retry_env_id_404_without_cursor(tmp_path: Path) -> None:
    """env_id resume with no cursor on disk → 404 (nothing to resume)."""
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    _make_env_on_disk(data_root, env_id)  # rendered, but never ran → no cursor

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    response = client.post(
        "/api/bootstrap/retry",
        json={"env_id": env_id, "passphrase": "pw"},
    )
    assert response.status_code == 404


def test_retry_env_id_rejects_path_traversal(tmp_path: Path) -> None:
    """A malicious env_id must be rejected before any path use (400)."""
    data_root = tmp_path / "data"
    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    response = client.post(
        "/api/bootstrap/retry",
        json={"env_id": "../../etc/passwd", "passphrase": "pw"},
    )
    assert response.status_code == 400


# ─── /api/envs landing affordance ────────────────────────────────────────────


def test_list_envs_reports_resumable(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _make_env_on_disk(data_root, "env-failed")
    _write_resume_cursor(data_root, "env-failed", failed_step_id="post-seed")
    _make_env_on_disk(data_root, "env-clean")  # rendered, no cursor

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)

    response = client.get("/api/envs")
    assert response.status_code == 200
    envs = {e["env_id"]: e for e in response.json()["envs"]}

    assert envs["env-failed"]["resumable"] is True
    assert envs["env-failed"]["failed_step_id"] == "post-seed"
    assert envs["env-failed"]["profile"] == "sandbox-single-node"
    assert envs["env-clean"]["resumable"] is False


def test_list_envs_active_env_not_resumable(tmp_path: Path) -> None:
    """An env with a live run in flight must not be offered as resumable."""
    data_root = tmp_path / "data"
    _make_env_on_disk(data_root, "env-active")
    _write_resume_cursor(data_root, "env-active", failed_step_id="post-seed")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)
    with app.state.bootstrap_lock:
        app.state.active_runs["env-active"] = "live-run"

    response = client.get("/api/envs")
    envs = {e["env_id"]: e for e in response.json()["envs"]}
    assert envs["env-active"]["active"] is True
    assert envs["env-active"]["resumable"] is False
