"""Workstation screenshot definition-of-done (Phase 15.5 Task 9) — a standalone dev tool, NOT a
pytest test (its name doesn't match ``test_*``), like ``office_dod.py`` / ``graph_dod.py``.

Unlike the per-screen DoDs, this boots the WHOLE shell: it serves a COPY of the static dir + a
harness that stubs ``fetch`` (returns per-state seeded JSON) and ``WebSocket`` (no server socket),
sets the theme + hash, and imports the REAL ``app.js`` — so the rail, status bar, conversation
header, Daily hero + dashboard, palette, Hub, graph, and voice controls all render exactly as
shipped. Then ``analyze_overlap`` (no element past the viewport, no horizontal scroll) across
9 states × noir/light/neon × 1440/1024/390 = 81 shots.

Usage (after ``uv sync --extra browser`` + ``uv run playwright install chromium``)::

    uv run python tests/ui/workbench_dod.py

Exits non-zero on any layout violation; PNGs land under ``data/screenshots/workbench`` (gitignored).
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

from jarvis.ui.screenshots import (
    OVERLAP_PROBE_JS,
    THEMES,
    VIEWPORTS,
    analyze_overlap,
    screenshot_name,
)
from jarvis.ui.server import STATIC_DIR

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "screenshots" / "workbench"

_MODELS = {
    "current": "claude-opus-4-8",
    "models": [
        {"id": "claude-fable-5", "label": "Fable 5", "provider": "anthropic", "selectable": True,
         "current": False, "reason": ""},
        {"id": "claude-opus-4-8", "label": "Opus 4.8", "provider": "anthropic", "selectable": True,
         "current": True, "reason": ""},
        {"id": "claude-sonnet-5", "label": "Sonnet 5", "provider": "anthropic", "selectable": True,
         "current": False, "reason": ""},
    ],
    "external": [
        {"id": "openai", "label": "gpt-5.2", "provider": "openai", "selectable": False,
         "current": False, "state": "missing_credentials",
         "reason": "receives your private conversation context — not enabled for the main chat "
                   "(missing_credentials)"},
        {"id": "gemini", "label": "gemini-3.5-flash", "provider": "gemini", "selectable": False,
         "current": False, "state": "disabled", "reason": "not enabled for the main chat"},
    ],
}
_CAPS = {
    "connectors": [
        {"name": "Google Calendar", "state": "connected", "exposed_to_chat": True, "reason": ""},
        {"name": "Gmail", "state": "connected", "exposed_to_chat": True, "reason": ""},
        {"name": "Google Drive", "state": "needs_reconnect", "exposed_to_chat": False,
         "reason": "Google sign-in expired — reconnect in the Hub."},
        {"name": "Telegram", "state": "connected", "exposed_to_chat": False,
         "reason": "Delivers notifications; not a chat tool."},
        {"name": "Kakao", "state": "not_configured", "exposed_to_chat": False,
         "reason": "Add Kakao in settings to receive notifications."},
    ],
    "providers": [
        {"name": "Anthropic", "state": "available", "exposed_to_chat": True, "reason": ""},
        {"name": "openai", "state": "missing_credentials", "exposed_to_chat": False,
         "reason": "Not enabled for the main chat (would receive private context)."},
    ],
    "services": [
        {"name": "firecrawl", "state": "available", "exposed_to_chat": True, "reason": ""},
        {"name": "exa", "state": "disabled", "exposed_to_chat": False,
         "reason": "Service disabled."},
    ],
    "voice": {"state": "off", "exposed_to_chat": False, "reason": "Voice is off — enable it."},
    "mcp": {"state": "not_configured", "exposed_to_chat": False, "reason": "No MCP client yet."},
    "summary": "Google Calendar, Gmail · 1 service · voice off",
}
_DIGEST = {
    "summary": "3 events today, 5 unread emails, 2 tasks due.",
    "sections": [{"title": "Schedule", "status": "ok", "items": [1, 2, 3]},
                 {"title": "Email", "status": "ok", "items": [1, 2, 3, 4, 5]}],
    "suggested_actions": ["Reply to the design thread", "Confirm the 3pm meeting"],
}
_ARTIFACTS = [
    {"id": 1, "title": "Security review report", "kind": "orchestration", "pinned": True,
     "has_content": False, "created_at": "2026-07-08T10:00:00+00:00"},
    {"id": 2, "title": "Weekly digest", "kind": "digest", "has_content": True,
     "created_at": "2026-07-09T08:00:00+00:00"},
]
_RUN = {"title": "Security · review", "workflow": "security_review", "team": "security",
        "status": "ok", "actual_cost_usd": 0.42, "finished_at": "2026-07-08T10:00:00+00:00"}


def _base() -> dict:
    return {
        "_hash": "chat",
        "/api/runner": {
            "runner_running": True, "turn_busy": False, "mode": "approval",
            "project": None, "today_spend_usd": 0.42, "ledger_degraded": False,
            "pending_approvals": 0, "session_id": None, "session_title": None,
            "model": "claude-opus-4-8", "effort": "high",
        },
        "/api/voice/status": {"enabled": False, "listening": "idle", "reason": "Voice is off.",
                              "stt": "local", "tts": "local", "playback": False},
        "/api/notices": {"notices": []},
        "/api/tasks": [],
        "/api/models": _MODELS,
        "/api/capabilities": _CAPS,
        "/api/projects": {"projects": [{"id": 1, "name": "Kairo"}, {"id": 2, "name": "Website"}],
                          "active_project_id": None},
        "/api/sessions": {"sessions": [
            {"id": 5, "title": "Design review", "updated_at": "2026-07-09T09:00:00+00:00",
             "pinned": False},
            {"id": 4, "title": "Debugging the parser", "updated_at": "2026-07-08T14:00:00+00:00",
             "pinned": True}]},
        "/api/daily": {
            "digest": _DIGEST, "recent_artifacts": _ARTIFACTS, "latest_run": _RUN,
            "repos": [], "evals": {"ever_run": True, "stale": False, "verdict": "PASS"},
            "kb_review_count": 0, "demo": False, "capabilities": _CAPS,
        },
        "/api/hub": {
            "providers": {"anthropic": True, "voyage": True, "openai": False},
            "egress": {"audio_bytes": 0, "text_chars": 0}, "capabilities": _CAPS,
            "mcp": {"connected": False, "note": "not connected — future phase"},
        },
        "/api/graph/search": {"results": []},
        "_default": {},
    }


def _seed_for(state: str) -> dict:
    s = _base()
    r = s["/api/runner"]
    if state == "daily-empty":
        s["_hash"] = "daily"
        s["/api/daily"] = {"digest": None, "recent_artifacts": [], "latest_run": None,
                           "repos": [], "evals": {"ever_run": False}, "demo": False,
                           "capabilities": _CAPS}
        s["/api/sessions"] = {"sessions": []}
    elif state == "chat-fresh":
        s["_hash"] = "chat"
    elif state == "chat-project":
        s["_hash"] = "chat"
        r["project"] = {"id": 1, "name": "Kairo"}
        r["session_id"] = 5
        r["session_title"] = "Design review"
        s["/api/projects"]["active_project_id"] = 1
        s["/api/sessions/5"] = {"messages": [
            {"role": "user", "text": "Summarize the security review findings."},
            {"role": "assistant", "text": "Three findings: a hardcoded token, TLS verification "
                                          "disabled, and a credential-shaped literal. Details on "
                                          "screen; none exfiltrated."}]}
    elif state == "model-selector":
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
        s["_trigger"] = "model"
    elif state == "palette":
        s["_trigger"] = "palette"
    elif state == "hub-truth":
        s["_hash"] = "hub"
    elif state == "graph-discovery":
        r["project"] = {"id": 1, "name": "Kairo"}
        s["/api/projects"]["active_project_id"] = 1
        s["_hash"] = "workspace/1/graph"
        s["/api/workspace/1/graph"] = {"nodes": [], "edges": [], "counts": {"by_kind": {}},
                                       "focus": "project:1", "project_id": 1}
        s["/api/workspace/1"] = {"project": {"id": 1, "name": "Kairo"}}
    elif state == "voice":
        s["/api/voice/status"] = {"enabled": True, "listening": "idle", "reason": "",
                                  "stt": "openai", "tts": "openai", "playback": True}
        s["/api/capabilities"] = {**_CAPS, "voice": {"state": "on", "exposed_to_chat": True,
                                                     "reason": ""}}
    return s


STATES = ["daily-empty", "daily-populated", "chat-fresh", "chat-project", "model-selector",
          "palette", "hub-truth", "graph-discovery", "voice"]

HARNESS = """<!doctype html><html lang="en" data-theme="noir" data-density="comfortable"
data-layout="focused"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/kairo.css"></head>
<body>%BODY%
<script>
(function () {
  var q = new URLSearchParams(location.search);
  var th = q.get('theme') || 'noir';
  try { localStorage.setItem('kairo:appearance', JSON.stringify({ theme: th })); } catch (e) {}
  window.WebSocket = function () {
    return { readyState: 3, send: function () {}, close: function () {},
             addEventListener: function () {}, set onopen(f) {}, set onmessage(f) {},
             set onclose(f) {}, set onerror(f) {} };
  };
  var real = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    var u = (typeof url === 'string') ? url : url.url;
    if (u.indexOf('__wb_') !== -1 || u.indexOf('/static/') !== -1) return real(url, opts);
    var seed = window.__SEED__ || {};
    var path = u.split('?')[0].replace(location.origin, '');
    var body = (path in seed) ? seed[path] : ('_default' in seed ? seed['_default'] : {});
    return Promise.resolve(new Response(JSON.stringify(body),
      { status: 200, headers: { 'content-type': 'application/json' } }));
  };
})();
</script>
<script type="module">
  var q = new URLSearchParams(location.search);
  var state = q.get('state') || 'daily-populated';
  window.__SEED__ = await (await fetch('./__wb_' + state + '.json')).json();
  location.hash = window.__SEED__._hash || 'chat';
  await import('/static/app.js');
  await new Promise(function (r) { setTimeout(r, 500); });
  var trigger = window.__SEED__._trigger;
  if (trigger === 'palette') {
    var ev = { key: 'k', ctrlKey: true, bubbles: true };
    document.dispatchEvent(new KeyboardEvent('keydown', ev));
    await new Promise(function (r) { setTimeout(r, 300); });
  } else if (trigger === 'model') {
    var sel = document.querySelector('.hdr-model .hdr-select');
    if (sel) sel.focus();
  }
  window.__READY__ = true;
</script></body></html>"""


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
    total = len(THEMES) * len(VIEWPORTS) * len(STATES)
    async with async_playwright() as pw:
        browser = await asyncio.wait_for(pw.chromium.launch(), timeout=60)
        try:
            for theme in THEMES:
                for width, height in VIEWPORTS:
                    for state in STATES:
                        print(f"  shot {shots + 1}/{total}: {theme} {width}w {state}", flush=True)
                        ctx = await browser.new_context(viewport={"width": width, "height": height})
                        page = await ctx.new_page()
                        await page.goto(f"{base}/__wb.html?state={state}&theme={theme}",
                                        wait_until="load")
                        try:
                            await page.wait_for_function("window.__READY__ === true", timeout=8000)
                        except Exception:
                            problems.append(f"[{theme} {width}w {state}] shell not ready")
                        await page.wait_for_timeout(250)
                        await page.screenshot(
                            path=str(out / screenshot_name("workbench", state, theme, width)),
                            full_page=True)
                        shots += 1
                        for v in analyze_overlap(await page.evaluate(OVERLAP_PROBE_JS)):
                            problems.append(f"[{theme} {width}w {state}] {v}")
                        await ctx.close()
        finally:
            await browser.close()
    print(f"\ncaptured {shots} workbench shots -> {out}")
    if problems:
        print(f"{len(problems)} layout violation(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"GREEN: no layout violations across states x themes x viewports "
          f"({len(STATES)} x {len(THEMES)} x {len(VIEWPORTS)})")
    return 0


async def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="workbench-dod-"))
    try:
        static = work / "static"
        shutil.copytree(STATIC_DIR, static / "static")  # served under /static (absolute refs)
        # Body of index.html (rail/status/main/overlay) — reused verbatim so the shell is real.
        # (index.html's header comment itself contains the text "<body>", so split on the LAST
        # occurrence — the real opening tag — not the first.)
        index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        body = index.split("<body>")[-1].rsplit("<script", 1)[0]
        (static / "__wb.html").write_text(HARNESS.replace("%BODY%", body), encoding="utf-8")
        for st in STATES:
            (static / f"__wb_{st}.json").write_text(json.dumps(_seed_for(st)), encoding="utf-8")
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
