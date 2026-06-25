from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dmf_init.main import create_app
from dmf_init.package import PackageError, build_package, find_latest_artifact
from dmf_init.settings import Settings

ENV_ID = "sandbox-pkg"


def _seed_env(data_root: Path, env_id: str = ENV_ID) -> Path:
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
                "node_ip": "203.0.113.171",
                "base_domain": "dmf.test",
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
    return data_root


def _seed_artifact(data_root: Path, env_id: str = ENV_ID, stamp: str = "20260610T120000Z") -> Path:
    artifacts_dir = data_root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"dmf-backup-{env_id}-{stamp}.tar.age"
    path.write_bytes(b"encrypted-backup-bytes-" + stamp.encode())
    return path


FAKE_CA = {
    "filename": "dmf-ca.crt",
    "pem": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----",
    "present": True,
    "note": "",
    "requirement_note": "note",
}
FAKE_HOSTS = {
    "entries": ["203.0.113.171 console.dmf.test", "203.0.113.171 auth.dmf.test"],
    "node_ip": "203.0.113.171",
    "base_domain": "dmf.test",
    "note": "",
    "dns_note": "",
}


def test_find_latest_artifact_picks_newest(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed_artifact(data_root, stamp="20260610T100000Z")
    newest = _seed_artifact(data_root, stamp="20260610T130000Z")
    assert find_latest_artifact(data_root, ENV_ID) == newest


def test_find_latest_artifact_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(PackageError):
        find_latest_artifact(tmp_path / "data", ENV_ID)


def test_build_package_contents_and_manifest(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    artifact = _seed_artifact(data_root)

    result = build_package(data_root, ENV_ID, FAKE_CA, FAKE_HOSTS)
    assert result.artifact_name == artifact.name
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()

    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        names = set(archive.namelist())
        assert names == {artifact.name, "dmf-ca.crt", "README.md", "MANIFEST.json"}
        manifest = json.loads(archive.read("MANIFEST.json"))
        digests = {entry["name"]: entry["sha256"] for entry in manifest["files"]}
        assert digests[artifact.name] == hashlib.sha256(artifact.read_bytes()).hexdigest()
        readme = archive.read("README.md").decode("utf-8")
        assert artifact.name in readme
        assert "203.0.113.171 console.dmf.test" in readme
        # The passphrase must never be in the package; the README says so.
        assert "passphrase is NOT in this package" in " ".join(readme.split())


def test_build_package_without_ca(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed_artifact(data_root)
    absent_ca = {**FAKE_CA, "present": False, "pem": ""}

    result = build_package(data_root, ENV_ID, absent_ca, FAKE_HOSTS)
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert "dmf-ca.crt" not in archive.namelist()
        assert "not reachable" in archive.read("README.md").decode("utf-8")


def test_package_endpoint_streams_zip_and_records_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = _seed_env(tmp_path / "data")
    _seed_artifact(data_root)

    monkeypatch.setattr("dmf_init.main.build_ca_cert_payload", lambda ctx: FAKE_CA)
    monkeypatch.setattr("dmf_init.main.build_hosts_map_payload", lambda ctx: FAKE_HOSTS)

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}

    before = client.get(f"/api/package/{ENV_ID}/status")
    assert before.status_code == 200
    # An artifact is seeded, so the bundle is available but not yet downloaded
    # (and therefore not "current": nothing has been saved).
    assert before.json() == {
        "downloaded_at": None,
        "available": True,
        "current": False,
    }

    response = client.get(f"/api/package/{ENV_ID}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    body = response.content
    assert response.headers["x-package-sha256"] == hashlib.sha256(body).hexdigest()
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert "MANIFEST.json" in archive.namelist()

    after = client.get(f"/api/package/{ENV_ID}/status").json()
    assert after["downloaded_at"] is not None
    assert after["sha256"] == response.headers["x-package-sha256"]


def test_package_status_available_true_when_artifact_present(tmp_path: Path) -> None:
    """#140: status reports available=True once a backup artifact exists, so the
    persistent download affordance can show from render onward."""
    data_root = _seed_env(tmp_path / "data")
    _seed_artifact(data_root)

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}

    status_body = client.get(f"/api/package/{ENV_ID}/status").json()
    assert status_body["available"] is True
    assert status_body["downloaded_at"] is None


def test_package_status_available_false_without_artifact(tmp_path: Path) -> None:
    """No artifact yet (pre-render / mid-render) → available=False, so the UI
    does not offer a download that would 404."""
    data_root = _seed_env(tmp_path / "data")  # env rendered, no artifact seeded

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}

    status_body = client.get(f"/api/package/{ENV_ID}/status").json()
    assert status_body["available"] is False
    assert status_body["downloaded_at"] is None
    assert status_body["current"] is False


def test_package_status_current_goes_stale_when_newer_artifact_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#140 P1: an early download is `current`, but once a newer backup is sealed
    the saved bundle is stale (`current=False`) so the UI stops claiming
    'safe to delete' against an incomplete recovery point."""
    data_root = _seed_env(tmp_path / "data")
    _seed_artifact(data_root, stamp="20260610T120000Z")  # early checkpoint backup

    monkeypatch.setattr("dmf_init.main.build_ca_cert_payload", lambda ctx: FAKE_CA)
    monkeypatch.setattr("dmf_init.main.build_hosts_map_payload", lambda ctx: FAKE_HOSTS)

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}

    # Download the early bundle → recorded against that artifact → current.
    assert client.get(f"/api/package/{ENV_ID}").status_code == 200
    after_download = client.get(f"/api/package/{ENV_ID}/status").json()
    assert after_download["downloaded_at"] is not None
    assert after_download["current"] is True

    # A later checkpoint backup is sealed → the saved bundle is now stale.
    _seed_artifact(data_root, stamp="20260610T130000Z")
    after_newer = client.get(f"/api/package/{ENV_ID}/status").json()
    assert after_newer["available"] is True
    assert after_newer["downloaded_at"] is not None
    assert after_newer["current"] is False


def test_package_endpoint_missing_artifact_404(tmp_path: Path) -> None:
    data_root = _seed_env(tmp_path / "data")

    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}

    # No artifact seeded → 404 from the artifact check, before any CA/hosts
    # builder (which would try SSH) runs.
    response = client.get(f"/api/package/{ENV_ID}")
    assert response.status_code == 404
    assert "no backup artifact" in response.text


def test_package_endpoint_unknown_env_404(tmp_path: Path) -> None:
    app = create_app(Settings(data_root=tmp_path / "data", tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}

    response = client.get("/api/package/sandbox-missing")
    assert response.status_code == 404
