from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("dmf_init.tls")

_SAN_RE = re.compile(r"^(DNS|IP):[A-Za-z0-9._:\[\]-]+$")


def _sanitize_sans(raw_sans: tuple[str, ...]) -> tuple[str, ...]:
    """Keep only DNS:/IP: SAN entries that look safe."""
    return tuple(s for s in raw_sans if _SAN_RE.match(s))


def ensure_self_signed(
    cert_dir: Path,
    sans: tuple[str, ...] = ("DNS:localhost", "IP:127.0.0.1", "IP:::1"),
) -> tuple[Path, Path]:
    """Generate a self-signed cert+key pair into *cert_dir*.

    Returns ``(cert_path, key_path)``.
    """
    cert_path = cert_dir / "tls.crt"
    key_path = cert_dir / "tls.key"

    if cert_path.is_file() and key_path.is_file():
        # P2: enforce permissions even on reuse
        cert_dir.chmod(0o700)
        cert_path.chmod(0o644)
        key_path.chmod(0o600)
        logger.info("reusing existing tls cert/key", extra={"event": "tls_reuse"})
        return cert_path, key_path

    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_dir.chmod(0o700)
    safe_sans = _sanitize_sans(sans) if sans else ()
    # Always include localhost basics
    baseline = {"DNS:localhost", "IP:127.0.0.1", "IP:::1"}
    san_set = baseline | set(safe_sans)
    san_value = ",".join(sorted(san_set))

    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "3650",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-subj",
            "/CN=localhost",
            "-addext",
            f"subjectAltName={san_value}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    cert_path.chmod(0o644)
    key_path.chmod(0o600)
    logger.info("self-signed tls cert generated", extra={"event": "tls_cert_generated"})
    return cert_path, key_path
