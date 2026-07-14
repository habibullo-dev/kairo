"""HTTP and runtime integration for the single-owner authentication boundary."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from jarvis.config import load_config
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.owner_auth import Argon2PasswordHasher, OwnerAuthService
from jarvis.ui.server import AUTH_GRANT_COOKIE, create_app

TOKEN = "owner-launch-token-canary"
PASSWORD = "A unique owner passphrase 2026!"
REPLACEMENT = "A replacement owner passphrase 2026!"
ORIGIN = {"origin": "http://127.0.0.1"}


class _FakeWebSocket:
    def __init__(self) -> None:
        self.close_code: int | None = None

    async def close(self, *, code: int) -> None:
        self.close_code = code


@asynccontextmanager
async def _owner_client(tmp_path: Path, *, pre_enrolled: bool = False):
    db = await connect(tmp_path / "owner-routes.db")
    store = SessionStore(db)
    now = [dt.datetime(2026, 7, 14, tzinfo=dt.UTC)]
    owner = OwnerAuthService(
        db,
        store.lock,
        hasher=Argon2PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=lambda: now[0],
    )
    if pre_enrolled:
        grant = await owner.issue_auth_grant("enroll")
        await owner.enroll(grant.token, "habib", PASSWORD)
    auth = AuthManager(token=TOKEN)
    app = create_app(load_config(root=tmp_path, env_file=None), auth=auth, owner_auth=owner)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            follow_redirects=False,
        ) as client:
            yield client, app, owner, now
    finally:
        await db.close()


async def _login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    return await client.post(
        "/auth/login",
        json={"username": "habib", "password": password},
        headers=ORIGIN,
    )


async def test_launch_grant_enrolls_once_without_direct_app_authority(tmp_path: Path) -> None:
    async with _owner_client(tmp_path) as (client, _app, _owner, _now):
        root = await client.get("/")
        assert root.status_code == 303 and root.headers["location"] == "/setup"
        assert (await client.get("/setup")).status_code == 401

        exchange = await client.get(f"/?token={TOKEN}")
        assert exchange.status_code == 303 and exchange.headers["location"] == "/setup"
        assert AUTH_GRANT_COOKIE in client.cookies
        assert SESSION_COOKIE not in client.cookies
        assert TOKEN not in exchange.text + str(exchange.headers)
        assert (await client.get("/api/tasks")).status_code == 401
        assert (await client.get("/static/app.js")).status_code == 401
        assert (await client.get("/setup")).json() == {"page": "setup", "ready": True}
        assert (await client.get(f"/?token={TOKEN}")).status_code == 401

        enrolled = await client.post(
            "/auth/enroll",
            json={"username": "habib", "password": PASSWORD},
            headers=ORIGIN,
        )
        assert enrolled.status_code == 200 and enrolled.json()["username"] == "habib"
        assert SESSION_COOKIE in client.cookies
        assert AUTH_GRANT_COOKIE not in client.cookies
        assert (await client.get("/")).status_code == 200
        setup_closed = await client.get("/setup")
        assert setup_closed.status_code == 303 and setup_closed.headers["location"] == "/"
        session = await client.get("/auth/session")
        assert session.status_code == 200
        assert session.json()["username"] == "habib"
        assert session.json()["fresh"] is True


async def test_login_logout_revokes_cookie_socket_and_session(tmp_path: Path) -> None:
    async with _owner_client(tmp_path, pre_enrolled=True) as (client, app, owner, _now):
        root = await client.get("/")
        assert root.status_code == 303 and root.headers["location"] == "/login"
        assert (await _login(client, "A wrong owner passphrase 2026!")).status_code == 401
        logged_in = await _login(client)
        assert logged_in.status_code == 200
        old_bearer = client.cookies[SESSION_COOKIE]

        socket = _FakeWebSocket()
        conn = app.state.connections.register(socket, owner_session=old_bearer)
        logged_out = await client.post("/auth/logout", headers=ORIGIN)
        assert logged_out.status_code == 200
        assert SESSION_COOKIE not in client.cookies
        assert await owner.validate_session(old_bearer) is None
        assert app.state.connections.get(conn.id) is None
        assert socket.close_code == 1008
        assert (await client.get("/auth/session")).status_code == 401
        assert (await _login(client)).status_code == 200


async def test_recovery_revokes_every_old_session_and_changes_password(tmp_path: Path) -> None:
    async with _owner_client(tmp_path, pre_enrolled=True) as (client, _app, owner, _now):
        assert (await _login(client)).status_code == 200
        first_bearer = client.cookies[SESSION_COOKIE]
        second = await owner.login("habib", PASSWORD)
        assert second is not None

        exchange = await client.get(f"/?token={TOKEN}")
        assert exchange.status_code == 303 and exchange.headers["location"] == "/recover"
        assert (await client.get("/recover")).status_code == 200
        recovered = await client.post(
            "/auth/recover", json={"password": REPLACEMENT}, headers=ORIGIN
        )
        assert recovered.status_code == 200
        new_bearer = client.cookies[SESSION_COOKIE]
        assert new_bearer != first_bearer
        assert await owner.validate_session(first_bearer) is None
        assert await owner.validate_session(second.session.token) is None
        assert await owner.validate_session(new_bearer) is not None

        assert (await client.post("/auth/logout", headers=ORIGIN)).status_code == 200
        assert (await _login(client)).status_code == 401
        assert (await _login(client, REPLACEMENT)).status_code == 200


async def test_step_up_rotates_session_and_renewal_never_extends_absolute(tmp_path: Path) -> None:
    async with _owner_client(tmp_path, pre_enrolled=True) as (client, app, owner, now):
        assert (await _login(client)).status_code == 200
        old_bearer = client.cookies[SESSION_COOKIE]
        old_state = await owner.validate_session(old_bearer)
        assert old_state is not None

        now[0] += dt.timedelta(minutes=6)
        stale = await client.get("/auth/session")
        assert stale.status_code == 200 and stale.json()["fresh"] is False
        socket = _FakeWebSocket()
        app.state.connections.register(socket, owner_session=old_bearer)
        stepped_up = await client.post("/auth/step-up", json={"password": PASSWORD}, headers=ORIGIN)
        assert stepped_up.status_code == 200
        new_bearer = client.cookies[SESSION_COOKIE]
        new_state = await owner.validate_session(new_bearer)
        assert new_bearer != old_bearer
        assert await owner.validate_session(old_bearer) is None
        assert new_state is not None and new_state.fresh
        assert new_state.absolute_expires_at == old_state.absolute_expires_at
        assert socket.close_code == 1008

        now[0] += dt.timedelta(hours=25)
        renewed = await client.get("/auth/session")
        assert renewed.status_code == 200
        assert "kairo_session=" in renewed.headers.get("set-cookie", "")
        assert renewed.json()["absolute_expires_at"] == old_state.absolute_expires_at


async def test_auth_boundary_preserves_host_origin_and_body_limits(tmp_path: Path) -> None:
    async with _owner_client(tmp_path) as (client, _app, _owner, _now):
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/api/health", headers={"host": "evil.test"})).status_code == 400
        no_origin = await client.post(
            "/auth/login", json={"username": "habib", "password": PASSWORD}
        )
        assert no_origin.status_code == 403
        foreign_origin = await client.post(
            "/auth/login",
            json={"username": "habib", "password": PASSWORD},
            headers={"origin": "http://evil.test"},
        )
        assert foreign_origin.status_code == 403

        assert (await client.get(f"/?token={TOKEN}")).status_code == 303
        oversized = await client.post(
            "/auth/enroll",
            content=b"{" + b'"password":"' + (b"x" * 5000) + b'"}',
            headers={**ORIGIN, "content-type": "application/json"},
        )
        assert oversized.status_code == 413
        malformed = await client.post(
            "/auth/enroll",
            content=b"{not-json",
            headers={**ORIGIN, "content-type": "application/json"},
        )
        assert malformed.status_code == 400
