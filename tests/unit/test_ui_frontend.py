"""Frontend serving pins (Phase 8, Task 7).

The frontend carries no safety logic (it renders and clicks), so the pins here are about the
*serving* contract: assets come back under the strict CSP, every asset is session-gated,
path traversal is refused, and — the supply-chain pin — nothing references an external host
(fully self-contained, offline). Screen behavior is exercised live in Task 11.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.config import load_config
from jarvis.ui.auth import SESSION_COOKIE, AuthManager
from jarvis.ui.server import STATIC_DIR, create_app

STATIC_FILES = sorted(STATIC_DIR.rglob("*.*"))


def _client(tmp_path: Path):
    cfg = load_config(root=tmp_path, env_file=None)
    auth = AuthManager(token="tok")
    app = create_app(cfg, auth=auth)
    return TestClient(app, base_url="http://127.0.0.1"), auth


def _cookie(auth: AuthManager) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE}={auth.mint_session()}"}


def test_index_served_authenticated_under_csp(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    r = client.get("/", headers=_cookie(auth))
    assert r.status_code == 200
    assert "KAIRO" in r.text and "/static/app.js" in r.text
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")


def test_static_assets_served_and_gated(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    for path in ("/static/kairo.css", "/static/app.js", "/static/screens/gate.js"):
        assert client.get(path).status_code == 401, path  # no session ⇒ refused
        r = client.get(path, headers=_cookie(auth))
        assert r.status_code == 200, path
        assert r.headers.get("referrer-policy") == "no-referrer"


def test_static_path_traversal_refused(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    # Escaping STATIC_DIR must 404 (never serve server.py / secrets).
    for bad in ("/static/../server.py", "/static/../../config.py"):
        assert client.get(bad, headers=_cookie(auth)).status_code == 404, bad


def test_no_external_resources_in_any_asset() -> None:
    # Supply-chain pin: no CDN, no external font/script/style. The ONLY http(s) URLs allowed
    # are W3C XML namespaces (identifiers in the inline SVG — never fetched).
    assert STATIC_FILES, "expected static assets on disk"
    url = re.compile(r"https?://[^\s\"')]+")
    for f in STATIC_FILES:
        for match in url.findall(f.read_text(encoding="utf-8")):
            assert match.startswith("http://www.w3.org/"), f"{f.name}: external ref {match!r}"


def test_assets_have_no_inline_event_handlers() -> None:
    # CSP forbids inline handlers anyway; assert we didn't write any (addEventListener only).
    for f in STATIC_FILES:
        if f.suffix in {".html", ".js"}:
            text = f.read_text(encoding="utf-8")
            assert " onclick=" not in text and " onload=" not in text, f.name


def test_shell_hides_via_class_not_blocked_inline_style() -> None:
    # The strict CSP (style-src 'self') BLOCKS inline style= — so an element that must start hidden
    # would be left VISIBLE by style="display:none" (this was the Talk-button / empty-card bug).
    # The shell must hide via the is-hidden CLASS (toggled by classList, which CSP allows).
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'style="display:none"' not in index, "use the is-hidden class, not blocked inline style"
    assert "is-hidden" in index
    from jarvis.ui.server import STATIC_DIR as SD
    assert ".is-hidden" in (SD / "kairo.css").read_text(encoding="utf-8")
