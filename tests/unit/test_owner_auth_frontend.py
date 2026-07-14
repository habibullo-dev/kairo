"""The anonymous auth shell is minimal; the workstation bundle remains session-gated."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx

from jarvis.config import load_config
from jarvis.persistence.db import connect
from jarvis.persistence.sessions import SessionStore
from jarvis.ui.auth import AuthManager
from jarvis.ui.owner_auth import Argon2PasswordHasher, OwnerAuthService
from jarvis.ui.server import create_app

TOKEN = "frontend-owner-token"
PASSWORD = "A unique owner passphrase 2026!"
ORIGIN = {"origin": "http://127.0.0.1"}


async def test_auth_shell_is_exactly_public_and_has_no_external_dependencies(
    tmp_path: Path,
) -> None:
    db = await connect(tmp_path / "owner-frontend.db")
    store = SessionStore(db)
    owner = OwnerAuthService(
        db,
        store.lock,
        hasher=Argon2PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        clock=lambda: dt.datetime(2026, 7, 14, tzinfo=dt.UTC),
    )
    app = create_app(
        load_config(root=tmp_path, env_file=None),
        auth=AuthManager(token=TOKEN),
        owner_auth=owner,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            follow_redirects=False,
        ) as client:
            assert (await client.get(f"/?token={TOKEN}")).status_code == 303
            setup = await client.get("/setup")
            assert setup.status_code == 200
            assert setup.headers["content-type"].startswith("text/html")
            assert "Create the owner account" in setup.text
            assert '<script type="module" src="/static/auth/auth.js"></script>' in setup.text
            assert "<script>" not in setup.text and "<style" not in setup.text
            assert "https://" not in setup.text and "http://" not in setup.text
            assert setup.text.count('method="post"') == 3
            assert 'action="/auth/login"' in setup.text

            css = await client.get("/static/auth/auth.css")
            script = await client.get("/static/auth/auth.js")
            favicon = await client.get("/static/assets/kairo-favicon.svg")
            assert css.status_code == script.status_code == favicon.status_code == 200
            assert "app.js" not in script.text
            assert "location.replace(\"/\")" in script.text

            # Exact allowlist: neither the workstation shell nor an arbitrary auth-prefix file
            # becomes anonymous just because the login bundle is public.
            assert (await client.get("/static/app.js")).status_code == 401
            assert (await client.get("/static/kairo.css")).status_code == 401
            assert (await client.get("/static/auth/not-real.js")).status_code == 401

            enrolled = await client.post(
                "/auth/enroll",
                json={"username": "habib", "password": PASSWORD},
                headers=ORIGIN,
            )
            assert enrolled.status_code == 200
            client.cookies.clear()
            login = await client.get("/login")
            assert login.status_code == 200 and "Welcome back" in login.text
            recovery = await client.get("/recover")
            assert recovery.status_code == 303 and recovery.headers["location"] == "/login"
    finally:
        await db.close()


def test_workstation_shell_handles_session_expiry_and_explicit_lock() -> None:
    static = Path(__file__).parents[2] / "src" / "jarvis" / "ui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="st-logout"' in html
    assert 'location.replace("/login")' in script
    assert 'api.post("/auth/logout"' in script
    assert 'fetch("/auth/session"' in script
    assert "else if (response.ok) connect()" in script
    assert "waitForWorkspaceAfter(generation)" in script
    assert 'api.post("/auth/step-up", { password })' in script
