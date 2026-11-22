from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dmf_init.backup import (
    BackupManifestMeta,
    _build_inner_tar_bytes,
    backup,
    restore,
)


def _require_tools() -> None:
    missing = [tool for tool in ("age", "age-keygen") if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


def _run_age_keygen(key_path: Path) -> None:
    subprocess.run(
        ["age-keygen", "-o", str(key_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_backup_round_trip_and_determinism(tmp_path: Path) -> None:
    _require_tools()

    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir = data_root / "envs" / env_id
    env_dir.mkdir(parents=True)
    (env_dir / "bundle.sops.yaml").write_text("sops: {}\n", encoding="utf-8")
    (env_dir / "manifest.yaml").write_text("manifest: {}\n", encoding="utf-8")
    (env_dir / "inventory").mkdir()
    (env_dir / "inventory" / "hosts.yml").write_text("all: {}\n", encoding="utf-8")
    (env_dir / "openbao-keys").mkdir()
    (env_dir / "openbao-keys" / "tier3.key").write_text("unseal-key\n", encoding="utf-8")
    (env_dir / "ssh").mkdir()
    (env_dir / "ssh" / "id_ed25519").write_text("ssh-private-key\n", encoding="utf-8")
    answers_file = data_root / "answers.yaml"
    answers_file.write_text("operator: alice\n", encoding="utf-8")

    provenance_dir = data_root / "provenance"
    provenance_dir.mkdir()
    (provenance_dir / "repos.json").write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "name": "dmf-env",
                        "ref": "main",
                        "sha": "abc123",
                        "source_url": "https://example.invalid/dmf-env.git",
                        "destination": "/tmp/dmf-env",
                    }
                ]
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    age_key_path = data_root / "backup.age.key"
    _run_age_keygen(age_key_path)
    passphrase = "correct horse battery staple"

    result = backup(
        env_dir=env_dir,
        age_key_path=age_key_path,
        answers_file=answers_file,
        passphrase=passphrase,
        manifest_meta=BackupManifestMeta(
            env_id=env_id,
            profile="sandbox-single-node",
            schema_version="2026-06-02",
        ),
    )

    artifact_path = Path(result.artifact_path)
    assert artifact_path.exists()
    assert result.manifest_path.endswith("MANIFEST.json")
    assert result.inner_sha256 == hashlib.sha256(_build_inner_tar_bytes(env_dir)).hexdigest()
    assert result.manifest.age_recipient.startswith("age1")

    restore_dir = tmp_path / "restore"
    restore_result = restore(artifact_path, passphrase, restore_dir)

    assert restore_result.verified is True
    assert restore_result.inner_sha256 == result.inner_sha256
    assert Path(restore_result.age_key_path).read_text(encoding="utf-8") == age_key_path.read_text(
        encoding="utf-8"
    )
    assert Path(restore_result.answers_file_path).read_text(
        encoding="utf-8"
    ) == answers_file.read_text(encoding="utf-8")
    assert Path(restore_result.manifest_path).exists()
    assert restore_result.manifest.inner_sha256 == result.inner_sha256

    second_inner_sha256 = hashlib.sha256(_build_inner_tar_bytes(env_dir)).hexdigest()
    assert second_inner_sha256 == result.inner_sha256

    rendered = json.dumps(result.model_dump(), sort_keys=True)
    rendered += json.dumps(restore_result.model_dump(), sort_keys=True)
    assert passphrase not in rendered
    assert age_key_path.read_text(encoding="utf-8") not in rendered

    # cleanup contract: restore_root holds plaintext secrets and must be removable.
    restore_root = Path(restore_result.restore_root)
    assert restore_root.exists()
    restore_result.cleanup()
    assert not restore_root.exists()
