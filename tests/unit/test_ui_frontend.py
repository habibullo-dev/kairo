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
TEXT_ASSET_SUFFIXES = {".css", ".html", ".js", ".json", ".svg", ".txt"}
CSP_BLOCKED_STYLE_PATTERNS = {
    "inline style element": re.compile(r"<style(?:\s|>)", re.IGNORECASE),
    "inline style attribute": re.compile(
        r"<[a-z][^<>]*\sstyle\s*=", re.IGNORECASE
    ),
    # Reserve the ``style`` object key throughout production UI JavaScript. This intentionally
    # errs safe: every DOM builder funnels presentation through classes, and the shared ``el``
    # helper also rejects computed/spread style keys at runtime.
    "style object key": re.compile(r"\bstyle\s*:", re.IGNORECASE),
    "setAttribute style attribute": re.compile(
        r"\.setAttribute\(\s*[\"']style[\"']", re.IGNORECASE
    ),
    # Chromium currently permits cssText through CSSOM, but banning it keeps dynamic changes
    # reviewable as discrete properties and avoids browser-specific CSP behavior.
    "cssText assignment": re.compile(r"\.style\.cssText\s*=", re.IGNORECASE),
}


def _without_js_comments(source: str) -> str:
    """Remove JavaScript comments while preserving quoted and template-string source."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(source):
        char = source[i]
        following = source[i + 1] if i + 1 < len(source) else ""
        if quote is not None:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            i += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            out.append(char)
            i += 1
            continue
        if char == "/" and following == "/":
            i += 2
            while i < len(source) and source[i] not in "\r\n":
                i += 1
            continue
        if char == "/" and following == "*":
            i += 2
            while i + 1 < len(source) and source[i : i + 2] != "*/":
                if source[i] in "\r\n":
                    out.append(source[i])
                i += 1
            i = min(len(source), i + 2)
            continue
        out.append(char)
        i += 1
    return "".join(out)


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
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "'unsafe-inline'" not in csp


def test_static_assets_served_and_gated(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    for path in (
        "/static/kairo.css", "/static/app.js", "/static/screens/gate.js",
        "/static/assets/kira-workspace-bg-noir.jpg",
    ):
        assert client.get(path).status_code == 401, path  # no session ⇒ refused
        r = client.get(path, headers=_cookie(auth))
        assert r.status_code == 200, path
        assert r.headers.get("referrer-policy") == "no-referrer"
    assert r.headers.get("content-type", "").startswith("image/jpeg")


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
        if f.suffix.lower() not in TEXT_ASSET_SUFFIXES:
            continue  # local binary assets are served/gated, but cannot contain text URLs to scan
        for match in url.findall(f.read_text(encoding="utf-8")):
            assert match.startswith("http://www.w3.org/"), f"{f.name}: external ref {match!r}"


def test_assets_have_no_inline_event_handlers() -> None:
    # CSP forbids inline handlers anyway; assert we didn't write any (addEventListener only).
    for f in STATIC_FILES:
        if f.suffix in {".html", ".js"}:
            text = f.read_text(encoding="utf-8")
            assert " onclick=" not in text and " onload=" not in text, f.name


def test_assets_have_no_csp_blocked_inline_styles() -> None:
    """Keep every shipped style declaration compatible with ``style-src 'self'``.

    Static HTML attributes, template-string attributes, and ``el(..., {style: ...})`` create
    inline style attributes that Chromium refuses under the production CSP.  Direct property
    updates such as ``node.style.width = ...`` remain available for genuinely dynamic geometry;
    durable presentation belongs in ``kairo.css``.  ``cssText`` is banned as a portability and
    reviewability convention even though current Chromium applies it through CSSOM.
    """
    for f in STATIC_FILES:
        if f.suffix not in {".html", ".js"}:
            continue
        text = f.read_text(encoding="utf-8")
        if f.suffix == ".js":
            text = _without_js_comments(text)
        for label, pattern in CSP_BLOCKED_STYLE_PATTERNS.items():
            assert pattern.search(text) is None, f"{f.relative_to(STATIC_DIR)}: {label}"


def test_csp_style_scanner_ignores_comments_but_checks_runtime_source() -> None:
    comments_only = _without_js_comments(
        '// style="display:none"\n/* el("div", {style: "display:none"}) */\n'
    )
    assert all(
        pattern.search(comments_only) is None
        for pattern in CSP_BLOCKED_STYLE_PATTERNS.values()
    )
    safe_sources = (
        "const style = getComputedStyle(node);",
        'node.style.display = "none";',
        'node.style.setProperty("--accent", value);',
    )
    for source in safe_sources:
        assert all(
            pattern.search(_without_js_comments(source)) is None
            for pattern in CSP_BLOCKED_STYLE_PATTERNS.values()
        ), source
    blocked_sources = (
        'const rules = "<style>body{display:none}</style>";',
        'const markup = `<div style="display:none"></div>`;',
        'el("div", {style: "display:none"});',
        'node.setAttribute("style", "display:none");',
        'node.style.cssText = "display:none";',
    )
    for source in blocked_sources:
        assert any(
            pattern.search(_without_js_comments(source)) is not None
            for pattern in CSP_BLOCKED_STYLE_PATTERNS.values()
        ), source


def test_shell_hides_via_class_not_blocked_inline_style() -> None:
    # The strict CSP (style-src 'self') BLOCKS inline style= — so an element that must start hidden
    # would be left VISIBLE by style="display:none" (this was the Talk-button / empty-card bug).
    # The shell must hide via the is-hidden CLASS (toggled by classList, which CSP allows).
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'style="display:none"' not in index, "use the is-hidden class, not blocked inline style"
    assert "is-hidden" in index
    from jarvis.ui.server import STATIC_DIR as SD
    assert ".is-hidden" in (SD / "kairo.css").read_text(encoding="utf-8")
