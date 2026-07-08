"""Screenshot definition-of-done harness (Phase 11 R4) — a standalone dev tool, NOT a pytest
test (its filename doesn't match ``test_*`` so pytest never collects it).

It drives a real headless browser (the ``browser`` extra) against an ALREADY-RUNNING workstation
UI: it exchanges the one-shot launch ``?token=`` for the httponly session cookie, then for each
screen × theme × viewport it saves a PNG and runs the no-overlap / no-horizontal-scroll check.
The pure machinery (viewport matrix, filename, overlap analysis) lives in
``jarvis.ui.screenshots`` and is unit-tested keyless; this file is only the Playwright glue.

Usage (after `uv sync --extra browser` and `uv run playwright install chromium`, with the UI
running so you have the tokened URL it printed):

    uv run python tests/ui/capture.py --base http://127.0.0.1:8787 --token <LAUNCH_TOKEN> \\
        --screen daily:daily:populated --screen projects:projects:populated

Each --screen is ``hash:screen:state`` (the URL hash to visit, and the screen/state labels for
the filename). Exits non-zero if any viewport shows a layout violation.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from jarvis.ui.screenshots import (
    OVERLAP_PROBE_JS,
    THEMES,
    VIEWPORTS,
    analyze_overlap,
    screenshot_name,
)

# localStorage key the appearance layer (theme.js, Phase 11 T5) reads on load — a JSON blob
# {theme, density, layout, motion, accent}. The harness writes {"theme": <name>} before first
# paint so initTheme() applies the requested theme (merged over the defaults).
_THEME_STORAGE_KEY = "kairo:appearance"


def _parse_screens(specs: list[str]) -> list[tuple[str, str, str]]:
    """``hash:screen:state`` → (hash, screen, state). ``hash`` may be empty for the root."""
    out: list[tuple[str, str, str]] = []
    for spec in specs:
        parts = spec.split(":")
        if len(parts) != 3:
            raise SystemExit(f"--screen must be hash:screen:state, got {spec!r}")
        out.append((parts[0], parts[1], parts[2]))
    return out


async def _run(base: str, token: str, out_dir: Path, screens: list[tuple[str, str, str]],
               themes: list[str]) -> int:
    from playwright.async_api import async_playwright  # lazy: only when actually run

    problems: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context()
            # Exchange the one-shot launch token for the httponly session cookie (a 303 to "/").
            login = await context.new_page()
            await login.goto(f"{base}/?token={token}", wait_until="load")
            await login.close()

            for theme in themes:
                for width, height in VIEWPORTS:
                    page = await context.new_page()
                    await page.set_viewport_size({"width": width, "height": height})
                    # Force the theme before first paint (theme.js reads this on load).
                    await page.add_init_script(
                        f"try{{localStorage.setItem('{_THEME_STORAGE_KEY}',"
                        f"JSON.stringify({{theme:'{theme}'}}));}}catch(e){{}}"
                    )
                    for hash_, screen, state in screens:
                        target = f"{base}/#{hash_}" if hash_ else f"{base}/"
                        # A fragment change alone is JS-driven and networkidle won't wait for the
                        # render it triggers; reload for a FRESH load at this hash so app init ->
                        # navigate -> render runs and networkidle waits for its fetches.
                        await page.goto(target, wait_until="load")
                        await page.reload(wait_until="networkidle")
                        await page.wait_for_timeout(500)  # let debounced/async fills settle
                        name = screenshot_name(screen, state, theme, width)
                        await page.screenshot(path=str(out_dir / name), full_page=True)
                        metrics = await page.evaluate(OVERLAP_PROBE_JS)
                        for v in analyze_overlap(metrics):
                            problems.append(f"[{theme} {width}w {screen}/{state}] {v}")
                    await page.close()
        finally:
            await browser.close()

    print(f"captured {len(screens) * len(themes) * len(VIEWPORTS)} shots -> {out_dir}")
    if problems:
        print(f"\n{len(problems)} layout violation(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("no layout violations (no horizontal overflow, no clipped elements)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Kairo screenshot DoD harness (browser extra).")
    ap.add_argument("--base", default="http://127.0.0.1:8787", help="running UI base URL")
    ap.add_argument("--token", required=True, help="the one-shot launch token")
    ap.add_argument("--out", default="data/screenshots", help="output dir (gitignored)")
    ap.add_argument("--screen", action="append", default=[], metavar="HASH:SCREEN:STATE",
                    help="a screen to capture; repeatable")
    ap.add_argument("--themes", default=",".join(THEMES), help="comma-separated theme list")
    args = ap.parse_args()
    screens = _parse_screens(args.screen) or [("", "daily", "default")]
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_run(args.base, args.token, out, screens, themes))


if __name__ == "__main__":
    raise SystemExit(main())
