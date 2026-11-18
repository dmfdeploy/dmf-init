"""The install's local certificate authority.

Each install has one CA. dmf-init creates it on the workstation, so the key
travels with the env directory (backup and recovery package) and goes to the
node from there. The CA is name-constrained: it can only vouch for the names
the install uses, so trusting it on a device does not let it vouch for any
other site.

All cryptography goes through the openssl command line, as in `tls.py`.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CA_DIRNAME = "ca"
CA_KEY_NAME = "ca.key"
CA_CERT_NAME = "ca.crt"

_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
_DNS_NAME = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")


class LocalCaError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaInfo:
    cert_path: Path
    key_path: Path
    fingerprint_sha256: str
    not_after: str
    permitted_names: tuple[str, ...]


def ca_paths(env_root: Path) -> tuple[Path, Path]:
    """Return the (certificate, key) paths for the env's CA."""
    ca_dir = env_root / CA_DIRNAME
    return ca_dir / CA_CERT_NAME, ca_dir / CA_KEY_NAME


def _openssl(args: list[str], *, input_text: str | None = None) -> str:
    try:
        proc = subprocess.run(
            ["openssl", *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise LocalCaError("openssl is not installed; dmf-init needs it to create the CA") from exc
    except subprocess.CalledProcessError as exc:
        raise LocalCaError(f"openssl {args[0]} failed: {exc.stderr.strip()}") from exc
    return proc.stdout


def create_local_ca(env_root: Path, names: list[str], days: int = 730) -> CaInfo:
    """Create the env's CA, or return the existing one unchanged.

    An existing CA is never replaced: the node and every device that trusts
    it would stop matching.
    """
    cert_path, key_path = ca_paths(env_root)
    if cert_path.is_file() and key_path.is_file():
        return ca_info(env_root)
    if cert_path.is_file():
        raise LocalCaError(
            f"The CA certificate {cert_path} exists but its key {key_path} is missing. "
            "Restore the key from a backup; dmf-init will not make a second CA next to it."
        )
    # A key with no certificate was never used by anything, so a run that
    # stopped between the two steps can start again.
    key_path.unlink(missing_ok=True)
    if not names:
        raise LocalCaError("the CA needs at least one permitted DNS name")
    for name in names:
        if not _DNS_NAME.match(name):
            raise LocalCaError(f"not a valid DNS name for the CA: {name!r}")

    ca_dir = cert_path.parent
    ca_dir.mkdir(parents=True, exist_ok=True)
    ca_dir.chmod(0o700)
    constraints = ",".join(f"permitted;DNS:{name}" for name in names)
    config = "\n".join(
        [
            "[req]",
            "distinguished_name = dn",
            "x509_extensions = ext",
            "prompt = no",
            "[dn]",
            f"CN = DMF sandbox CA {env_root.name}",
            "[ext]",
            "basicConstraints = critical,CA:TRUE,pathlen:0",
            "keyUsage = critical,keyCertSign,cRLSign",
            f"nameConstraints = critical,{constraints}",
            "subjectKeyIdentifier = hash",
            "",
        ]
    )
    with tempfile.TemporaryDirectory() as scratch:
        config_path = Path(scratch) / "ca.cnf"
        config_path.write_text(config, encoding="utf-8")
        # openssl prints the key; Python creates the file, so it is 0600 from the
        # first byte instead of taking the process umask and being fixed later.
        key_pem = _openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout"])
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as key_file:
            key_file.write(key_pem)
        _openssl(
            [
                "req", "-x509", "-new", "-key", str(key_path),
                "-sha256", "-days", str(days),
                "-config", str(config_path), "-out", str(cert_path),
            ]
        )
    cert_path.chmod(0o644)
    return ca_info(env_root)


def ca_info(env_root: Path) -> CaInfo:
    """Read the fingerprint, expiry and permitted names from the CA certificate."""
    cert_path, key_path = ca_paths(env_root)
    if not cert_path.is_file():
        raise LocalCaError(f"no local CA at {cert_path}")
    text = _openssl(
        ["x509", "-in", str(cert_path), "-noout", "-fingerprint", "-sha256", "-enddate"]
    )
    fingerprint = re.search(r"sha256 Fingerprint=([0-9A-Fa-f:]+)", text, re.IGNORECASE)
    end_date = re.search(r"notAfter=(.+)", text)
    if not fingerprint or not end_date:
        raise LocalCaError("could not read the CA certificate")
    not_after = datetime.strptime(end_date.group(1).strip(), "%b %d %H:%M:%S %Y %Z")
    details = _openssl(["x509", "-in", str(cert_path), "-noout", "-ext", "nameConstraints"])
    permitted: list[str] = []
    in_permitted = False
    for line in details.splitlines():
        stripped = line.strip()
        if stripped.startswith("Permitted:"):
            in_permitted = True
        elif stripped.startswith("Excluded:"):
            in_permitted = False
        elif in_permitted and stripped.startswith("DNS:"):
            permitted.append(stripped[len("DNS:"):])
    return CaInfo(
        cert_path=cert_path,
        key_path=key_path,
        fingerprint_sha256=fingerprint.group(1).upper(),
        not_after=not_after.replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        permitted_names=tuple(permitted),
    )
