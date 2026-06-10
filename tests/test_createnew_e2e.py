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
