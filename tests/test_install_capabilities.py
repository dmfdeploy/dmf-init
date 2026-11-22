"""dmf-init refuses a dmf-infra checkout that predates install-time seeding.

The install seeds OpenBao itself, and no step after it seeds. A cached checkout
from before that change would run an install that never seeds, so every path
that starts or resumes bootstrap steps checks the checkout first. A run that
failed at the retired seed-bao step is rebuilt, not resumed (ADR-0051).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from install_capabilities import write_install_capabilities
from test_main import _create_repo
from test_resume import _make_env_on_disk, _write_resume_cursor
from test_retry import _authenticate

from dmf_init.bootstrap_steps import ensure_runtime_repos, retired_step_message
from dmf_init.main import create_app
from dmf_init.repos import (
    INSTALL_CAPABILITIES_FILE,
    REPO_NAMES,
    RepoFetchRequest,
    StaleRuntimeReposError,
    missing_install_capabilities,
    require_install_capabilities,
)
from dmf_init.settings import Settings

CAPABILITY = "openbao-seed-in-install"
MARKER_PATH = INSTALL_CAPABILITIES_FILE.as_posix().removeprefix("dmf-infra/")


def _git_checkouts(data_root: Path) -> None:
    """Make every runtime repo a real checkout, as a reused data root has."""
    for name in REPO_NAMES:
        repo = data_root / "repos" / name
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e",
             "commit", "-q", "--allow-empty", "-m", "x"],
            check=True,
        )


def _remotes(tmp_path: Path, infra_files: dict[str, str]) -> Path:
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    for name in REPO_NAMES:
        files = infra_files if name == "dmf-infra" else {"README.md": f"{name}\n"}
        _create_repo(remotes, name, "main", files)
    return remotes


# -- the check itself ---------------------------------------------------------


def test_missing_file_counts_as_missing_every_capability(tmp_path: Path) -> None:
    assert missing_install_capabilities(tmp_path / "repos") == [CAPABILITY]


def test_file_without_the_capability_is_missing_it(tmp_path: Path) -> None:
    write_install_capabilities(tmp_path, ("something-else",))
    assert missing_install_capabilities(tmp_path / "repos") == [CAPABILITY]


def test_file_with_the_capability_passes_and_ignores_comments(tmp_path: Path) -> None:
    path = write_install_capabilities(tmp_path)
    path.write_text(f"# {CAPABILITY} is only named in a comment\n\n", encoding="utf-8")
    assert missing_install_capabilities(tmp_path / "repos") == [CAPABILITY]
    path.write_text(f"# header\n\n{CAPABILITY}\n", encoding="utf-8")
    assert missing_install_capabilities(tmp_path / "repos") == []
    require_install_capabilities(tmp_path / "repos")


def test_refusal_names_the_checkout_and_the_capability(tmp_path: Path) -> None:
    with pytest.raises(StaleRuntimeReposError) as raised:
        require_install_capabilities(tmp_path / "repos")
    message = str(raised.value)
    assert str(tmp_path / "repos" / "dmf-infra") in message
    assert CAPABILITY in message
    assert f"Remove {tmp_path / 'repos'}" in message


# -- ensure_runtime_repos: reuse and fresh clone ------------------------------


def test_reused_stale_checkout_is_refused(tmp_path: Path) -> None:
    _git_checkouts(tmp_path)
    with pytest.raises(StaleRuntimeReposError):
        ensure_runtime_repos(tmp_path, RepoFetchRequest())


def test_reused_current_checkout_is_accepted(tmp_path: Path) -> None:
    _git_checkouts(tmp_path)
    write_install_capabilities(tmp_path)
    result = ensure_runtime_repos(tmp_path, RepoFetchRequest())
    assert {repo.name for repo in result.repos} == set(REPO_NAMES)


def test_fresh_clone_of_a_stale_ref_is_refused(tmp_path: Path) -> None:
    remotes = _remotes(tmp_path, {"README.md": "old infra\n"})
    with pytest.raises(StaleRuntimeReposError):
        ensure_runtime_repos(
            tmp_path / "data", RepoFetchRequest(base_url=remotes.as_uri())
        )


def test_fresh_clone_of_a_current_ref_is_accepted(tmp_path: Path) -> None:
    remotes = _remotes(tmp_path, {MARKER_PATH: f"{CAPABILITY}\n"})
    result = ensure_runtime_repos(
        tmp_path / "data", RepoFetchRequest(base_url=remotes.as_uri())
    )
    assert {repo.name for repo in result.repos} == set(REPO_NAMES)


# -- endpoints ----------------------------------------------------------------


def _client(data_root: Path) -> tuple[TestClient, object]:
    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    _authenticate(client, app)
    return client, app


def _make_checkout_stale(data_root: Path) -> None:
    (data_root / "repos" / INSTALL_CAPABILITIES_FILE).unlink()


def test_repo_fetch_endpoint_refuses_a_stale_checkout(tmp_path: Path) -> None:
    _git_checkouts(tmp_path / "data")
    client, _ = _client(tmp_path / "data")
    response = client.post("/api/repos/fetch", json={})
    assert response.status_code == 409
    assert CAPABILITY in response.json()["detail"]
    assert "Remove" in response.json()["detail"]


def test_bootstrap_start_refuses_a_stale_checkout(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _make_env_on_disk(data_root, "sandbox-alpha")
    _make_checkout_stale(data_root)
    client, _ = _client(data_root)
    response = client.post(
        "/api/bootstrap/start",
        json={
            "env_id": "sandbox-alpha",
            "passphrase": "correct horse battery staple",
            "passphrase_confirm": "correct horse battery staple",
            "remotes": [],
        },
    )
    assert response.status_code == 409
    assert CAPABILITY in response.json()["detail"]


def test_retry_refuses_a_stale_checkout(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _make_env_on_disk(data_root, "sandbox-alpha")
    _write_resume_cursor(data_root, "sandbox-alpha", failed_step_id="pre-seed")
    _make_checkout_stale(data_root)
    client, _ = _client(data_root)
    response = client.post(
        "/api/bootstrap/retry",
        json={"env_id": "sandbox-alpha", "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 409
    assert CAPABILITY in response.json()["detail"]
    assert "Remove" in response.json()["detail"]


def test_retry_refuses_a_stale_checkout_for_a_live_run(tmp_path: Path) -> None:
    """The in-memory path reaches the same check as the disk path."""
    from dmf_init.orchestrate import BootstrapRun, CommandStep

    data_root = tmp_path / "data"
    _make_env_on_disk(data_root, "sandbox-alpha")
    _make_checkout_stale(data_root)
    client, app = _client(data_root)
    run = BootstrapRun(
        run_id="live-run",
        steps=[CommandStep(id="pre-seed", argv=["ignored"])],
        env_id="sandbox-alpha",
        passphrase="x",
        remotes=[],
        setup_request_gate=False,
    )
    run.final_status = "error"
    run.failed_step_id = "pre-seed"
    app.state.bootstrap_runs["live-run"] = run
    response = client.post(
        "/api/bootstrap/retry",
        json={"run_id": "live-run", "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 409
    assert CAPABILITY in response.json()["detail"]


# -- retired step ids ---------------------------------------------------------


def test_retired_step_message_covers_only_retired_ids() -> None:
    assert "no longer exists" in retired_step_message("seed-bao")
    assert retired_step_message("post-seed") is None
    assert retired_step_message("no-such-step") is None


def test_retry_of_a_legacy_seed_bao_cursor_says_rebuild(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _make_env_on_disk(data_root, "sandbox-alpha")
    _write_resume_cursor(data_root, "sandbox-alpha", failed_step_id="seed-bao")
    client, _ = _client(data_root)
    response = client.post(
        "/api/bootstrap/retry",
        json={"env_id": "sandbox-alpha", "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "seed-bao" in detail
    assert "no longer exists" in detail
    assert "Remove this environment and install again" in detail
    assert "not in build" not in detail


def test_unknown_failed_step_keeps_the_not_in_build_error(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    env_dir, _ = _make_env_on_disk(data_root, "sandbox-alpha")
    (env_dir / "openbao-keys.json").write_text("{}", encoding="utf-8")
    _write_resume_cursor(data_root, "sandbox-alpha", failed_step_id="no-such-step")
    client, _ = _client(data_root)
    response = client.post(
        "/api/bootstrap/retry",
        json={"env_id": "sandbox-alpha", "passphrase": "correct horse battery staple"},
    )
    assert response.status_code == 409
    assert "not in build" in response.json()["detail"]
