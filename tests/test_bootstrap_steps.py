from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from dmf_init import bootstrap_steps
from dmf_init.backup import BackupResult, RcloneRemoteSpec, restore
from dmf_init.bootstrap_steps import (
    BootstrapContext,
    BootstrapRenderMeta,
    _iter_secret_strings,
    build_bootstrap_steps,
    build_ca_cert_payload,
    build_hosts_map_payload,
    build_passkey_payload,
    make_checkpoint_fn,
)
from dmf_init.createnew import (
    CreateNewRenderRequest,
    OperatorInputs,
    SandboxInputs,
    run_render_create_new,
)
from dmf_init.orchestrate import BootstrapRun, CommandStep, run_worker
from dmf_init.repos import RepoFetchRequest, fetch_runtime_repos


@dataclass
class FakeExecutor:
    scripts: dict[str, tuple[list[str], int]]
    gates: dict[str, threading.Event] = field(default_factory=dict)

    def run(self, step: CommandStep):
        lines, exit_code = self.scripts[step.id]
        for line in lines:
            yield line, None
        gate = self.gates.get(step.id)
        if gate is not None and not gate.wait(timeout=5):
            raise AssertionError(f"timed out waiting for gate on {step.id}")
        yield "", exit_code


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


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


def _create_repo(base_dir: Path, name: str, branch: str, files: dict[str, str]) -> Path:
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


def _write_rclone_alias_config(config_path: Path, remote_a: Path, remote_b: Path) -> None:
    config_path.write_text(
        "\n".join(
            [
                "[remote-a]",
                "type = alias",
                f"remote = {remote_a}",
                "",
                "[remote-b]",
                "type = alias",
                f"remote = {remote_b}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)


def _make_context(
    tmp_path: Path, *, node_ip: str | None, base_domain: str | None
) -> BootstrapContext:
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
    render_json = {
        "env_id": env_id,
        "profile": "sandbox-single-node",
        "schema_version": 1,
        "render_dir": str(render_dir),
        "age_key_path": str(age_key_path),
        "answers_file_path": str(answers_file_path),
    }
    if node_ip is not None:
        render_json["node_ip"] = node_ip
    if base_domain is not None:
        render_json["base_domain"] = base_domain
    (render_dir / "render.json").write_text(
        json.dumps(render_json, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return BootstrapContext.from_data_root(data_root, env_id, [])


def test_build_ca_cert_payload_uses_live_ssh_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_context(tmp_path, node_ip="203.0.113.171", base_domain="dmf.test")

    def fake_run_openbao_ssh(
        _ctx: BootstrapContext, remote_argv: list[str]
    ) -> subprocess.CompletedProcess[str]:
        assert remote_argv[-1] == "json"
        return subprocess.CompletedProcess(
            remote_argv,
            0,
            stdout=json.dumps(
                {
                    "data": {
                        "tls.crt": "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCkNBCg=="
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(bootstrap_steps, "_run_openbao_ssh", fake_run_openbao_ssh)

    result = build_ca_cert_payload(ctx)
    assert result["filename"] == "dmf-ca.crt"
    assert result["pem"] == "-----BEGIN CERTIFICATE-----\nCA"
    assert result["present"] is True
    assert result["note"] == ""
    assert "requirement_note" in result


def test_build_hosts_map_payload_uses_live_ingress_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_context(tmp_path, node_ip="203.0.113.171", base_domain="dmf.test")

    def fake_run_openbao_ssh(
        _ctx: BootstrapContext, remote_argv: list[str]
    ) -> subprocess.CompletedProcess[str]:
        assert "kubectl" in remote_argv
        stdout = json.dumps(
            {
                "items": [
                    {
                        "spec": {
                            "rules": [
                                {"host": "console.dmf.test"},
                                {"host": "auth.dmf.test"},
                            ]
                        }
                    },
                    {
                        "spec": {
                            "routes": [
                                {"match": "Host(`registry.dmf.test`) && PathPrefix(`/`)"},
                                {"match": "Host(`grafana.dmf.test`)"},
                            ]
                        }
                    },
                ]
            }
        )
        return subprocess.CompletedProcess(remote_argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(bootstrap_steps, "_run_openbao_ssh", fake_run_openbao_ssh)

    payload = build_hosts_map_payload(ctx)
    assert payload["entries"] == [
        "203.0.113.171 console.dmf.test",
        "203.0.113.171 auth.dmf.test",
        "203.0.113.171 registry.dmf.test",
        "203.0.113.171 grafana.dmf.test",
    ]
    assert payload["node_ip"] == "203.0.113.171"
    assert payload["base_domain"] == "dmf.test"
    assert "hostnames" in payload["dns_note"]
    assert "hostnames" in payload["note"]


def test_build_passkey_payload_uses_live_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_context(tmp_path, node_ip="203.0.113.171", base_domain="dmf.test")

    def fake_run_passkey_helper(_ctx: BootstrapContext) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["get-passkey-enrollment-url.sh", "sandbox-alpha"],
            0,
            stdout=(
                "enrollment_url: https://auth.dmf.test/enroll\n"
                "confirmed passkeys: 3/4\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(bootstrap_steps, "_run_passkey_helper", fake_run_passkey_helper)

    assert build_passkey_payload(ctx) == {
        "enrollment_url": "https://auth.dmf.test/enroll",
        "confirmed": 3,
        "required": 4,
        "hint": "run get-passkey-enrollment-url.sh sandbox-alpha on the host",
        "host_hint": "run get-passkey-enrollment-url.sh sandbox-alpha on the host",
    }


def test_parse_inventory_ssh_target_handles_k3s_before_control(
    tmp_path: Path,
) -> None:
    hosts_ini = tmp_path / "hosts.ini"
    hosts_ini.write_text(
        "\n".join(
            [
                "[k3s]",
                (
                    "g830-j8ou-01 ansible_host=203.0.113.171 "
                    "k3s_node_ip=203.0.113.171 ansible_user=deploy"
                ),
                "",
                "[k3s_control]",
                "g830-j8ou-01",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert bootstrap_steps._parse_inventory_ssh_target(hosts_ini) == "deploy@203.0.113.171"


def test_bootstrap_graph_redacts_checkpoint_secrets_and_pauses_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_context(tmp_path, node_ip="203.0.113.171", base_domain="dmf.test")
    (ctx.env_dir / "openbao-keys.json").write_text(
        json.dumps(
            {
                "unseal_keys_b64": ["UNSEAL-SENTINEL"],
                "root_token": "ROOT-SENTINEL",
            }
        ),
        encoding="utf-8",
    )

    def fake_run_openbao_ssh(
        _ctx: BootstrapContext, remote_argv: list[str]
    ) -> subprocess.CompletedProcess[str]:
        if remote_argv[-1] == "json":
            return subprocess.CompletedProcess(
                remote_argv,
                0,
                stdout=json.dumps(
                    {
                        "data": {
                            "tls.crt": "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCkNBCg=="
                        }
                    }
                ),
                stderr="",
            )
        stdout = json.dumps(
            {
                "items": [
                    {
                        "spec": {
                            "rules": [
                                {"host": "console.dmf.test"},
                                {"host": "auth.dmf.test"},
                                {"host": "forgejo.dmf.test"},
                                {"host": "awx.dmf.test"},
                                {"host": "grafana.dmf.test"},
                                {"host": "netbox.dmf.test"},
                                {"host": "registry.dmf.test"},
                            ]
                        }
                    }
                ]
            }
        )
        return subprocess.CompletedProcess(remote_argv, 0, stdout=stdout, stderr="")

    def fake_run_passkey_helper(_ctx: BootstrapContext) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["get-passkey-enrollment-url.sh", "sandbox-alpha"],
            0,
            stdout=(
                "enrollment_url: https://auth.dmf.test/enroll\n"
                "confirmed passkeys: 2/4\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(bootstrap_steps, "_run_openbao_ssh", fake_run_openbao_ssh)
    monkeypatch.setattr(bootstrap_steps, "_run_passkey_helper", fake_run_passkey_helper)

    steps = build_bootstrap_steps(ctx)
    assert [step.id for step in steps] == [
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

    result = build_ca_cert_payload(ctx)
    assert result["filename"] == "dmf-ca.crt"
    assert result["pem"] == "-----BEGIN CERTIFICATE-----\nCA"
    assert result["present"] is True
    assert result["note"] == ""
    assert "requirement_note" in result
    assert build_hosts_map_payload(ctx)["entries"] == [
        "203.0.113.171 dmf.test",
        "203.0.113.171 console.dmf.test",
        "203.0.113.171 auth.dmf.test",
        "203.0.113.171 forgejo.dmf.test",
        "203.0.113.171 awx.dmf.test",
        "203.0.113.171 grafana.dmf.test",
        "203.0.113.171 netbox.dmf.test",
        "203.0.113.171 registry.dmf.test",
    ]
    assert build_passkey_payload(ctx) == {
        "enrollment_url": "https://auth.dmf.test/enroll",
        "confirmed": 2,
        "required": 4,
        "hint": "run get-passkey-enrollment-url.sh sandbox-alpha on the host",
        "host_hint": "run get-passkey-enrollment-url.sh sandbox-alpha on the host",
    }

    workstation = bootstrap_steps.build_workstation_payload(ctx)
    assert workstation["ca"] == build_ca_cert_payload(ctx)
    assert workstation["hosts"] == build_hosts_map_payload(ctx)

    absent_ctx = BootstrapContext(
        data_root=ctx.data_root,
        env_id=ctx.env_id,
        render_dir=ctx.render_dir,
        env_dir=ctx.env_dir,
        age_key_path=ctx.age_key_path,
        answers_file_path=ctx.answers_file_path,
        repos_root=ctx.repos_root,
        render_meta=BootstrapRenderMeta(
            profile="sandbox-single-node",
            schema_version=1,
            node_ip=None,
            base_domain=None,
        ),
    )
    absent_hosts = build_hosts_map_payload(absent_ctx)
    assert absent_hosts["entries"] == []
    assert absent_hosts["node_ip"] == ""
    assert absent_hosts["base_domain"] == ""
    assert "unavailable" in absent_hosts["note"]

    executor = FakeExecutor(
        scripts={
            "pre-seed": (["pre-seed ok"], 0),
            "unseal": (["unseal UNSEAL-SENTINEL"], 0),
            "seed-bao": (["seed ROOT-SENTINEL"], 0),
            "post-seed": (["post-seed ok"], 0),
            "configure": (["configure ok"], 0),
            "verify": (["verify ok"], 0),
        }
    )
    checkpoint_calls: list[int] = []

    def checkpoint_fn(run: BootstrapRun, n: int) -> dict[str, Any]:
        checkpoint_calls.append(n)
        if n == 2:
            data = json.loads((ctx.env_dir / "openbao-keys.json").read_text(encoding="utf-8"))
            for secret in _iter_secret_strings(data):
                run.add_secret(secret)
        return {"artifact_name": f"artifact-{n}", "remotes": ["remote-a", "remote-b"]}

    run = BootstrapRun(
        run_id="run-1",
        steps=steps,
        executor=executor,
        checkpoint_fn=checkpoint_fn,
        passphrase="test-passphrase",
    )
    worker = threading.Thread(target=run_worker, args=(run,), daemon=True)
    worker.start()

    # The forced export gate pauses first, immediately after checkpoint-2 and
    # before the long unattended phases. (At the orchestrate level it's a plain
    # pause; the download-proof enforcement lives in the /api/bootstrap/resume
    # endpoint, covered by test_export_gate.py.)
    _wait_for(
        lambda: any(
            ev["event"] == "pause" and ev["pause_id"] == "checkpoint-2-export-gate"
            for ev in run.events
        )
    )
    assert any(ev["event"] == "checkpoint" and ev["n"] == 2 for ev in run.events)
    assert not any(ev["event"] == "pause" and ev["pause_id"] == "workstation" for ev in run.events)
    run.resume("checkpoint-2-export-gate")

    _wait_for(
        lambda: any(ev["event"] == "pause" and ev["pause_id"] == "workstation" for ev in run.events)
    )
    assert not any(ev["event"] == "pause" and ev["pause_id"] == "passkey" for ev in run.events)
    log_lines = [event["line"] for event in run.events if event["event"] == "log"]
    assert log_lines
    assert all("UNSEAL-SENTINEL" not in line for line in log_lines)
    assert all("ROOT-SENTINEL" not in line for line in log_lines)
    assert any("[REDACTED]" in line for line in log_lines)

    run.resume("workstation")
    _wait_for(
        lambda: any(ev["event"] == "pause" and ev["pause_id"] == "passkey" for ev in run.events)
    )
    run.resume("passkey")

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert checkpoint_calls == [2, 3]
    pause_ids = [event["pause_id"] for event in run.events if event["event"] == "pause"]
    assert pause_ids == ["checkpoint-2-export-gate", "workstation", "passkey"]
    assert run.events[-1]["event"] == "complete"
    assert run.events[-1]["checkpoints"] == [2, 3]


def test_missing_openbao_keys_at_checkpoint_2_halts_before_unseal(tmp_path: Path) -> None:
    # qwen P1: if pre-seed did not produce openbao-keys.json, checkpoint #2 must
    # NOT silently proceed (which would stream the unseal key/root token in clear
    # at the unseal/seed-bao steps). It must terminate the run with an error,
    # before unseal starts.
    ctx = _make_context(tmp_path, node_ip="203.0.113.171", base_domain="dmf.test")
    assert not (ctx.env_dir / "openbao-keys.json").exists()

    executor = FakeExecutor(
        scripts={
            "pre-seed": (["pre-seed ok"], 0),
            # unseal/seed-bao would emit the crown jewels — they must NEVER run.
            "unseal": (["unseal SHOULD-NOT-RUN"], 0),
            "seed-bao": (["seed SHOULD-NOT-RUN"], 0),
        }
    )
    run = BootstrapRun(
        run_id="run-missing-keys",
        steps=build_bootstrap_steps(ctx),
        executor=executor,
        checkpoint_fn=make_checkpoint_fn(ctx),
        passphrase="test-passphrase",
    )
    worker = threading.Thread(target=run_worker, args=(run,), daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()

    assert run.events[-1]["event"] == "error"
    assert "openbao-keys.json" in run.events[-1]["error"]
    step_starts = {ev["step"] for ev in run.events if ev["event"] == "step_start"}
    assert "unseal" not in step_starts
    assert "seed-bao" not in step_starts
    # secrets wiped on terminal error
    assert run.passphrase is None
    assert run.secrets == set()


def _require_tools() -> None:
    missing = [
        tool
        for tool in ("sops", "age", "age-keygen", "yq", "rclone", "ssh-keygen", "git", "bash")
        if shutil.which(tool) is None
    ]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


def test_checkpoint_backup_round_trip_records_manifest_and_restores_openbao_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _create_repo(repo_root, "dmf-infra", "main", {"README.md": "infra\n"})
    _create_repo(repo_root, "dmf-runbooks", "main", {"README.md": "runbooks\n"})
    _create_repo(repo_root, "dmf-cms", "main", {"README.md": "cms\n"})
    _create_repo(repo_root, "dmf-media", "main", {"README.md": "media\n"})
    _create_repo(repo_root, "dmf-promsd", "main", {"README.md": "promsd\n"})

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

    env_dir = data_root / "envs" / render_result.env_id
    (env_dir / "openbao-keys.json").write_text(
        json.dumps(
            {
                "unseal_keys_b64": ["UNSEAL-SENTINEL"],
                "root_token": "ROOT-SENTINEL",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    remote_a = _create_local_remote(work_root, "remote-a")
    remote_b = _create_local_remote(work_root, "remote-b")
    rclone_config_path = work_root / "rclone.conf"
    _write_rclone_alias_config(rclone_config_path, remote_a, remote_b)
    remotes = [
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
    ]

    ctx = BootstrapContext.from_data_root(
        data_root,
        render_result.env_id,
        remotes,
    )
    captured_results: list[BackupResult] = []
    real_backup = bootstrap_steps.backup

    def recording_backup(*args: Any, **kwargs: Any):
        result = real_backup(*args, **kwargs)
        captured_results.append(result)
        return result

    monkeypatch.setattr(bootstrap_steps, "backup", recording_backup)
    checkpoint_fn = make_checkpoint_fn(ctx)
    run = BootstrapRun(
        run_id="run-checkpoint",
        steps=[],
        passphrase="correct horse battery staple",
        remotes=remotes,
    )

    checkpoint_2 = checkpoint_fn(run, 2)
    assert "artifact_name" in checkpoint_2
    assert captured_results[0].manifest.checkpoint == 2
    assert "UNSEAL-SENTINEL" in run.secrets
    assert "ROOT-SENTINEL" in run.secrets

    checkpoint_3 = checkpoint_fn(run, 3)
    assert "artifact_name" in checkpoint_3
    assert captured_results[1].manifest.checkpoint == 3

    restore_dir = work_root / "restore"
    # Artifact persisted to data_root/artifacts/
    artifact_path = data_root / "artifacts" / captured_results[0].artifact_name
    restore_result = restore(
        artifact_path,
        "correct horse battery staple",
        restore_dir,
    )
    restored_keys = Path(restore_result.env_dir) / "openbao-keys.json"
    assert restored_keys.exists()
    restored_payload = json.loads(restored_keys.read_text(encoding="utf-8"))
    assert "UNSEAL-SENTINEL" in json.dumps(restored_payload)
    assert "ROOT-SENTINEL" in json.dumps(restored_payload)
    restore_result.cleanup()
