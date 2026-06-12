from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dmf_init.backup import RcloneRemoteSpec, restore
from dmf_init.createnew import (
    CreateNewBackupRequest,
    CreateNewRenderRequest,
    OperatorInputs,
    SandboxInputs,
    run_backup_create_new,
    run_render_create_new,
)
from dmf_init.repos import RepoFetchRequest, fetch_runtime_repos


def _require_tools() -> None:
    missing = [
        tool
        for tool in ("sops", "age", "age-keygen", "yq", "rclone", "ssh-keygen", "git", "bash")
        if shutil.which(tool) is None
    ]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


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
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _create_git_repo(base_dir: Path, name: str, branch: str, files: dict[str, str]) -> Path:
    worktree = base_dir / f"{name}-work"
    worktree.mkdir()
    _run_git(["init"], cwd=worktree)
    _run_git(["checkout", "-b", branch], cwd=worktree)
    for relative_path, content in files.items():
        path = worktree / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run_git(["add", "."], cwd=worktree)
    _run_git(["commit", "-m", "initial"], cwd=worktree, env=_git_env())
    bare_repo = base_dir / f"{name}.git"
    _run_git(["clone", "--bare", str(worktree), str(bare_repo)], cwd=base_dir)
    return bare_repo


def _create_local_remote(base_dir: Path, name: str) -> Path:
    remote_dir = base_dir / name
    remote_dir.mkdir()
    return remote_dir


def test_create_new_hermetic_round_trip(tmp_path: Path) -> None:
    _require_tools()

    source_checkout = os.getenv("DMF_TEST_DMF_ENV_REPO")
    if not source_checkout:
        pytest.skip("DMF_TEST_DMF_ENV_REPO is not set")

    source_repo = Path(source_checkout)
    if not source_repo.is_dir():
        pytest.skip(f"DMF_TEST_DMF_ENV_REPO is not a directory: {source_repo}")

    branch = subprocess.run(
        ["git", "-C", str(source_repo), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not branch:
        pytest.skip("DMF_TEST_DMF_ENV_REPO must be on a named branch")

    work_root = tmp_path / "work"
    work_root.mkdir()
    repo_root = work_root / "repos"
    repo_root.mkdir()
    _run_git(["clone", "--bare", str(source_repo), str(repo_root / "dmf-env.git")], cwd=repo_root)
    _create_git_repo(repo_root, "dmf-infra", "main", {"README.md": "infra\n"})
    _create_git_repo(repo_root, "dmf-runbooks", "main", {"README.md": "runbooks\n"})
    _create_git_repo(repo_root, "dmf-cms", "main", {"README.md": "cms\n"})
    _create_git_repo(repo_root, "dmf-media", "main", {"README.md": "media\n"})
    _create_git_repo(repo_root, "dmf-promsd", "main", {"README.md": "promsd\n"})

    data_root = work_root / "data"
    fetch_result = fetch_runtime_repos(
        data_root,
        RepoFetchRequest(
            base_url=repo_root.as_uri(),
            refs={
                "dmf-env": branch,
                "dmf-infra": "main",
                "dmf-runbooks": "main",
                "dmf-cms": "main",
                "dmf-media": "main",
                "dmf-promsd": "main",
            },
        ),
    )
    assert fetch_result.repos[0].name == "dmf-env"

    ssh_key_path = work_root / "sandbox-node-key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(ssh_key_path), "-C", "dmf-init-e2e"],
        check=True,
        capture_output=True,
        text=True,
    )
    ssh_private_key = ssh_key_path.read_text(encoding="utf-8")

    render_result = run_render_create_new(
        data_root,
        CreateNewRenderRequest(
            operator=OperatorInputs(
                username="marty-mcfly",
                email="marty@dmf.test",
                display="Marty McFly",
            ),
            sandbox=SandboxInputs(
                label="demo",
                node_ip="203.0.113.171",
                ansible_user="lima",
                iface="lima0",
                ssh_private_key=ssh_private_key,
            ),
        ),
    )
    env_root = data_root / "envs" / render_result.env_id
    render_json = json.loads(
        (data_root / "runs" / render_result.env_id / "render.json").read_text(
            encoding="utf-8"
        )
    )
    env_ssh_key = env_root / "ssh" / "sandbox-node.key"
    env_ssh_pub = env_root / "ssh" / "sandbox-node.key.pub"
    rendered_inventory = env_root / "inventory" / "group_vars" / "all" / "main.yml"

    assert render_result.ssh_private_key_path == str(env_ssh_key)
    assert render_json["node_ip"] == "203.0.113.171"
    assert render_json["base_domain"] == "dmf-sandbox.dmf.test"
    assert env_ssh_key.exists()
    assert env_ssh_pub.exists()
    assert env_ssh_key.read_text(encoding="utf-8") == ssh_private_key
    assert stat_mode(env_ssh_key) == 0o600
    assert stat_mode(env_ssh_pub) == 0o644
    assert str(env_ssh_key) in rendered_inventory.read_text(encoding="utf-8")

    remote_a = _create_local_remote(work_root, "remote-a")
    remote_b = _create_local_remote(work_root, "remote-b")
    backup_result = run_backup_create_new(
        data_root,
        CreateNewBackupRequest(
            env_id=render_result.env_id,
            passphrase="correct horse battery staple",
            passphrase_confirm="correct horse battery staple",
            remotes=[
                RcloneRemoteSpec(
                    name="remote-a",
                    type="alias",
                    options={"remote": str(remote_a)},
                    destination_prefix="backups",
                ),
                RcloneRemoteSpec(
                    name="remote-b",
                    type="alias",
                    options={"remote": str(remote_b)},
                    destination_prefix="backups",
                ),
            ],
        ),
    )

    remote_artifact = backup_result.backup.remote_artifacts[0]
    restore_root = work_root / "restore"
    restore_result = restore(
        remote_artifact,
        "correct horse battery staple",
        restore_root,
        rclone_config_path=Path(backup_result.backup.rclone_config_path),
    )

    restored_env_root = Path(restore_result.env_dir)
    assert (restored_env_root / "ssh" / "sandbox-node.key").exists()
    assert (restored_env_root / "ssh" / "sandbox-node.key.pub").exists()

    home_root = work_root / "home"
    dmf_dir = home_root / ".dmfdeploy" / "envs"
    dmf_dir.mkdir(parents=True, exist_ok=True)
    restored_env_link = dmf_dir / render_result.env_id
    restored_env_link.symlink_to(Path(restore_result.env_dir))

    doctor_env = os.environ.copy()
    doctor_env.update(
        {
            "HOME": str(home_root),
            "SOPS_AGE_KEY_FILE": restore_result.age_key_path,
        }
    )
    subprocess.run(
        [
            str(source_repo / "bin" / "bootstrap-secrets.sh"),
            "doctor",
            render_result.env_id,
        ],
        cwd=source_repo,
        check=True,
        capture_output=True,
        text=True,
        env=doctor_env,
    )

    restore_result.cleanup()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_stream_render_emits_error_event_for_invalid_ssh_key(tmp_path):
    """A garbage key must produce a clean {"event":"error"} line BEFORE the
    wizard runs — not an exception escaping the generator (which the browser
    sees as a dropped connection)."""
    import shutil as _shutil

    import pytest as _pytest

    if not _shutil.which("ssh-keygen"):
        _pytest.skip("ssh-keygen not available")

    import json as _json

    from dmf_init.createnew import (
        CreateNewRenderRequest,
        OperatorInputs,
        SandboxInputs,
        stream_render_create_new,
    )

    data_root = tmp_path / "data"
    # Minimal fake wizard so _require_runtime_repo passes; it must NOT run.
    wizard = data_root / "repos" / "dmf-env" / "bin" / "init-wizard.sh"
    wizard.parent.mkdir(parents=True)
    wizard.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    wizard.chmod(0o755)

    request = CreateNewRenderRequest(
        operator=OperatorInputs(username="op", email="op@dmf.test", display="Op"),
        sandbox=SandboxInputs(
            label="demo",
            node_ip="203.0.113.99",
            ansible_user="lima",
            iface="lima0",
            ssh_private_key="not a real key",
        ),
    )
    events = [_json.loads(line) for line in stream_render_create_new(data_root, request)]
    assert events, "stream produced no events"
    assert events[-1]["event"] == "error"
    assert "private key" in events[-1]["error"]


def test_stream_render_wraps_unexpected_failures_as_error_event(tmp_path):
    """CreateNewError (e.g. repos missing) surfaces as an error event, not an
    exception out of the generator."""
    import json as _json

    from dmf_init.createnew import (
        CreateNewRenderRequest,
        OperatorInputs,
        SandboxInputs,
        stream_render_create_new,
    )

    request = CreateNewRenderRequest(
        operator=OperatorInputs(username="op", email="op@dmf.test", display="Op"),
        sandbox=SandboxInputs(
            label="demo",
            node_ip="203.0.113.99",
            ansible_user="lima",
            iface="lima0",
            ssh_private_key="irrelevant",
        ),
    )
    events = [_json.loads(line) for line in stream_render_create_new(tmp_path / "empty", request)]
    assert events[-1]["event"] == "error"
    assert "repos" in events[-1]["error"]


def test_inner_generator_kills_wizard_on_close(tmp_path: Path) -> None:
    """Regression: on GeneratorExit the wizard AND ALL its descendants must be
    killed before the caller clears the single-flight flag.

    The fake wizard spawns a BACKGROUND DESCENDANT that writes a sentinel after
    2 seconds, then the parent sleeps 60s. We advance the generator once
    (wizard is running), close it, wait 3s, and assert the sentinel was NOT
    written. With the OLD parent-only kill the descendant survives (FAILS);
    with the group-kill the descendant dies (PASSES)."""
    import json as _json
    import time

    from dmf_init.createnew import (
        CreateNewRenderRequest,
        OperatorInputs,
        SandboxInputs,
        _stream_render_create_new_inner,
    )

    data_root = tmp_path / "data"
    sentinel = data_root / "SENTINEL"

    # Parent prints one line, spawns a background descendant, then sleeps forever.
    wizard = data_root / "repos" / "dmf-env" / "bin" / "init-wizard.sh"
    wizard.parent.mkdir(parents=True)
    wizard.write_text(
        "#!/bin/sh\n"
        "echo 'env_id: sandbox-test: started'\n"
        "sh -c 'sleep 2; touch \"$DMF_DATA_ROOT/SENTINEL\"' &\n"
        "sleep 60\n",
        encoding="utf-8",
    )
    wizard.chmod(0o755)

    # Valid SSH key so pre-wizard validation passes.
    key_path = tmp_path / "test-key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-C", "test"],
        check=True,
        capture_output=True,
    )
    ssh_key = key_path.read_text(encoding="utf-8")

    request = CreateNewRenderRequest(
        operator=OperatorInputs(username="op", email="op@dmf.test", display="Op"),
        sandbox=SandboxInputs(
            label="demo",
            node_ip="203.0.113.99",
            ansible_user="lima",
            iface="lima0",
            ssh_private_key=ssh_key,
        ),
    )
    gen = _stream_render_create_new_inner(data_root, request)
    first_event = _json.loads(next(gen))
    assert first_event["event"] == "log", f"expected log event, got {first_event}"

    # Cancel the generator — should trigger GeneratorExit, which kills the
    # entire process group (parent + descendant).
    gen.close()
    time.sleep(3)  # descendant would have written sentinel at t=2 without group-kill

    assert not sentinel.exists(), "descendant wrote sentinel after generator was closed"
