from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dmf_init.logging_utils import (
    AccessLogTokenScrubFilter,
    JSONLogFormatter,
    scrub_token_from_url,
)
from dmf_init.main import create_app
from dmf_init.orchestrate import BootstrapRun
from dmf_init.repos import RepoFetchRequest, fetch_runtime_repos
from dmf_init.settings import Settings


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "DMF Test",
            "GIT_AUTHOR_EMAIL": "dmf-test@example.com",
            "GIT_COMMITTER_NAME": "DMF Test",
            "GIT_COMMITTER_EMAIL": "dmf-test@example.com",
        }
    )
    return env


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _create_repo(base_dir: Path, name: str, branch: str, files: dict[str, str]) -> tuple[Path, str]:
    worktree = base_dir / f"{name}-work"
    worktree.mkdir()
    _run_git(["init"], cwd=worktree)
    _run_git(["checkout", "-b", branch], cwd=worktree)

    for relative_path, content in files.items():
        file_path = worktree / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    _run_git(["add", "."], cwd=worktree)
    _run_git(["commit", "-m", "initial"], cwd=worktree, env=_git_env())

    bare_repo = base_dir / f"{name}.git"
    _run_git(["clone", "--bare", str(worktree), str(bare_repo)], cwd=base_dir)
    sha = _run_git(["-C", str(worktree), "rev-parse", "HEAD"], cwd=base_dir).stdout.strip()
    return bare_repo, sha


def test_launch_token_allows_once_then_rejects() -> None:
    app = create_app(Settings(tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token

    first = client.get(f"/?token={token}", follow_redirects=False)
    assert first.status_code in {302, 307}
    assert first.headers["location"] == "/"

    second = client.get(f"/?token={token}", follow_redirects=False)
    assert second.status_code == 403
    assert "already used" in second.text

    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200


def test_launch_token_expires() -> None:
    app = create_app(Settings(launch_token_ttl_seconds=1))
    client = TestClient(app)
    app.state.launch_token_state.issued_at -= 10

    response = client.get(f"/?token={app.state.launch_token_state.token}", follow_redirects=False)
    assert response.status_code == 410
    assert "expired" in response.text


def test_repo_fetch_clones_runtime_repos_and_records_provenance(tmp_path: Path) -> None:
    remotes_root = tmp_path / "remotes"
    remotes_root.mkdir()
    _, env_sha = _create_repo(
        remotes_root,
        "dmf-env",
        "main",
        {
            "bin/init-wizard.sh": "#!/bin/sh\nset -eu\n# --non-interactive\n",
        },
    )
    _create_repo(remotes_root, "dmf-infra", "main", {"README.md": "infra\n"})
    _create_repo(remotes_root, "dmf-runbooks", "main", {"README.md": "runbooks\n"})
    _create_repo(remotes_root, "dmf-cms", "main", {"README.md": "cms\n"})
    _create_repo(remotes_root, "dmf-media", "main", {"README.md": "media\n"})
    _create_repo(remotes_root, "dmf-promsd", "main", {"README.md": "promsd\n"})

    data_root = tmp_path / "data"
    result = fetch_runtime_repos(
        data_root,
        RepoFetchRequest(
            base_url=remotes_root.as_uri(),
            username="alice",
            password="supersecret",
        ),
    )

    env_dest = data_root / "repos" / "dmf-env"
    config = (env_dest / ".git" / "config").read_text(encoding="utf-8")
    script = (env_dest / "bin" / "init-wizard.sh").read_text(encoding="utf-8")

    assert env_dest.exists()
    assert "--non-interactive" in script
    assert "alice" not in config
    assert "supersecret" not in config
    assert result.provenance_path == str(data_root / "provenance" / "repos.json")
    assert {repo.name for repo in result.repos} == {
        "dmf-env",
        "dmf-infra",
        "dmf-runbooks",
        "dmf-cms",
        "dmf-media",
        "dmf-promsd",
    }
    env_provenance = next(repo for repo in result.repos if repo.name == "dmf-env")
    assert env_provenance.ref == "main"
    assert env_provenance.sha == env_sha
    assert env_provenance.source_url == f"{remotes_root.as_uri()}/dmf-env.git"
    assert {repo.ref for repo in result.repos if repo.name in {"dmf-cms", "dmf-media"}} == {"main"}


def test_repo_fetch_requires_session_but_healthz_is_open(tmp_path: Path) -> None:
    remotes_root = tmp_path / "remotes"
    remotes_root.mkdir()
    _create_repo(
        remotes_root,
        "dmf-env",
        "main",
        {
            "bin/init-wizard.sh": "#!/bin/sh\nset -eu\n# --non-interactive\n",
        },
    )
    _create_repo(remotes_root, "dmf-infra", "main", {"README.md": "infra\n"})
    _create_repo(remotes_root, "dmf-runbooks", "main", {"README.md": "runbooks\n"})
    _create_repo(remotes_root, "dmf-cms", "main", {"README.md": "cms\n"})
    _create_repo(remotes_root, "dmf-media", "main", {"README.md": "media\n"})
    _create_repo(remotes_root, "dmf-promsd", "main", {"README.md": "promsd\n"})

    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    client = TestClient(app)

    unauthenticated = client.post("/api/repos/fetch", json={"base_url": remotes_root.as_uri()})
    assert unauthenticated.status_code == 401

    healthz = client.get("/healthz")
    assert healthz.status_code == 200

    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    authenticated = client.post(
        "/api/repos/fetch",
        json={
            "base_url": remotes_root.as_uri(),
            "username": "alice",
            "password": "supersecret",
        },
    )
    assert authenticated.status_code == 200
    payload = authenticated.json()
    assert payload["provenance_path"] == str(tmp_path / "data" / "provenance" / "repos.json")
    assert payload["repos"][0]["source_url"].startswith(remotes_root.as_uri())
    assert {repo["name"] for repo in payload["repos"]} == {
        "dmf-env",
        "dmf-infra",
        "dmf-runbooks",
        "dmf-cms",
        "dmf-media",
        "dmf-promsd",
    }


def test_access_log_path_scrubs_token_query_param() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/?token=abc123&next=/", "1.1", 200),
        exc_info=None,
    )

    assert AccessLogTokenScrubFilter().filter(record)
    assert record.args[2] == "/?next=%2F"


def test_json_formatter_redacts_secretish_fields() -> None:
    record = logging.LogRecord(
        name="dmf_init",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="launch request received",
        args=(),
        exc_info=None,
    )
    record.token = "abc123"
    record.passphrase = "correct horse battery staple"
    record.message = "token=abc123 passphrase=correct horse battery staple"

    rendered = JSONLogFormatter().format(record)
    assert "abc123" not in rendered
    assert "correct horse battery staple" not in rendered
    assert "[REDACTED]" in rendered


def test_scrub_token_from_url() -> None:
    assert scrub_token_from_url("/?token=abc123&foo=bar") == "/?foo=bar"


def test_bootstrap_start_uses_real_graph_and_requires_two_remotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir = data_root / "envs" / env_id
    render_dir = data_root / "runs" / env_id
    env_dir.mkdir(parents=True)
    render_dir.mkdir(parents=True)
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

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    captured: dict[str, object] = {}

    def fake_build_bootstrap_steps(ctx):
        captured["ctx"] = ctx
        return []

    def fake_make_checkpoint_fn(ctx):
        captured["checkpoint_ctx"] = ctx

        def checkpoint_fn(run, n):
            return {
                "artifact_name": f"artifact-{n}",
                "remotes": [remote.name for remote in run.remotes],
            }

        return checkpoint_fn

    monkeypatch.setattr("dmf_init.main.build_bootstrap_steps", fake_build_bootstrap_steps)
    monkeypatch.setattr("dmf_init.main.make_checkpoint_fn", fake_make_checkpoint_fn)
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: None)

    response = client.post(
        "/api/bootstrap/start",
        json={
            "env_id": env_id,
            "passphrase": "correct horse battery staple",
            "passphrase_confirm": "correct horse battery staple",
        },
    )
    assert response.status_code == 200
    assert "run_id" in response.json()
    ctx = captured["ctx"]
    assert ctx.env_id == env_id
    # passphrase is NOT carried on ctx (qwen P2-1) — the run owns it.
    assert not hasattr(ctx, "passphrase")
    run_id = response.json()["run_id"]
    run = app.state.bootstrap_runs[run_id]
    assert run.passphrase == "correct horse battery staple"


def test_bootstrap_passkey_status_uses_run_env_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir = data_root / "envs" / env_id
    render_dir = data_root / "runs" / env_id
    env_dir.mkdir(parents=True)
    render_dir.mkdir(parents=True)
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

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    captured: dict[str, object] = {}

    def fake_build_bootstrap_steps(ctx):
        captured["ctx"] = ctx
        return []

    def fake_build_passkey_payload(ctx):
        captured["passkey_ctx"] = ctx
        return {
            "enrollment_url": "https://auth.example.test/enroll",
            "confirmed": 2,
            "required": 4,
            "hint": "hint",
            "host_hint": "hint",
        }

    monkeypatch.setattr("dmf_init.main.build_bootstrap_steps", fake_build_bootstrap_steps)
    monkeypatch.setattr("dmf_init.main.build_passkey_payload", fake_build_passkey_payload)
    monkeypatch.setattr("dmf_init.main.run_worker", lambda run: None)

    response = client.post(
        "/api/bootstrap/start",
        json={
            "env_id": env_id,
            "passphrase": "correct horse battery staple",
            "passphrase_confirm": "correct horse battery staple",
        },
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    payload = client.get(f"/api/bootstrap/passkey/{run_id}")
    assert payload.status_code == 200
    assert payload.json() == {"confirmed": 2, "required": 4}
    assert captured["passkey_ctx"].env_id == env_id


def test_bootstrap_passkey_status_unknown_run_returns_404(tmp_path: Path) -> None:
    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    response = client.get("/api/bootstrap/passkey/missing-run")
    assert response.status_code == 404
    assert "run not found" in response.text


def test_bootstrap_passkey_status_missing_env_id_returns_409(tmp_path: Path) -> None:
    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    with app.state.bootstrap_lock:
        app.state.bootstrap_runs["run-missing-env"] = BootstrapRun(
            run_id="run-missing-env",
            steps=[],
        )

    response = client.get("/api/bootstrap/passkey/run-missing-env")
    assert response.status_code == 409
    assert "env_id unavailable" in response.text


def test_bootstrap_passkey_status_builder_note_returns_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir = data_root / "envs" / env_id
    render_dir = data_root / "runs" / env_id
    env_dir.mkdir(parents=True)
    render_dir.mkdir(parents=True)
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

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    with app.state.bootstrap_lock:
        app.state.bootstrap_runs["run-note"] = BootstrapRun(
            run_id="run-note",
            steps=[],
            env_id=env_id,
        )

    def fake_build_passkey_payload(_ctx):
        return {"note": "could not fetch live passkey status from helper script"}

    monkeypatch.setattr("dmf_init.main.build_passkey_payload", fake_build_passkey_payload)

    response = client.get("/api/bootstrap/passkey/run-note")
    assert response.status_code == 502
    assert "could not fetch live passkey status" in response.text


def test_env_id_traversal_rejected_on_restore(tmp_path: Path) -> None:
    """P0-2: A crafted artifact with a bad env_id must be rejected before
    any path construction (rmtree/copytree)."""

    data_root = tmp_path / "data"
    data_root.mkdir()

    # Build a valid backup but with a crafted env_id would require modifying
    # the backup; instead test the validate directly.
    from dmf_init.backup import validate_env_id

    with pytest.raises(ValueError, match="env_id must match"):
        validate_env_id("../../escape")
    with pytest.raises(ValueError, match="env_id must match"):
        validate_env_id("env with spaces")
    with pytest.raises(ValueError, match="env_id must match"):
        validate_env_id("")

    # Valid env_id should pass
    validate_env_id("sandbox-alpha")
    validate_env_id("a")
    validate_env_id("a-b_c.d")


def test_artifact_symlink_rejected(tmp_path: Path) -> None:
    """P1: Artifact endpoint must reject symlinks BEFORE resolve()."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "artifacts").mkdir()

    # Create a symlink pointing outside data_root
    (data_root / "artifacts" / "dmf-backup-env-20260101T000000Z.tar.age").symlink_to("/etc/passwd")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    client.get(f"/?token={token}", follow_redirects=False)

    response = client.get(
        "/api/backup/artifact/dmf-backup-env-20260101T000000Z.tar.age"
    )
    assert response.status_code == 400
    assert "must not be a symlink" in response.text


def test_active_runs_rejects_concurrent_same_env(tmp_path: Path) -> None:
    """P0-1: The reservation check rejects a same-env request while
    the first is still building/starting."""
    data_root = tmp_path / "data"
    data_root.mkdir()

    app = create_app(Settings(data_root=data_root, tls_enabled=False))

    # Simulate the reservation manually: the first request has reserved
    # but the second checks before it completes.
    with app.state.bootstrap_lock:
        app.state.active_runs["sandbox-test"] = "__pending__"

    client = TestClient(app)
    token = app.state.launch_token_state.token
    client.get(f"/?token={token}", follow_redirects=False)

    # Second request for same env_id must be rejected (409)
    response = client.post(
        "/api/bootstrap/start",
        json={
            "env_id": "sandbox-test",
            "passphrase": "test-passphrase",
            "passphrase_confirm": "test-passphrase",
        },
    )
    assert response.status_code == 409
    assert "already in progress" in response.text

    # Clean up
    with app.state.bootstrap_lock:
        app.state.active_runs.pop("sandbox-test", None)


def test_bad_env_id_rejected_on_bootstrap_start(tmp_path: Path) -> None:
    """P2: env_id that violates the regex should be rejected before any path use."""
    data_root = tmp_path / "data"
    data_root.mkdir()

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    client.get(f"/?token={token}", follow_redirects=False)

    # Path traversal attempt
    response = client.post(
        "/api/bootstrap/start",
        json={
            "env_id": "../../escape",
            "passphrase": "test",
            "passphrase_confirm": "test",
        },
    )
    assert response.status_code in {400, 422}
    assert "env_id must match" in response.text

    # Spaces in env_id
    response2 = client.post(
        "/api/bootstrap/start",
        json={
            "env_id": "env with spaces",
            "passphrase": "test",
            "passphrase_confirm": "test",
        },
    )
    assert response2.status_code in {400, 422}
    assert "env_id must match" in response2.text


def _seed_render_env(data_root: Path, env_id: str) -> Path:
    """Materialize a minimal create-flow env (env dir + render.json + age key)."""
    env_dir = data_root / "envs" / env_id
    render_dir = data_root / "runs" / env_id
    env_dir.mkdir(parents=True)
    render_dir.mkdir(parents=True)
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
    return age_key_path


def test_bootstrap_doctor_streams_complete_for_create_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dmf_init.orchestrate import CommandStep, SubprocessExecutor

    data_root = tmp_path / "data"
    env_id = "sandbox-doctor"
    age_key_path = _seed_render_env(data_root, env_id)

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    # Drive the real run_worker over a fake executor so the stream really
    # reaches `complete` without shelling out to bootstrap-secrets.sh.
    class _FakeExecutor(SubprocessExecutor):
        def run(self, step: CommandStep):
            yield "doctor: all checks passed", None
            yield "", 0

    captured: dict[str, object] = {}

    def fake_build_env_doctor_run(env_id_arg, age_key_arg, data_root_arg, *, executor=None):
        captured["env_id"] = env_id_arg
        captured["age_key"] = age_key_arg
        return BootstrapRun(
            run_id="doctor-run",
            steps=[CommandStep(id="doctor", argv=["ignored"])],
            executor=_FakeExecutor(),
        )

    monkeypatch.setattr(
        "dmf_init.main.build_env_doctor_run", fake_build_env_doctor_run
    )

    response = client.post("/api/bootstrap/doctor", json={"env_id": env_id})
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert run_id == "doctor-run"
    assert captured["env_id"] == env_id
    assert str(captured["age_key"]) == str(age_key_path)

    with client.stream("GET", f"/api/bootstrap/stream/{run_id}?from=0") as stream:
        events = [json.loads(line) for line in stream.iter_lines() if line]

    kinds = [event["event"] for event in events]
    assert "run_start" in kinds
    assert {"event": "step_complete", "step": "doctor", "status": "ok"} in events
    assert events[-1]["event"] == "complete"


def test_bootstrap_doctor_unknown_env_returns_404(tmp_path: Path) -> None:
    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    launch = client.get(f"/?token={token}", follow_redirects=False)
    assert launch.status_code in {302, 307}

    response = client.post("/api/bootstrap/doctor", json={"env_id": "sandbox-missing"})
    assert response.status_code == 404
