"""Tests for the install's local CA and the network-mode input."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dmf_init import bootstrap_steps
from dmf_init.bootstrap_steps import BootstrapContext, build_ca_cert_payload
from dmf_init.ca import LocalCaError, ca_info, create_local_ca
from dmf_init.createnew import (
    CreateNewRenderRequest,
    OperatorInputs,
    SandboxInputs,
    stream_render_create_new,
)
from dmf_init.main import create_app
from dmf_init.package import build_package
from dmf_init.settings import Settings

ALLOWED = "dmf.test"
PAGE = "dmf-abcd.local"

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is missing")


def _openssl(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["openssl", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _issue_leaf(env_root: Path, workdir: Path, name: str) -> Path:
    """Sign a leaf for `name` with the CA key and return the leaf certificate."""
    key = workdir / f"{name}.key"
    csr = workdir / f"{name}.csr"
    cert = workdir / f"{name}.crt"
    ext = workdir / f"{name}.ext"
    ext.write_text(f"subjectAltName=DNS:{name}\nextendedKeyUsage=serverAuth\n", encoding="utf-8")
    genkey = _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
    assert genkey.returncode == 0
    request = _openssl("req", "-new", "-key", str(key), "-subj", f"/CN={name}", "-out", str(csr))
    assert request.returncode == 0
    signed = _openssl(
        "x509", "-req", "-in", str(csr),
        "-CA", str(env_root / "ca" / "ca.crt"), "-CAkey", str(env_root / "ca" / "ca.key"),
        "-CAcreateserial", "-days", "30", "-extfile", str(ext), "-out", str(cert),
    )
    assert signed.returncode == 0, signed.stderr
    return cert


@needs_openssl
def test_name_constraint_allows_listed_names_and_rejects_others(tmp_path: Path) -> None:
    """Catches a CA built without nameConstraints, or with the wrong names:
    both would let the leaf for an unrelated name verify."""
    env_root = tmp_path / "env-one"
    create_local_ca(env_root, [ALLOWED, PAGE])
    ca_crt = env_root / "ca" / "ca.crt"

    for name in (f"console.{ALLOWED}", PAGE):
        leaf = _issue_leaf(env_root, tmp_path, name)
        ok = _openssl("verify", "-CAfile", str(ca_crt), str(leaf))
        assert ok.returncode == 0, ok.stdout + ok.stderr

    other = _issue_leaf(env_root, tmp_path, "bank.example.com")
    bad = _openssl("verify", "-CAfile", str(ca_crt), str(other))
    assert bad.returncode != 0
    assert "permitted subtree violation" in (bad.stdout + bad.stderr)


@needs_openssl
def test_create_local_ca_is_idempotent_and_keeps_key_private(tmp_path: Path) -> None:
    """Catches an implementation that regenerates the CA on a second call (the
    node and every trusting device would stop matching) or leaves the key
    readable by others."""
    env_root = tmp_path / "env-two"
    first = create_local_ca(env_root, [ALLOWED, PAGE])
    cert_bytes = (env_root / "ca" / "ca.crt").read_bytes()
    key_bytes = (env_root / "ca" / "ca.key").read_bytes()

    second = create_local_ca(env_root, ["other.test"])

    assert second.fingerprint_sha256 == first.fingerprint_sha256
    assert second.permitted_names == (ALLOWED, PAGE)
    assert (env_root / "ca" / "ca.crt").read_bytes() == cert_bytes
    assert (env_root / "ca" / "ca.key").read_bytes() == key_bytes
    assert stat.S_IMODE((env_root / "ca" / "ca.key").stat().st_mode) == 0o600


@needs_openssl
def test_stale_key_without_certificate_is_replaced(tmp_path: Path) -> None:
    """Catches a retry that fails forever because an earlier run stopped after
    writing the key and before writing the certificate."""
    env_root = tmp_path / "env-stale"
    (env_root / "ca").mkdir(parents=True)
    (env_root / "ca" / "ca.key").write_text("stale", encoding="utf-8")

    info = create_local_ca(env_root, [ALLOWED, PAGE])

    assert (env_root / "ca" / "ca.key").read_text(encoding="utf-8") != "stale"
    leaf = _issue_leaf(env_root, tmp_path, f"console.{ALLOWED}")
    ok = _openssl("verify", "-CAfile", str(env_root / "ca" / "ca.crt"), str(leaf))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert info.permitted_names == (ALLOWED, PAGE)


def test_certificate_without_key_is_an_error_and_writes_nothing(tmp_path: Path) -> None:
    """Catches a call that silently makes a new key next to an old certificate."""
    env_root = tmp_path / "env-nokey"
    (env_root / "ca").mkdir(parents=True)
    (env_root / "ca" / "ca.crt").write_text("old certificate", encoding="utf-8")

    with pytest.raises(LocalCaError, match="key .* is missing"):
        create_local_ca(env_root, [ALLOWED, PAGE])

    assert sorted(p.name for p in (env_root / "ca").iterdir()) == ["ca.crt"]
    assert (env_root / "ca" / "ca.crt").read_text(encoding="utf-8") == "old certificate"


@needs_openssl
def test_ca_key_is_created_private_not_fixed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a key written with the process umask and chmodded afterwards:
    with chmod disabled and a permissive umask, that key would be 0644."""
    monkeypatch.setattr(Path, "chmod", lambda *_args, **_kwargs: None)
    old_umask = os.umask(0o022)
    try:
        env_root = tmp_path / "env-private"
        create_local_ca(env_root, [ALLOWED, PAGE])
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE((env_root / "ca" / "ca.key").stat().st_mode) == 0o600


@needs_openssl
def test_fingerprint_matches_openssl_and_info_reads_the_certificate(tmp_path: Path) -> None:
    """Catches a fingerprint taken over the PEM text instead of the DER form,
    or in the wrong case or separator."""
    env_root = tmp_path / "env-three"
    info = create_local_ca(env_root, [ALLOWED, PAGE])
    expected = _openssl(
        "x509", "-in", str(env_root / "ca" / "ca.crt"), "-noout", "-fingerprint", "-sha256"
    ).stdout.strip().split("=", 1)[1]

    assert info.fingerprint_sha256 == expected
    assert ca_info(env_root).fingerprint_sha256 == expected
    assert info.permitted_names == (ALLOWED, PAGE)
    assert info.not_after.endswith("Z")


# --- render ---------------------------------------------------------------

FIXTURE_NAME = "fixture-main.yml"


def _needs_render_tools() -> None:
    missing = [t for t in ("openssl", "age-keygen", "ssh-keygen") if shutil.which(t) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")


def _seed_fake_wizard(data_root: Path, main_yml: str) -> None:
    """A wizard that renders a fixed inventory instead of running dmf-env."""
    (data_root).mkdir(parents=True, exist_ok=True)
    (data_root / FIXTURE_NAME).write_text(main_yml, encoding="utf-8")
    wizard = data_root / "repos" / "dmf-env" / "bin" / "init-wizard.sh"
    wizard.parent.mkdir(parents=True)
    wizard.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'vars="$DMF_DATA_ROOT/envs/sandbox-test/inventory/group_vars/all"\n'
        'mkdir -p "$vars"\n'
        f'cp "$DMF_DATA_ROOT/{FIXTURE_NAME}" "$vars/main.yml"\n'
        "echo 'env_id: sandbox-test'\n",
        encoding="utf-8",
    )
    wizard.chmod(0o755)


def _render(data_root: Path, request: CreateNewRenderRequest) -> list[dict]:
    """Run the render the way the app does and return its NDJSON events."""
    return [json.loads(line) for line in stream_render_create_new(data_root, request)]


def _render_request(tmp_path: Path, mode: str) -> CreateNewRenderRequest:
    key = tmp_path / "node-key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "test"],
        check=True,
        capture_output=True,
    )
    return CreateNewRenderRequest(
        operator=OperatorInputs(username="op", email="op@dmf.test", display="Op"),
        sandbox=SandboxInputs(
            label="demo",
            node_ip="203.0.113.99",
            ansible_user="root",
            iface="eth0",
            ssh_private_key=key.read_text(encoding="utf-8"),
            network_mode=mode,
        ),
    )


def _main_yml(mode: str, page: str | None) -> str:
    lines = [
        "ansible_ssh_private_key_file: '/placeholder'",
        f"dmf_network_mode: {mode}",
        "dmf_sandbox_base_domain: 'abcd.sandbox.test'",
    ]
    if page:
        lines.append(f"dmf_approval_page_name: '{page}'")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("mode", "page"),
    [("lan", "dmf-abcd.local"), ("public", "unlock.abcd.sandbox.test")],
)
def test_render_writes_local_ca_vars_for_both_modes(tmp_path: Path, mode: str, page: str) -> None:
    """Catches a render that skips the CA, builds the constraint from the wrong
    names (for example the base domain alone), or writes different variable
    names than dmf-infra reads."""
    _needs_render_tools()
    data_root = tmp_path / "data"
    _seed_fake_wizard(data_root, _main_yml(mode, page))

    events = _render(data_root, _render_request(tmp_path, mode))

    assert events[-1]["event"] == "complete", events[-1]
    env_id = events[-1]["env_id"]
    env_root = data_root / "envs" / env_id
    info = ca_info(env_root)
    assert info.permitted_names == ("abcd.sandbox.test", page)
    local_ca = (env_root / "inventory" / "group_vars" / "all" / "local_ca.yml").read_text(
        encoding="utf-8"
    )
    assert "dmf_local_ca_import: true" in local_ca
    assert 'dmf_local_ca_cert_file: "{{ inventory_dir }}/../ca/ca.crt"' in local_ca
    assert 'dmf_local_ca_key_file: "{{ inventory_dir }}/../ca/ca.key"' in local_ca
    assert f"dmf_local_ca_fingerprint_sha256: '{info.fingerprint_sha256}'" in local_ca
    assert f"dmf_local_ca_not_after: '{info.not_after}'" in local_ca
    answers = (data_root / "runs" / env_id / "answers.yaml").read_text(encoding="utf-8")
    assert f"network_mode: '{mode}'" in answers


def test_render_fails_clearly_without_approval_page_name(tmp_path: Path) -> None:
    """Catches a render that proceeds with an older dmf-env and builds a CA
    that cannot cover the approval page."""
    _needs_render_tools()
    data_root = tmp_path / "data"
    _seed_fake_wizard(data_root, _main_yml("lan", None))

    events = _render(data_root, _render_request(tmp_path, "lan"))

    assert events[-1]["event"] == "error"
    assert "dmf_approval_page_name" in events[-1]["error"]
    assert "dmf-env#43" in events[-1]["error"]
    assert not (data_root / "envs" / "sandbox-test" / "ca").exists()


def test_network_mode_outside_lan_public_is_422(tmp_path: Path) -> None:
    """Catches a free-text network_mode that would reach the answers file."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    app = create_app(Settings(data_root=data_root, tls_enabled=False))
    client = TestClient(app)
    token = app.state.launch_token_state.token
    assert client.get(f"/?token={token}", follow_redirects=False).status_code in {302, 307}

    payload = {
        "operator": {"username": "op", "email": "op@dmf.test", "display": "Op"},
        "sandbox": {
            "label": "demo",
            "node_ip": "203.0.113.99",
            "ansible_user": "root",
            "iface": "eth0",
            "ssh_private_key": "key",
            "network_mode": "wan",
        },
    }
    assert client.post("/api/render", json=payload).status_code == 422


def test_network_mode_defaults_to_lan() -> None:
    sandbox = SandboxInputs(
        node_ip="1.2.3.4", ansible_user="root", iface="eth0", ssh_private_key="k"
    )
    assert sandbox.network_mode == "lan"


# --- download and package -------------------------------------------------


def _make_ctx(tmp_path: Path) -> BootstrapContext:
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    render_dir = data_root / "runs" / env_id
    (data_root / "envs" / env_id).mkdir(parents=True)
    (render_dir / "age").mkdir(parents=True)
    (render_dir / "age" / "keys.txt").write_text("AGE-SECRET-KEY-1TEST\n", encoding="utf-8")
    (render_dir / "answers.yaml").write_text("operator: test\n", encoding="utf-8")
    (render_dir / "render.json").write_text(
        json.dumps(
            {
                "env_id": env_id,
                "profile": "sandbox-single-node",
                "schema_version": 1,
                "render_dir": str(render_dir),
                "age_key_path": str(render_dir / "age" / "keys.txt"),
                "answers_file_path": str(render_dir / "answers.yaml"),
            }
        ),
        encoding="utf-8",
    )
    return BootstrapContext.from_data_root(data_root, env_id)


@needs_openssl
def test_ca_payload_prefers_local_ca_over_ssh_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a payload that still asks the node first: with a local CA the
    SSH fetch must not run, and the payload carries the fingerprint."""
    ctx = _make_ctx(tmp_path)
    info = create_local_ca(ctx.env_dir, [ALLOWED, PAGE])

    def no_ssh(*_args, **_kwargs):
        raise AssertionError("the SSH fetch must not run when a local CA exists")

    monkeypatch.setattr(bootstrap_steps, "_run_openbao_ssh", no_ssh)

    payload = build_ca_cert_payload(ctx)

    assert payload["present"] is True
    assert payload["filename"] == "dmf-ca.crt"
    assert payload["pem"] == (ctx.env_dir / "ca" / "ca.crt").read_text(encoding="utf-8").strip()
    assert payload["fingerprint_sha256"] == info.fingerprint_sha256
    assert "requirement_note" in payload
    assert "BEGIN EC PRIVATE KEY" not in payload["pem"]


@needs_openssl
def test_package_uses_local_ca_and_readme_names_fingerprint(tmp_path: Path) -> None:
    """Catches a package that ships the stale payload certificate, omits the
    fingerprint, or does not say where the CA key is kept."""
    data_root = tmp_path / "data"
    env_id = "sandbox-pkg"
    env_root = data_root / "envs" / env_id
    info = create_local_ca(env_root, [ALLOWED, PAGE])
    artifacts = data_root / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / f"dmf-backup-{env_id}-20260610T120000Z.tar.age").write_bytes(b"x")
    stale = {
        "filename": "dmf-ca.crt",
        "pem": "-----BEGIN CERTIFICATE-----\nSTALE\n-----END CERTIFICATE-----",
        "present": True,
    }

    result = build_package(data_root, env_id, stale, {"entries": [], "node_ip": "203.0.113.9"})

    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        shipped = archive.read("dmf-ca.crt").decode("utf-8")
        readme = " ".join(archive.read("README.md").decode("utf-8").split())
    assert shipped == (env_root / "ca" / "ca.crt").read_text(encoding="utf-8")
    assert "STALE" not in shipped
    assert info.fingerprint_sha256 in readme
    assert "private key" in readme and "encrypted backup" in readme
