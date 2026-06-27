from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .backup import (
    BackupDecryptError,
    BackupError,
    BackupIntegrityError,
)
from .bootstrap_steps import (
    BAO_PREFLIGHT_ID,
    CHECKPOINT_EXPORT_GATE_ID,
    BootstrapContext,
    build_bao_preflight_step,
    build_bootstrap_steps,
    build_ca_cert_payload,
    build_hosts_map_payload,
    build_passkey_payload,
    ensure_runtime_repos,
    make_checkpoint_fn,
    seed_openbao_redactions,
    supports_auto_unseal,
)
from .createnew import (
    CreateNewBackupRequest,
    CreateNewError,
    CreateNewRenderRequest,
    run_backup_create_new,
    stream_render_create_new,
)
from .logging_utils import AccessLogTokenScrubFilter, JSONLogFormatter, SecretRedactionFilter
from .manage import (
    ManageDoctorRequest,
    ManageError,
    ManageRestoreRequest,
    ManageSession,
    assert_data_root_tmpfs,
    build_doctor_run,
    build_env_doctor_run,
    run_manage_restore,
)
from .manage_actions import (
    ManageAction,
    build_action_run,
)
from .orchestrate import (
    BootstrapRun,
    CheckpointStep,
    SubprocessExecutor,
    run_worker,
    stream_events,
)
from .package import PackageError, build_package
from .repos import RepoFetchRequest
from .settings import Settings, load_settings
from .tls import ensure_self_signed

PACKAGE_ROOT = Path(__file__).resolve().parent
STATIC_APP_DIR = PACKAGE_ROOT / "static" / "app"
logger = logging.getLogger("dmf_init")


@dataclass
class LaunchTokenState:
    token: str
    issued_at: float
    used_at: float | None = None

    def expired(self, now: float, ttl_seconds: int) -> bool:
        return now - self.issued_at > ttl_seconds

    def valid(self, supplied: str, now: float, ttl_seconds: int) -> bool:
        return (
            self.used_at is None
            and supplied == self.token
            and not self.expired(now, ttl_seconds)
        )

    def consume(self, now: float) -> None:
        self.used_at = now


class LaunchTokenMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        query = scope.get("query_string", b"").decode("latin-1")
        params = {key: value for key, value in parse_qsl(query, keep_blank_values=True)}
        supplied = params.get("token")
        if not supplied:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        state: LaunchTokenState = scope["app"].state.launch_token_state
        settings: Settings = scope["app"].state.settings
        now = time.time()

        # Recovery is in-band (facet b): re-mint a fresh link on the *running*
        # container — no restart, so the run's tmpfs state is preserved.
        remint_hint = (
            "To get a fresh link without restarting (and without losing the "
            "in-progress run), have the running container re-mint one: send it "
            "SIGHUP — e.g. <code>docker kill --signal=HUP &lt;container&gt;</code> — "
            "then open the new link printed in its logs."
        )
        link_min = max(1, settings.launch_token_ttl_seconds // 60)
        if state.expired(now, settings.launch_token_ttl_seconds):
            response = _launch_notice_html(
                settings,
                heading="Launch link expired",
                body=(
                    f"This one-time launch link is valid for about {link_min} minutes, and "
                    f"that window has passed. {remint_hint}"
                ),
                status_code=410,
            )
        elif state.used_at is not None:
            response = _launch_notice_html(
                settings,
                heading="Launch link already used",
                body=(
                    "This launch link is single-use and has already opened a session. If "
                    "you still have the browser tab where you opened it, keep using that "
                    f"one. {remint_hint}"
                ),
                status_code=403,
            )
        elif supplied != state.token:
            response = _launch_notice_html(
                settings,
                heading="Invalid launch link",
                body=(
                    "This token doesn't match the running container. Use the exact link printed "
                    f"in the container logs. {remint_hint}"
                ),
                status_code=403,
            )
        else:
            state.consume(now)
            request.session["dmf_init_launch"] = {"started_at": now}
            response = RedirectResponse(url=request.url.path or "/", status_code=302)
        await response(scope, receive, send)


def require_session(request: Request) -> None:
    """Gate protected endpoints on a live launch session, and **slide** it.

    Two clocks: a sliding *idle* TTL (the cookie ``max_age``) and a hard
    *absolute* cap. Starlette only re-issues the cookie when the session is
    marked modified, and it tracks modification only on **top-level** mutation —
    so we reassign the whole ``dmf_init_launch`` key (not a nested field) to push
    the idle window forward on every authenticated request. The absolute cap is
    checked against ``started_at`` so a session can't live forever.
    """
    sess = request.session.get("dmf_init_launch")
    if not isinstance(sess, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="launch session required",
        )

    settings: Settings = request.app.state.settings
    now = time.time()
    started_at = sess.get("started_at", now)
    if now - started_at > settings.session_absolute_cap_seconds:
        request.session.clear()  # top-level mutation → cookie cleared
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="launch session expired",
        )

    # Slide the idle window: reassign the top-level key so Starlette re-issues the
    # cookie with a fresh max_age. Preserve started_at (the absolute-cap anchor).
    request.session["dmf_init_launch"] = {**sess, "started_at": started_at, "last_seen": now}


def download_is_current(
    record: dict[str, Any] | None, latest_artifact_name: str | None
) -> bool:
    """True iff a recovery bundle has been fully downloaded AND it is the latest.

    The download record is written only when a package stream completes end to end
    (an aborted download leaves no record), and ``artifact`` names the ``.tar.age``
    that was bundled. Comparing it to the latest artifact on disk makes "current"
    honest: a stale download (a newer checkpoint sealed since) is not current. This
    is the proof that the checkpoint bundle has left tmpfs (ADR-0044).
    """
    return bool(
        record
        and record.get("downloaded_at")
        and record.get("artifact")
        and latest_artifact_name is not None
        and record["artifact"] == latest_artifact_name
    )


class BootstrapStartRequest(BaseModel):
    env_id: str
    passphrase: str
    passphrase_confirm: str


class ManageRestoreUpload(BaseModel):
    """Multipart form for restore: file + passphrase."""
    passphrase: str = Field(min_length=1)


class BootstrapResumeRequest(BaseModel):
    run_id: str
    pause_id: str
    payload: dict[str, Any] | None = None


class BootstrapRetryRequest(BaseModel):
    # run_id resumes a live (still in-memory) failed run; env_id resumes from
    # the on-disk cursor after the run was GC'd or lost to a --rm restart.
    # At least one must be supplied.
    run_id: str | None = None
    env_id: str | None = None
    passphrase: str | None = None


class BootstrapDoctorRequest(BaseModel):
    env_id: str


class BootstrapPasskeyStatusResponse(BaseModel):
    confirmed: int
    required: int


class ManageSessionRequest(BaseModel):
    session_id: str


class ManageActionStartRequest(BaseModel):
    session_id: str
    action: ManageAction
    params: dict[str, Any] | None = None


def _print_launch_link(state: LaunchTokenState, settings: Settings) -> None:
    """Print the one-time launch URL to stdout (the container logs)."""
    scheme = "https" if settings.tls_enabled else "http"
    link_min = max(1, settings.launch_token_ttl_seconds // 60)
    idle_h = settings.session_ttl_seconds / 3600
    # The bind interface (often 0.0.0.0 in a container) is not browsable — always
    # show a host the operator can actually open.
    display_host = settings.bind_host
    if display_host in ("0.0.0.0", "::", ""):
        display_host = "localhost"
    launch_url = f"{scheme}://{display_host}:{settings.bind_port}/"
    print(f"launch token: {state.token}", flush=True)
    print(f"open {launch_url}?token={state.token}", flush=True)
    print(
        f"  (single-use link, valid ~{link_min} min; your session then lasts "
        f"~{idle_h:.0f}h idle and refreshes while you're active)",
        flush=True,
    )
    if settings.tls_enabled:
        print(
            "  note: the self-signed cert shows a one-time 'Not secure' warning — "
            "click Proceed; the page is still a secure context. Reaching it as "
            "http://localhost avoids the warning entirely.",
            flush=True,
        )


def _remint_launch_token(app: FastAPI) -> None:
    """Re-mint the single-use launch token and print a fresh link to the logs.

    Facet (b): in-band re-entry. If the operator's session expired and the
    original launch link is spent/expired, a ``SIGHUP`` mints a new token on the
    *running* container — no restart, so tmpfs state and the session secret (and
    thus any still-valid sessions) survive. The trust surface is unchanged:
    sending SIGHUP requires the same host/docker access as reading the logs the
    link is printed to.
    """
    state: LaunchTokenState = app.state.launch_token_state
    settings: Settings = app.state.settings
    state.token = secrets.token_urlsafe(24)
    state.issued_at = time.time()
    state.used_at = None
    print("re-minting launch token (SIGHUP) — previous link is now invalid", flush=True)
    _print_launch_link(state, settings)
    logger.info("launch token re-minted", extra={"event": "launch_token_reminted"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: LaunchTokenState = app.state.launch_token_state
    settings: Settings = app.state.settings
    _print_launch_link(state, settings)
    logger.info("launch token generated", extra={"event": "launch_token_issued"})
    # Facet (b): SIGHUP re-mints the launch link in-band. Best-effort — not the
    # main thread (e.g. TestClient) or a platform without SIGHUP just skips it.
    if hasattr(signal, "SIGHUP"):
        try:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGHUP, _remint_launch_token, app
            )
        except (NotImplementedError, RuntimeError, ValueError):
            logger.debug("SIGHUP re-mint handler not installed (non-main thread?)")
    yield


def build_uvicorn_log_config() -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "redact": {"()": SecretRedactionFilter},
            "access_scrub": {"()": AccessLogTokenScrubFilter},
        },
        "formatters": {
            "json": {"()": JSONLogFormatter},
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json",
                "filters": ["redact"],
            },
            "access": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json",
                "filters": ["redact", "access_scrub"],
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            "dmf_init": {"handlers": ["default"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["default"], "level": "INFO"},
    }


def _index_html() -> Path | None:
    candidate = STATIC_APP_DIR / "index.html"
    return candidate if candidate.exists() else None


def _fallback_html(settings: Settings) -> HTMLResponse:
    return HTMLResponse(
        content=(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{settings.app_name}</title>"
            "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;"
            "background:linear-gradient(135deg,#08111f,#131c31 55%,#020617);"
            "color:#e5eefc;font-family:system-ui,sans-serif}.card{max-width:34rem;"
            "padding:2rem;border:1px solid rgba(148,163,184,.18);border-radius:1.25rem;"
            "background:rgba(12,18,32,.82);box-shadow:0 30px 70px rgba(0,0,0,.35)}"
            "h1{margin:0 0 .75rem;font-size:2rem}p{margin:.5rem 0;color:#cbd5e1;"
            "line-height:1.5}"
            "</style></head><body><main class='card'><h1>DMF Init</h1>"
            "<p>Create new and Manage flows will attach here in a later phase.</p>"
            "<p>Launch token exchange is live; build the frontend to replace this fallback.</p>"
            "</main></body></html>"
        )
    )


def _launch_notice_html(
    settings: Settings, *, heading: str, body: str, status_code: int
) -> HTMLResponse:
    """Styled page for an expired/used/invalid launch link (no bare plaintext)."""
    return HTMLResponse(
        status_code=status_code,
        content=(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{settings.app_name} — launch link</title>"
            "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;"
            "background:linear-gradient(135deg,#08111f,#131c31 55%,#020617);"
            "color:#e5eefc;font-family:system-ui,sans-serif}.card{max-width:34rem;"
            "padding:2rem;border:1px solid rgba(148,163,184,.18);border-radius:1.25rem;"
            "background:rgba(12,18,32,.82);box-shadow:0 30px 70px rgba(0,0,0,.35)}"
            "h1{margin:0 0 .75rem;font-size:1.6rem}p{margin:.5rem 0;color:#cbd5e1;"
            "line-height:1.5}code{background:rgba(148,163,184,.15);padding:.1rem .35rem;"
            "border-radius:.35rem;font-size:.85em}</style></head>"
            f"<body><main class='card'><h1>{heading}</h1><p>{body}</p></main></body></html>"
        ),
    )


# The resume cursor lives next to render.json in runs/<env_id>/. It records
# *where* a terminal run got to so retry/resume can be reconstructed from disk
# after the in-memory run is GC'd (run_ttl_seconds) or lost to a --rm restart.
RESUME_CURSOR_NAME = "resume.json"
RESUME_CURSOR_SCHEMA_VERSION = 1


def _resume_cursor_path(data_root: Path, env_id: str) -> Path:
    return data_root / "runs" / env_id / RESUME_CURSOR_NAME


def _load_resume_cursor(data_root: Path, env_id: str) -> dict[str, Any] | None:
    """Read the on-disk resume cursor for an env, or None if absent/unreadable."""
    path = _resume_cursor_path(data_root, env_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def make_resume_journal_fn(ctx: BootstrapContext) -> Callable[[BootstrapRun], None]:
    """Build a journal_fn that persists the resume cursor when a run goes terminal.

    On a terminal *error* the cursor (failed step id + status) is written so a
    later disk-backed retry can resume from the failed step. On *complete* the
    cursor is removed — a finished env is not resumable.
    """
    cursor_path = _resume_cursor_path(ctx.data_root, ctx.env_id)

    def _journal(run: BootstrapRun) -> None:
        if run.final_status == "complete":
            cursor_path.unlink(missing_ok=True)
            return
        payload = {
            "schema_version": RESUME_CURSOR_SCHEMA_VERSION,
            "env_id": run.env_id,
            "run_id": run.run_id,
            "failed_step_id": _canonical_failed_step_id(run),
            "final_status": run.final_status,
            "finished_at": run.finished_at,
        }
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cursor_path.with_name(cursor_path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(cursor_path)  # atomic swap into place

    return _journal


def _canonical_failed_step_id(run: BootstrapRun) -> str | None:
    """Map the synthetic bao-preflight step back to the canonical step it guards.

    The preflight (ADR-0044 facet d) is prepended to a retry slice and is NOT in
    build_bootstrap_steps(). If a run fails there, persisting/returning
    ``bao-preflight`` as the failed step would dead-end the next retry (idx lookup
    409s "not in build") and poison the disk cursor. Since the preflight always
    sits immediately before the canonical step it guards, resolve to that step.
    """
    fsid = run.failed_step_id
    if fsid != BAO_PREFLIGHT_ID:
        return fsid
    for i, step in enumerate(run.steps):
        if step.id == BAO_PREFLIGHT_ID:
            return run.steps[i + 1].id if i + 1 < len(run.steps) else None
    return fsid


def _run_worker_wrapper(run: BootstrapRun) -> None:
    """Wrapper that catches exceptions so the finally in the caller still runs."""
    try:
        run_worker(run)
    except Exception:
        pass  # run_worker already emits error event; we just don't crash the thread


def _manage_action_wrapper(
    run: BootstrapRun,
    *,
    session: ManageSession,
    is_teardown: bool,
) -> None:
    """Wrapper for manage actions — P2a: teardown marks session terminal + wipes."""
    try:
        run_worker(run)
    except Exception:
        pass
    finally:
        if is_teardown:
            session.terminal = True
            session.finished_at = time.time()
            session.wipe_secrets()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.data_root.mkdir(parents=True, exist_ok=True)
    # ADR-0044: fail closed if the data root isn't RAM-backed, so env secrets
    # never touch host disk. Raises DataRootNotTmpfsError → the appliance refuses
    # to start. No-op on non-Linux / undeterminable, and skipped when the operator
    # set DMF_ALLOW_NON_TMPFS_DATA_ROOT (require_tmpfs_data_root=False).
    assert_data_root_tmpfs(settings.data_root, enforce=settings.require_tmpfs_data_root)

    token_state = LaunchTokenState(token=secrets.token_urlsafe(24), issued_at=time.time())
    app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.launch_token_state = token_state
    app.state.bootstrap_runs = {}
    app.state.bootstrap_lock = threading.Lock()
    app.state.manage_sessions = {}
    # env_id -> {downloaded_at, sha256, filename}; recorded only when the
    # package stream ran to completion (honest "we sent all the bytes").
    app.state.package_downloads = {}
    # P0-1: Active operation guard — keyed by env_id, prevents concurrent ops on same env
    app.state.active_runs: dict[str, str] = {}  # env_id -> run_id
    # Create-new single-flight guard: only one render can run at a time (no env_id
    # exists at submit time, so this is a global lock rather than per-env).
    app.state.createnew_lock = threading.Lock()
    app.state.createnew_active = False

    def _export_proven(env_id: str | None) -> bool:
        """Has the current recovery bundle for env_id been downloaded off tmpfs?"""
        if not env_id:
            return False
        from .package import find_latest_artifact
        with app.state.bootstrap_lock:
            record = app.state.package_downloads.get(env_id)
        try:
            latest_name: str | None = find_latest_artifact(settings.data_root, env_id).name
        except PackageError:
            latest_name = None
        return download_is_current(record, latest_name)

    app.add_middleware(LaunchTokenMiddleware)
    cookie_secure = settings.tls_enabled
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_urlsafe(32),
        max_age=settings.session_ttl_seconds,
        same_site="lax",
        https_only=cookie_secure,
    )
    if STATIC_APP_DIR.exists():
        app.mount("/static/app", StaticFiles(directory=str(STATIC_APP_DIR), html=True), name="app")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": settings.app_name,
                "data_root": str(settings.data_root),
            }
        )

    @app.get(
        "/api/session",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def session_status(request: Request) -> JSONResponse:
        """Remaining-session signal for a pre-expiry warning (facet a).

        Note: hitting this endpoint *is* activity, so it also slides the idle
        window (via require_session). The actionable number for a warning is
        ``absolute_remaining_seconds`` — the hard ceiling that activity cannot
        extend.
        """
        sess = request.session["dmf_init_launch"]
        now = time.time()
        started_at = sess.get("started_at", now)
        absolute_remaining = max(
            0, settings.session_absolute_cap_seconds - (now - started_at)
        )
        return JSONResponse(
            {
                "idle_ttl_seconds": settings.session_ttl_seconds,
                "absolute_remaining_seconds": int(absolute_remaining),
            }
        )

    @app.post(
        "/api/repos/fetch",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def fetch_repos(payload: RepoFetchRequest) -> JSONResponse:
        result = ensure_runtime_repos(
            settings.data_root,
            payload,
            fallback_username=settings.repo_username,
            fallback_password=settings.repo_password,
            fallback_base_url=settings.repo_base_url,
        )
        return JSONResponse(result.model_dump())

    @app.post(
        "/api/render",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def render_create_new(payload: CreateNewRenderRequest) -> StreamingResponse:
        wizard_path = settings.data_root / "repos" / "dmf-env" / "bin" / "init-wizard.sh"
        if not wizard_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="fetch repos first",
            )

        # Single-flight: reject a concurrent render BEFORE it can mint a second env.
        with app.state.createnew_lock:
            if app.state.createnew_active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="a create-new render is already in progress",
                )
            app.state.createnew_active = True

        def stream() -> Iterator[str]:
            try:
                yield from stream_render_create_new(settings.data_root, payload)
            except CreateNewError as exc:
                yield (
                    json.dumps({"event": "error", "error": str(exc)}, separators=(",", ":"))
                    + "\n"
                )
            finally:
                with app.state.createnew_lock:
                    app.state.createnew_active = False

        try:
            return StreamingResponse(stream(), media_type="application/x-ndjson")
        except BaseException:
            # Construction failed before the generator could own cleanup — release.
            with app.state.createnew_lock:
                app.state.createnew_active = False
            raise

        # NOTE: the inner generator kills/waits for the wizard subprocess on
        # cancellation (GeneratorExit) before this finally clears the flag,
        # so a mid-stream disconnect cannot reopen the duplicate-env race.
        # The only residual narrow edge is if the ASGI server never iterates
        # the body at all (e.g. client disconnect before streaming starts) —
        # the finally may not run. Acceptable for this short-lived localhost
        # container (restart-recoverable). A future idempotency-key would
        # close it fully; sequential re-submit (fresh env) remains intentional.

    @app.post(
        "/api/backup",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def backup_create_new(payload: CreateNewBackupRequest) -> JSONResponse:
        try:
            result = run_backup_create_new(settings.data_root, payload)
        except CreateNewError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return JSONResponse(result.model_dump())

    def _gc_terminal_runs() -> None:
        now = time.time()
        stale: list[str] = []
        with app.state.bootstrap_lock:
            for run_id, run in list(app.state.bootstrap_runs.items()):
                if not run.terminal or run.finished_at is None:
                    continue
                if now - run.finished_at > settings.run_ttl_seconds:
                    stale.append(run_id)
            for run_id in stale:
                run = app.state.bootstrap_runs.pop(run_id, None)
                if run is not None:
                    run.wipe_secrets()

    def _gc_terminal_manage_sessions() -> None:
        now = time.time()
        stale: list[str] = []
        with app.state.bootstrap_lock:
            for session_id, session in list(app.state.manage_sessions.items()):
                if not session.terminal or session.finished_at is None:
                    continue
                if now - session.finished_at > settings.run_ttl_seconds:
                    stale.append(session_id)
            for session_id in stale:
                session = app.state.manage_sessions.pop(session_id, None)
                if session is not None:
                    session.wipe_secrets()

    def _manage_session_or_404(session_id: str) -> ManageSession:
        with app.state.bootstrap_lock:
            session = app.state.manage_sessions.get(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="manage session not found",
            )
        return session

    def _register_and_spawn(
        env_id: str,
        run: BootstrapRun,
        clear_active: Callable[[], None],
    ) -> None:
        try:
            with app.state.bootstrap_lock:
                app.state.bootstrap_runs[run.run_id] = run
                app.state.active_runs[env_id] = run.run_id
            worker = threading.Thread(
                target=lambda: (_run_worker_wrapper(run), clear_active()),
                daemon=True,
            )
            worker.start()
        except Exception:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(env_id, None)
                app.state.bootstrap_runs.pop(run.run_id, None)
            raise

    @app.post(
        "/api/bootstrap/start",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def bootstrap_start(payload: BootstrapStartRequest) -> JSONResponse:
        _gc_terminal_runs()
        if payload.passphrase != payload.passphrase_confirm:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="passphrase confirmation does not match",
            )
        # P2: validate env_id BEFORE any path use or reservation
        from .backup import validate_env_id
        try:
            validate_env_id(payload.env_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        # P1-1: Reserve under lock; ANY pre-spawn failure clears.
        with app.state.bootstrap_lock:
            if payload.env_id in app.state.active_runs:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="an operation is already in progress for this env",
                )
            app.state.active_runs[payload.env_id] = "__pending__"

        def _clear_active() -> None:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(payload.env_id, None)

        try:
            render_dir = settings.data_root / "runs" / payload.env_id
            env_dir = settings.data_root / "envs" / payload.env_id
            render_json = render_dir / "render.json"
            if not env_dir.is_dir():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"env_dir not found for env_id={payload.env_id}",
                )
            if not render_json.is_file():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"render metadata not found for env_id={payload.env_id}",
                )
            ctx = BootstrapContext.from_data_root(
                settings.data_root,
                payload.env_id,
                remotes=[],
            )
            run_id = secrets.token_urlsafe(8)
            run = BootstrapRun(
                run_id=run_id,
                steps=build_bootstrap_steps(ctx),
                env_id=payload.env_id,
                passphrase=payload.passphrase,
                remotes=[],
                executor=SubprocessExecutor(),
                checkpoint_fn=make_checkpoint_fn(ctx),
                journal_fn=make_resume_journal_fn(ctx),
            )
        except Exception:
            _clear_active()
            raise

        _register_and_spawn(payload.env_id, run, _clear_active)
        return JSONResponse({"run_id": run_id})

    @app.post(
        "/api/bootstrap/retry",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def bootstrap_retry(payload: BootstrapRetryRequest) -> JSONResponse:
        _gc_terminal_runs()
        from .backup import validate_env_id

        env_id: str | None = None
        failed_step_id: str | None = None

        # Path A — live in-memory run (within run_ttl_seconds). Authoritative:
        # a found run dictates the outcome (incl. the not-failed 409 guard).
        if payload.run_id:
            with app.state.bootstrap_lock:
                run = app.state.bootstrap_runs.get(payload.run_id)
            if run is not None:
                if run.final_status != "error":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="run is not in a failed state",
                    )
                if run.env_id is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="run env_id unavailable",
                    )
                env_id = run.env_id
                failed_step_id = _canonical_failed_step_id(run)
                if failed_step_id is None:
                    for ev in reversed(run.events):
                        if ev.get("event") == "error" and ev.get("step"):
                            failed_step_id = ev["step"]
                            break

        # Path B — disk-backed cursor (run GC'd or lost to a --rm restart). The
        # env survives on disk; the persisted cursor supplies the failed step.
        if env_id is None and payload.env_id:
            try:
                validate_env_id(payload.env_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from exc
            cursor = _load_resume_cursor(settings.data_root, payload.env_id)
            if cursor is not None and cursor.get("final_status") == "error":
                env_id = payload.env_id
                failed_step_id = cursor.get("failed_step_id")

        if env_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="run not found",
            )
        if failed_step_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="failed step id not found in run",
            )

        with app.state.bootstrap_lock:
            if env_id in app.state.active_runs:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="an operation is already in progress for this env",
                )
            app.state.active_runs[env_id] = "__pending__"

        def _clear_active() -> None:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(env_id, None)

        try:
            try:
                ctx = BootstrapContext.from_data_root(settings.data_root, env_id, remotes=[])
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
                ) from exc

            all_steps = build_bootstrap_steps(ctx)
            try:
                idx = next(
                    i for i, s in enumerate(all_steps) if s.id == failed_step_id
                )
            except StopIteration:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"failed step '{failed_step_id}' not in build",
                ) from None
            steps = all_steps[idx:]

            # ADR-0044 forced export gate also applies to retry/resume-from-cursor:
            # if we'd resume *past* the gate (the gate step isn't in this slice),
            # the normal /resume enforcement is skipped, so re-check the proof here.
            # If the slice still contains the gate, the resume endpoint enforces it
            # when the operator reaches that pause.
            gate_idx = next(
                (i for i, s in enumerate(all_steps) if s.id == CHECKPOINT_EXPORT_GATE_ID),
                None,
            )
            if gate_idx is not None and idx > gate_idx and not _export_proven(env_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "recovery bundle not saved yet — download the current bundle "
                        "before resuming past checkpoint-2 (it's your only recovery if "
                        "the container is lost during the long unattended phases)"
                    ),
                )

            if not any(
                isinstance(s, CheckpointStep) and s.n == 2 for s in steps
            ) and not (ctx.env_dir / "openbao-keys.json").is_file():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot safely resume: openbao-keys.json missing "
                    "(would stream unredacted secrets)",
                )

            # ADR-0044 facet (d): a retry that resumes PAST the unseal step would
            # otherwise run Bao-dependent phases against an OpenBao that the env
            # node's reboot re-sealed — the exact opaque `configure` failure from
            # the lockout incident. On the auto-unsealable sandbox profile, prepend
            # a loud, idempotent preflight that re-unseals if needed (no-op if not).
            unseal_idx = next(
                (i for i, s in enumerate(all_steps) if s.id == "unseal"), None
            )
            if (
                unseal_idx is not None
                and idx > unseal_idx
                and supports_auto_unseal(ctx.render_meta.profile)
            ):
                steps = [build_bao_preflight_step(ctx), *steps]

            has_checkpoint = any(isinstance(s, CheckpointStep) for s in steps)
            if has_checkpoint and not payload.passphrase:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="passphrase required to resume (checkpoint backups need it)",
                )

            retry_run_id = secrets.token_urlsafe(8)
            retry_run = BootstrapRun(
                run_id=retry_run_id,
                steps=steps,
                env_id=env_id,
                passphrase=payload.passphrase,
                remotes=[],
                executor=SubprocessExecutor(),
                checkpoint_fn=make_checkpoint_fn(ctx),
                journal_fn=make_resume_journal_fn(ctx),
            )
            seed_openbao_redactions(retry_run, ctx)
            _register_and_spawn(env_id, retry_run, _clear_active)
        except Exception:
            _clear_active()
            raise
        return JSONResponse({"run_id": retry_run_id})

    @app.get(
        "/api/envs",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def list_envs() -> JSONResponse:
        """List rendered envs on disk + their resume state.

        Landing affordance: lets a reloaded / GC'd / --rm-restarted session
        re-enter and resume an env instead of dead-ending on a 404 retry. An
        env is `resumable` when a disk cursor records a terminal error and the
        env is not currently the subject of a live run.
        """
        from .backup import validate_env_id

        runs_root = settings.data_root / "runs"
        envs: list[dict[str, Any]] = []
        if runs_root.is_dir():
            for entry in sorted(runs_root.iterdir()):
                if not entry.is_dir():
                    continue
                env_id = entry.name
                try:
                    validate_env_id(env_id)
                except ValueError:
                    continue
                render_json = entry / "render.json"
                if not render_json.is_file():
                    continue
                try:
                    meta = json.loads(render_json.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}
                cursor = _load_resume_cursor(settings.data_root, env_id)
                with app.state.bootstrap_lock:
                    active = env_id in app.state.active_runs
                resumable = bool(
                    cursor
                    and cursor.get("final_status") == "error"
                    and cursor.get("failed_step_id")
                    and not active
                )
                envs.append(
                    {
                        "env_id": env_id,
                        "profile": meta.get("profile"),
                        "active": active,
                        "resumable": resumable,
                        "failed_step_id": cursor.get("failed_step_id") if cursor else None,
                        "finished_at": cursor.get("finished_at") if cursor else None,
                    }
                )
        return JSONResponse({"envs": envs})

    @app.get(
        "/api/bootstrap/stream/{run_id}",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def bootstrap_stream(
        run_id: str, from_: int = Query(0, alias="from", ge=0)
    ) -> StreamingResponse:
        _gc_terminal_runs()
        with app.state.bootstrap_lock:
            run = app.state.bootstrap_runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        return StreamingResponse(
            stream_events(run, from_),
            media_type="application/x-ndjson",
        )

    @app.post(
        "/api/bootstrap/resume",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def bootstrap_resume(payload: BootstrapResumeRequest) -> JSONResponse:
        with app.state.bootstrap_lock:
            run = app.state.bootstrap_runs.get(payload.run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        # ADR-0044 forced export gate: refuse to advance past checkpoint-2 into the
        # long unattended phases until the current recovery bundle is proven
        # downloaded off tmpfs. Server-enforced so a frontend can't skip it.
        if payload.pause_id == CHECKPOINT_EXPORT_GATE_ID and not _export_proven(run.env_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "recovery bundle not saved yet — download the current bundle "
                    "before continuing (it's your only recovery if the container is "
                    "lost during the long unattended phases)"
                ),
            )
        try:
            run.resume(payload.pause_id, payload.payload)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.get(
        "/api/bootstrap/passkey/{run_id}",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def bootstrap_passkey_status(run_id: str) -> JSONResponse:
        with app.state.bootstrap_lock:
            run = app.state.bootstrap_runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        if not run.env_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="run env_id unavailable"
            )
        ctx = BootstrapContext.from_data_root(settings.data_root, run.env_id, run.remotes)
        payload = build_passkey_payload(ctx)
        note = payload.get("note")
        if note:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(note))
        return JSONResponse(BootstrapPasskeyStatusResponse(
            confirmed=int(payload.get("confirmed", 0) or 0),
            required=int(payload.get("required", 0) or 0),
        ).model_dump())

    @app.post(
        "/api/bootstrap/doctor",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def bootstrap_doctor(payload: BootstrapDoctorRequest) -> JSONResponse:
        """Optional post-bootstrap re-validation for the create flow.

        Runs the read-only `doctor` against the env still living in this
        container's tmpfs (no Manage restore needed). Streams over the same
        /api/bootstrap/stream contract as every other run.
        """
        _gc_terminal_runs()
        from .backup import validate_env_id
        try:
            validate_env_id(payload.env_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        # Resolve the env (also validates it exists) to get its age key path.
        try:
            ctx = BootstrapContext.from_data_root(settings.data_root, payload.env_id)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc

        # P1-1: Reserve under lock; ANY pre-spawn failure clears.
        with app.state.bootstrap_lock:
            if payload.env_id in app.state.active_runs:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="an operation is already in progress for this env",
                )
            app.state.active_runs[payload.env_id] = "__pending__"

        def _clear_active() -> None:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(payload.env_id, None)

        try:
            run = build_env_doctor_run(payload.env_id, ctx.age_key_path, settings.data_root)
        except Exception as exc:
            _clear_active()
            if isinstance(exc, ManageError):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                ) from exc
            raise

        # P3: wrap set+spawn so any thread-start failure clears reservation.
        try:
            with app.state.bootstrap_lock:
                app.state.bootstrap_runs[run.run_id] = run
                app.state.active_runs[payload.env_id] = run.run_id

            worker = threading.Thread(
                target=lambda: (_run_worker_wrapper(run), _clear_active()),
                daemon=True,
            )
            worker.start()
        except Exception:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(payload.env_id, None)
                app.state.bootstrap_runs.pop(run.run_id, None)
            raise
        return JSONResponse({"run_id": run.run_id})

    @app.delete(
        "/api/bootstrap/runs/{run_id}",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def bootstrap_delete(run_id: str) -> JSONResponse:
        with app.state.bootstrap_lock:
            run = app.state.bootstrap_runs.pop(run_id, None)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        run.wipe_secrets()
        return JSONResponse({"ok": True})

    # P0-2: Artifact download (session-protected, path-safe, no-store)
    # P0/P1-3: Tight regex + resolve() safety + symlink check
    _ARTIFACT_NAME_RE = re.compile(
        r"^dmf-backup-[A-Za-z0-9][A-Za-z0-9_.-]*-[0-9]{8}T[0-9]{6}Z\.tar\.age$"
    )

    @app.get(
        "/api/backup/artifact/{artifact_name}",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def download_artifact(artifact_name: str) -> FileResponse:
        if not _ARTIFACT_NAME_RE.match(artifact_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid artifact name",
            )
        artifacts_dir = settings.data_root / "artifacts"
        # P1: Check symlink on UNRESOLVED path BEFORE resolve()
        unresolved = artifacts_dir / artifact_name
        if unresolved.is_symlink():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="artifact must not be a symlink",
            )
        candidate = unresolved.resolve()
        # Guard against path traversal
        try:
            candidate.relative_to(artifacts_dir.resolve())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid artifact path",
            ) from None
        if not candidate.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="artifact not found",
            )
        return FileResponse(
            str(candidate),
            filename=artifact_name,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    # Recovery package: one zip with everything the operator must keep
    # (checkpoint #3 backup + CA cert + README + sha256 MANIFEST).
    @app.get(
        "/api/package/{env_id}",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def download_package(env_id: str) -> StreamingResponse:
        from .backup import validate_env_id
        try:
            validate_env_id(env_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        try:
            ctx = BootstrapContext.from_data_root(settings.data_root, env_id)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        # Cheap artifact check first — no point in SSHing for CA/hosts when
        # there is nothing to package.
        from .package import find_latest_artifact
        try:
            find_latest_artifact(settings.data_root, env_id)
        except PackageError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        # Payload builders degrade gracefully (present=False / empty entries)
        # if the node is unreachable; the README states what is missing.
        ca_payload = build_ca_cert_payload(ctx)
        hosts_payload = build_hosts_map_payload(ctx)
        try:
            result = build_package(settings.data_root, env_id, ca_payload, hosts_payload)
        except PackageError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc

        def _stream():
            chunk = 1 << 16
            for offset in range(0, len(result.data), chunk):
                yield result.data[offset : offset + chunk]
            # Reached only if every byte was handed to the client; an aborted
            # download cancels the generator and never records completion.
            with app.state.bootstrap_lock:
                app.state.package_downloads[env_id] = {
                    "downloaded_at": time.time(),
                    "sha256": result.sha256,
                    "filename": result.filename,
                    # Source backup artifact this package was built from, so the
                    # UI can tell a current download from a stale one once a
                    # newer checkpoint backup appears (#140).
                    "artifact": result.artifact_name,
                }

        return StreamingResponse(
            _stream(),
            media_type="application/zip",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{result.filename}"',
                "Content-Length": str(len(result.data)),
                "X-Package-Sha256": result.sha256,
            },
        )

    @app.get(
        "/api/package/{env_id}/status",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def package_status(env_id: str) -> JSONResponse:
        from .backup import validate_env_id
        try:
            validate_env_id(env_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        with app.state.bootstrap_lock:
            record = app.state.package_downloads.get(env_id)
        # `available`: is there a backup artifact to package right now? Lets the
        # UI offer the download only when it would actually succeed (an initial
        # backup exists from render onward) instead of presenting a 404-in-
        # waiting as if it were ready.
        # `current`: was the downloaded bundle built from the *latest* backup?
        # A pre-deploy (checkpoint-1) download goes stale once a later checkpoint
        # backup is sealed; the UI must not keep reassuring "safe to delete"
        # against a stale bundle (#140, Console UX Art. 1).
        from .package import find_latest_artifact
        try:
            latest_name = find_latest_artifact(settings.data_root, env_id).name
            available = True
        except PackageError:
            latest_name = None
            available = False
        payload = dict(record or {"downloaded_at": None})
        payload["available"] = available
        payload["current"] = download_is_current(record, latest_name)
        return JSONResponse(payload)

    # Change 4: CA certificate endpoint (session-protected, available post-bootstrap)
    @app.get(
        "/api/ca-cert/{env_id}",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def ca_cert(env_id: str) -> JSONResponse:
        # P0-2: validate env_id
        from .backup import validate_env_id
        validate_env_id(env_id)
        try:
            ctx = BootstrapContext.from_data_root(settings.data_root, env_id, remotes=[])
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        # Returns the ca-cert payload (present/note/pem/filename/requirement_note)
        payload = build_ca_cert_payload(ctx)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.post(
        "/api/manage/restore",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def manage_restore(
        file: UploadFile,
        passphrase: str = Form(...),
    ) -> JSONResponse:
        _gc_terminal_manage_sessions()
        # P1-4: Ignore client-supplied filename; cap upload size (100 MB)
        MAX_UPLOAD = 100 * 1024 * 1024
        content = file.file.read(MAX_UPLOAD + 1)
        if len(content) > MAX_UPLOAD:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="upload exceeds 100 MB limit",
            )
        tmp_dir = Path(tempfile.mkdtemp(prefix="manage-upload-", dir=settings.data_root))
        local_path = tmp_dir / "backup.tar.age"
        try:
            local_path.write_bytes(content)
            request = ManageRestoreRequest(
                artifact_path=local_path,
                passphrase=passphrase,
            )
            session, result = run_manage_restore(
                settings.data_root, request, enforce_tmpfs=settings.require_tmpfs_data_root
            )
        except ManageError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except BackupDecryptError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="restore decryption failed (wrong passphrase or corrupt artifact)",
            ) from exc
        except BackupIntegrityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except BackupError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        with app.state.bootstrap_lock:
            app.state.manage_sessions[session.session_id] = session
        return JSONResponse(result.model_dump())

    @app.post(
        "/api/manage/doctor",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def manage_doctor(payload: ManageDoctorRequest) -> JSONResponse:
        _gc_terminal_manage_sessions()
        with app.state.bootstrap_lock:
            session = app.state.manage_sessions.get(payload.session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="manage session not found",
            )
        # P1-1: Reserve under lock; ANY pre-spawn failure clears.
        with app.state.bootstrap_lock:
            if session.env_id in app.state.active_runs:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="an operation is already in progress for this env",
                )
            app.state.active_runs[session.env_id] = "__pending__"

        def _clear_active() -> None:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(session.env_id, None)

        try:
            run = build_doctor_run(session, settings.data_root)
        except Exception as exc:
            _clear_active()
            if isinstance(exc, ManageError):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            raise

        # P3: wrap set+spawn
        try:
            with app.state.bootstrap_lock:
                app.state.bootstrap_runs[run.run_id] = run
                app.state.active_runs[session.env_id] = run.run_id

            worker = threading.Thread(
                target=lambda: (_run_worker_wrapper(run), _clear_active()),
                daemon=True,
            )
            worker.start()
        except Exception:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(session.env_id, None)
                app.state.bootstrap_runs.pop(run.run_id, None)
            raise
        return JSONResponse({"run_id": run.run_id})

    @app.delete(
        "/api/manage/sessions/{session_id}",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def manage_delete(session_id: str) -> JSONResponse:
        with app.state.bootstrap_lock:
            session = app.state.manage_sessions.pop(session_id, None)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="manage session not found",
            )
        session.terminal = True
        session.finished_at = time.time()
        session.wipe_secrets()
        return JSONResponse({"ok": True})

    @app.post(
        "/api/manage/action/start",
        include_in_schema=False,
        dependencies=[Depends(require_session)],
    )
    async def manage_action_start(payload: ManageActionStartRequest) -> JSONResponse:
        session = _manage_session_or_404(payload.session_id)
        # P1-1: Reserve under lock; ANY pre-spawn failure clears.
        with app.state.bootstrap_lock:
            if session.env_id in app.state.active_runs:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="an operation is already in progress for this env",
                )
            app.state.active_runs[session.env_id] = "__pending__"

        def _clear_active() -> None:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(session.env_id, None)

        try:
            checkpoint_n = session.last_checkpoint + 1
            # P2b: wrap build_action_run errors
            run = build_action_run(
                session,
                settings.data_root,
                payload.action,
                payload.params,
                checkpoint_n=checkpoint_n,
            )
        except Exception as exc:
            _clear_active()
            if isinstance(exc, ManageError):
                msg = str(exc)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=msg,
                ) from exc
            raise

        # P3: wrap set+spawn; roll back last_checkpoint on failure
        prior_checkpoint = session.last_checkpoint
        try:
            with app.state.bootstrap_lock:
                app.state.bootstrap_runs[run.run_id] = run
                app.state.active_runs[session.env_id] = run.run_id
                session.last_checkpoint = checkpoint_n

            is_teardown = payload.action == "teardown"
            worker = threading.Thread(
                target=lambda: (
                    _manage_action_wrapper(run, session=session, is_teardown=is_teardown),
                    _clear_active(),
                ),
                daemon=True,
            )
            worker.start()
        except Exception:
            with app.state.bootstrap_lock:
                app.state.active_runs.pop(session.env_id, None)
                app.state.bootstrap_runs.pop(run.run_id, None)
                session.last_checkpoint = prior_checkpoint
            raise
        return JSONResponse({"run_id": run.run_id})

    @app.get("/", include_in_schema=False)
    async def index(_: Request) -> Response:
        index_html = _index_html()
        if index_html is not None:
            return FileResponse(index_html)
        return _fallback_html(settings)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str, _: Request) -> Response:
        if path.startswith("static/"):
            return PlainTextResponse("not found", status_code=404)
        index_html = _index_html()
        if index_html is not None:
            return FileResponse(index_html)
        return _fallback_html(settings)

    return app


def main() -> None:
    settings = load_settings()
    # ADR-0044: prove the data root is tmpfs *before* writing anything into it —
    # the TLS self-signed key below lands under data_root, so the gate must run
    # first (create_app re-checks; idempotent). (codex P2.2.)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    assert_data_root_tmpfs(settings.data_root, enforce=settings.require_tmpfs_data_root)
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    if settings.tls_enabled:
        if settings.tls_cert and settings.tls_key:
            ssl_certfile = str(settings.tls_cert)
            ssl_keyfile = str(settings.tls_key)
        else:
            tls_dir = settings.data_root / "tls"
            cert_path, key_path = ensure_self_signed(tls_dir, settings.tls_sans)
            ssl_certfile = str(cert_path)
            ssl_keyfile = str(key_path)
    uvicorn.run(
        create_app,
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=build_uvicorn_log_config(),
        access_log=True,
        log_level=settings.log_level,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


if __name__ == "__main__":
    main()
