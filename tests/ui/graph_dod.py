"""Memory-Graph screenshot definition-of-done (Phase 15 Task 8) — a standalone dev tool, NOT a
pytest test (its name doesn't match ``test_*``), like ``office_dod.py``.

Self-contained: seeds subgraph JSON in-process for the graph states, serves a COPY of the static dir
+ a harness that runs the REAL ``screens/workspace/graph.js`` (+ the canvas engine + kira.css) in
headless chromium under REDUCED MOTION (so the deterministic layout settles and draws once — stable
pixels), then screenshots and runs ``analyze_overlap`` (no element overflow / no horizontal scroll)
across states x themes x viewports.

Usage (after ``uv sync --extra browser`` + ``uv run playwright install chromium``)::

    uv run python tests/ui/graph_dod.py

Exits non-zero on any layout violation; PNGs land under ``data/screenshots/graph`` (gitignored).
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
from jarvis.graph import GraphStore
from jarvis.graph.builder import rebuild
from jarvis.graph.service import dependency_subgraph, subgraph
from jarvis.orchestration import OrchestrationStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore
from jarvis.ui.screenshots import (
    OVERLAP_PROBE_JS,
    THEMES,
    VIEWPORTS,
    analyze_overlap,
    screenshot_name,
)
from jarvis.ui.server import STATIC_DIR

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "screenshots" / "graph"
STATES = ["focus", "expanded", "filtered", "code-map", "empty"]
_TS = "2026-03-15T00:00:00+00:00"

HARNESS = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="./kira.css">
<style>html,body{height:auto!important;overflow:visible!important;display:block!important}
#root{padding:16px}</style></head><body>
<div id="root"></div>
<script type="module">
try { const a = JSON.parse(localStorage.getItem('kira:appearance')||'{}');
  if (a.theme) document.documentElement.dataset.theme = a.theme;
  document.documentElement.classList.add('reduce-motion'); } catch(e){}
const q = new URLSearchParams(location.search);
const data = await (await fetch('./__graph_'+(q.get('state')||'focus')+'.json')).json();
if (q.get('state') === 'code-map') {
  localStorage.setItem('kairo:graph:v4:1:graph-dod', JSON.stringify({view:'dependencies',depth:6}));
}
const api = { get: async (url) => (url.includes('/node/') ? {} : data), post: async () => ({}) };
const { render } = await import('./screens/workspace/graph.js');
await render(document.getElementById('root'), api,
  { projectId: 1, project: {id:1, created_at:'graph-dod'} });
if (q.get('state') === 'code-map' && !localStorage.getItem('kira:graph:v4:1:graph-dod')) {
  throw new Error('legacy graph state was not migrated');
}
window.__READY__ = true;
</script></body></html>"""


async def _seed_json(static_dir: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="graph-dod-db-"))
    db = await connect(tmp / "g.db")
    lock = asyncio.Lock()
    ps = ProjectStore(db, lock)
    await ps.create(name="P")  # 1 (populated)
    await ps.create(name="Empty")  # 2 (no edges)
    orch, runs = OrchestrationStore(db, lock), AgentRunStore(db, lock)
    rid = await orch.begin_run(project_id=1, workflow="security_review", title="Security review",
                               config={"team": "security"}, context_manifest=[],
                               estimated_cost_usd=0.1, budget_usd=1.0)
    for role in ("security", "utility", "security"):
        await runs.begin_run(parent_session_id=None, parent_trace_id=None, title=f"security:{role}",
                             prompt="p", tools_scope=["read_file"], project_id=1,
                             orchestration_run_id=rid, role=role, stage="council")
    await db.execute("INSERT INTO sessions (created_at, updated_at, title, kind, project_id) "
                     "VALUES (?, ?, 'Design chat', 'interactive', 1)", (_TS, _TS))
    await db.execute(
        "INSERT INTO kb_sources (kind, origin, content_hash, raw_path, markdown_path, "
        "markdown_hash, converter, converter_version, byte_size, status, review_status, "
        "created_by, created_at, updated_at, project_id) VALUES "
        "('url','http://x','h','r','m','mh','trafilatura','1',10,'live','unreviewed','agent',?,?,1)",
        (_TS, _TS))
    code_sources: list[tuple[int, str, str]] = []
    seed_code = [
        ("repo/src/atlas/app.py", "from .core import runner\n"),
        ("repo/src/atlas/core.py", "from .ui import render\n"),
        ("repo/src/atlas/ui.py", "def render(): pass\n"),
        ("repo/src/atlas/connectors/mail.py", "from atlas.core import runner\n"),
    ]
    # Keep the visual harness honest for the real use case: a deep uploaded codebase must draw
    # as a dense, inspectable network rather than only proving a four-node demo. These are local
    # relative imports in a deterministic ring, so graph derivation remains source-only and does
    # not execute anything.
    module_count = 120
    seed_code.extend(
        (
            f"repo/src/atlas/modules/module_{index:03}.py",
            f"from .module_{(index + 1) % module_count:03} import run\n",
        )
        for index in range(module_count)
    )
    for title, text in seed_code:
        cur = await db.execute(
            "INSERT INTO kb_sources (kind, origin, title, content_hash, raw_path, markdown_path, "
            "markdown_hash, converter, converter_version, byte_size, status, review_status, "
            "created_by, created_at, updated_at, project_id) VALUES "
            "('file', ?, ?, ?, 'r', 'm', 'mh', 'passthrough', '1', 10, 'live', 'reviewed', "
            "'user', ?, ?, 1)",
            (f"chat-upload:1:{title}", title, f"hash-{title}", _TS, _TS),
        )
        assert cur.lastrowid is not None
        code_sources.append((cur.lastrowid, title, text))
    for source_id, _title, text in code_sources:
        await db.execute(
            "INSERT INTO kb_chunks (source_id, wiki_path, heading_path, seq, text, embedding, "
            "embedding_model, created_at) VALUES (?, NULL, '', 0, ?, X'00', 'fake', ?)",
            (source_id, text, _TS),
        )
    await db.commit()
    store = GraphStore(db, lock)
    try:
        await rebuild(store)
        dumps = {
            "focus": await subgraph(store, 1, depth=1),
            "expanded": await subgraph(store, 1, depth=2),
            "filtered": await subgraph(store, 1, depth=2, kinds={"source", "run", "team"}),
            "code-map": await dependency_subgraph(store, 1),
            "empty": {"project_id": 2, "focus": "project:2", "nodes": [], "edges": [],
                      "counts": {"by_kind": {}, "by_trust": {}}, "truncated": False},
        }
    finally:
        await db.close()  # ALWAYS close — a live aiosqlite worker thread would hang process exit
    shutil.rmtree(tmp, ignore_errors=True)
    for label, data in dumps.items():
        (static_dir / f"__graph_{label}.json").write_text(json.dumps(data), encoding="utf-8")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _capture(base: str, out: Path) -> int:
    from playwright.async_api import async_playwright

    problems: list[str] = []
    shots = 0
    async with async_playwright() as pw:
        browser = await asyncio.wait_for(pw.chromium.launch(), timeout=60)
        try:
            for theme in THEMES:
                for width, height in VIEWPORTS:
                    for state in STATES:
                        total = len(THEMES) * len(VIEWPORTS) * len(STATES)
                        print(f"  shot {shots + 1}/{total}: {theme} {width}w {state}", flush=True)
                        ctx = await browser.new_context(viewport={"width": width, "height": height})
                        await ctx.add_init_script(
                            "try{localStorage.setItem('kira:appearance',JSON.stringify({theme:'"
                            + theme + "'}));}catch(e){}")
                        page = await ctx.new_page()
                        await page.goto(f"{base}/__graph_dod.html?state={state}", wait_until="load")
                        try:
                            await page.wait_for_function("window.__READY__ === true", timeout=5000)
                        except Exception:
                            problems.append(f"[{theme} {width}w {state}] graph did not render")
                        if state == "code-map":
                            migrated_view = await page.evaluate(
                                "JSON.parse(localStorage.getItem("
                                "'kira:graph:v4:1:graph-dod') || '{}').view"
                            )
                            code_map_pressed = await page.evaluate(
                                "() => Array.from(document.querySelectorAll("
                                "'.graph-view-toggle button')).find("
                                "button => button.textContent === 'Code map')?.getAttribute("
                                "'aria-pressed')"
                            )
                            if migrated_view != "dependencies" or code_map_pressed != "true":
                                problems.append(
                                    f"[{theme} {width}w {state}] legacy Code map state not "
                                    f"applied: view={migrated_view!r}, pressed={code_map_pressed!r}"
                                )
                        await page.wait_for_timeout(150)
                        await page.screenshot(
                            path=str(out / screenshot_name("graph", state, theme, width)),
                            full_page=True)
                        shots += 1
                        for v in analyze_overlap(await page.evaluate(OVERLAP_PROBE_JS)):
                            problems.append(f"[{theme} {width}w {state}] {v}")
                        await ctx.close()
        finally:
            await browser.close()
    print(f"\ncaptured {shots} graph shots -> {out}")
    if problems:
        print(f"{len(problems)} layout violation(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"GREEN: no layout violations across states x themes x viewports "
          f"({len(STATES)} x {len(THEMES)} x {len(VIEWPORTS)})")
    return 0


async def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="graph-dod-"))
    try:
        static = work / "static"
        shutil.copytree(STATIC_DIR, static)
        # Kira's real stylesheet references local theme imagery under /static/assets.  Mirror
        # that mount inside this otherwise root-served harness so graph captures include the same
        # local background/veil treatment as the workstation rather than noisy 404 fallbacks.
        shutil.copytree(STATIC_DIR, static / "static")
        (static / "__graph_dod.html").write_text(HARNESS, encoding="utf-8")
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
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main()))
