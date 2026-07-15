"""Pin the API routes that the browser intentionally does (and does not) consume.

This is deliberately a source-level contract.  It does not prove a click succeeds; it
prevents a new server route from becoming silently unreachable by requiring an explicit
browser reference or a concise, reviewed reason why it is not a browser surface.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from kira.config import load_config
from kira.ui.auth import AuthManager
from kira.ui.server import create_app

ROOT = Path(__file__).parents[2]
STATIC_DIR = ROOT / "src" / "kira" / "ui" / "static"

# A non-browser route is a deliberate product/API decision, not an accidental gap.  Keep each
# reason short so adding an exemption is a visible review decision.
INTENTIONALLY_NON_BROWSER: dict[str, str] = {
    "/api/artifacts/{param}": "The list and hardened content route cover the current artifact UI.",
    "/api/graph/search": "Federated palette search supersedes the graph-specific query surface.",
    "/api/health": "Unauthenticated health check is for local monitoring, not the browser UI.",
    "/api/intents/{param}": (
        "The queue list contains the currently supported intent inspection detail."
    ),
    "/api/voice/listen": "Server-mic capture is superseded by browser voice capture.",
}


def _iter_js_strings(source: str) -> Iterator[str]:
    """Yield JavaScript string bodies without treating comment text as a request."""
    i = 0
    while i < len(source):
        if source.startswith("//", i):
            newline = source.find("\n", i + 2)
            i = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = len(source) if end < 0 else end + 2
            continue
        if source[i] not in {"'", '"', "`"}:
            i += 1
            continue
        quote = source[i]
        i += 1
        start = i
        while i < len(source):
            if source[i] == "\\":
                i += 2
                continue
            if source[i] == quote:
                yield source[start:i]
                i += 1
                break
            i += 1


def _first_argument(source: str, open_paren: int) -> str:
    """Return one call's first argument, preserving dynamic URL expressions."""
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    i = open_paren + 1
    start = i
    while i < len(source):
        char = source[i]
        if char in {"'", '"', "`"}:
            quote = char
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in {",", ")"}:
            return source[start:i]
        i += 1
    return source[start:]


def _path_from_expression(expression: str) -> str | None:
    """Join literal URL pieces, placing a parameter marker across dynamic expressions."""
    pieces = list(_iter_js_strings(expression))
    if not pieces or not pieces[0].startswith("/api/"):
        return None
    # An outer call such as ``Promise.all(api.get(...), api.get(...))`` is not itself a URL
    # expression. Its nested calls are scanned independently.
    if sum(piece.startswith("/api/") for piece in pieces) != 1:
        return None
    joined = "{param}".join(
        re.sub(r"\$\{.*?\}", "{param}", piece, flags=re.DOTALL) for piece in pieces
    )
    return joined.split("?", maxsplit=1)[0].rstrip("/") or "/"


def _ui_route_templates() -> set[str]:
    """Collect API URL templates from direct calls and simple URL variables.

    The call parser handles templates and ``'/api/x/' + id + '/tail'`` expressions;
    the string pass covers simple local variables later passed to ``fetch(variable)``.
    """
    found: set[str] = set()
    for path in STATIC_DIR.rglob("*.js"):
        source = path.read_text(encoding="utf-8")
        for offset, char in enumerate(source):
            if char == "(":
                template = _path_from_expression(_first_argument(source, offset))
                if template is not None:
                    found.add(template)
        for literal in _iter_js_strings(source):
            if literal.startswith("/api/"):
                template = re.sub(r"\$\{.*?\}", "{param}", literal, flags=re.DOTALL)
                if not template.endswith("/"):
                    found.add(template.split("?", maxsplit=1)[0].rstrip("/"))
    return found


def _route_template(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{param}", path).rstrip("/") or "/"


def test_route_consumption_manifest(tmp_path: Path) -> None:
    app = create_app(load_config(root=tmp_path, env_file=None), auth=AuthManager(token="test"))
    api_routes = {route.path for route in app.routes if route.path.startswith("/api/")}
    api_templates = {_route_template(path) for path in api_routes}
    browser_routes = _ui_route_templates()

    unaccounted = sorted(api_templates - browser_routes - set(INTENTIONALLY_NON_BROWSER))
    stale_exemptions = sorted(set(INTENTIONALLY_NON_BROWSER) - api_templates)
    stale_browser_references = sorted(browser_routes - api_templates)
    redundant_exemptions = sorted(set(INTENTIONALLY_NON_BROWSER) & browser_routes)

    assert all(
        reason and "\n" not in reason and len(reason) <= 120
        for reason in INTENTIONALLY_NON_BROWSER.values()
    ), "each intentional exemption needs one concise, non-empty reason"
    assert not stale_exemptions, f"manifest names routes no longer registered: {stale_exemptions}"
    assert not stale_browser_references, (
        "browser source names API routes that are not registered: "
        f"{stale_browser_references}"
    )
    assert not redundant_exemptions, (
        "manifest routes now have browser consumers; remove their exemptions: "
        f"{redundant_exemptions}"
    )
    assert not unaccounted, (
        "Every API route must have a browser consumer or an intentional exemption with a concise "
        f"reason. Unaccounted routes: {unaccounted}"
    )


def test_route_template_scanner_keeps_dynamic_segments() -> None:
    # This is the important case a naive literal search misses.
    source = 'api.get("/api/workspace/" + projectId + "/activity");'
    assert _path_from_expression(_first_argument(source, source.index("("))) == (
        "/api/workspace/{param}/activity"
    )
    assert _route_template("/api/workspace/{project_id}/activity") == (
        "/api/workspace/{param}/activity"
    )
