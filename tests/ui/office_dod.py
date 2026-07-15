"""AI Team Office screenshot definition-of-done (Phase 14 M4) — a standalone dev tool, NOT a pytest
test (its filename doesn't match ``test_*`` so pytest never collects it), like ``capture.py``.

Unlike ``capture.py`` (which drives an already-running, authed app), this is SELF-CONTAINED: it
seeds ``office_overview`` JSON in-process for empty / populated / large projects, serves a COPY of
the static dir plus a tiny harness that runs the REAL ``screens/workspace/office.js`` + kira.css in
headless chromium, then screenshots and runs the same ``analyze_overlap`` (no element overflow / no
horizontal scroll) the workstation DoD uses — across the Office's states x themes x viewports.
No running server, no auth, no DB serving needed.

Usage (after ``uv sync --extra browser`` and ``uv run playwright install chromium``)::

    uv run python tests/ui/office_dod.py

Exits non-zero on any layout violation; PNGs land in ``data/screenshots/office`` (gitignored).
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import jarvis.core  # noqa: F401 - load core first (ledger<->core.context import cycle in isolation)
from jarvis.agents import AgentRunStore
from jarvis.config import load_config
from jarvis.orchestration import OrchestrationStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectService, ProjectStore
from jarvis.ui.readmodels import UiServices, office_overview
from jarvis.ui.screenshots import (
    OVERLAP_PROBE_JS,
    THEMES,
    VIEWPORTS,
    analyze_overlap,
    screenshot_name,
)
from jarvis.ui.server import STATIC_DIR

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "screenshots" / "office"  # gitignored

# (filename label, data state to fetch, office view mode)
STATES = [
    ("compact-populated", "populated", "compact"),
    ("office-populated", "populated", "office"),
    ("empty", "empty", "compact"),
    ("large", "large", "office"),
]

# The Office normally lives inside <main> of the app's body grid; isolated here, so reset the app's
# body (100vh grid + overflow:hidden) to normal flow — full_page + overflow measuring then reflect
# the Office's OWN layout. Theme is applied from localStorage (this harness has no theme.js).
HARNESS = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="./kira.css">
<style>html,body{height:auto!important;overflow:visible!important;display:block!important}
#root{padding:16px}</style></head><body>
<div id="root"></div>
<script type="module">
try { const a = JSON.parse(localStorage.getItem('kira:appearance')||'{}');
      if (a.theme) document.documentElement.dataset.theme = a.theme; } catch(e){}
const q = new URLSearchParams(location.search);
const data = await (await fetch('./__office_'+(q.get('state')||'populated')+'.json')).json();
const api = { get: async () => data, post: async () => ({ok:true, status:200, data:{}}) };
const { render } = await import('./screens/workspace/office.js');
await render(document.getElementById('root'), api, { projectId: 1 });
window.__READY__ = true;
</script></body></html>"""


async def _seed_json(static_dir: Path) -> None:
    """Seed 3 projects (empty/populated/large); dump each project's office_overview as JSON."""
    tmp = Path(tempfile.mkdtemp(prefix="office-dod-db-"))
    cfg = load_config(root=tmp, env_file=None)
    db = await connect(tmp / "dod.db")
    lock = asyncio.Lock()
    pstore = ProjectStore(db, lock)
    for _ in range(3):
        await pstore.create(name="P")  # ids 1 (empty), 2 (populated), 3 (large)
    store, run_store = OrchestrationStore(db, lock), AgentRunStore(db, lock)
    svc = UiServices(orchestration=store, run_store=run_store, projects=ProjectService(pstore))

    async def seed_run(pid, title, team, workflow, members):
        rid = await store.begin_run(
            project_id=pid, workflow=workflow, title=title, config={"team": team},
            context_manifest=[], estimated_cost_usd=0.4, budget_usd=2.0,
        )
        for role, mstage, ok in members:
            mid = await run_store.begin_run(
                parent_session_id=None, parent_trace_id=None, title=f"{team}:{role}",
                prompt="seed", tools_scope=["read_file"], project_id=pid,
                orchestration_run_id=rid, role=role, stage=mstage,
            )
            await run_store.complete_run(mid, status="ok" if ok else "error", result_text="seed")
        return rid

    dumps = {}
    try:
        await seed_run(2, "Security · review", "security", "security_review",
                       [("security", "council", True), ("utility", "council", True),
                        ("security", "review", True)])
        for i in range(24):  # stress recent_runs + feed
            await seed_run(3, f"Run {i} · refactor", "backend", "review_diff",
                           [("reviewer", "review", True), ("coder", "execution", i % 2 == 0)])
        for label, pid in (("empty", 1), ("populated", 2), ("large", 3)):
            dumps[label] = await office_overview(cfg, svc, pid)
    finally:
        await db.close()  # ALWAYS close — a live aiosqlite worker thread would hang process exit
    for label, data in dumps.items():
        (static_dir / f"__office_{label}.json").write_text(json.dumps(data), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _capture(base: str, out: Path) -> int:
    from playwright.async_api import async_playwright  # lazy: only when actually run

    problems: list[str] = []
    shots = 0
    async with async_playwright() as pw:
        browser = await asyncio.wait_for(pw.chromium.launch(), timeout=60)
        try:
            for theme in THEMES:
                for width, height in VIEWPORTS:
                    for label, dstate, mode in STATES:
                        print(f"  shot {shots + 1}/{len(THEMES) * len(VIEWPORTS) * len(STATES)}: "
                              f"{theme} {width}w {label}", flush=True)
                        ctx = await browser.new_context(viewport={"width": width, "height": height})
                        # theme/mode are trusted constants; concatenation dodges brace/percent
                        # clashes with the JS object literals in the script.
                        await ctx.add_init_script(
                            "try{localStorage.setItem('kira:appearance',JSON.stringify({theme:'"
                            + theme + "'}));localStorage.setItem('kairo:office:1',JSON.stringify("
                            "{mode:'" + mode + "'}));}catch(e){}"
                        )
                        page = await ctx.new_page()
                        local_resource_failures: list[str] = []
                        page.on(
                            "response",
                            lambda response, failures=local_resource_failures: failures.append(
                                f"HTTP {response.status}: {response.url}"
                            )
                            if response.status >= 400
                            else None,
                        )
                        page.on(
                            "requestfailed",
                            lambda request, failures=local_resource_failures: failures.append(
                                f"request failed: {request.url}"
                            ),
                        )
                        await page.goto(f"{base}/__dod.html?state={dstate}", wait_until="load")
                        try:
                            await page.wait_for_function("window.__READY__ === true", timeout=5000)
                        except Exception:
                            problems.append(f"[{theme} {width}w {label}] office did not render")
                        if not await page.evaluate("localStorage.getItem('kira:office:1')"):
                            problems.append(
                                f"[{theme} {width}w {label}] legacy layout not migrated"
                            )
                        applied_layout = await page.evaluate(
                            "expected => ({ stored: JSON.parse(localStorage.getItem("
                            "'kira:office:1') || '{}').mode, "
                            "root: document.querySelector('.office')?.classList.contains("
                            "expected === 'office' ? 'office-full' : 'office-compact'), "
                            "pressed: document.querySelector("
                            "`.mode-btn[data-mode='${expected}']`)?.getAttribute("
                            "'aria-pressed') })",
                            mode,
                        )
                        if applied_layout != {"stored": mode, "root": True, "pressed": "true"}:
                            problems.append(
                                f"[{theme} {width}w {label}] legacy layout not applied: "
                                f"{applied_layout!r}"
                            )
                        await page.wait_for_timeout(150)
                        await page.screenshot(
                            path=str(out / screenshot_name("office", label, theme, width)),
                            full_page=True,
                        )
                        shots += 1
                        for v in analyze_overlap(await page.evaluate(OVERLAP_PROBE_JS)):
                            problems.append(f"[{theme} {width}w {label}] {v}")
                        for failure in local_resource_failures:
                            problems.append(
                                f"[{theme} {width}w {label}] local resource {failure}"
                            )
                        await ctx.close()
        finally:
            await browser.close()
    print(f"\ncaptured {shots} office shots -> {out}")
    if problems:
        print(f"{len(problems)} layout violation(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"GREEN: no layout violations across states x themes x viewports "
          f"({len(STATES)} x {len(THEMES)} x {len(VIEWPORTS)})")
    return 0


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="office-dod-"))
    try:
        static = tmp / "static"
        shutil.copytree(STATIC_DIR, static)
        # Production CSS uses root-absolute ``/static/...`` asset URLs. Mirror the production
        # mount so screenshots cannot silently omit theme backgrounds while still passing layout.
        shutil.copytree(STATIC_DIR, static / "static")
        (static / "__dod.html").write_text(HARNESS, encoding="utf-8")
        await _seed_json(static)
        port = _free_port()
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(static))
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            return await _capture(f"http://127.0.0.1:{port}", OUT)
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main()))
