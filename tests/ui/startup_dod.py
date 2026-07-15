"""Browser definition-of-done for Kira's theme bootstrap and startup preloader.

This is a standalone development check, not a pytest test. It serves a copy of the production
static shell with only ``app.js`` removed, so the real head bootstrap, preloader markup, CSS, and
assets run without needing an authenticated backend or WebSocket.

Usage (after ``uv sync --extra browser`` and ``uv run playwright install chromium``)::

    uv run python tests/ui/startup_dod.py
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import re
import shutil
import socket
import tempfile
import threading
from pathlib import Path

from kira.ui.server import STATIC_DIR

_HARNESS_NAME = "__startup_dod.html"
_SEED_SENTINEL = "__kira_startup_dod_seeded__"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _shell_without_application() -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html, replacements = re.subn(
        r'<script\s+type="module"\s+src="/static/app\.js"></script>', "", html, count=1
    )
    if replacements != 1:
        raise RuntimeError("production shell no longer has the expected external app.js tag")
    return html


def _seed_script(
    *,
    canonical: dict[str, str] | None = None,
    legacy: dict[str, str] | None = None,
    near_miss: dict[str, str] | None = None,
    ready_on_dom_content_loaded: bool = False,
) -> str:
    seed = json.dumps({"canonical": canonical, "legacy": legacy, "nearMiss": near_miss})
    ready = "true" if ready_on_dom_content_loaded else "false"
    return f"""
      (() => {{
        if (!sessionStorage.getItem({json.dumps(_SEED_SENTINEL)})) {{
          localStorage.clear();
          sessionStorage.clear();
          const seed = {seed};
          if (seed.canonical !== null) {{
            localStorage.setItem('kira:appearance', JSON.stringify(seed.canonical));
          }}
          if (seed.legacy !== null) {{
            localStorage.setItem('kairo:appearance', JSON.stringify(seed.legacy));
          }}
          if (seed.nearMiss !== null) {{
            localStorage.setItem('kairo:appearance:backup', JSON.stringify(seed.nearMiss));
          }}
          sessionStorage.setItem({json.dumps(_SEED_SENTINEL)}, '1');
        }}
        if ({ready}) {{
          document.addEventListener('DOMContentLoaded', () => {{
            document.dispatchEvent(new Event('kira:app-ready'));
          }}, {{ once: true }});
        }}
      }})();
    """


async def _new_page(
    browser: object,
    base: str,
    *,
    canonical: dict[str, str] | None = None,
    legacy: dict[str, str] | None = None,
    near_miss: dict[str, str] | None = None,
    reduced_motion: str = "no-preference",
    ready_on_dom_content_loaded: bool = False,
    viewport: dict[str, int] | None = None,
) -> tuple[object, object, list[str]]:
    context = await browser.new_context(
        viewport=viewport or {"width": 1024, "height": 768}, reduced_motion=reduced_motion
    )
    await context.add_init_script(
        _seed_script(
            canonical=canonical,
            legacy=legacy,
            near_miss=near_miss,
            ready_on_dom_content_loaded=ready_on_dom_content_loaded,
        )
    )
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("requestfailed", lambda request: errors.append(f"request failed: {request.url}"))
    page.on(
        "response",
        lambda response: errors.append(f"HTTP {response.status}: {response.url}")
        if response.status >= 400
        else None,
    )
    await page.goto(f"{base}/{_HARNESS_NAME}", wait_until="domcontentloaded")
    return context, page, errors


async def _snapshot(page: object) -> dict[str, object]:
    return await page.evaluate(
        """() => {
          const root = document.documentElement;
          const loader = document.querySelector('.kira-preloader');
          const style = loader ? getComputedStyle(loader) : null;
          const opacity = style ? Number(style.opacity) : 0;
          return {
            state: root.getAttribute('data-kira-boot'),
            theme: root.dataset.theme,
            density: root.dataset.density,
            layout: root.dataset.layout,
            reducedClass: root.classList.contains('reduce-motion'),
            accent: root.style.getPropertyValue('--accent').trim().toLowerCase(),
            accentRgb: root.style.getPropertyValue('--accent-rgb').trim(),
            exists: Boolean(loader),
            hidden: loader ? loader.hidden : true,
            pointerEvents: style?.pointerEvents || '',
            hitTargetIsLoader: document.elementFromPoint(innerWidth / 2, innerHeight / 2)
              === loader,
            visible: Boolean(loader && !loader.hidden && style.display !== 'none'
              && style.visibility !== 'hidden' && opacity > 0),
            seen: sessionStorage.getItem('kira:preloader-seen:v1')
          };
        }"""
    )


async def _wait_for_state(page: object, state: str, *, timeout_ms: int = 2_000) -> None:
    await page.wait_for_function(
        "state => document.documentElement.getAttribute('data-kira-boot') === state",
        arg=state,
        timeout=timeout_ms,
    )


async def _wait_until_hidden(page: object, *, timeout_ms: int = 1_000) -> None:
    await page.wait_for_function(
        """() => {
          const loader = document.querySelector('.kira-preloader');
          if (!loader || loader.hidden) return true;
          const style = getComputedStyle(loader);
          return style.display === 'none' || style.visibility === 'hidden'
            || Number(style.opacity) === 0;
        }""",
        timeout=timeout_ms,
    )


async def _assert_theme_and_ready_dismissal(browser: object, base: str) -> None:
    cases = [
        (
            "noir",
            {"theme": "noir", "density": "compact", "layout": "expanded", "motion": "on",
             "accent": "#12AbEf"},
            None,
        ),
        ("light", {"theme": "light", "motion": "on"}, {"theme": "neon"}),
        ("neon", None, {"theme": "neon", "motion": "on"}),
    ]
    for expected_theme, canonical, legacy in cases:
        context, page, errors = await _new_page(
            browser, base, canonical=canonical, legacy=legacy
        )
        try:
            await _wait_for_state(page, "pending")
            pending = await _snapshot(page)
            assert pending["exists"] and pending["visible"], (expected_theme, pending)
            assert pending["pointerEvents"] == "none", (expected_theme, pending)
            assert pending["theme"] == expected_theme, pending
            if expected_theme == "noir":
                assert pending["density"] == "compact" and pending["layout"] == "expanded"
                assert pending["accent"] == "#12abef" and pending["accentRgb"] == "18, 171, 239"

            await page.evaluate("document.dispatchEvent(new Event('kira:app-ready'))")
            await _wait_for_state(page, "ready")
            await _wait_until_hidden(page)
            ready = await _snapshot(page)
            assert not ready["visible"] and ready["seen"] is not None, ready
            assert errors == [], errors

            if expected_theme == "light":
                # The canonical key wins over an exact legacy alias, and this tab does not replay
                # the animation after a navigation/reload.
                await page.reload(wait_until="domcontentloaded")
                await _wait_for_state(page, "skipped")
                skipped = await _snapshot(page)
                assert skipped["theme"] == "light" and not skipped["visible"], skipped
                assert errors == [], errors
        finally:
            await context.close()


async def _assert_near_miss_legacy_key_is_ignored(browser: object, base: str) -> None:
    context, page, errors = await _new_page(browser, base, near_miss={"theme": "neon"})
    try:
        await _wait_for_state(page, "pending")
        snapshot = await _snapshot(page)
        assert snapshot["theme"] == "noir", snapshot
        await page.evaluate("document.dispatchEvent(new Event('kira:app-ready'))")
        await _wait_for_state(page, "ready")
        assert errors == [], errors
    finally:
        await context.close()


async def _assert_reduced_motion_never_arms(browser: object, base: str) -> None:
    cases = [
        ({"theme": "light", "motion": "on"}, "reduce"),
        ({"theme": "neon", "motion": "off"}, "no-preference"),
    ]
    for appearance, browser_motion in cases:
        context, page, errors = await _new_page(
            browser, base, canonical=appearance, reduced_motion=browser_motion
        )
        try:
            await _wait_for_state(page, "skipped")
            await page.wait_for_timeout(180)  # longer than the normal delayed reveal
            snapshot = await _snapshot(page)
            assert not snapshot["visible"] and snapshot["seen"] is None, snapshot
            assert snapshot["reducedClass"] is (appearance["motion"] == "off")
            assert errors == [], errors
        finally:
            await context.close()


async def _assert_fast_application_does_not_flash(browser: object, base: str) -> None:
    context, page, errors = await _new_page(
        browser,
        base,
        canonical={"theme": "noir", "motion": "on"},
        ready_on_dom_content_loaded=True,
    )
    try:
        await _wait_for_state(page, "ready")
        await page.wait_for_timeout(180)
        snapshot = await _snapshot(page)
        assert not snapshot["visible"], snapshot
        assert errors == [], errors
    finally:
        await context.close()


async def _assert_short_landscape_keeps_brand_in_view(browser: object, base: str) -> None:
    for viewport in ({"width": 667, "height": 375}, {"width": 320, "height": 240}):
        context, page, errors = await _new_page(
            browser,
            base,
            canonical={"theme": "noir", "motion": "on"},
            viewport=viewport,
        )
        try:
            await _wait_for_state(page, "pending")
            bounds = await page.evaluate(
                """() => {
                  const project = element => {
                    const rect = element.getBoundingClientRect();
                    return {
                      top: rect.top, right: rect.right,
                      bottom: rect.bottom, left: rect.left
                    };
                  };
                  return {
                    width: innerWidth, height: innerHeight,
                    stage: project(document.querySelector('.kira-preloader__stage')),
                    caption: project(document.querySelector('.kira-preloader__caption'))
                  };
                }"""
            )
            for name in ("stage", "caption"):
                rect = bounds[name]
                assert rect["top"] >= -0.5 and rect["left"] >= -0.5, (viewport, bounds)
                assert rect["right"] <= bounds["width"] + 0.5, (viewport, bounds)
                assert rect["bottom"] <= bounds["height"] + 0.5, (viewport, bounds)
            assert errors == [], errors
        finally:
            await context.close()


async def _assert_missing_component_css_stays_hidden(browser: object, base: str) -> None:
    context = await browser.new_context(viewport={"width": 1024, "height": 768})
    await context.add_init_script(
        _seed_script(canonical={"theme": "noir", "motion": "on"})
    )
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.route(
        "**/static/kira-preloader.css",
        lambda route: route.fulfill(status=200, content_type="text/css", body=""),
    )
    try:
        await page.goto(f"{base}/{_HARNESS_NAME}", wait_until="domcontentloaded")
        await _wait_for_state(page, "failed-open", timeout_ms=1_000)
        snapshot = await _snapshot(page)
        assert snapshot["hidden"] and not snapshot["visible"], snapshot
        assert not snapshot["hitTargetIsLoader"], snapshot
        assert errors == [], errors
    finally:
        await context.close()


async def _assert_missing_ready_event_fails_open(browser: object, base: str) -> None:
    context, page, errors = await _new_page(
        browser, base, canonical={"theme": "noir", "motion": "on"}
    )
    try:
        await _wait_for_state(page, "pending")
        started = await page.evaluate("performance.now()")
        await _wait_for_state(page, "failed-open", timeout_ms=3_800)
        elapsed_ms = float(await page.evaluate("performance.now()")) - float(started)
        await _wait_until_hidden(page)
        snapshot = await _snapshot(page)
        assert 2_300 <= elapsed_ms <= 3_500, elapsed_ms
        assert not snapshot["visible"], snapshot
        assert errors == [], errors
    finally:
        await context.close()


async def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="kira-startup-dod-"))
    try:
        shutil.copytree(STATIC_DIR, work / "static")
        (work / _HARNESS_NAME).write_text(_shell_without_application(), encoding="utf-8")
        port = _free_port()
        handler = functools.partial(_QuietHandler, directory=str(work))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                browser = await asyncio.wait_for(playwright.chromium.launch(), timeout=60)
                try:
                    base = f"http://127.0.0.1:{port}"
                    checks = [
                        (
                            "themes + ready dismissal + once-per-tab",
                            _assert_theme_and_ready_dismissal,
                        ),
                        ("exact legacy fallback", _assert_near_miss_legacy_key_is_ignored),
                        ("reduced motion", _assert_reduced_motion_never_arms),
                        ("fast startup", _assert_fast_application_does_not_flash),
                        ("short landscape", _assert_short_landscape_keeps_brand_in_view),
                        ("missing component CSS", _assert_missing_component_css_stays_hidden),
                        ("3-second fail-open", _assert_missing_ready_event_fails_open),
                    ]
                    for label, check in checks:
                        print(f"startup-dod: {label}", flush=True)
                        await asyncio.wait_for(check(browser, base), timeout=15)
                finally:
                    await browser.close()
        finally:
            server.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("GREEN: Kira startup is theme-correct, motion-safe, once-per-tab, and fail-open")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
