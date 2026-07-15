"""Real Playwright driver for the inspect-only browser-QA tool (the ``browser`` extra).

Playwright is imported **lazily** (only inside :meth:`PlaywrightInspectDriver.inspect`), so this
module always imports — with or without the extra. :func:`install_if_available` is what
``run_ui`` calls: it wires the real driver *only when playwright is installed*; otherwise
``PlaywrightInspectTool`` keeps its :class:`~kira.services.playwright_local._NotInstalledDriver`
stub and cleanly errors when invoked (never a crash at startup).

The safety floor is unchanged and lives in the TOOL, not here: ``PlaywrightInspectTool`` enforces
the localhost URL allowlist and the five-verb inspect-only set BEFORE ``inspect`` is ever called
(B3). This driver only dispatches an already-validated (verb, localhost-url) to a headless
browser — no click/type/submit/eval verb exists to reach.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from kira.observability import get_logger
from kira.services.playwright_local import set_driver

log = get_logger("kira.services.playwright_driver")


def playwright_available() -> bool:
    """True iff the ``browser`` extra (playwright) is importable — checked without importing it."""
    return importlib.util.find_spec("playwright") is not None


def _safe_shot_name(url: str) -> str:
    """A filesystem-safe .png name derived from a URL (for the ad-hoc screenshot verb; the DoD
    harness names shots itself via kira.ui.screenshots.screenshot_name)."""
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-") or "shot"
    return f"{slug[:80]}.png"


def _summarize_a11y(node: dict | None) -> str:
    if not node:
        return "no accessibility tree"
    count = 0
    stack = [node]
    while stack:
        cur = stack.pop()
        count += 1
        stack.extend(cur.get("children", []) or [])
    return f"{count} nodes, root role={node.get('role')!r} name={node.get('name')!r}"


class PlaywrightInspectDriver:
    """Drives a headless Chromium for the five inspect verbs. Implements the
    :class:`~kira.services.playwright_local.InspectDriver` protocol."""

    def __init__(self, *, screenshot_dir: Path | None = None) -> None:
        self._shot_dir = screenshot_dir

    async def inspect(self, verb: str, url: str, selector: str) -> str:
        # Lazy import: only reached when the tool actually invokes a validated verb+localhost-url.
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page()
                resp = await page.goto(url, wait_until="load")
                status = resp.status if resp else None
                if verb == "navigate":
                    title = await page.title()
                    return f"navigate ok: {page.url} (status={status}, title={title!r})"
                if verb == "screenshot":
                    png = await page.screenshot(full_page=True)
                    if self._shot_dir is not None:
                        self._shot_dir.mkdir(parents=True, exist_ok=True)
                        dest = self._shot_dir / _safe_shot_name(url)
                        dest.write_bytes(png)
                        return f"screenshot: {len(png)} bytes -> {dest}"
                    return f"screenshot: {len(png)} bytes (not saved; no screenshot_dir)"
                if verb == "dom_inspect":
                    sel = selector or "body"
                    el = await page.query_selector(sel)
                    if el is None:
                        return f"dom_inspect: no element matches {sel!r}"
                    return f"dom_inspect {sel!r}: {(await el.inner_html())[:2000]}"
                if verb == "a11y_check":
                    return f"a11y_check: {_summarize_a11y(await page.accessibility.snapshot())}"
                if verb == "visual_diff":
                    png = await page.screenshot(full_page=True)
                    return f"visual_diff: rendered {len(png)} bytes (no baseline configured yet)"
                return f"unsupported verb {verb!r}"  # unreachable — the tool validates first
            finally:
                await browser.close()


def install_if_available(*, screenshot_dir: Path | None = None) -> bool:
    """Wire the real Playwright driver iff the ``browser`` extra is installed. Returns True when
    wired, False when the extra is absent (the tool keeps its degrading stub). Safe to call at
    UI startup regardless of whether playwright is present."""
    if not playwright_available():
        return False
    set_driver(PlaywrightInspectDriver(screenshot_dir=screenshot_dir))
    log.info("playwright_driver_wired")
    return True
