from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
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
    BootstrapContext,
    build_bootstrap_steps,
    build_ca_cert_payload,
    build_hosts_map_payload,
    build_passkey_payload,
    ensure_runtime_repos,
    make_checkpoint_fn,
    seed_openbao_redactions,
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

        link_min = max(1, settings.launch_token_ttl_seconds // 60)
        if state.expired(now, settings.launch_token_ttl_seconds):
            response = _launch_notice_html(
                settings,
                heading="Launch link expired",
                body=(
                    f"This one-time launch link is valid for about {link_min} minutes after the "
                    "container starts, and that window has passed. Restart the container and open "
                    "the fresh link printed in its logs."
                ),
                status_code=410,
            )
        elif state.used_at is not None:
            response = _launch_notice_html(
                settings,
                heading="Launch link already used",
                body=(
                    "This launch link is single-use and has already opened a session. If you "
                    "still have the browser tab where you opened it, keep using that one. "
                    "Otherwise restart the container for a fresh link."
                ),
                status_code=403,
            )
        elif supplied != state.token:
            response = _launch_notice_html(
                settings,
                heading="Invalid launch link",
                body=(
                    "This token doesn't match the running container. Use the exact link printed in "
                    "the container logs, or restart the container for a fresh one."
                ),
                status_code=403,
            )
        else:
            state.consume(now)
            request.session["dmf_init_launch"] = {"started_at": now}
            response = RedirectResponse(url=request.url.path or "/", status_code=302)
        await response(scope, receive, send)


def require_session(request: Request) -> None:
    if "dmf_init_launch" not in request.session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="launch session required",
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
    run_id: str
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: LaunchTokenState = app.state.launch_token_state
    settings: Settings = app.state.settings
    scheme = "https" if settings.tls_enabled else "http"
    link_min = max(1, settings.launch_token_ttl_seconds // 60)
    session_h = settings.session_ttl_seconds / 3600
    # The bind interface (often 0.0.0.0 in a container) is not browsable — always
    # show a host the operator can actually open.
    display_host = settings.bind_host
    if display_host in ("0.0.0.0", "::", ""):
        display_host = "localhost"
    launch_url = f"{scheme}://{display_host}:{settings.bind_port}/"
    print(f"launch token: {state.token}", flush=True)
    print(f"open {launch_url}?token={state.token}", flush=True)
    print(
        f"  (single-use link, valid ~{link_min} min after start; "
        f"your session then lasts ~{session_h:.0f}h)",
        flush=True,
    )
    if settings.tls_enabled:
        print(
            "  note: the self-signed cert shows a one-time 'Not secure' warning — "
            "click Proceed; the page is still a secure context. Reaching it as "
            "http://localhost avoids the warning entirely.",
            flush=True,
        )
    logger.info("launch token generated", extra={"event": "launch_token_issued"})
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
        with app.state.bootstrap_lock:
            run = app.state.bootstrap_runs.get(payload.run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="run not found",
            )
        if run.final_status != "error":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="run is not in a failed state",
            )
        env_id = run.env_id
        if env_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="run env_id unavailable",
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
            failed_step_id = run.failed_step_id
            if failed_step_id is None:
                for ev in reversed(run.events):
                    if ev.get("event") == "error" and ev.get("step"):
                        failed_step_id = ev["step"]
                        break
            if failed_step_id is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="failed step id not found in run",
                )
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

            if not any(
                isinstance(s, CheckpointStep) and s.n == 2 for s in steps
            ) and not (ctx.env_dir / "openbao-keys.json").is_file():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot safely resume: openbao-keys.json missing "
                    "(would stream unredacted secrets)",
                )

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
            )
            seed_openbao_redactions(retry_run, ctx)
            _register_and_spawn(env_id, retry_run, _clear_active)
        except Exception:
            _clear_active()
            raise
        return JSONResponse({"run_id": retry_run_id})

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
        return JSONResponse(record or {"downloaded_at": None})

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
            session, result = run_manage_restore(settings.data_root, request)
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
