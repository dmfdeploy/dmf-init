from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Since the public release (2026-06-11) all component repos are public under the
# dmfdeploy org, so this is the correct out-of-the-box base for the runtime fetch
# `{base_url}/<repo>.git`. Operators pointing at a private mirror override via the
# DMF_REPO_BASE_URL env var or the UI base-URL field.
DEFAULT_REPO_BASE_URL = "https://github.com/dmfdeploy"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_tuple(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    data_root: Path = Path("/tmp/dmf-init-data")
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    repo_base_url: str = DEFAULT_REPO_BASE_URL
    run_ttl_seconds: int = 1800
    # Launch link must be opened within this window after container start (single-use).
    launch_token_ttl_seconds: int = 1800
    # Session must outlive a full bootstrap (a single step can run RUNBOOK_TIMEOUT=5400s).
    session_ttl_seconds: int = 43200
    log_level: str = "info"
    app_name: str = "DMF Init"
    # TLS — OFF by default: the appliance is reached as http://localhost, which is
    # already a secure context (clipboard/WebAuthn work) with no cert warning.
    # Opt in (DMF_TLS_ENABLED=true) only when reaching it by a non-localhost address.
    tls_enabled: bool = False
    tls_cert: Path | None = None
    tls_key: Path | None = None
    tls_sans: tuple[str, ...] = field(default_factory=lambda: ("DNS:localhost", "IP:127.0.0.1"))
    # Repo fallback creds (never surfaced in UI)
    repo_username: str | None = None
    repo_password: str | None = None


def load_settings() -> Settings:
    repo_base_url = os.getenv("DMF_REPO_BASE_URL", "").strip() or DEFAULT_REPO_BASE_URL
    tls_cert_raw = os.getenv("DMF_TLS_CERT", "").strip() or None
    tls_key_raw = os.getenv("DMF_TLS_KEY", "").strip() or None
    tls_sans = _env_tuple(
        "DMF_TLS_SANS",
        default=("DNS:localhost", "IP:127.0.0.1"),
    )
    repo_username = os.getenv("DMF_REPO_USERNAME", "").strip() or None
    repo_password = os.getenv("DMF_REPO_PASSWORD", "").strip() or None
    return Settings(
        data_root=Path(os.getenv("DMF_DATA_ROOT", "/tmp/dmf-init-data")),
        bind_host=os.getenv("DMF_BIND_HOST", "127.0.0.1"),
        bind_port=_env_int("DMF_BIND_PORT", 8000),
        repo_base_url=repo_base_url,
        run_ttl_seconds=_env_int("DMF_BOOTSTRAP_RUN_TTL_SECONDS", 1800),
        launch_token_ttl_seconds=_env_int("DMF_LAUNCH_TOKEN_TTL_SECONDS", 1800),
        session_ttl_seconds=_env_int("DMF_SESSION_TTL_SECONDS", 43200),
        log_level=os.getenv("DMF_LOG_LEVEL", "info").lower().strip() or "info",
        tls_enabled=os.getenv("DMF_TLS_ENABLED", "false").lower().strip() == "true",
        tls_cert=Path(tls_cert_raw) if tls_cert_raw else None,
        tls_key=Path(tls_key_raw) if tls_key_raw else None,
        tls_sans=tls_sans,
        repo_username=repo_username,
        repo_password=repo_password,
    )
