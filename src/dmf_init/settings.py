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
    # Re-mintable in-band via SIGHUP (a fresh link is printed to the logs) so an
    # expired link on a live container is recoverable without a restart.
    launch_token_ttl_seconds: int = 1800
    # Session IDLE TTL: the cookie max_age, **slid forward on every authenticated
    # request** (require_session re-stamps the session so Starlette re-issues the
    # cookie). An active operator never times out; the window only starts counting
    # once requests stop. Must comfortably exceed the longest gap between authed
    # requests (incl. an operator stepping away mid-run).
    session_ttl_seconds: int = 43200
    # Absolute session lifetime regardless of activity — a hard ceiling on the
    # sliding idle TTL so a session can't live forever. 7 days by default; safe
    # because the trust surface is localhost (see ADR-0044 / re-entry plan).
    session_absolute_cap_seconds: int = 604800
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
    # ADR-0044: DMF_DATA_ROOT must be tmpfs so env secrets (age key, openbao
    # keys) never touch host disk. Enforced fail-closed at startup. The dataclass
    # default is False so a directly-constructed Settings (tests) stays lenient;
    # load_settings() turns it on for the real runtime unless the operator sets
    # DMF_ALLOW_NON_TMPFS_DATA_ROOT=true (a deliberate dev/test escape hatch).
    require_tmpfs_data_root: bool = False
    # Machine-readable launch sentinel (DMF_LAUNCH_JSON=true): when on, the
    # container additionally prints a parseable `DMF_LAUNCH {json}` line so the
    # launcher can extract the token without scraping human-formatted log text.
    launch_json: bool = False


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
        session_absolute_cap_seconds=_env_int(
            "DMF_SESSION_ABSOLUTE_CAP_SECONDS", 604800
        ),
        log_level=os.getenv("DMF_LOG_LEVEL", "info").lower().strip() or "info",
        tls_enabled=os.getenv("DMF_TLS_ENABLED", "false").lower().strip() == "true",
        tls_cert=Path(tls_cert_raw) if tls_cert_raw else None,
        tls_key=Path(tls_key_raw) if tls_key_raw else None,
        tls_sans=tls_sans,
        repo_username=repo_username,
        repo_password=repo_password,
        # Enforced by default in the real runtime; the escape hatch is explicit.
        require_tmpfs_data_root=(
            os.getenv("DMF_ALLOW_NON_TMPFS_DATA_ROOT", "false").lower().strip() != "true"
        ),
        launch_json=os.getenv("DMF_LAUNCH_JSON", "false").lower().strip() == "true",
    )
