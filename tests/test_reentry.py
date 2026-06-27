"""Tests for the long-run re-entry hardening (facets a + b, issue #150).

Facet (a): the session slides on each authenticated request (idle TTL) but is
bounded by an absolute cap. Facet (b): SIGHUP re-mints the launch token in-band.
"""
from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dmf_init import main
from dmf_init.main import _remint_launch_token, create_app
from dmf_init.settings import Settings


def _app(tmp_path: Path, **overrides) -> main.FastAPI:
    return create_app(Settings(data_root=tmp_path / "data", tls_enabled=False, **overrides))


def _launch(client: TestClient, app) -> None:
    token = app.state.launch_token_state.token
    resp = client.get(f"/?token={token}", follow_redirects=False)
    assert resp.status_code in {302, 307}


# --- facet (a): sliding idle window + absolute cap ----------------------------


def test_each_authenticated_request_reissues_the_cookie(tmp_path: Path) -> None:
    """The slide: every protected request marks the session modified, so
    Starlette re-issues the cookie with a fresh max_age."""
    app = _app(tmp_path)
    client = TestClient(app)
    _launch(client, app)

    resp = client.get("/api/session")
    assert resp.status_code == 200
    # Set-Cookie present => the session was re-stamped (idle window slid forward).
    assert "set-cookie" in {k.lower() for k in resp.headers.keys()}


def test_absolute_cap_expires_even_with_activity(tmp_path: Path, monkeypatch) -> None:
    clock = {"t": 1_000.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])

    app = _app(tmp_path, session_absolute_cap_seconds=100)
    client = TestClient(app)
    _launch(client, app)  # started_at = 1000

    clock["t"] = 1_050.0  # within cap
    assert client.get("/api/session").status_code == 200

    clock["t"] = 1_101.0  # past the 100s absolute cap
    expired = client.get("/api/session")
    assert expired.status_code == 401
    assert expired.json()["detail"] == "launch session expired"


def test_session_status_reports_absolute_remaining(tmp_path: Path, monkeypatch) -> None:
    clock = {"t": 5_000.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    # High idle TTL so the cookie doesn't idle-expire over the jump; we're
    # isolating the absolute-cap math here.
    app = _app(tmp_path, session_absolute_cap_seconds=3600, session_ttl_seconds=100_000)
    client = TestClient(app)
    _launch(client, app)

    clock["t"] = 5_600.0  # 600s elapsed of a 3600s cap
    body = client.get("/api/session").json()
    assert body["idle_ttl_seconds"] == 100_000
    assert body["absolute_remaining_seconds"] == 3000


def test_missing_session_still_401(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    resp = client.get("/api/session")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "launch session required"


# --- facet (b): SIGHUP re-mint ------------------------------------------------


def test_remint_issues_fresh_usable_token_without_restart(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    old_token = app.state.launch_token_state.token

    # Consume the original link.
    _launch(client, app)
    assert app.state.launch_token_state.used_at is not None

    # SIGHUP path: re-mint on the running app.
    _remint_launch_token(app)
    new_token = app.state.launch_token_state.token
    assert new_token != old_token
    assert app.state.launch_token_state.used_at is None  # usable again

    # The old link is now invalid...
    assert client.get(f"/?token={old_token}", follow_redirects=False).status_code in {403, 410}

    # ...and the new link establishes a session for a fresh (cookieless) client.
    fresh = TestClient(app)
    assert fresh.get(f"/?token={new_token}", follow_redirects=False).status_code in {302, 307}
    assert fresh.get("/api/session").status_code == 200


def test_remint_prints_fresh_link(tmp_path: Path, capsys) -> None:
    app = _app(tmp_path)
    capsys.readouterr()  # clear startup output
    _remint_launch_token(app)
    out = capsys.readouterr().out
    assert "re-minting launch token" in out
    assert app.state.launch_token_state.token in out


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_lifespan_actually_registers_the_sighup_handler(tmp_path: Path) -> None:
    """Discriminating against accidental removal of the add_signal_handler line:
    entering the lifespan must install a SIGHUP handler (not SIG_DFL)."""
    app = _app(tmp_path)
    assert signal.getsignal(signal.SIGHUP) in (signal.SIG_DFL, signal.SIG_IGN)

    async def _run() -> object:
        async with main.lifespan(app):
            return signal.getsignal(signal.SIGHUP)

    installed = asyncio.run(_run())
    assert installed not in (signal.SIG_DFL, signal.SIG_IGN, None)
