"""Browser-backed DoD for route ownership, stale-read isolation, and screen recovery."""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

from workbench_dod import HARNESS, _free_port, _QuietHandler, _seed_for

from jarvis.ui.server import STATIC_DIR


async def _open_page(browser: object, base: str) -> tuple[object, object, list[str]]:
    context = await browser.new_context(viewport={"width": 1024, "height": 768})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{base}/__wb.html?state=router&theme=noir", wait_until="load")
    await page.wait_for_function("window.__READY__ === true")
    await _handshake(page)
    return context, page, errors


async def _handshake(
    page: object,
    *,
    workspace_id: str = "router-workspace",
    session_id: int = 5,
    project_id: int | None = 1,
    context_revision: int = 1,
) -> None:
    await page.evaluate(
        """({ workspaceId, sessionId, projectId, contextRevision }) => {
          const runner = window.__SEED__['/api/runner'];
          runner.session_id = sessionId;
          runner.context_revision = contextRevision;
          runner.project = projectId == null ? null : {
            id: projectId, name: `Project ${projectId}`
          };
          window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
            type: 'workspace', workspace_id: workspaceId,
            meeting_recording_epoch: 'router-process',
            session_id: sessionId, project_id: projectId, context_revision: contextRevision
          }) });
        }""",
        {
            "workspaceId": workspace_id,
            "sessionId": session_id,
            "projectId": project_id,
            "contextRevision": context_revision,
        },
    )


async def _defer_get(page: object, path: str) -> None:
    await page.evaluate(
        """async path => {
          const { api } = await import('/static/app.js');
          window.__routerOriginalGet = api.get.bind(api);
          window.__routerDeferredPath = path;
          window.__routerRequests = 0;
          window.__routerResolvers = [];
          api.get = (requested, options) => {
            if (requested !== path) return window.__routerOriginalGet(requested);
            window.__routerRequests += 1;
            return new Promise((resolve, reject) => {
              window.__routerResolvers.push({ resolve, reject });
            });
          };
        }""",
        path,
    )


async def _assert_pending_route_is_fail_closed(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _defer_get(page, "/api/memory")
        await page.evaluate("location.hash = 'memory'")
        await page.wait_for_function("window.__routerRequests === 1")

        assert await page.locator("#chat-input").count() == 0
        assert await page.locator("#screen").get_attribute("aria-busy") == "true"
        loading = page.locator('#screen .route-loading[role="status"]')
        assert "Opening Memory" in (await loading.text_content() or "")
        await page.evaluate("location.hash = 'chat'")
        await page.wait_for_selector("#chat-input")
        await page.evaluate(
            "window.__routerResolvers[0].resolve([{ id: 1, content: 'stale memory', "
            "type: 'fact', source: 'old route' }])"
        )
        await page.wait_for_timeout(100)
        assert await page.locator("#chat-input").count() == 1
        assert await page.locator("#mem-tbl").count() == 0
        assert await page.evaluate("location.hash") == "#chat"
        assert errors == []
    finally:
        await context.close()


async def _assert_newest_same_route_read_wins(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await _defer_get(page, "/api/artifacts?limit=200")
        await page.evaluate("location.hash = 'artifacts'")
        await page.wait_for_function("window.__routerRequests === 1")

        # The server sends this same-workspace frame on reconnect. It must start a newer read now,
        # not wait behind a stalled older request and not replace a live Chat draft.
        await _handshake(page)
        await page.wait_for_function("window.__routerRequests === 2")
        await page.evaluate(
            "window.__routerResolvers[1].resolve({ artifacts: [{ id: 2, "
            "title: 'New artifact', kind: 'report', pinned: false, has_content: false }] })"
        )
        await page.wait_for_function("document.body.textContent.includes('New artifact')")
        await page.evaluate(
            "window.__routerResolvers[0].resolve({ artifacts: [{ id: 1, "
            "title: 'Stale artifact', kind: 'report', pinned: false, has_content: false }] })"
        )
        await page.wait_for_timeout(100)
        body = await page.locator("#screen").text_content() or ""
        assert "New artifact" in body, body
        assert "Stale artifact" not in body
        assert await page.locator("#screen .route-loading").count() == 0
        assert errors == []
    finally:
        await context.close()


async def _assert_same_workspace_refresh_preserves_chat_draft(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        composer = page.locator("#chat-input")
        await composer.fill("unsent draft")
        await page.evaluate("window.__SEED__['/api/runner'].mode = 'auto'")
        await _handshake(page)
        await page.wait_for_function(
            "document.querySelector('.hdr-model-menu summary')?.textContent.includes('Auto')"
        )
        assert await composer.input_value() == "unsent draft"
        assert await page.evaluate("document.activeElement?.id") == "chat-input"
        assert errors == []
    finally:
        await context.close()


async def _assert_transport_failure_retries(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """() => {
              window.__SEED__['/api/memory'] = [
                { id: 9, content: 'Recovered preference', type: 'preference', source: 'user' }
              ];
              window.__routerFailMemory = true;
              const originalFetch = window.fetch;
              window.fetch = (url, options) => {
                const value = typeof url === 'string' ? url : url.url;
                const path = value.split('?')[0].replace(location.origin, '');
                if (path === '/api/memory' && window.__routerFailMemory) {
                  return Promise.reject(new TypeError('offline'));
                }
                return originalFetch(url, options);
              };
            }"""
        )
        await page.evaluate("location.hash = 'memory'")
        failure = page.locator('#screen .route-failure[role="alert"]')
        await failure.wait_for()
        text = await failure.text_content() or ""
        assert "Memory couldn't open" in text
        assert "offline" not in text
        assert await page.locator("#screen").get_attribute("aria-busy") == "false"

        await page.evaluate("window.__routerFailMemory = false")
        await failure.get_by_role("button", name="Try again").click()
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('Recovered preference')"
        )
        assert await page.locator("#screen").get_attribute("aria-busy") == "false"
        assert await page.locator('#screen .route-failure[role="alert"]').count() == 0
        assert errors == []
    finally:
        await context.close()


async def _assert_never_settling_initial_render_times_out_and_retries(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalVoiceStatus = api.voiceStatus.bind(api);
              window.__KAIRO_INITIAL_ROUTE_READ_TIMEOUT_MS__ = 60;
              window.__stallInitialVoiceStatus = true;
              window.__initialVoiceStatusReads = 0;
              api.voiceStatus = () => {
                window.__initialVoiceStatusReads += 1;
                if (window.__stallInitialVoiceStatus) return new Promise(() => {});
                return originalVoiceStatus();
              };
              window.__SEED__['/api/voice/status'] = {
                enabled: true, meeting_available: true, meeting_reason: '', stt: 'local',
                meeting: 'idle', meeting_recording: false,
                meeting_recording_epoch: 'router-process', meeting_revision: 1,
                meeting_recording_revision: 1
              };
              location.hash = 'meetings';
            }"""
        )
        failure = page.locator('#screen .route-failure[role="alert"]')
        await failure.wait_for()
        assert "Meetings couldn't open" in (await failure.text_content() or "")
        assert await page.locator("#screen").get_attribute("aria-busy") == "false"
        assert await page.evaluate("window.__initialVoiceStatusReads") == 1

        await page.evaluate("window.__stallInitialVoiceStatus = false")
        await failure.get_by_role("button", name="Try again").click()
        await page.wait_for_function(
            "document.getElementById('mtg-availability')?.textContent.includes('microphone opens')"
        )
        assert await page.evaluate("window.__initialVoiceStatusReads") == 2
        assert await page.locator('#screen .route-failure[role="alert"]').count() == 0
        assert await page.locator("#screen").get_attribute("aria-busy") == "false"
        assert errors == []
    finally:
        await context.close()


async def _assert_unexpected_http_failure_retries(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """() => {
              window.__routerFailMemoryHttp = true;
              const originalFetch = window.fetch;
              window.fetch = (url, options) => {
                const value = typeof url === 'string' ? url : url.url;
                const path = value.split('?')[0].replace(location.origin, '');
                if (path === '/api/memory' && window.__routerFailMemoryHttp) {
                  return Promise.resolve(new Response('{}', {
                    status: 500, headers: { 'content-type': 'application/json' }
                  }));
                }
                return originalFetch(url, options);
              };
            }"""
        )
        await page.evaluate("location.hash = 'memory'")
        failure = page.locator('#screen .route-failure[role="alert"]')
        await failure.wait_for()
        assert "Memory couldn't open" in (await failure.text_content() or "")
        assert "long-term memory off" not in (await failure.text_content() or "")

        await page.evaluate(
            """() => {
              window.__routerFailMemoryHttp = false;
              window.__SEED__['/api/memory'] = [{
                id: 12, content: 'HTTP recovered', type: 'fact', source: 'user'
              }];
            }"""
        )
        await failure.get_by_role("button", name="Try again").click()
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('HTTP recovered')"
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_render_exception_retries(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate("window.__SEED__['/api/memory'] = { length: 1 }")
        await page.evaluate("location.hash = 'memory'")
        failure = page.locator('#screen .route-failure[role="alert"]')
        await failure.wait_for()
        assert "Memory couldn't open" in (await failure.text_content() or "")
        assert "iterable" not in (await failure.text_content() or "")

        await page.evaluate(
            "window.__SEED__['/api/memory'] = [{ id: 10, content: 'Recovered fact', "
            "type: 'fact', source: 'user' }]"
        )
        await failure.get_by_role("button", name="Try again").click()
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('Recovered fact')"
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_partial_cost_failure_keeps_spend_and_retries(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """() => {
              window.__routerFailRoi = true;
              const originalFetch = window.fetch;
              window.fetch = (url, options) => {
                const value = typeof url === 'string' ? url : url.url;
                const path = value.split('?')[0].replace(location.origin, '');
                if (path === '/api/roi' && window.__routerFailRoi) {
                  return Promise.resolve(new Response('{}', {
                    status: 503, headers: { 'content-type': 'application/json' }
                  }));
                }
                return originalFetch(url, options);
              };
            }"""
        )
        await page.evaluate("location.hash = 'costs'")
        partial = page.locator("#screen .route-partial-failure")
        await partial.wait_for()
        text = await page.locator("#screen").text_content() or ""
        assert "Cost Center" in text
        assert "ROI unavailable" in text
        assert await page.locator('#screen .route-failure[role="alert"]').count() == 0

        await page.evaluate("window.__routerFailRoi = false")
        await partial.get_by_role("button", name="Try again").click()
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('Cost Center') "
            "&& !document.querySelector('#screen .route-partial-failure')"
        )
        assert "Cost Center" in (await page.locator("#screen").text_content() or "")
        assert errors == []
    finally:
        await context.close()


async def _assert_optional_gate_failure_keeps_attention(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """() => {
              window.__SEED__['/api/attention'] = { items: [{
                source: 'system', ref: 91, kind: 'warning', priority: 'urgent',
                title: 'Review the live approval'
              }] };
              const originalFetch = window.fetch;
              window.fetch = (url, options) => {
                const value = typeof url === 'string' ? url : url.url;
                const path = value.split('?')[0].replace(location.origin, '');
                if (path === '/api/agents') return Promise.reject(new TypeError('offline'));
                return originalFetch(url, options);
              };
            }"""
        )
        await page.evaluate("location.hash = 'gate'")
        await page.wait_for_function(
            "document.getElementById('screen').textContent"
            ".includes('Delegation history is unavailable')"
        )
        text = await page.locator("#screen").text_content() or ""
        assert "Review the live approval" in text
        assert "Delegation history is unavailable" in text
        assert await page.locator('#screen .route-failure[role="alert"]').count() == 0
        assert errors == []
    finally:
        await context.close()


async def _assert_async_refresh_burst_aborts_superseded_reads(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """() => {
              const originalFetch = window.fetch;
              window.__routeFetches = 0;
              window.__routeAborts = 0;
              window.__routePending = 0;
              window.__routeResolvers = [];
              window.fetch = (url, options = {}) => {
                const value = typeof url === 'string' ? url : url.url;
                if (value.split('?')[0].replace(location.origin, '') !== '/api/artifacts') {
                  return originalFetch(url, options);
                }
                window.__routeFetches += 1;
                window.__routePending += 1;
                return new Promise((resolve, reject) => {
                  let settled = false;
                  const finish = callback => {
                    if (settled) return;
                    settled = true;
                    window.__routePending -= 1;
                    callback();
                  };
                  options.signal?.addEventListener('abort', () => finish(() => {
                    window.__routeAborts += 1;
                    reject(new DOMException('superseded', 'AbortError'));
                  }), { once: true });
                  window.__routeResolvers.push(data => finish(() => resolve(new Response(
                    JSON.stringify(data),
                    { status: 200, headers: { 'content-type': 'application/json' } }
                  ))));
                });
              };
            }"""
        )
        await page.evaluate("location.hash = 'artifacts'")
        await page.wait_for_function("window.__routeFetches === 1")
        for _ in range(5):
            await _handshake(page)
        await page.wait_for_function("window.__routeFetches === 6 && window.__routeAborts === 5")
        assert await page.evaluate("window.__routePending") == 1
        await page.evaluate(
            "window.__routeResolvers.at(-1)({ artifacts: [{ id: 77, title: 'Bounded result', "
            "kind: 'report', pinned: false, has_content: false }] })"
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('Bounded result')"
        )
        assert await page.locator("#screen .route-loading").count() == 0
        assert errors == []
    finally:
        await context.close()


async def _assert_latest_header_refresh_wins(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const { refreshHeader } = await import('/static/ui/header.js');
              const originalGet = api.get.bind(api);
              window.__headerResolvers = [];
              window.__headerRequests = 0;
              api.get = (path, options) => {
                if (path !== '/api/projects') return originalGet(path, options);
                window.__headerRequests += 1;
                return new Promise(resolve => window.__headerResolvers.push(resolve));
              };
              window.__headerJobs = [refreshHeader(), refreshHeader()];
            }"""
        )
        await page.wait_for_function("window.__headerRequests === 2")
        await page.evaluate(
            "window.__headerResolvers[1]({ projects: [{ id: 1, name: 'NEW HEADER' }] })"
        )
        await page.wait_for_function(
            "document.querySelector('.hdr-scope')?.textContent.includes('NEW HEADER')"
        )
        await page.evaluate(
            "window.__headerResolvers[0]({ projects: [{ id: 1, name: 'OLD HEADER' }] })"
        )
        await page.evaluate("Promise.all(window.__headerJobs)")
        text = await page.locator(".hdr-scope").text_content() or ""
        assert "NEW HEADER" in text
        assert "OLD HEADER" not in text
        assert errors == []
    finally:
        await context.close()


async def _assert_failed_header_write_reconciles_authoritative_value(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              window.__headerWriteRequests = 0;
              api.post = (path, body) => {
                if (path !== '/api/mode') return originalPost(path, body);
                window.__headerWriteRequests += 1;
                return new Promise(resolve => { window.__headerWriteResolve = resolve; });
              };
            }"""
        )
        await page.locator('.hdr-model-menu summary[aria-label="Model, effort, and mode"]').click()
        mode = page.get_by_label("Mode", exact=True)
        assert await mode.input_value() == "approval"
        await mode.select_option("auto")
        await page.wait_for_function("window.__headerWriteRequests === 1")
        assert await mode.is_disabled()
        assert await mode.get_attribute("aria-busy") == "true"

        # A status refresh rebuilds the header controls in place. The logical mode operation must
        # remain authoritative across that remount, so the replacement is still busy and even a
        # synthetic opposing change cannot submit a second write.
        await page.evaluate(
            """async () => {
              window.__pendingHeaderMode = document.querySelector('select[aria-label="Mode"]');
              const { refreshHeader } = await import('/static/ui/header.js');
              await refreshHeader();
            }"""
        )
        await page.locator('.hdr-model-menu summary[aria-label="Model, effort, and mode"]').click()
        mode = page.get_by_label("Mode", exact=True)
        assert await page.evaluate("window.__pendingHeaderMode.isConnected") is False
        assert await mode.is_disabled()
        assert await mode.get_attribute("aria-busy") == "true"
        await mode.evaluate(
            """control => {
              control.value = 'plan';
              control.dispatchEvent(new Event('change'));
            }"""
        )
        await page.wait_for_timeout(50)
        assert await page.evaluate("window.__headerWriteRequests") == 1

        await page.evaluate(
            "window.__headerWriteResolve({ ok: false, status: 409, "
            "data: { message: 'MODE REJECTED' } })"
        )
        await page.wait_for_function(
            "document.querySelector('select[aria-label=\"Mode\"]')?.value === 'approval'"
        )
        assert await page.get_by_text("MODE REJECTED", exact=True).count() == 1
        assert await page.get_by_label("Mode", exact=True).is_enabled()
        assert await page.evaluate("window.__headerWriteRequests") == 1
        assert errors == []
    finally:
        await context.close()


async def _assert_latest_daily_briefing_read_wins(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page, session_id=6, project_id=None, context_revision=2)
        await _defer_get(page, "/api/daily")
        await page.evaluate("location.hash = 'daily'")
        await page.wait_for_function("window.__routerRequests === 1")
        await page.get_by_role("button", name="Refresh").click()
        await page.wait_for_function("window.__routerRequests === 2")
        await page.evaluate(
            "window.__routerResolvers[1].resolve({ digest: { "
            "summary: 'NEW MANUAL', sections: [] } })"
        )
        await page.wait_for_function(
            "document.getElementById('daily-briefing-body').textContent.includes('NEW MANUAL')"
        )
        await page.evaluate(
            "window.__routerResolvers[0].resolve({ digest: { "
            "summary: 'OLD SCHEDULED', sections: [] } })"
        )
        await page.wait_for_timeout(100)
        text = await page.locator("#daily-briefing-body").text_content() or ""
        assert "NEW MANUAL" in text
        assert "OLD SCHEDULED" not in text
        assert errors == []
    finally:
        await context.close()


async def _assert_runner_read_cannot_cross_workspace_epoch(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalGet = api.get.bind(api);
              window.__runnerResolvers = [];
              window.__runnerSignals = [];
              window.__runnerRequests = 0;
              api.get = (path, options = {}) => {
                if (path !== '/api/runner') return originalGet(path, options);
                window.__runnerRequests += 1;
                window.__runnerSignals.push(options.signal);
                return new Promise(resolve => window.__runnerResolvers.push(resolve));
              };
              window.__oldRunnerJob = api.runnerStatus({ refresh: true });
            }"""
        )
        await page.wait_for_function("window.__runnerRequests === 1")
        await _handshake(page, session_id=9, project_id=2, context_revision=2)
        await page.wait_for_function("window.__runnerRequests === 2")
        assert await page.evaluate("window.__runnerSignals[0].aborted") is True

        await page.evaluate(
            "window.__runnerResolvers[0]({ session_id: 5, project: { id: 1 }, "
            "mode: 'approval', runner_running: true, turn_busy: false })"
        )
        # A superseded caller adopts the replacement request, so it intentionally remains
        # pending here. Give the stale resolution a microtask turn and verify it cannot write.
        await page.wait_for_timeout(50)
        context_after_old = await page.evaluate(
            "(async () => (await import('/static/app.js')).api.state.context)()"
        )
        assert context_after_old == {
            "session_id": 9,
            "project_id": 2,
            "context_revision": 2,
        }, context_after_old

        await page.evaluate(
            "window.__runnerResolvers[1]({ session_id: 9, project: { id: 2, name: 'New' }, "
            "mode: 'auto', runner_running: true, turn_busy: false })"
        )
        await page.evaluate("window.__oldRunnerJob")
        await page.wait_for_function("document.getElementById('st-mode').textContent === 'auto'")
        final_context = await page.evaluate(
            "(async () => (await import('/static/app.js')).api.state.context)()"
        )
        assert final_context == {
            "session_id": 9,
            "project_id": 2,
            "context_revision": 2,
        }
        assert errors == []
    finally:
        await context.close()


async def _assert_runner_refresh_supersession_adopts_newest_read(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalGet = api.get.bind(api);
              window.__supersededRunnerResolvers = [];
              window.__supersededRunnerSignals = [];
              api.get = (path, options = {}) => {
                if (path !== '/api/runner') return originalGet(path, options);
                window.__supersededRunnerSignals.push(options.signal);
                return new Promise(resolve => window.__supersededRunnerResolvers.push(resolve));
              };
              const first = api.runnerStatus({ refresh: true });
              const second = api.runnerStatus({ refresh: true });
              window.__supersededRunnerJobs = Promise.all([first, second]);
            }"""
        )
        await page.wait_for_function("window.__supersededRunnerResolvers.length === 2")
        assert await page.evaluate("window.__supersededRunnerSignals[0].aborted") is True
        await page.evaluate(
            """() => {
              window.__supersededRunnerResolvers[0]({
                session_id: 5, project: { id: 1, name: 'Old' }, context_revision: 1,
                mode: 'approval', runner_running: true, turn_busy: false
              });
              window.__supersededRunnerResolvers[1]({
                session_id: 5, project: { id: 1, name: 'Current' }, context_revision: 1,
                mode: 'auto', runner_running: true, turn_busy: false
              });
            }"""
        )
        results = await page.evaluate("async () => await window.__supersededRunnerJobs")
        assert [result["mode"] for result in results] == ["auto", "auto"]
        assert errors == []
    finally:
        await context.close()


async def _assert_terminal_event_beats_an_older_busy_runner_snapshot(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.wait_for_selector("#chat-input")
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const { refreshHeader } = await import('/static/ui/header.js');
              api.state.runner = {
                ...window.__SEED__['/api/runner'], session_id: 5,
                project: { id: 1, name: 'Project 1' }, context_revision: 1,
                turn_busy: true, turn_id: 'turn-old'
              };
              const originalGet = api.get.bind(api);
              window.__settlementRunnerResolvers = [];
              api.get = (path, options = {}) => path === '/api/runner'
                ? new Promise(resolve => window.__settlementRunnerResolvers.push(resolve))
                : originalGet(path, options);
              window.__settlementHeaderJob = refreshHeader({ refreshRunner: true });
            }"""
        )
        await page.wait_for_function("window.__settlementRunnerResolvers.length === 1")
        await page.evaluate(
            """() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
              kind: 'event', type: 'turn_completed', stop_reason: 'end_turn',
              workspace_id: 'router-workspace', session_id: 5, project_id: 1,
              context_revision: 1
            }) })"""
        )
        await page.wait_for_function("window.__settlementRunnerResolvers.length === 2")
        await page.evaluate(
            """() => {
              window.__settlementRunnerResolvers[0]({
                session_id: 5, project: { id: 1, name: 'Project 1' }, context_revision: 1,
                session_title: 'Stale busy chat', mode: 'approval', runner_running: true,
                turn_busy: true, turn_id: 'turn-old'
              });
              window.__settlementRunnerResolvers[1]({
                session_id: 5, project: { id: 1, name: 'Project 1' }, context_revision: 1,
                session_title: 'Settled chat', mode: 'approval', runner_running: true,
                turn_busy: false, turn_id: null
              });
            }"""
        )
        await page.evaluate("window.__settlementHeaderJob")
        await page.wait_for_function("document.getElementById('st-turn').textContent === 'ready'")
        settled = await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              return { busy: api.state.runner.turn_busy, turnId: api.state.runner.turn_id };
            }"""
        )
        assert settled == {"busy": False, "turnId": None}
        assert "unavailable" not in (
            (await page.locator(".hdr-title").text_content() or "").lower()
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_workspace_authority_clears_state_and_stale_hydration(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalGet = api.get.bind(api);
              window.__oldTranscriptResolvers = [];
              window.__oldTranscriptRequests = 0;
              api.get = (path, options) => {
                if (path !== '/api/sessions/5') return originalGet(path, options);
                window.__oldTranscriptRequests += 1;
                return new Promise(resolve => window.__oldTranscriptResolvers.push(resolve));
              };
            }"""
        )
        await _handshake(page)
        await page.wait_for_function("window.__oldTranscriptRequests === 1")
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              api.state.chat = [{ role: 'user', text: 'PRIVATE OLD CHAT' }];
              api.state.chatAttachments = [{ title: 'secret-old.txt' }];
              api.state.notices = [{ text: 'old notice' }];
            }"""
        )
        await _handshake(page)
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('PRIVATE OLD CHAT')"
        )

        await _handshake(
            page,
            workspace_id="replacement-workspace",
            session_id=9,
            project_id=2,
        )
        await page.wait_for_function(
            "!document.getElementById('screen').textContent.includes('PRIVATE OLD CHAT')"
        )
        await page.evaluate(
            "window.__oldTranscriptResolvers[0]({ messages: [{ role: 'assistant', "
            "text: 'OLD TRANSCRIPT SECRET' }] })"
        )
        await page.wait_for_timeout(100)
        state = await page.evaluate(
            """(async () => {
              const { api } = await import('/static/app.js');
              return {
                context: api.state.context,
                chat: api.state.chat,
                attachments: api.state.chatAttachments,
                notices: api.state.notices
              };
            })()"""
        )
        assert state == {
            "context": {"session_id": 9, "project_id": 2, "context_revision": 1},
            "chat": [],
            "attachments": [],
            "notices": [],
        }
        assert "OLD TRANSCRIPT SECRET" not in (await page.locator("#screen").text_content() or "")
        assert errors == []
    finally:
        await context.close()


async def _assert_session_lifecycle_invalidates_runner_read(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.wait_for_function(
            "document.getElementById('st-project').textContent !== 'global'"
        )
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalGet = api.get.bind(api);
              window.__lifecycleRunnerResolvers = [];
              window.__lifecycleRunnerSignals = [];
              window.__lifecycleRunnerRequests = 0;
              api.get = (path, options = {}) => {
                if (path !== '/api/runner') return originalGet(path, options);
                window.__lifecycleRunnerRequests += 1;
                window.__lifecycleRunnerSignals.push(options.signal);
                return new Promise(resolve => window.__lifecycleRunnerResolvers.push(resolve));
              };
              window.__lifecycleOldRunner = api.runnerStatus({ refresh: true });
            }"""
        )
        await page.wait_for_function("window.__lifecycleRunnerRequests === 1")
        await page.evaluate(
            """() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
              kind: 'session_new', workspace_id: 'router-workspace',
              session_id: 6, project_id: 2, context_revision: 2
            }) })"""
        )
        await page.wait_for_function("window.__lifecycleRunnerRequests === 2")
        assert await page.evaluate("window.__lifecycleRunnerSignals[0].aborted") is True
        await page.evaluate(
            "window.__lifecycleRunnerResolvers[0]({ session_id: 5, project: { id: 1 }, "
            "mode: 'approval', runner_running: true, turn_busy: false })"
        )
        await page.evaluate("window.__lifecycleOldRunner")
        stale_context = await page.evaluate(
            "(async () => (await import('/static/app.js')).api.state.context)()"
        )
        assert stale_context == {
            "session_id": 6,
            "project_id": 2,
            "context_revision": 2,
        }
        await page.evaluate(
            "window.__lifecycleRunnerResolvers[1]({ session_id: 6, "
            "project: { id: 2, name: 'Two' }, mode: 'auto', "
            "runner_running: true, turn_busy: false })"
        )
        await page.wait_for_function("document.getElementById('st-mode').textContent === 'auto'")
        assert errors == []
    finally:
        await context.close()


async def _assert_daily_refresh_reenables_after_remount(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page, session_id=6, project_id=None, context_revision=2)
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              api.post = (path, body) => {
                if (path !== '/api/digest/run') return originalPost(path, body);
                return new Promise(resolve => { window.__digestRunResolve = resolve; });
              };
              location.hash = 'daily';
            }"""
        )
        refresh = page.get_by_role("button", name="Refresh")
        await refresh.click()
        await page.wait_for_function(
            "document.getElementById('daily-briefing-refresh').textContent === 'Refreshing…'"
        )
        await page.evaluate("location.hash = 'chat'")
        await page.wait_for_selector("#chat-input")
        await page.evaluate("location.hash = 'daily'")
        await page.wait_for_function(
            "document.getElementById('daily-briefing-refresh').textContent === 'Refreshing…'"
        )
        await page.evaluate("window.__digestRunResolve({ ok: true, status: 200, data: {} })")
        await page.wait_for_function(
            "document.getElementById('daily-briefing-refresh').textContent === 'Refresh' "
            "&& !document.getElementById('daily-briefing-refresh').disabled"
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_upload_batches_stop_at_authority_change(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              window.__uploadCalls = 0;
              window.__uploadForms = [];
              window.__uploadResolvers = [];
              api.upload = (_path, form) => {
                window.__uploadCalls += 1;
                window.__uploadForms.push({
                  session: form.get('expected_session_id'),
                  project: form.get('expected_project_id'),
                });
                return new Promise(resolve => window.__uploadResolvers.push(resolve));
              };
            }"""
        )
        files = [
            {"name": f"note-{index}.txt", "mimeType": "text/plain", "buffer": b"old"}
            for index in range(3)
        ]
        await page.locator("#chat-file-input").set_input_files(files)
        await page.wait_for_function("window.__uploadCalls === 1")
        await _handshake(
            page,
            workspace_id="upload-replacement",
            session_id=8,
            project_id=2,
        )
        await page.evaluate(
            "window.__uploadResolvers[0]({ ok: true, data: { "
            "action: 'added', source_id: 1, title: 'old' } })"
        )
        await page.wait_for_timeout(100)
        assert await page.evaluate("window.__uploadCalls") == 1
        assert await page.evaluate("window.__uploadForms") == [{"session": "5", "project": "1"}]

        await page.evaluate(
            "window.__uploadCalls = 0; window.__uploadForms = []; window.__uploadResolvers = []"
        )
        project_files = [
            {
                "name": f"module-{index}.py",
                "mimeType": "text/x-python",
                "buffer": b"print('old')",
            }
            for index in range(6)
        ]
        await page.locator("#chat-folder-input").evaluate(
            "node => node.removeAttribute('webkitdirectory')"
        )
        await page.locator("#chat-folder-input").set_input_files(project_files)
        await page.wait_for_function("window.__uploadCalls === 4")
        await _handshake(
            page,
            workspace_id="upload-final",
            session_id=9,
            project_id=3,
        )
        await page.evaluate(
            "window.__uploadResolvers.forEach(resolve => resolve({ "
            "ok: true, data: { action: 'added', source_id: 2 } }))"
        )
        await page.wait_for_timeout(150)
        assert await page.evaluate("window.__uploadCalls") == 4
        assert await page.evaluate("window.__uploadForms") == [
            {"session": "8", "project": "2"},
            {"session": "8", "project": "2"},
            {"session": "8", "project": "2"},
            {"session": "8", "project": "2"},
        ]
        assert (
            await page.evaluate(
                "(async () => (await import('/static/app.js')).api.state.projectImport)()"
            )
            is None
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_post_action_refresh_survives_same_key_refresh(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/tasks'] = [{
                id: 41, title: 'Cancel me', kind: 'job', status: 'active',
                next_run_at: '2026-07-15T09:00:00Z'
              }];
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              api.post = (path, body) => {
                if (path !== '/api/tasks/41/cancel') return originalPost(path, body);
                return new Promise(resolve => { window.__cancelTaskResolve = resolve; });
              };
              location.hash = 'tasks';
            }"""
        )
        await page.get_by_role("button", name="Cancel").click()
        await _handshake(page)
        await page.get_by_role("button", name="Cancel").wait_for()
        await page.evaluate(
            """() => {
              window.__SEED__['/api/tasks'] = [{
                id: 41, title: 'Cancel me', kind: 'job', status: 'cancelled',
                next_run_at: null
              }];
              window.__cancelTaskResolve({ ok: true, status: 200, data: { ok: true } });
            }"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('cancelled')"
        )
        assert await page.get_by_role("button", name="Cancel").count() == 0
        assert errors == []
    finally:
        await context.close()


async def _assert_settings_cache_is_authority_scoped(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/runner'].mode = 'OLD_SETTINGS_MODE';
              const { api } = await import('/static/app.js');
              api.state.runner.mode = 'OLD_SETTINGS_MODE';
              location.hash = 'settings';
            }"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('OLD_SETTINGS_MODE')"
        )
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalGet = api.get.bind(api);
              const pending = new Set(['/api/runner', '/api/hub', '/api/costs', '/api/settings']);
              window.__settingsPending = 0;
              window.__settingsResolvers = [];
              api.get = (path, options) => {
                if (!pending.has(path)) return originalGet(path, options);
                window.__settingsPending += 1;
                return new Promise(resolve => window.__settingsResolvers.push(resolve));
              };
            }"""
        )
        await _handshake(
            page,
            workspace_id="settings-replacement",
            session_id=9,
            project_id=2,
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('Loading status…')"
        )
        text = await page.locator("#screen").text_content() or ""
        assert "OLD_SETTINGS_MODE" not in text
        assert await page.evaluate("window.__settingsPending") >= 4
        assert errors == []
    finally:
        await context.close()


async def _assert_meeting_operation_recovers_on_remount(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/voice/status'] = {
                enabled: true, meeting_available: true, meeting_reason: '', stt: 'local',
                meeting: 'idle', meeting_recording: false,
                meeting_recording_epoch: 'router-process', meeting_revision: 1,
                meeting_recording_revision: 1
              };
               const { api } = await import('/static/app.js');
               const originalPost = api.post.bind(api);
               window.__meetingResolvers = [];
               window.__meetingRequests = 0;
               api.post = (path, body) => {
                 if (path !== '/api/voice/meeting') return originalPost(path, body);
                 window.__meetingRequests += 1;
                 return new Promise(resolve => { window.__meetingResolvers.push(resolve); });
               };
              location.hash = 'meetings';
            }"""
        )
        consent = page.locator("#mtg-consent")
        await consent.check()
        await page.get_by_role("button", name="Capture spoken note").click()
        await page.evaluate("location.hash = 'chat'")
        await page.wait_for_selector("#chat-input")
        await page.evaluate("location.hash = 'meetings'")
        await page.wait_for_function(
            "document.getElementById('screen').dataset.meetingPhase === 'finalizing'"
        )
        await page.evaluate("window.__meetingResolvers[0]({ ok: false, status: 503, data: {} })")
        await page.wait_for_function(
            "document.getElementById('screen').dataset.meetingPhase === 'idle'"
        )
        await page.get_by_text(
            "Meeting-note capture is unavailable. Check the voice setup and microphone.",
            exact=True,
        ).wait_for()
        assert await page.locator("#mtg-consent").is_enabled()

        await page.locator("#mtg-title").fill("Recovered remount")
        await page.locator("#mtg-consent").check()
        await page.get_by_role("button", name="Capture spoken note").click()
        await page.wait_for_function("window.__meetingRequests === 2")
        await page.evaluate("location.hash = 'chat'")
        await page.wait_for_selector("#chat-input")
        await page.evaluate(
            """() => window.__meetingResolvers[1]({ ok: true, status: 200, data: {
              ok: true, source_id: 73, source_status: 'live', index_state: 'indexed',
              review_status: 'unreviewed', title: 'Recovered remount'
            } })"""
        )
        # The request settles with Meetings unmounted. Receipt clearing proves the completion
        # continuation ran before we return; the same-authority outcome must then replay on mount.
        await page.wait_for_function(
            "sessionStorage.getItem('kairo:meeting-capture:project-1') === null"
        )
        assert "Recovered remount" not in (await page.locator("#screen").text_content() or "")
        await page.evaluate("location.hash = 'meetings'")
        await page.get_by_text(
            "Saved “Recovered remount” as unreviewed source #73.", exact=False
        ).wait_for()
        assert await page.get_by_role("link", name="Open Knowledge →").count() == 1
        assert await page.evaluate("window.__meetingRequests") == 2
        assert errors == []
    finally:
        await context.close()


async def _assert_studio_confirmation_cannot_cross_authority(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/studio'].active_project_id = 1;
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              window.__studioBodies = [];
              api.post = (path, body) => {
                if (path !== '/api/orchestration/run') return originalPost(path, body);
                window.__studioBodies.push(body);
                return new Promise(resolve => { window.__studioRunResolve = resolve; });
              };
              location.hash = 'studio';
            }"""
        )
        await page.locator("#st-task").fill("A task")
        await page.locator("#st-run").click()
        await page.wait_for_function("window.__studioBodies.length === 1")
        await page.evaluate(
            """() => {
              window.__SEED__['/api/studio'].active_project_id = 2;
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'project_changed', workspace_id: 'router-workspace',
                session_id: 9, project_id: 2, context_revision: 2, name: 'Project Two'
              }) });
            }"""
        )
        await page.wait_for_function(
            "document.querySelector('#st-task') && document.querySelector('#st-task').value === ''"
        )
        await page.evaluate(
            """() => window.__studioRunResolve({
              ok: true, status: 200, data: {
                needs_confirmation: true,
                estimate: { decision: 'confirm', reason: 'A COST MARKER', total_usd: 4,
                  members: [], unpriced: [] }
              }
            })"""
        )
        await page.wait_for_timeout(100)
        text = await page.locator("#screen").text_content() or ""
        assert "A COST MARKER" not in text
        assert await page.locator("#st-confirm").count() == 0
        assert await page.evaluate("window.__studioBodies.length") == 1
        assert errors == []
    finally:
        await context.close()


async def _assert_live_chat_wins_over_hydration_and_old_turn_response(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalGet = api.get.bind(api);
              const originalPost = api.post.bind(api);
              api.get = (path, options) => {
                if (path !== '/api/sessions/5') return originalGet(path, options);
                return new Promise(resolve => { window.__hydrateResolve = resolve; });
              };
              api.post = (path, body) => {
                if (path !== '/api/turn') return originalPost(path, body);
                return new Promise(resolve => { window.__turnResolve = resolve; });
              };
            }"""
        )
        await _handshake(page)
        await page.wait_for_function("typeof window.__hydrateResolve === 'function'")
        await page.locator("#chat-input").fill("LIVE USER MESSAGE")
        await page.locator("#chat-input").press("Enter")
        await page.evaluate(
            "window.__hydrateResolve({ messages: [{ role: 'assistant', "
            "text: 'STALE HYDRATION' }] })"
        )
        await page.wait_for_timeout(100)
        text = await page.locator("#screen").text_content() or ""
        assert "LIVE USER MESSAGE" in text
        assert "STALE HYDRATION" not in text

        await _handshake(
            page,
            workspace_id="turn-replacement",
            session_id=9,
            project_id=2,
        )
        await page.evaluate(
            "window.__turnResolve({ ok: false, status: 409, data: { message: 'OLD BUSY' } })"
        )
        await page.wait_for_timeout(100)
        state_chat = await page.evaluate(
            "(async () => (await import('/static/app.js')).api.state.chat)()"
        )
        assert all("OLD BUSY" not in str(item) for item in state_chat)
        assert errors == []
    finally:
        await context.close()


async def _assert_turn_admission_is_single_flight_and_restores_cross_route_draft(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              window.__turnAdmissionRequests = 0;
              api.post = (path, body) => {
                if (path !== '/api/turn') return originalPost(path, body);
                window.__turnAdmissionRequests += 1;
                return new Promise(resolve => { window.__turnAdmissionResolve = resolve; });
              };
            }"""
        )
        draft = "KEEP THIS FAILED DRAFT"
        await page.locator("#chat-input").fill(draft)
        await page.evaluate(
            """() => {
              const form = document.getElementById('chat-composer');
              form.requestSubmit();
              form.requestSubmit();
            }"""
        )
        await page.wait_for_function("window.__turnAdmissionRequests === 1")
        assert await page.locator(".chat-send").is_disabled()
        assert await page.locator(".chat-send").text_content() == "Sending…"
        assert await page.locator("article.msg.user", has_text=draft).count() == 1

        await page.evaluate("location.hash = '#settings'")
        await page.wait_for_function("location.hash === '#settings'")
        await page.evaluate(
            "window.__turnAdmissionResolve({ ok: false, status: 503, "
            "data: { message: 'TEMPORARY FAILURE' } })"
        )
        await page.wait_for_function(
            """async () => {
              const { api } = await import('/static/app.js');
              return api.state.turnAdmission === null
                && api.state.turnDraft === 'KEEP THIS FAILED DRAFT';
            }"""
        )
        await page.evaluate("location.hash = '#chat'")
        await page.wait_for_function(
            "document.getElementById('chat-input')?.value === 'KEEP THIS FAILED DRAFT'"
        )
        assert await page.locator("article.msg.user", has_text=draft).count() == 0
        assert await page.locator(".chat-send").is_enabled()
        assert await page.evaluate("window.__turnAdmissionRequests") == 1
        assert errors == []
    finally:
        await context.close()


async def _assert_resume_chat_reconciles_without_ws_and_rejects_old_callbacks(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              api.post = (path, body) => {
                if (!(path.startsWith('/api/sessions/') && path.endsWith('/resume'))) {
                  return originalPost(path, body);
                }
                if (window.__deferResume) {
                  return new Promise(resolve => { window.__resumeResolve = resolve; });
                }
                return Promise.resolve({ ok: true, status: 200, data: { ok: true } });
              };
              location.hash = 'daily';
            }"""
        )
        await page.locator("#daily-briefing-refresh").wait_for()
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              api.state.chat = [{ role: 'assistant', text: 'OLD TRANSCRIPT' }];
              location.hash = 'chat';
            }"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('OLD TRANSCRIPT')"
        )
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/runner'].session_id = 7;
              window.__SEED__['/api/runner'].project = { id: 2, name: 'Project 2' };
              window.__SEED__['/api/runner'].context_revision = 2;
              window.__SEED__['/api/sessions/7'] = { messages: [
                { role: 'user', text: 'TARGET USER' },
                { role: 'assistant', text: 'TARGET ASSISTANT' },
              ] };
              const { api } = await import('/static/app.js');
              window.__resumeResult = await api.resumeChat(7);
              if (window.__resumeResult) location.hash = 'chat';
            }"""
        )
        assert await page.evaluate("window.__resumeResult") is True
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('TARGET ASSISTANT')"
        )
        text = await page.locator("#screen").text_content() or ""
        assert "OLD TRANSCRIPT" not in text
        assert await page.evaluate(
            "(async () => (await import('/static/app.js')).api.state.context)()"
        ) == {"session_id": 7, "project_id": 2, "context_revision": 2}

        await page.evaluate(
            """async () => {
              window.__deferResume = true;
              const { api } = await import('/static/app.js');
              window.__staleResumeResult = api.resumeChat(8);
            }"""
        )
        await page.wait_for_function("typeof window.__resumeResolve === 'function'")
        await _handshake(
            page,
            workspace_id="resume-replacement",
            session_id=9,
            project_id=3,
        )
        await page.evaluate("location.hash = '#chat'")
        await page.wait_for_function("location.hash === '#chat'")
        await page.evaluate("window.__resumeResolve({ ok: true, status: 200, data: { ok: true } })")
        assert await page.evaluate("async () => await window.__staleResumeResult") is False
        assert await page.evaluate("location.hash") == "#chat"

        # Returning to the requested session after an intervening transition must not revive the
        # old callback. Session/project equality is not an epoch; only the exact +1 successor is.
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              window.__abaResumeResult = api.resumeChat(8).then(ok => {
                if (ok) location.hash = 'chat';
                return ok;
              });
            }"""
        )
        await page.wait_for_function("typeof window.__resumeResolve === 'function'")
        await _handshake(
            page,
            workspace_id="resume-replacement",
            session_id=10,
            project_id=4,
            context_revision=2,
        )
        await _handshake(
            page,
            workspace_id="resume-replacement",
            session_id=8,
            project_id=5,
            context_revision=3,
        )
        await page.evaluate("location.hash = '#settings'")
        await page.wait_for_function("location.hash === '#settings'")
        await page.evaluate("window.__resumeResolve({ ok: true, status: 200, data: { ok: true } })")
        assert await page.evaluate("async () => await window.__abaResumeResult") is False
        assert await page.evaluate("location.hash") == "#settings"
        assert errors == []
    finally:
        await context.close()


async def _assert_voice_permission_and_playback_are_authority_cancelled(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const voice = await import('/static/ui/voice.js');
              window.__voiceTrackStops = 0;
              window.__voiceRecorderConstructions = 0;
              window.__voiceStates = [];
              Object.defineProperty(navigator, 'mediaDevices', {
                configurable: true,
                value: { getUserMedia: () => new Promise(resolve => {
                  window.__resolveVoicePermission = resolve;
                }) },
              });
              window.MediaRecorder = class {
                constructor() { window.__voiceRecorderConstructions += 1; }
              };
              window.__voicePermissionTask = voice.toggleTalk({
                onState: state => window.__voiceStates.push(state),
              });
            }"""
        )
        await page.wait_for_function("typeof window.__resolveVoicePermission === 'function'")
        await page.evaluate(
            """async () => {
              const voice = await import('/static/ui/voice.js');
              voice.cancelCapture(state => window.__voiceStates.push(state));
              window.__resolveVoicePermission({ getTracks: () => [{
                stop: () => { window.__voiceTrackStops += 1; },
              }] });
              await window.__voicePermissionTask;
            }"""
        )
        assert await page.evaluate("window.__voiceTrackStops") == 1
        assert await page.evaluate("window.__voiceRecorderConstructions") == 0
        assert "idle" in await page.evaluate("window.__voiceStates")

        await page.evaluate(
            """async () => {
              const voice = await import('/static/ui/voice.js');
              voice.setPlayback(true);
              const originalFetch = window.fetch.bind(window);
              window.__voiceObjectUrls = 0;
              window.__voiceAudioConstructions = 0;
              URL.createObjectURL = () => {
                window.__voiceObjectUrls += 1;
                return 'blob:retired';
              };
              window.Audio = class { constructor() { window.__voiceAudioConstructions += 1; } };
              window.fetch = (path, options) => {
                if (path !== '/api/voice/tts') return originalFetch(path, options);
                return new Promise(resolve => { window.__resolveVoicePlayback = resolve; });
              };
              window.__voicePlaybackTask = voice.playCaption('safe caption');
            }"""
        )
        await page.wait_for_function("typeof window.__resolveVoicePlayback === 'function'")
        await page.evaluate(
            """async () => {
              const voice = await import('/static/ui/voice.js');
              voice.stopCaption();
              window.__resolveVoicePlayback({
                status: 200, blob: async () => new Blob(['retired audio']),
              });
              await window.__voicePlaybackTask;
            }"""
        )
        assert await page.evaluate("window.__voiceObjectUrls") == 0
        assert await page.evaluate("window.__voiceAudioConstructions") == 0
        assert errors == []
    finally:
        await context.close()


async def _assert_dictation_survives_same_authority_navigation(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              api.state.voice.enabled = true;
              api.state.voice.mode = 'dictation';
              const track = { stop() {} };
              Object.defineProperty(navigator, 'mediaDevices', {
                configurable: true,
                value: { getUserMedia: async () => ({ getTracks: () => [track] }) },
              });
              window.MediaRecorder = class {
                constructor() {
                  this.state = 'inactive';
                  this.mimeType = 'audio/webm';
                  this.listeners = {};
                }
                addEventListener(kind, callback) { this.listeners[kind] = callback; }
                start() { this.state = 'recording'; }
                stop() {
                  this.state = 'inactive';
                  this.listeners.dataavailable?.({ data: new Blob(['spoken draft']) });
                  this.listeners.stop?.();
                }
              };
              const originalFetch = window.fetch.bind(window);
              window.fetch = (url, options) => {
                const value = typeof url === 'string' ? url : url.url;
                if (!value.startsWith('/api/voice/utterance?mode=dictation')) {
                  return originalFetch(url, options);
                }
                return new Promise(resolve => { window.__dictationResolve = resolve; });
              };
              window.__dictationOldInput = document.getElementById('chat-input');
            }"""
        )
        mic = page.locator("#chat-mic")
        await mic.click()
        await page.wait_for_function(
            "document.getElementById('chat-voice-status')?.textContent.includes('Listening')"
        )
        await mic.click()
        await page.wait_for_function("typeof window.__dictationResolve === 'function'")
        await page.evaluate("location.hash = 'settings'")
        await page.wait_for_function("location.hash === '#settings'")
        await page.evaluate(
            """() => window.__dictationResolve(new Response(
              JSON.stringify({ transcript: 'DICTATION ROUTE DRAFT' }),
              { status: 200, headers: { 'content-type': 'application/json' } }
            ))"""
        )
        await page.wait_for_function(
            """async () => {
              const { api } = await import('/static/app.js');
              return api.state.turnDraft === 'DICTATION ROUTE DRAFT';
            }"""
        )
        assert await page.evaluate("window.__dictationOldInput.isConnected") is False
        assert await page.evaluate("window.__dictationOldInput.value") == ""
        await page.evaluate("location.hash = 'chat'")
        await page.wait_for_function(
            "document.getElementById('chat-input')?.value === 'DICTATION ROUTE DRAFT'"
        )
        assert (
            await page.evaluate(
                "(async () => (await import('/static/app.js')).api.state.turnDraft)()"
            )
            is None
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_palette_results_cannot_cross_authority(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              const { openPalette } = await import('/static/ui/palette.js');
              openPalette();
            }"""
        )
        await page.locator(".command-overlay.open").wait_for()
        await page.wait_for_timeout(100)
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalGet = api.get.bind(api);
              window.__paletteSearches = 0;
              api.get = (path, options) => {
                if (!path.startsWith('/api/search?')) return originalGet(path, options);
                window.__paletteSearches += 1;
                return new Promise(resolve => { window.__paletteSearchResolve = resolve; });
              };
            }"""
        )
        await page.locator(".command-overlay.open input").fill("old private result")
        await page.wait_for_function("window.__paletteSearches === 1")
        await _handshake(
            page,
            workspace_id="palette-replacement",
            session_id=5,
            project_id=1,
        )
        await page.wait_for_function(
            "!document.querySelector('.command-overlay')?.classList.contains('open')"
        )
        await page.evaluate(
            """() => window.__paletteSearchResolve({ results: [{
              domain: 'tasks', ref_id: 7, title: 'OLD PALETTE SECRET', snippet: 'old scope'
            }] })"""
        )
        await page.wait_for_timeout(100)
        await page.evaluate(
            """async () => {
              const { openPalette } = await import('/static/ui/palette.js');
              openPalette();
            }"""
        )
        await page.locator(".command-overlay.open").wait_for()
        palette_text = await page.locator(".command-overlay").text_content() or ""
        assert "OLD PALETTE SECRET" not in palette_text
        assert errors == []
    finally:
        await context.close()


async def _assert_palette_action_callback_cannot_cross_authority(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              const originalRunner = api.runnerStatus.bind(api);
              window.__paletteActionRunnerReads = 0;
              api.post = (path, body) => {
                if (path !== '/api/sessions/new') return originalPost(path, body);
                return new Promise(resolve => { window.__paletteActionResolve = resolve; });
              };
              api.runnerStatus = (...args) => {
                window.__paletteActionRunnerReads += 1;
                return originalRunner(...args);
              };
              const { openPalette } = await import('/static/ui/palette.js');
              openPalette();
            }"""
        )
        await page.locator(".command-overlay.open").wait_for()
        await page.get_by_text("New Chat", exact=True).click()
        await page.wait_for_function("typeof window.__paletteActionResolve === 'function'")
        await _handshake(
            page,
            workspace_id="palette-action-replacement",
            session_id=5,
            project_id=1,
        )
        await page.wait_for_function(
            "!document.querySelector('.command-overlay')?.classList.contains('open')"
        )
        await page.evaluate("location.hash = '#chat'")
        await page.wait_for_function("location.hash === '#chat'")
        reads_after_replacement = await page.evaluate("window.__paletteActionRunnerReads")
        await page.evaluate(
            "window.__paletteActionResolve({ ok: true, status: 200, data: { ok: true } })"
        )
        await page.wait_for_timeout(150)
        assert await page.evaluate("location.hash") == "#chat"
        assert await page.evaluate("window.__paletteActionRunnerReads") == reads_after_replacement
        assert errors == []
    finally:
        await context.close()


async def _assert_daily_refresh_is_authority_owned(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page, project_id=None, context_revision=2)
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              window.__dailyPosts = [];
              api.post = (path, body) => {
                if (path !== '/api/digest/run') return originalPost(path, body);
                return new Promise(resolve => window.__dailyPosts.push({ resolve, body }));
              };
              location.hash = 'daily';
            }"""
        )
        refresh = page.locator("#daily-briefing-refresh")
        await refresh.click()
        await page.wait_for_function("window.__dailyPosts.length === 1")
        await _handshake(
            page,
            workspace_id="daily-replacement",
            session_id=5,
            project_id=None,
        )
        await refresh.wait_for()
        await page.wait_for_function("!document.getElementById('daily-briefing-refresh').disabled")
        await refresh.click()
        await page.wait_for_function("window.__dailyPosts.length === 2")
        await page.evaluate(
            "window.__dailyPosts[0].resolve({ ok: true, status: 200, data: { ok: true } })"
        )
        await page.wait_for_timeout(100)
        assert await refresh.is_disabled()
        assert await refresh.text_content() == "Refreshing…"
        assert await page.get_by_text("Briefing refreshed.", exact=True).count() == 0
        await page.evaluate(
            "window.__dailyPosts[1].resolve({ ok: true, status: 200, data: { ok: true } })"
        )
        await page.wait_for_function("!document.getElementById('daily-briefing-refresh').disabled")
        assert await page.get_by_text("Briefing refreshed.", exact=True).count() == 1
        assert errors == []
    finally:
        await context.close()


async def _assert_task_history_is_route_and_authority_owned(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/tasks'] = [{
                id: 41, title: 'Scoped task', kind: 'job', status: 'active',
                next_run_at: '2026-07-15T09:00:00Z', payload: 'scoped payload'
              }];
              window.__SEED__['/api/tasks/41/runs'] = [{
                id: 3, status: 'ok', result_text: 'CURRENT HISTORY'
              }];
              const { api } = await import('/static/app.js');
              const originalGet = api.get.bind(api);
              window.__deferTaskHistory = true;
              api.get = (path, options) => {
                if (path !== '/api/tasks/41/runs' || !window.__deferTaskHistory) {
                  return originalGet(path, options);
                }
                return new Promise(resolve => { window.__taskHistoryResolve = resolve; });
              };
              location.hash = 'tasks';
            }"""
        )
        await page.get_by_role("button", name="History").click()
        await page.wait_for_function("typeof window.__taskHistoryResolve === 'function'")
        await _handshake(
            page,
            workspace_id="task-history-replacement",
            session_id=5,
            project_id=1,
        )
        await page.evaluate(
            """() => window.__taskHistoryResolve([
              { id: 2, status: 'ok', result_text: 'OLD HISTORY SECRET' }
            ])"""
        )
        await page.wait_for_timeout(100)
        assert await page.locator(".task-history-dialog").count() == 0
        assert "OLD HISTORY SECRET" not in (await page.locator("body").text_content() or "")

        await page.evaluate("window.__deferTaskHistory = false")
        await page.get_by_role("button", name="History").click()
        await page.locator(".task-history-dialog").wait_for()
        await _handshake(
            page,
            workspace_id="task-history-replacement-2",
            session_id=5,
            project_id=1,
        )
        await page.wait_for_function("!document.querySelector('.task-history-dialog')")
        assert errors == []
    finally:
        await context.close()


async def _assert_studio_state_and_runs_are_authority_owned(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/studio'].active_project_id = 1;
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              window.__studioPosts = [];
              api.post = (path, body) => {
                if (path !== '/api/orchestration/run') return originalPost(path, body);
                return new Promise(resolve => window.__studioPosts.push({ resolve, body }));
              };
              location.hash = 'studio';
            }"""
        )
        await page.locator("#st-task").fill("One committed run")
        await page.evaluate(
            """() => {
              const button = document.getElementById('st-run');
              button.click();
              button.click();
            }"""
        )
        await page.wait_for_function("window.__studioPosts.length === 1")
        assert await page.locator("#st-task").is_disabled()
        await _handshake(page)
        await page.wait_for_function("document.getElementById('st-run')?.disabled === true")
        await page.evaluate(
            """() => window.__studioPosts[0].resolve({
              ok: true, status: 202, data: { estimate: {
                decision: 'allow', reason: 'RUN COMMITTED', total_usd: 1,
                members: [], unpriced: []
              } }
            })"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('RUN COMMITTED')"
        )
        assert not await page.locator("#st-run").is_disabled()
        assert await page.evaluate("window.__studioPosts.length") == 1

        await page.locator("#st-task").fill("Needs confirmation")
        await page.locator("#st-run").click()
        await page.wait_for_function("window.__studioPosts.length === 2")
        await page.evaluate(
            """() => window.__studioPosts[1].resolve({
              ok: true, status: 200, data: { needs_confirmation: true, estimate: {
                decision: 'confirm', reason: 'Confirm once', total_usd: 2,
                members: [], unpriced: []
              } }
            })"""
        )
        await page.locator("#st-confirm").wait_for()
        await page.evaluate(
            """() => {
              const button = document.getElementById('st-confirm');
              button.click();
              button.click();
            }"""
        )
        await page.wait_for_function("window.__studioPosts.length === 3")
        assert (
            await page.evaluate(
                "window.__studioPosts.filter(item => item.body.confirmed === true).length"
            )
            == 1
        )
        await page.evaluate(
            """() => window.__studioPosts[2].resolve({
              ok: true, status: 202, data: { estimate: {
                decision: 'allow', reason: 'CONFIRMED COMMITTED', total_usd: 2,
                members: [], unpriced: []
              } }
            })"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('CONFIRMED COMMITTED')"
        )

        await page.locator("#st-task").fill("OLD STUDIO DRAFT")
        await page.evaluate(
            """() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
              kind: 'orchestration_started', workspace_id: 'router-workspace',
              session_id: 5, project_id: 1, context_revision: 1,
              run_id: 88, team: 't', workflow: 'w',
              title: 'OLD LIVE RUN'
            }) })"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('OLD LIVE RUN')"
        )
        await _handshake(
            page,
            workspace_id="studio-same-context-replacement",
            session_id=5,
            project_id=1,
        )
        await page.wait_for_function(
            "document.querySelector('#st-task') && document.querySelector('#st-task').value === ''"
        )
        assert "OLD LIVE RUN" not in (await page.locator("#screen").text_content() or "")

        await page.locator("#st-task").fill("Prompt owned by the first replacement")
        await page.locator("#st-run").click()
        await page.wait_for_function("window.__studioPosts.length === 4")
        await page.evaluate(
            """() => window.__studioPosts[3].resolve({
              ok: true, status: 200, data: { needs_confirmation: true, estimate: {
                decision: 'confirm', reason: 'Authority-owned prompt', total_usd: 3,
                members: [], unpriced: []
              } }
            })"""
        )
        await page.locator("#st-confirm").wait_for()
        await page.evaluate("window.__oldStudioConfirm = document.getElementById('st-confirm')")
        await _handshake(
            page,
            workspace_id="studio-confirm-replacement",
            session_id=5,
            project_id=1,
        )
        await page.wait_for_function(
            "document.querySelector('#st-task') && document.querySelector('#st-task').value === ''"
        )
        await page.evaluate("window.__oldStudioConfirm.click()")
        await page.wait_for_timeout(100)
        assert await page.evaluate("window.__studioPosts.length") == 4
        assert (
            await page.evaluate(
                "window.__studioPosts.filter(item => item.body.confirmed === true).length"
            )
            == 1
        )

        await page.locator("#st-task").fill("Fresh replacement run")
        await page.locator("#st-run").click()
        await page.wait_for_function("window.__studioPosts.length === 5")
        await page.evaluate(
            """() => window.__studioPosts[4].resolve({
              ok: true, status: 202, data: { estimate: {
                decision: 'allow', reason: 'FRESH AUTHORITY COMMITTED', total_usd: 1,
                members: [], unpriced: []
              } }
            })"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('FRESH AUTHORITY COMMITTED')"
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_studio_detail_actions_use_the_owned_route_api(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/studio'].active_project_id = 1;
              const detail = window.__SEED__['/api/orchestration/1'];
              detail.run.can_resume = true;
              detail.run.project_id = 1;
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              window.__studioResumePosts = [];
              api.post = (path, body) => {
                if (path !== '/api/orchestration/1/resume') return originalPost(path, body);
                return new Promise(resolve => window.__studioResumePosts.push({ resolve, body }));
              };
              location.hash = 'studio/1';
            }"""
        )
        await page.locator(".run-actions-promote").first.wait_for()
        await page.locator(".run-actions-promote").first.click()
        await page.locator(".task-draft-dialog").wait_for()
        await page.get_by_role("button", name="Cancel").click()
        await page.locator("#st-run-resume-task").fill("Exact original task brief")
        await page.evaluate("window.__oldStudioResume = document.getElementById('st-run-resume')")

        await _handshake(
            page,
            workspace_id="studio-detail-replacement",
            session_id=5,
            project_id=1,
        )
        await page.locator("#st-run-resume").wait_for()
        await page.evaluate("window.__oldStudioResume.click()")
        await page.wait_for_timeout(100)
        assert await page.evaluate("window.__studioResumePosts.length") == 0

        await page.locator("#st-run-resume-task").fill("Fresh authority task brief")
        await page.evaluate(
            """() => {
              const button = document.getElementById('st-run-resume');
              button.click();
              button.click();
            }"""
        )
        await page.wait_for_function("window.__studioResumePosts.length === 1")
        assert await page.locator("#st-run-resume").is_disabled()
        assert await page.evaluate("window.__studioResumePosts[0].body.task") == (
            "Fresh authority task brief"
        )
        await page.evaluate(
            "window.__studioResumePosts[0].resolve({ ok: true, status: 202, data: { ok: true } })"
        )
        await page.get_by_text("Continuation started.", exact=False).wait_for()
        assert errors == []
    finally:
        await context.close()


async def _assert_project_lifecycle_rerenders_gate_and_redirects_workspace(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate(
            "window.__SEED__['/api/attention'] = { items: [{ source: 'system', ref: 1, "
            "kind: 'warning', title: 'OLD PROJECT ITEM' }] }; location.hash = 'gate'"
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('OLD PROJECT ITEM')"
        )
        await page.evaluate(
            """() => {
              window.__SEED__['/api/attention'] = { items: [{ source: 'system', ref: 2,
                kind: 'warning', title: 'NEW PROJECT ITEM' }] };
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'project_changed', workspace_id: 'router-workspace',
                session_id: 6, project_id: 2, context_revision: 2, name: 'Project Two'
              }) });
            }"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('NEW PROJECT ITEM')"
        )
        gate_text = await page.locator("#screen").text_content() or ""
        assert "OLD PROJECT ITEM" not in gate_text

        await page.evaluate(
            """() => {
              window.__SEED__['/api/workspace/2'] = { project: { id: 2, name: 'Project Two' } };
              location.hash = 'workspace/2';
            }"""
        )
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('Project Two')"
        )
        await page.evaluate(
            """() => {
              window.__SEED__['/api/workspace/3'] = { project: { id: 3, name: 'Project Three' } };
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'project_changed', workspace_id: 'router-workspace',
                session_id: 7, project_id: 3, context_revision: 3, name: 'Project Three'
              }) });
            }"""
        )
        await page.wait_for_function("location.hash === '#workspace/3'")
        await page.wait_for_function(
            "document.getElementById('screen').textContent.includes('Project Three')"
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_any_runner_refresh_recovers_a_missed_lifecycle_frame(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _handshake(page)
        await page.evaluate("location.hash = 'workspace/1'")
        await page.wait_for_function("location.hash === '#workspace/1'")
        old_authority = await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              window.__routerApi = api;
              api.state.chat = [{ role: 'user', content: 'old authority' }];
              api.state.chatAttachments = [{ id: 7, name: 'old.txt' }];
              api.state.notices = [{ id: 'old-notice', message: 'old authority' }];
              window.__SEED__['/api/runner'].session_id = 12;
              window.__SEED__['/api/runner'].project = { id: 2, name: 'Project 2' };
              window.__SEED__['/api/workspace/2'] = {
                project: { id: 2, name: 'Project 2' }
              };
              return api.authorityToken();
            }"""
        )
        # No lifecycle WebSocket frame is sent. A direct shared-reader consumer must reconcile
        # immediately, not four seconds later in pollStatus.
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              await api.runnerStatus({ refresh: true });
            }"""
        )
        await page.wait_for_function(
            """oldAuthority => {
              const api = window.__routerApi;
              return api && api.authorityToken() > oldAuthority
                && api.state.context?.session_id === 12
                && location.hash === '#workspace/2';
            }""",
            arg=old_authority,
            timeout=2000,
        )
        snapshot = await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              return {
                chat: api.state.chat,
                attachments: api.state.chatAttachments,
                notices: api.state.notices,
                projectId: api.state.context?.project_id,
              };
            }"""
        )
        assert snapshot == {
            "chat": [],
            "attachments": [],
            "notices": [],
            "projectId": 2,
        }
        assert errors == []
    finally:
        await context.close()


async def _assert_artifact_refresh_preserves_owned_ui_state(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """() => {
              window.__SEED__['/api/artifacts'] = { artifacts: [
                { id: 1, title: 'Needle report', kind: 'report', labels: ['review'],
                  project_id: 1, origin_type: 'report', origin_id: '1', has_content: false },
                { id: 2, title: 'Other output', kind: 'draft', labels: [],
                  project_id: 1, origin_type: 'draft', origin_id: '2', has_content: false }
              ] };
              location.hash = 'artifacts';
            }"""
        )
        search = page.get_by_role("searchbox", name="Search artifacts")
        await search.fill("needle")
        await page.locator(".art-row").filter(has_text="Needle report").click()
        await page.wait_for_function(
            "document.getElementById('art-preview').textContent.includes('Needle report')"
        )
        await page.evaluate("window.dispatchEvent(new HashChangeEvent('hashchange'))")
        await page.wait_for_function(
            "document.querySelector('.ws-search')?.value === 'needle' "
            "&& document.getElementById('art-preview')?.textContent.includes('Needle report')"
        )
        await _handshake(
            page,
            workspace_id="artifact-replacement",
            session_id=9,
            project_id=1,
            context_revision=1,
        )
        await page.wait_for_function("document.querySelector('.ws-search')?.value === ''")
        assert "No artifact selected" in (await page.locator("#art-preview").text_content() or "")
        assert errors == []
    finally:
        await context.close()


async def _assert_vault_review_is_single_flight_in_both_surfaces(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/vault'] = {
                stats: { unreviewed: 1 }, project_id: 1,
                unreviewed: [{ id: 41, title: 'Shared source', review_status: 'unreviewed',
                  origin: 'upload', preview: 'shared preview' }]
              };
              window.__SEED__['/api/chat/knowledge'] = {
                project_id: 1, source_count: 0, sources: [], graph: { available: false }
              };
              window.__SEED__['/api/projects'].active_project_id = 1;
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              window.__vaultReviewPaths = [];
              window.__vaultReviewResolvers = [];
              api.post = (path, body) => {
                if (!path.startsWith('/api/vault/sources/')) return originalPost(path, body);
                window.__vaultReviewPaths.push(path);
                return new Promise(resolve => window.__vaultReviewResolvers.push(resolve));
              };
              location.hash = 'vault';
            }"""
        )
        global_card = page.locator(".review-item").filter(has_text="Shared source")
        await global_card.wait_for()
        await global_card.get_by_role("button", name="Approve").click()
        await page.wait_for_function("window.__vaultReviewPaths.length === 1")
        await page.evaluate("window.dispatchEvent(new HashChangeEvent('hashchange'))")
        await page.wait_for_function(
            "document.querySelector('.review-item')?.getAttribute('aria-busy') === 'true'"
        )
        global_card = page.locator(".review-item").filter(has_text="Shared source")
        assert await global_card.get_attribute("aria-busy") == "true"
        assert all(
            await global_card.locator("button").evaluate_all("bs => bs.map(b => b.disabled)")
        )
        # The same source is also visible in Workspace. That replacement surface must attach to
        # the one shared operation, retain busy state, and reject an opposing synthetic click.
        await page.evaluate("location.hash = 'workspace/1/vault'")
        workspace_row = page.locator(".list-row").filter(has_text="Shared source")
        await workspace_row.wait_for()
        assert await workspace_row.get_attribute("aria-busy") == "true"
        assert all(
            await workspace_row.locator("button").evaluate_all("bs => bs.map(b => b.disabled)")
        )
        await workspace_row.locator("button").evaluate_all(
            "buttons => buttons.forEach(button => button.click())"
        )
        await page.wait_for_timeout(50)
        assert await page.evaluate("window.__vaultReviewPaths.length") == 1
        await page.evaluate(
            "window.__vaultReviewResolvers[0]({ ok: false, status: 503, "
            "data: { message: 'GLOBAL REVIEW FAILED' } })"
        )
        await page.wait_for_function(
            "Array.from(document.querySelectorAll('.list-row button')).every(b => !b.disabled)"
        )
        await page.get_by_text("GLOBAL REVIEW FAILED", exact=True).wait_for()

        await workspace_row.get_by_role("button", name="Approve").click()
        await page.wait_for_function("window.__vaultReviewPaths.length === 2")
        await page.evaluate("location.hash = 'vault'")
        await page.wait_for_function(
            "document.querySelector('.review-item')?.getAttribute('aria-busy') === 'true'"
        )
        global_card = page.locator(".review-item").filter(has_text="Shared source")
        assert await global_card.get_attribute("aria-busy") == "true"
        assert all(
            await global_card.locator("button").evaluate_all("bs => bs.map(b => b.disabled)")
        )
        await global_card.locator("button").evaluate_all(
            "buttons => buttons.forEach(button => button.click())"
        )
        await page.wait_for_timeout(50)
        assert await page.evaluate("window.__vaultReviewPaths.length") == 2
        await page.evaluate(
            "window.__vaultReviewResolvers[1]({ ok: false, status: 503, "
            "data: { message: 'WORKSPACE REVIEW FAILED' } })"
        )
        await page.wait_for_function(
            "Array.from(document.querySelectorAll('.review-item button')).every(b => !b.disabled)"
        )
        await page.get_by_text("WORKSPACE REVIEW FAILED", exact=True).wait_for()
        assert await page.evaluate("window.__vaultReviewPaths.length") == 2
        assert errors == []
    finally:
        await context.close()


async def _assert_vault_success_refresh_survives_detached_gap(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/vault'] = {
                stats: { unreviewed: 1 }, project_id: 1,
                unreviewed: [{ id: 42, title: 'Detached source', review_status: 'unreviewed',
                  origin: 'upload', preview: 'captured before the write' }]
              };
              window.__SEED__['/api/chat/knowledge'] = {
                project_id: 1, source_count: 0, sources: [], graph: { available: false }
              };
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              const originalGet = api.get.bind(api);
              window.__vaultGapPostResolve = null;
              window.__vaultGapVaultReads = 0;
              window.__vaultGapHeldKnowledge = null;
              window.__vaultGapHoldNextKnowledge = false;
              api.post = (path, body) => {
                if (path !== '/api/vault/sources/42/approve') return originalPost(path, body);
                return new Promise(resolve => { window.__vaultGapPostResolve = resolve; });
              };
              api.get = (path, options) => {
                if (path.startsWith('/api/vault')) window.__vaultGapVaultReads += 1;
                if (path.startsWith('/api/chat/knowledge')
                    && window.__vaultGapHoldNextKnowledge) {
                  window.__vaultGapHoldNextKnowledge = false;
                  return new Promise(resolve => { window.__vaultGapHeldKnowledge = resolve; });
                }
                return originalGet(path, options);
              };
              location.hash = 'vault';
            }"""
        )
        card = page.locator(".review-item").filter(has_text="Detached source")
        await card.wait_for()
        await card.get_by_role("button", name="Approve").click()
        await page.wait_for_function("typeof window.__vaultGapPostResolve === 'function'")

        # Cross-surface navigation disconnects the originating route facade. Hold Workspace's
        # second dependency after /api/vault has synchronously captured its stale body, leaving no
        # live row (and no usable originating refresh facade) when the write settles.
        await page.evaluate(
            """() => {
              window.__vaultGapOldRow = Array.from(document.querySelectorAll('.review-item'))
                .find(row => row.textContent.includes('Detached source'));
              window.__vaultGapHoldNextKnowledge = true;
              location.hash = 'workspace/1/vault';
            }"""
        )
        await page.wait_for_function("typeof window.__vaultGapHeldKnowledge === 'function'")
        assert await page.evaluate("window.__vaultGapOldRow.isConnected") is False
        reads_before_settlement = await page.evaluate("window.__vaultGapVaultReads")

        # Simulate the authoritative post-write queue. The held render still owns the pre-write
        # Response body; recovery therefore requires a distinct read started by settlement.
        await page.evaluate(
            """() => {
              window.__SEED__['/api/vault'].stats.unreviewed = 0;
              window.__SEED__['/api/vault'].unreviewed = [];
              window.__vaultGapPostResolve({ ok: true, status: 200, data: { ok: true } });
            }"""
        )
        # The stale replacement now binds to the retained success. Its own current route facade
        # must launch the post-write read; the disconnected global facade cannot do that for it.
        await page.evaluate(
            "window.__vaultGapHeldKnowledge(window.__SEED__['/api/chat/knowledge'])"
        )
        await page.wait_for_function(
            "reads => window.__vaultGapVaultReads > reads",
            arg=reads_before_settlement,
        )
        await page.wait_for_function(
            "!Array.from(document.querySelectorAll('.list-row'))"
            ".some(row => row.textContent.includes('Detached source'))"
        )
        await page.wait_for_timeout(50)
        assert await page.get_by_text("Detached source", exact=True).count() == 0
        assert errors == []
    finally:
        await context.close()


async def _assert_report_dialog_survives_passive_render_and_traps_keys(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """() => {
              window.__SEED__['/api/daily'] = {
                digest: null,
                project_assessment: { state: 'ready', report: {
                  id: 77, summary_preview: 'Current assessment', counts: {}
                } }
              };
              window.__SEED__['/api/project-intelligence/reports/77'] = { report: {
                id: 77, status: 'current', summary: 'Current full report', counts: {},
                strengths: [], weaknesses: [], security_candidates: [],
                frontend_backend_gaps: [], test_reliability_gaps: [],
                recommendations: [{ index: 0, title: 'Review scope', goal: 'Validate',
                  priority: 'high', studio_available: true }]
              } };
              location.hash = 'daily';
            }"""
        )
        await page.get_by_role("button", name="View report").click()
        dialog = page.get_by_role("dialog", name="Project assessment report")
        await dialog.wait_for()
        await page.evaluate("window.dispatchEvent(new HashChangeEvent('hashchange'))")
        await page.wait_for_function(
            "document.querySelector('.project-report-dialog')?.textContent"
            ".includes('Current full report')"
        )
        await page.get_by_role("button", name="Close").focus()
        await page.keyboard.press("Tab")
        assert await dialog.evaluate("node => node.contains(document.activeElement)") is True
        await page.keyboard.press("Control+K")
        assert await page.locator(".command-overlay.open").count() == 0
        await page.get_by_role("button", name="Review with AI team").click()
        await page.wait_for_function("location.hash === '#studio/report/77/0'")
        assert errors == []
    finally:
        await context.close()


async def _assert_delayed_project_navigation_respects_newer_authority_and_intent(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/projects'] = { projects: [
                { id: 1, name: 'Project One' }, { id: 2, name: 'Project Two' },
                { id: 3, name: 'Project Three' }
              ], active_project_id: 1 };
              window.__SEED__['/api/projects/overview'] = { projects: [
                { id: 1, name: 'Project One', status: 'active', health: {} },
                { id: 2, name: 'Project Two', status: 'active', health: {} },
                { id: 3, name: 'Project Three', status: 'active', health: {} }
              ], archived: [], active_project_id: 1 };
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              api.post = (path, body) => {
                if (path === '/api/projects/select' && body?.project_id === 2) {
                  return new Promise(resolve => { window.__projectTwoResolve = resolve; });
                }
                return originalPost(path, body);
              };
              location.hash = 'projects';
            }"""
        )
        card = page.locator(".project-card").filter(has_text="Project Two")
        await card.get_by_role("button", name="Open & switch").click()
        await page.wait_for_function("typeof window.__projectTwoResolve === 'function'")
        await page.evaluate(
            """() => {
              const runner = window.__SEED__['/api/runner'];
              runner.session_id = 7;
              runner.project = { id: 3, name: 'Project Three' };
              runner.context_revision = 3;
              window.__SEED__['/api/projects'].active_project_id = 3;
              for (const frame of [
                { session_id: 6, project_id: 2, context_revision: 2, name: 'Project Two' },
                { session_id: 7, project_id: 3, context_revision: 3, name: 'Project Three' }
              ]) window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'project_changed', workspace_id: 'router-workspace', ...frame
              }) });
              location.hash = 'settings';
            }"""
        )
        await page.wait_for_function("location.hash === '#settings'")
        await page.evaluate(
            "window.__projectTwoResolve({ ok: true, status: 200, data: { active_project_id: 2 } })"
        )
        await page.wait_for_timeout(150)
        assert await page.evaluate("location.hash") == "#settings"
        assert errors == []
    finally:
        await context.close()


async def _assert_workspace_chat_transition_cannot_navigate_a_newer_scope(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              window.__SEED__['/api/workspace/2'] = { project: { id: 2, name: 'Project Two' } };
              window.__SEED__['/api/workspace/3'] = { project: { id: 3, name: 'Project Three' } };
              window.__SEED__['/api/sessions'] = { sessions: [] };
              const { api } = await import('/static/app.js');
              const originalPost = api.post.bind(api);
              api.post = (path, body) => path === '/api/projects/select' && body?.project_id === 2
                ? new Promise(resolve => { window.__workspaceTwoResolve = resolve; })
                : originalPost(path, body);
              location.hash = 'workspace/2/chats';
            }"""
        )
        await page.get_by_role("button", name="Work in this project").click()
        await page.wait_for_function("typeof window.__workspaceTwoResolve === 'function'")
        await page.evaluate(
            """() => {
              const runner = window.__SEED__['/api/runner'];
              runner.session_id = 7;
              runner.project = { id: 3, name: 'Project Three' };
              runner.context_revision = 3;
              for (const frame of [
                { session_id: 6, project_id: 2, context_revision: 2, name: 'Project Two' },
                { session_id: 7, project_id: 3, context_revision: 3, name: 'Project Three' }
              ]) window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'project_changed', workspace_id: 'router-workspace', ...frame
              }) });
            }"""
        )
        await page.wait_for_function("location.hash.startsWith('#workspace/3')")
        await page.evaluate(
            "window.__workspaceTwoResolve({ ok: true, status: 200, data: "
            "{ active_project_id: 2 } })"
        )
        await page.wait_for_timeout(150)
        assert (await page.evaluate("location.hash")).startswith("#workspace/3")
        assert errors == []
    finally:
        await context.close()


async def _assert_inactive_workspace_deep_link_is_not_a_phantom(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """() => {
              window.__SEED__['/api/workspace/99'] = null;
              location.hash = 'workspace/99';
            }"""
        )
        await page.get_by_text("Workspace unavailable", exact=True).wait_for()
        screen = await page.locator("#screen").text_content() or ""
        assert "Project 99" not in screen
        assert await page.get_by_role("link", name="Open current workspace").count() == 1
        assert errors == []
    finally:
        await context.close()


async def _assert_global_runner_control_is_truthful_and_single_flight(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            """async () => {
              const { api } = await import('/static/app.js');
              window.__globalRunnerApi = api;
              window.__globalRunnerOriginalPost = api.post.bind(api);
              window.__globalRunnerOriginalStatus = api.runnerStatus.bind(api);
              window.__globalRunnerPostCalls = 0;
              window.__globalRunnerPostMode = 'http';
              window.__globalRunnerStatusMode = 'normal';
              window.__globalRunnerResolve = null;
              window.__globalRunnerStatusResolve = null;
              window.__globalApprovalSnapshot = null;
              window.__globalRunnerOriginalGet = api.get.bind(api);
              api.get = (path, options) => {
                if (path === '/api/approvals' && window.__globalApprovalSnapshot !== null) {
                  return Promise.resolve({
                    pending: [...window.__globalApprovalSnapshot]
                      .map(decision_id => ({ decision_id }))
                  });
                }
                return window.__globalRunnerOriginalGet(path, options);
              };
              api.runnerStatus = options => {
                if (window.__globalRunnerStatusMode === 'null') {
                  api.state.runnerStatusError = true;
                  return Promise.resolve(null);
                }
                if (window.__globalRunnerStatusMode === 'delay') {
                  return new Promise(resolve => { window.__globalRunnerStatusResolve = resolve; });
                }
                return window.__globalRunnerOriginalStatus(options);
              };
              api.post = (path, body) => {
                if (path !== '/api/runner/pause' && path !== '/api/runner/resume') {
                  return window.__globalRunnerOriginalPost(path, body);
                }
                window.__globalRunnerPostCalls += 1;
                if (window.__globalRunnerPostMode === 'throw') {
                  return Promise.reject(new TypeError('offline'));
                }
                if (window.__globalRunnerPostMode === 'delay') {
                  return new Promise(resolve => { window.__globalRunnerResolve = resolve; });
                }
                return Promise.resolve({
                  ok: false, status: 503, data: { message: 'runner unavailable' }
                });
              };
              window.__setGlobalRunner = patch => {
                Object.assign(window.__SEED__['/api/runner'], patch);
                window.__WB_SOCKET__._onmessage({
                  data: JSON.stringify({ kind: 'runner_state' })
                });
              };
            }"""
        )

        # A turn in another workspace and a background-only run both keep the global action in
        # shell chrome even though this chat's exact per-turn composer control remains absent.
        await page.evaluate(
            """() => window.__setGlobalRunner({
              runner_available: true, runner_running: true, turn_busy: false,
              global_turn_busy: true, background_busy: false
            })"""
        )
        await page.wait_for_function(
            "document.getElementById('st-runner').textContent.includes('another chat')"
        )
        assert await page.locator("#st-stop").is_visible()
        assert not await page.locator("#chat-turn-cancel").is_visible()
        assert await page.locator("#st-stop").get_attribute("aria-label") == (
            "Stop all chats and pause schedules"
        )
        await page.evaluate(
            """() => window.__setGlobalRunner({
              global_turn_busy: false, background_busy: true
            })"""
        )
        await page.wait_for_function(
            "document.getElementById('st-runner').textContent === 'Scheduled work is running'"
        )
        assert await page.locator("#st-stop").is_visible()

        # HTTP rejection and a thrown fetch both reconcile to the still-running snapshot without
        # inventing a paused state or leaving the single-flight lock/button stuck.
        await page.locator("#st-stop").click()
        await page.wait_for_function(
            """() => window.__globalRunnerPostCalls === 1
              && document.getElementById('runner-control-feedback').textContent
                .includes('Could not confirm Stop all')
              && !document.getElementById('st-stop').disabled"""
        )
        assert await page.locator("#st-stop").is_visible()
        assert not await page.locator("#st-resume").is_visible()
        await page.evaluate("window.__globalRunnerPostMode = 'throw'")
        await page.locator("#st-stop").click()
        await page.wait_for_function(
            """() => window.__globalRunnerPostCalls === 2
              && document.getElementById('runner-control-feedback').textContent
                .includes('Could not confirm Stop all')
              && !document.getElementById('st-stop').disabled"""
        )

        # The approval overlay owns focus/inertness, so it carries an accessible emergency copy.
        # Both copies feed one shell-level operation, which survives a route remount.
        await page.evaluate(
            """() => {
              window.__setGlobalRunner({
                runner_available: true, runner_running: true, turn_busy: true,
                global_turn_busy: true, background_busy: false, turn_id: 'global-stop-turn'
              });
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                type: 'approval', workspace_id: 'router-workspace', session_id: 5,
                project_id: 1, context_revision: 1, decision_id: 'global-stop-approval',
                kind: 'turn', tool: 'run_shell', input: { command: 'safe command' },
                reason: 'Needs confirmation', persistable: true
              }) });
            }"""
        )
        emergency = page.get_by_role(
            "button", name="Stop all chats and pause schedules", exact=True
        ).last
        await page.locator("#overlay.show").wait_for()
        assert await emergency.is_visible()
        inert_snapshot = await page.evaluate(
            """() => ({
              main: document.querySelector('main').inert,
              header: document.querySelector('header.status').inert,
              emergency: document.getElementById('ap-stop-all').inert,
              feedback: document.getElementById('runner-control-feedback').inert
            })"""
        )
        assert inert_snapshot == {
            "main": True,
            "header": True,
            "emergency": False,
            "feedback": False,
        }
        await page.evaluate("window.__globalRunnerPostMode = 'delay'")
        await emergency.click()
        await page.evaluate(
            """() => document.getElementById('st-stop').dispatchEvent(
              new MouseEvent('click', { bubbles: true }))"""
        )
        await page.wait_for_function("window.__globalRunnerPostCalls === 3")
        await page.wait_for_function(
            """() => document.getElementById('st-stop').disabled
              && document.getElementById('ap-stop-all').disabled
              && document.getElementById('st-stop').textContent === 'Stopping…'
              && document.getElementById('ap-stop-all').textContent === 'Stopping…'"""
        )
        await page.evaluate("location.hash = 'daily'")
        await page.wait_for_function("location.hash === '#daily'")
        assert await page.locator("#st-stop").text_content() == "Stopping…"
        assert await page.locator("#st-stop").is_disabled()

        await page.evaluate(
            """() => {
              Object.assign(window.__SEED__['/api/runner'], {
                runner_available: true, runner_running: false, turn_busy: false,
                turn_id: null, global_turn_busy: false, background_busy: false
              });
              window.__globalRunnerResolve({
                ok: true, status: 200, data: {
                  runner_available: true, runner_running: false, turn_busy: false,
                  turn_id: null, global_turn_busy: false, background_busy: false,
                  cancelled_turns: 2
                }
              });
            }"""
        )
        await page.wait_for_function(
            """() => !document.getElementById('overlay').classList.contains('show')
              && document.getElementById('st-resume').textContent === 'Resume schedules'
              && !document.getElementById('st-resume').classList.contains('is-hidden')
              && document.getElementById('daily-now-lead')?.textContent
                === 'Schedules are paused'"""
        )
        assert await page.locator("#runner-control-feedback").text_content() == (
            "Stopped 2 live chats. Schedules are paused."
        )
        assert await page.locator("#st-resume").get_attribute("title") == (
            "Resume schedules. Stopped chats stay stopped."
        )
        assert await page.evaluate("window.__globalRunnerPostCalls") == 3

        # Resume is a separate action with its own stable progress copy. While a new chat works
        # under paused schedules, starting Resume hides the opposite Stop action on desktop and
        # mobile for the entire reconciliation wait.
        await page.evaluate(
            """() => window.__setGlobalRunner({
              runner_available: true, runner_running: false, turn_busy: true,
              turn_id: 'resume-live-turn', global_turn_busy: true, background_busy: false
            })"""
        )
        await page.wait_for_function(
            """() => !document.getElementById('st-stop').classList.contains('is-hidden')
              && !document.getElementById('st-resume').classList.contains('is-hidden')"""
        )
        await page.evaluate(
            """() => {
              window.__globalRunnerPostMode = 'delay';
              window.__globalRunnerStatusMode = 'delay';
              document.getElementById('st-resume').click();
            }"""
        )
        await page.wait_for_function("window.__globalRunnerPostCalls === 4")
        await page.evaluate(
            """() => {
              Object.assign(window.__SEED__['/api/runner'], {
                runner_available: true, runner_running: true, turn_busy: true,
                turn_id: 'resume-live-turn', global_turn_busy: true, background_busy: false
              });
              window.__globalRunnerResolve({
                ok: true, status: 200, data: {
                  runner_available: true, runner_running: true, turn_busy: true,
                  turn_id: 'resume-live-turn', global_turn_busy: true, background_busy: false
                }
              });
            }"""
        )
        await page.wait_for_function(
            """() => window.__globalRunnerStatusResolve
              && document.getElementById('st-resume').textContent === 'Resuming…'"""
        )
        assert await page.locator("#st-resume").is_visible()
        assert await page.locator("#st-resume").is_disabled()
        assert not await page.locator("#st-stop").is_visible()
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.locator("#st-resume").is_visible()
        assert not await page.locator("#st-stop").is_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.evaluate(
            """() => window.__globalRunnerStatusResolve({
              ...window.__SEED__['/api/runner']
            })"""
        )
        await page.wait_for_function(
            """() => document.getElementById('runner-control-feedback').textContent
              === 'Schedules resumed. Stopped chats stay stopped.'"""
        )
        await page.set_viewport_size({"width": 1280, "height": 900})
        await page.evaluate(
            """() => {
              window.__globalRunnerStatusMode = 'normal';
              window.__globalRunnerPostMode = 'http';
            }"""
        )

        # Pausing schedules does not disable attended chat. If a new live turn starts, its work
        # copy wins and the independent Stop all and Resume schedules actions coexist.
        await page.evaluate(
            """() => window.__setGlobalRunner({
              runner_available: true, runner_running: false, turn_busy: true,
              turn_id: 'paused-live-turn', global_turn_busy: true, background_busy: false
            })"""
        )
        await page.wait_for_function(
            """() => document.getElementById('st-runner').textContent
                === 'Kairo is working in this chat'
              && document.getElementById('daily-now-lead')?.textContent === 'Kairo is working'
              && !document.getElementById('st-stop').classList.contains('is-hidden')
              && !document.getElementById('st-resume').classList.contains('is-hidden')"""
        )
        assert await page.locator("#st-stop").is_visible()
        assert await page.locator("#st-resume").is_visible()

        # No scheduler means no no-op Resume. A live chat is still stoppable process-wide, and its
        # busy copy outranks the scheduler-unavailable copy.
        await page.evaluate(
            """() => window.__setGlobalRunner({
              runner_available: false, runner_running: false, turn_busy: false,
              global_turn_busy: true, background_busy: false
            })"""
        )
        await page.wait_for_function(
            """() => document.getElementById('st-runner').textContent.includes('another chat')
              && document.getElementById('daily-now-lead')?.textContent
                .includes('another chat')"""
        )
        assert await page.locator("#st-stop").is_visible()
        assert not await page.locator("#st-resume").is_visible()

        # A terminal event for this exact context removes a scoped stale approval and releases
        # every background surface from inertness without touching other product subsystems.
        await page.evaluate(
            """() => {
              window.__setGlobalRunner({ turn_busy: true, turn_id: 'terminal-turn' });
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                type: 'approval', workspace_id: 'router-workspace', session_id: 5,
                project_id: 1, context_revision: 1, decision_id: 'terminal-approval',
                kind: 'turn', tool: 'write_file', input: {}, reason: 'confirm'
              }) });
            }"""
        )
        await page.locator("#overlay.show").wait_for()
        await page.evaluate(
            """() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
              kind: 'turn_cancelled', workspace_id: 'router-workspace', session_id: 5,
              project_id: 1, context_revision: 1
            }) })"""
        )
        await page.wait_for_function(
            """() => !document.getElementById('overlay').classList.contains('show')
              && !document.querySelector('main').inert
              && !document.querySelector('header.status').inert"""
        )

        # A new turn can start in the same workspace after cancelled chats settle while Stop all
        # is still draining scheduled work. Its newer busy state and approval survive the older
        # POST response even when reconciliation fails.
        await page.evaluate(
            """() => {
              window.__globalRunnerPostMode = 'delay';
              window.__setGlobalRunner({
                runner_available: true, runner_running: true, turn_busy: true,
                turn_id: 'same-authority-old', global_turn_busy: true, background_busy: true
              });
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                type: 'approval', workspace_id: 'router-workspace', session_id: 5,
                project_id: 1, context_revision: 1, decision_id: 'same-authority-old-approval',
                kind: 'turn', tool: 'run_shell', input: {}, reason: 'old turn'
              }) });
            }"""
        )
        await page.locator("#overlay.show").wait_for()
        await page.locator("#ap-stop-all").click()
        await page.wait_for_function("window.__globalRunnerPostCalls === 5")
        await page.evaluate(
            """() => {
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'turn_cancelled', workspace_id: 'router-workspace', session_id: 5,
                project_id: 1, context_revision: 1
              }) });
              window.__setGlobalRunner({
                runner_available: true, runner_running: true, turn_busy: true,
                turn_id: 'same-authority-new', global_turn_busy: true, background_busy: false
              });
            }"""
        )
        await page.wait_for_function(
            "window.__globalRunnerApi.state.runner?.turn_id === 'same-authority-new'"
        )
        await page.evaluate(
            """() => {
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                type: 'approval', workspace_id: 'router-workspace', session_id: 5,
                project_id: 1, context_revision: 1, decision_id: 'same-authority-new-approval',
                kind: 'turn', tool: 'write_file', input: {}, reason: 'new turn'
              }) });
              window.__globalRunnerStatusMode = 'null';
              window.__globalRunnerResolve({
                ok: true, status: 200, data: {
                  runner_available: true, runner_running: false, turn_busy: false,
                  turn_id: null, global_turn_busy: false, background_busy: false,
                  cancelled_turns: 1
                }
              });
            }"""
        )
        await page.wait_for_function(
            """() => document.getElementById('runner-control-feedback').textContent
                .includes('Current schedule status will refresh automatically')
              && !document.getElementById('ap-stop-all').disabled"""
        )
        same_authority = await page.evaluate(
            """() => ({
              statusError: window.__globalRunnerApi.state.runnerStatusError,
              turnBusy: window.__globalRunnerApi.state.runner?.turn_busy,
              turnId: window.__globalRunnerApi.state.runner?.turn_id,
              decision: document.getElementById('overlay').dataset.decision,
              runnerCopy: document.getElementById('st-runner').textContent,
              stopVisible: !document.getElementById('ap-stop-all').classList.contains('is-hidden')
            })"""
        )
        assert same_authority == {
            "statusError": True,
            "turnBusy": True,
            "turnId": "same-authority-new",
            "decision": "same-authority-new-approval",
            "runnerCopy": "Runner status is unavailable",
            "stopVisible": True,
        }
        await page.evaluate(
            """() => {
              window.__globalRunnerStatusMode = 'normal';
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'turn_cancelled', workspace_id: 'router-workspace', session_id: 5,
                project_id: 1, context_revision: 1
              }) });
              window.__setGlobalRunner({
                runner_available: true, runner_running: true, turn_busy: false,
                turn_id: null, global_turn_busy: false, background_busy: false
              });
            }"""
        )
        await page.wait_for_function(
            """() => !window.__globalRunnerApi.state.runnerStatusError
              && !document.getElementById('overlay').classList.contains('show')"""
        )

        # A shell operation survives a real workspace authority replacement, but its old scoped
        # response must not overwrite the replacement turn or clear a newly arrived approval when
        # the follow-up status read is unavailable.
        await page.evaluate(
            """() => {
              window.__globalRunnerPostMode = 'delay';
              window.__setGlobalRunner({
                runner_available: true, runner_running: true, turn_busy: true,
                turn_id: 'old-authority-turn', global_turn_busy: true, background_busy: false
              });
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                type: 'approval', workspace_id: 'router-workspace', session_id: 5,
                project_id: 1, context_revision: 1, decision_id: 'old-authority-approval',
                kind: 'turn', tool: 'run_shell', input: {}, reason: 'old authority'
              }) });
            }"""
        )
        await page.locator("#overlay.show").wait_for()
        await page.locator("#ap-stop-all").click()
        await page.wait_for_function("window.__globalRunnerPostCalls === 6")
        await page.evaluate(
            """() => {
              Object.assign(window.__SEED__['/api/runner'], {
                session_id: 9, context_revision: 2, project: { id: 1, name: 'Project 1' },
                runner_available: true, runner_running: true, turn_busy: true,
                turn_id: 'replacement-authority-turn', global_turn_busy: true,
                background_busy: false
              });
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'session_new', workspace_id: 'router-workspace', session_id: 9,
                project_id: 1, context_revision: 2
              }) });
            }"""
        )
        await page.wait_for_function(
            """() => window.__globalRunnerApi.state.context?.session_id === 9
              && window.__globalRunnerApi.state.runner?.turn_id
                === 'replacement-authority-turn'"""
        )
        await page.evaluate(
            """() => {
              window.__globalRunnerStatusMode = 'null';
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                type: 'approval', workspace_id: 'router-workspace', session_id: 9,
                project_id: 1, context_revision: 2, decision_id: 'replacement-approval',
                kind: 'turn', tool: 'write_file', input: {}, reason: 'replacement authority'
              }) });
              window.__globalRunnerResolve({
                ok: true, status: 200, data: {
                  runner_available: true, runner_running: false, turn_busy: false,
                  turn_id: null, global_turn_busy: false, background_busy: false,
                  cancelled_turns: 1
                }
              });
            }"""
        )
        await page.wait_for_function(
            """() => document.getElementById('runner-control-feedback').textContent
                .includes('Current schedule status will refresh automatically')
              && !document.getElementById('ap-stop-all').disabled"""
        )
        replacement = await page.evaluate(
            """() => ({
              turnBusy: window.__globalRunnerApi.state.runner?.turn_busy,
              turnId: window.__globalRunnerApi.state.runner?.turn_id,
              decision: document.getElementById('overlay').dataset.decision,
              overlay: document.getElementById('overlay').classList.contains('show'),
              mainInert: document.querySelector('main').inert
            })"""
        )
        assert replacement == {
            "turnBusy": True,
            "turnId": "replacement-authority-turn",
            "decision": "replacement-approval",
            "overlay": True,
            "mainInert": True,
        }
        await page.evaluate(
            """() => {
              window.__globalRunnerStatusMode = 'normal';
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'turn_cancelled', workspace_id: 'router-workspace', session_id: 9,
                project_id: 1, context_revision: 2
              }) });
            }"""
        )
        await page.wait_for_function(
            "!document.getElementById('overlay').classList.contains('show')"
        )

        # The same shell action remains reachable at the narrow supported viewport for scheduled
        # background work, with no horizontal spill.
        await page.evaluate(
            """() => window.__setGlobalRunner({
              runner_available: true, runner_running: true, turn_busy: false, turn_id: null,
              global_turn_busy: false, background_busy: true
            })"""
        )
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_function(
            "document.getElementById('st-runner').textContent === 'Scheduled work is running'"
        )
        assert await page.locator("#st-stop").is_visible()
        stop_box = await page.locator("#st-stop").bounding_box()
        assert stop_box is not None and stop_box["x"] + stop_box["width"] <= 390
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        # Chat terminal cleanup and the successful pause fallback are scoped away from voice and
        # provenance-ambiguous Studio subagents. The server read model confirms the Studio ASK is
        # still live; neither confirmation is hidden or silently denied by stopping Chat work.
        await page.evaluate(
            """() => {
              window.__globalApprovalSnapshot = new Set(['studio-subagent-approval']);
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                type: 'approval', workspace_id: 'router-workspace', session_id: 9,
                project_id: 1, context_revision: 2, decision_id: 'voice-scope-approval',
                kind: 'voice', tool: 'send_notification', input: {}, reason: 'voice confirm'
              }) });
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                type: 'approval', workspace_id: 'router-workspace', session_id: 9,
                project_id: 1, context_revision: 2, decision_id: 'studio-subagent-approval',
                kind: 'subagent', tool: 'write_file', input: {}, reason: 'Studio member confirm'
              }) });
              window.__WB_SOCKET__._onmessage({ data: JSON.stringify({
                kind: 'turn_cancelled', workspace_id: 'router-workspace', session_id: 9,
                project_id: 1, context_revision: 2
              }) });
            }"""
        )
        await page.wait_for_function(
            "document.getElementById('overlay').dataset.decision === 'voice-scope-approval'"
        )
        await page.evaluate("window.__globalRunnerPostMode = 'delay'")
        await page.locator("#ap-stop-all").click()
        await page.wait_for_function("window.__globalRunnerPostCalls === 7")
        await page.evaluate(
            """() => {
              Object.assign(window.__SEED__['/api/runner'], {
                runner_available: true, runner_running: false, turn_busy: false,
                turn_id: null, global_turn_busy: false, background_busy: false
              });
              window.__globalRunnerResolve({
                ok: true, status: 200, data: {
                  runner_available: true, runner_running: false, turn_busy: false,
                  turn_id: null, global_turn_busy: false, background_busy: false,
                  cancelled_turns: 0
                }
              });
            }"""
        )
        await page.wait_for_function(
            """() => document.getElementById('runner-control-feedback').textContent
                === 'No live chats needed stopping. Schedules are paused.'
              && !document.getElementById('ap-stop-all').disabled"""
        )
        voice_scope = await page.evaluate(
            """() => ({
              voicePending: window.__globalRunnerApi.state.pending.has('voice-scope-approval'),
              studioPending: window.__globalRunnerApi.state.pending
                .has('studio-subagent-approval'),
              decision: document.getElementById('overlay').dataset.decision,
              overlay: document.getElementById('overlay').classList.contains('show'),
              mainInert: document.querySelector('main').inert
            })"""
        )
        assert voice_scope == {
            "voicePending": True,
            "studioPending": True,
            "decision": "voice-scope-approval",
            "overlay": True,
            "mainInert": True,
        }
        assert errors == []
    finally:
        await context.close()


async def main() -> int:
    from playwright.async_api import async_playwright

    work = Path(tempfile.mkdtemp(prefix="router-dod-"))
    try:
        root = work / "site"
        shutil.copytree(STATIC_DIR, root / "static")
        index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        body = index.split("<body>")[-1].rsplit("<script", 1)[0]
        (root / "__wb.html").write_text(HARNESS.replace("%BODY%", body), encoding="utf-8")
        (root / "__wb_router.json").write_text(
            json.dumps(_seed_for("chat-fresh")), encoding="utf-8"
        )
        port = _free_port()
        handler = functools.partial(_QuietHandler, directory=str(root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    base = f"http://127.0.0.1:{port}"
                    checks = [
                        ("pending route", _assert_pending_route_is_fail_closed),
                        ("same-route newest read", _assert_newest_same_route_read_wins),
                        ("chat draft refresh", _assert_same_workspace_refresh_preserves_chat_draft),
                        ("transport retry", _assert_transport_failure_retries),
                        (
                            "initial render deadline",
                            _assert_never_settling_initial_render_times_out_and_retries,
                        ),
                        ("HTTP retry", _assert_unexpected_http_failure_retries),
                        ("render retry", _assert_render_exception_retries),
                        ("partial costs", _assert_partial_cost_failure_keeps_spend_and_retries),
                        ("optional gate", _assert_optional_gate_failure_keeps_attention),
                        ("refresh burst", _assert_async_refresh_burst_aborts_superseded_reads),
                        ("header newest read", _assert_latest_header_refresh_wins),
                        (
                            "header failed write",
                            _assert_failed_header_write_reconciles_authoritative_value,
                        ),
                        ("daily newest read", _assert_latest_daily_briefing_read_wins),
                        (
                            "project lifecycle",
                            _assert_project_lifecycle_rerenders_gate_and_redirects_workspace,
                        ),
                        (
                            "missed lifecycle",
                            _assert_any_runner_refresh_recovers_a_missed_lifecycle_frame,
                        ),
                        (
                            "runner workspace epoch",
                            _assert_runner_read_cannot_cross_workspace_epoch,
                        ),
                        (
                            "runner supersession",
                            _assert_runner_refresh_supersession_adopts_newest_read,
                        ),
                        (
                            "runner terminal settlement",
                            _assert_terminal_event_beats_an_older_busy_runner_snapshot,
                        ),
                        (
                            "workspace state clearing",
                            _assert_workspace_authority_clears_state_and_stale_hydration,
                        ),
                        ("session lifecycle", _assert_session_lifecycle_invalidates_runner_read),
                        ("daily remount", _assert_daily_refresh_reenables_after_remount),
                        ("upload authority", _assert_upload_batches_stop_at_authority_change),
                        (
                            "post-action refresh",
                            _assert_post_action_refresh_survives_same_key_refresh,
                        ),
                        ("settings authority", _assert_settings_cache_is_authority_scoped),
                        ("meeting remount", _assert_meeting_operation_recovers_on_remount),
                        ("studio confirmation", _assert_studio_confirmation_cannot_cross_authority),
                        (
                            "live chat ownership",
                            _assert_live_chat_wins_over_hydration_and_old_turn_response,
                        ),
                        (
                            "turn single-flight",
                            _assert_turn_admission_is_single_flight_and_restores_cross_route_draft,
                        ),
                        (
                            "resume reconciliation",
                            _assert_resume_chat_reconciles_without_ws_and_rejects_old_callbacks,
                        ),
                        (
                            "voice cancellation",
                            _assert_voice_permission_and_playback_are_authority_cancelled,
                        ),
                        (
                            "dictation navigation",
                            _assert_dictation_survives_same_authority_navigation,
                        ),
                        ("palette results", _assert_palette_results_cannot_cross_authority),
                        ("palette action", _assert_palette_action_callback_cannot_cross_authority),
                        ("daily authority", _assert_daily_refresh_is_authority_owned),
                        ("task history", _assert_task_history_is_route_and_authority_owned),
                        ("studio reads", _assert_studio_state_and_runs_are_authority_owned),
                        ("studio route API", _assert_studio_detail_actions_use_the_owned_route_api),
                        ("artifact state", _assert_artifact_refresh_preserves_owned_ui_state),
                        (
                            "vault single-flight",
                            _assert_vault_review_is_single_flight_in_both_surfaces,
                        ),
                        (
                            "vault detached settlement",
                            _assert_vault_success_refresh_survives_detached_gap,
                        ),
                        (
                            "report dialog",
                            _assert_report_dialog_survives_passive_render_and_traps_keys,
                        ),
                        (
                            "project navigation",
                            _assert_delayed_project_navigation_respects_newer_authority_and_intent,
                        ),
                        (
                            "workspace navigation",
                            _assert_workspace_chat_transition_cannot_navigate_a_newer_scope,
                        ),
                        (
                            "inactive workspace",
                            _assert_inactive_workspace_deep_link_is_not_a_phantom,
                        ),
                        (
                            "global runner control",
                            _assert_global_runner_control_is_truthful_and_single_flight,
                        ),
                    ]
                    case_filter = os.environ.get("ROUTER_DOD_CASE", "").strip().lower()
                    if case_filter:
                        checks = [item for item in checks if case_filter in item[0].lower()]
                        if not checks:
                            raise RuntimeError(f"no router DoD case matches {case_filter!r}")
                    for label, check in checks:
                        print(f"router-dod: {label}", flush=True)
                        await asyncio.wait_for(check(browser, base), timeout=45)
                finally:
                    await browser.close()
        finally:
            server.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(
        "GREEN: router loading, recovery, partial availability, ownership, cancellation, "
        "latest-read, lifecycle, and draft checks passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
