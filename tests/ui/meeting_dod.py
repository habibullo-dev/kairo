"""Browser-backed DoD for the truthful, consented short meeting-note workflow."""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import shutil
import tempfile
import threading
from pathlib import Path

from workbench_dod import HARNESS, _free_port, _QuietHandler, _seed_for

from jarvis.ui.server import STATIC_DIR

_INTERCEPT = r"""() => {
  window.__meetingPosts = [];
  window.__meetingMode = "deferred";
  window.__voiceStatusFails = false;
  window.__voiceStatusFailures = 0;
  window.__deferVoiceStatus = false;
  window.__voiceStatusDeferrals = 0;
  window.__voiceStatusResolvers = [];
  window.__meetingStateRevision = 0;
  window.__meetingRecordingRevision = 0;
  const originalFetch = window.fetch;
  window.fetch = (url, options = {}) => {
    const value = typeof url === "string" ? url : url.url;
    const path = value.split("?")[0].replace(location.origin, "");
    if (path === "/api/voice/status") {
      if (window.__voiceStatusFails) {
        window.__voiceStatusFailures += 1;
        return Promise.reject(new TypeError("voice status unavailable"));
      }
      if (window.__deferVoiceStatus || window.__voiceStatusDeferrals > 0) {
        window.__deferVoiceStatus = false;
        if (window.__voiceStatusDeferrals > 0) window.__voiceStatusDeferrals -= 1;
        return new Promise((resolve) => {
          window.__resolveVoiceStatus = resolve;
          window.__voiceStatusResolvers.push(resolve);
        });
      }
    }
    if (path !== "/api/voice/meeting") return originalFetch(url, options);
    window.__meetingPosts.push(JSON.parse(options.body || "{}"));
    if (window.__meetingMode === "network") {
      return Promise.reject(new TypeError("meeting transport unavailable"));
    }
    if (window.__meetingMode === "deferred") {
      return new Promise((resolve) => { window.__resolveMeeting = resolve; });
    }
    if (window.__meetingMode === "recovered") {
      return Promise.resolve(new Response(JSON.stringify({
        ok: true, review_status: "unreviewed", source_id: 92,
        title: "Recovered note", index_state: "ready", source_status: "live",
      }), { status: 200, headers: { "content-type": "application/json" } }));
    }
    const status = window.__meetingMode === "busy"
      ? 409
      : (window.__meetingMode === "unavailable" ? 503 : 422);
    const message = status === 409
      ? "busy"
      : (status === 503 ? "Meeting-note capture is unavailable" : "No speech was detected");
    return Promise.resolve(new Response(JSON.stringify({ ok: false, message }), {
      status,
      headers: { "content-type": "application/json" },
    }));
  };
}"""


async def _open_page(browser: object, base: str) -> tuple[object, object, list[str]]:
    context = await browser.new_context(viewport={"width": 390, "height": 720})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{base}/__wb.html?state=meetings&theme=noir", wait_until="load")
    await page.wait_for_function("window.__READY__ === true")
    await page.evaluate(_INTERCEPT)
    return context, page, errors


async def _meeting_state(page: object, state: str) -> None:
    await page.evaluate(
        "state => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
        "kind: 'meeting_state', state, revision: ++window.__meetingStateRevision }) })",
        state,
    )


async def _meeting_recording(page: object, active: bool) -> None:
    await page.evaluate(
        "active => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
        "kind: 'meeting_recording', active, "
        "epoch: 'workbench-process', "
        "revision: ++window.__meetingRecordingRevision }) })",
        active,
    )


async def _assert_success_and_single_flight(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        button = page.locator("#mtg-start")
        consent = page.locator("#mtg-consent")
        assert await button.is_disabled()
        assert await consent.get_attribute("type") == "checkbox"
        await consent.check()
        assert await button.is_enabled()
        await page.locator("#mtg-title").fill("Design sync")

        await page.evaluate(
            "() => { const button = document.getElementById('mtg-start'); "
            "button.click(); button.click(); }"
        )
        await page.wait_for_function("window.__meetingPosts.length === 1")
        assert await button.is_disabled()
        assert await page.locator("#mtg-state").text_content() == (
            "Checking the capture receipt and preparing audio…"
        )
        assert await page.locator("#mtg-out").text_content() == (
            "Checking for a saved result first. Kairo will show Listening only if the "
            "microphone opens."
        )
        assert not await page.locator("#rec-dot").evaluate(
            "node => node.classList.contains('show')"
        )
        assert not await page.locator("#mtg-state-dot").evaluate(
            "node => node.classList.contains('meeting-recording')"
        )
        assert await page.locator("#mtg-state-dot").evaluate(
            "node => node.classList.contains('busy')"
        )
        assert await page.locator("#mtg-controls").get_attribute("aria-busy") == "true"
        assert not await page.locator("#mtg-state").evaluate(
            "node => Boolean(node.closest('[aria-busy=true]'))"
        )
        assert not await page.locator("#mtg-out").evaluate(
            "node => Boolean(node.closest('[aria-busy=true]'))"
        )
        assert await page.locator("#mtg-state").get_attribute("role") is None
        assert await page.locator("#mtg-state").get_attribute("aria-live") is None
        assert await page.evaluate("window.__meetingPosts[0].title") == "Design sync"
        assert await page.evaluate("window.__meetingPosts[0].consent") is True
        capture_id = await page.evaluate("window.__meetingPosts[0].capture_id")
        assert isinstance(capture_id, str) and len(capture_id) == 36

        await _meeting_state(page, "recording")
        assert await page.locator("#mtg-state").text_content() == (
            "Listening through the workstation microphone…"
        )
        assert not await page.locator("#rec-dot").evaluate(
            "node => node.classList.contains('show')"
        )
        await _meeting_recording(page, True)
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        assert await page.locator(".mobile-more > summary [data-meeting-rec-dot]").is_visible()
        assert await page.locator("#meeting-recording-status").text_content() == (
            "Workstation microphone is recording a meeting note."
        )
        await page.evaluate("window.__voiceStatusFails = true")
        await page.wait_for_timeout(4200)
        assert await page.evaluate("window.__voiceStatusFailures") > 0
        assert await page.locator("#mtg-state").text_content() == (
            "Listening through the workstation microphone…"
        )
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        await page.evaluate("window.__voiceStatusFails = false")
        await _meeting_recording(page, False)
        assert await page.locator("#meeting-recording-status").text_content() == (
            "Workstation microphone closed."
        )
        assert not await page.locator("#rec-dot").evaluate(
            "node => node.classList.contains('show')"
        )
        await _meeting_state(page, "transcribing")
        assert await page.locator("#mtg-state").text_content() == "Transcribing the captured note…"
        assert await page.locator("#meeting-recording-status").text_content() == (
            "Workstation microphone closed."
        )
        await _meeting_state(page, "saving")
        assert await page.locator("#mtg-state").text_content() == (
            "Saving the transcript for review…"
        )
        assert await page.locator("#meeting-recording-status").text_content() == (
            "Workstation microphone closed."
        )
        await _meeting_state(page, "idle")

        await page.evaluate(
            "window.__resolveMeeting(new Response(JSON.stringify({ "
            "ok: true, review_status: 'unreviewed', source_id: 91, title: 'Design sync' "
            "}), { status: 200, headers: { 'content-type': 'application/json' } }))"
        )
        await page.wait_for_function(
            "document.getElementById('mtg-out').textContent.includes('source #91')"
        )
        assert await page.locator("#mtg-out a").get_attribute("href") == "#vault"
        assert not await consent.is_checked()
        assert await button.is_disabled()
        assert await page.locator("#mtg-controls").get_attribute("aria-busy") == "false"
        assert not await page.locator("#mtg-state").evaluate(
            "node => Boolean(node.closest('[aria-busy=true]'))"
        )
        assert not await page.locator("#mtg-out").evaluate(
            "node => Boolean(node.closest('[aria-busy=true]'))"
        )
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await button.scroll_into_view_if_needed()
        assert await button.is_visible()
        assert errors == []
    finally:
        await context.close()


async def _assert_failures_recover(browser: object, base: str) -> None:
    for mode, expected in (
        ("busy", "Another voice action is active"),
        ("empty", "No speech was detected"),
        ("unavailable", "capture is unavailable"),
        ("network", "before Kairo could confirm"),
    ):
        context, page, errors = await _open_page(browser, base)
        try:
            await page.evaluate("mode => { window.__meetingMode = mode; }", mode)
            await page.locator("#mtg-consent").check()
            await page.locator("#mtg-start").click()
            await page.wait_for_function(
                "expected => document.getElementById('mtg-out').textContent.includes(expected)",
                arg=expected,
            )
            assert await page.evaluate("window.__meetingPosts.length") == 1
            if mode == "network":
                assert not await page.locator("#mtg-consent").is_checked()
                assert await page.locator("#mtg-start").is_disabled()
                assert await page.locator("#mtg-state").text_content() == (
                    "The last capture result is unconfirmed"
                )
                first_id = await page.evaluate("window.__meetingPosts[0].capture_id")
                # A new chat in the same project must reconcile the project-scoped receipt,
                # not rotate identity and reopen the microphone.
                await page.evaluate(
                    "async () => { const { api } = await import('/static/app.js'); "
                    "api.state.context = { session_id: 6, project_id: 1 }; }"
                )
                await page.evaluate("window.__meetingMode = 'recovered'")
                await page.locator("#mtg-consent").check()
                await page.locator("#mtg-start").click()
                await page.wait_for_function("window.__meetingPosts.length === 2")
                await page.wait_for_function(
                    "document.getElementById('mtg-out').textContent.includes('source #92')"
                )
                assert await page.evaluate("window.__meetingPosts[1].capture_id") == first_id
                assert await page.locator("#mtg-controls").get_attribute("aria-busy") == "false"

                # A confirmed durable source clears the receipt; the next intentional capture
                # gets a fresh identity rather than overwriting/replaying the prior note.
                await page.evaluate("window.__meetingMode = 'deferred'")
                await page.locator("#mtg-consent").check()
                await page.locator("#mtg-start").click()
                await page.wait_for_function("window.__meetingPosts.length === 3")
                assert await page.evaluate("window.__meetingPosts[2].capture_id") != first_id
                assert await page.locator("#mtg-controls").get_attribute("aria-busy") == "true"
            elif mode == "busy":
                # A busy refusal happens before this request opens the microphone, so the same
                # explicit consent can be retried once the other physical capture closes.
                assert await page.locator("#mtg-consent").is_checked()
                assert await page.locator("#mtg-start").is_enabled()
                assert await page.locator("#mtg-controls").get_attribute("aria-busy") == "false"
            else:
                # A settled no-speech/provider response may follow actual audio capture; require
                # fresh consent even though the durable receipt identity remains available.
                assert not await page.locator("#mtg-consent").is_checked()
                assert await page.locator("#mtg-start").is_disabled()
                assert await page.locator("#mtg-controls").get_attribute("aria-busy") == "false"
            assert errors == []
        finally:
            await context.close()


async def _assert_stale_success_cannot_hide_recording(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate("window.__deferVoiceStatus = true")
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "kind: 'orchestration_completed' }) })"
        )
        await page.wait_for_function("typeof window.__resolveVoiceStatus === 'function'")
        await _meeting_state(page, "recording")
        await _meeting_recording(page, True)
        await page.evaluate(
            "window.__resolveVoiceStatus(new Response(JSON.stringify({ "
            "enabled: true, listening: 'idle', meeting: 'idle', meeting_available: true, "
            "meeting_revision: 0, meeting_recording: false, "
            "meeting_recording_epoch: 'workbench-process', "
            "meeting_recording_revision: 0, meeting_reason: '', reason: '', stt: 'local', "
            "playback: false "
            "}), { status: 200, headers: { 'content-type': 'application/json' } }))"
        )
        await page.wait_for_timeout(100)
        assert await page.locator("#mtg-state").text_content() == (
            "Listening through the workstation microphone…"
        )
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        assert await page.locator(".mobile-more > summary [data-meeting-rec-dot]").is_visible()
        assert await page.locator("#meeting-recording-status").text_content() == (
            "Workstation microphone is recording a meeting note."
        )
        await _meeting_state(page, "idle")
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        await _meeting_recording(page, False)
        assert errors == []
    finally:
        await context.close()


async def _assert_stale_poll_cannot_corrupt_concurrent_render(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate("window.__voiceStatusDeferrals = 2")
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "kind: 'orchestration_completed' }) })"
        )
        await page.wait_for_function("window.__voiceStatusResolvers.length === 1")
        await _meeting_state(page, "recording")
        await _meeting_recording(page, True)
        await page.evaluate(
            "async () => { const meetings = await import('/static/screens/meetings.js'); "
            "const { api } = await import('/static/app.js'); "
            "window.__meetingRenderPromise = meetings.render("
            "document.getElementById('screen'), api); }"
        )
        await page.wait_for_function("window.__voiceStatusResolvers.length === 2")

        # This poll began before the recording frame and is stale. Its provider fields remain
        # useful, but its raw meeting=idle value must be sanitized before screen fan-out.
        await page.evaluate(
            "window.__voiceStatusResolvers[0](new Response(JSON.stringify({ "
            "enabled: true, listening: 'idle', meeting: 'idle', meeting_available: true, "
            "meeting_revision: 0, meeting_recording: false, "
            "meeting_recording_epoch: 'workbench-process', "
            "meeting_recording_revision: 0, meeting_reason: '', reason: '', stt: 'local', "
            "playback: false "
            "}), { status: 200, headers: { 'content-type': 'application/json' } }))"
        )
        await page.wait_for_timeout(50)
        await page.evaluate(
            "window.__voiceStatusResolvers[1](new Response(JSON.stringify({ "
            "enabled: true, listening: 'idle', meeting: 'recording', meeting_available: true, "
            "meeting_revision: 1, meeting_recording: true, "
            "meeting_recording_epoch: 'workbench-process', "
            "meeting_recording_revision: 1, meeting_reason: '', reason: '', stt: 'local', "
            "playback: false "
            "}), { status: 200, headers: { 'content-type': 'application/json' } }))"
        )
        await page.evaluate("window.__meetingRenderPromise")
        assert await page.locator("#mtg-state").text_content() == (
            "Listening through the workstation microphone…"
        )
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        await _meeting_state(page, "idle")
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        await _meeting_recording(page, False)
        assert errors == []
    finally:
        await context.close()


async def _assert_late_http_cannot_hide_newer_recording(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.locator("#mtg-consent").check()
        await page.locator("#mtg-start").click()
        await page.wait_for_function("window.__meetingPosts.length === 1")
        await _meeting_state(page, "recording")
        await _meeting_recording(page, True)
        await _meeting_recording(page, False)
        for phase in ("transcribing", "saving", "idle"):
            await _meeting_state(page, phase)

        # Another workspace can now open the shared physical microphone while this workspace's
        # older request is completing. Its global privacy signal is visible here, but its local
        # workflow phase is intentionally not delivered to this Meetings screen.
        await _meeting_recording(page, True)

        # Request A's response arrives after request B in another tab has opened the mic. HTTP
        # completion must not infer idle and overwrite B's newer authoritative lifecycle frame.
        await page.evaluate(
            "window.__resolveMeeting(new Response(JSON.stringify({ "
            "ok: true, review_status: 'unreviewed', source_id: 93, title: 'Older note', "
            "index_state: 'ready', source_status: 'live' "
            "}), { status: 200, headers: { 'content-type': 'application/json' } }))"
        )
        await page.wait_for_function(
            "document.getElementById('mtg-out').textContent.includes('source #93')"
        )
        assert await page.locator("#mtg-state").text_content() == ("Ready for a short spoken note")
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        assert await page.locator("#mtg-controls").get_attribute("aria-busy") == "false"
        await _meeting_recording(page, False)
        assert errors == []
    finally:
        await context.close()


async def _assert_capability_and_context_recover(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        status = {
            "enabled": True,
            "listening": "idle",
            "meeting": "idle",
            "meeting_recording": False,
            "meeting_recording_epoch": "workbench-process",
            "meeting_revision": 0,
            "meeting_recording_revision": 0,
            "meeting_available": True,
            "meeting_reason": "",
            "reason": "",
            "stt": "local",
            "playback": False,
        }
        await page.evaluate(
            "async status => { const { api } = await import('/static/app.js'); "
            "const { emit } = await import('/static/ui/bus.js'); "
            "api.state.context = null; emit('voice_status', { status }); }",
            status,
        )
        await page.locator("#mtg-consent").check()
        assert await page.locator("#mtg-start").is_disabled()
        assert "Waiting for the authenticated workspace context" in (
            await page.locator("#mtg-availability").text_content()
        )

        await page.evaluate(
            "async status => { const { api } = await import('/static/app.js'); "
            "const { emit } = await import('/static/ui/bus.js'); "
            "api.state.context = { session_id: 5, project_id: 1 }; "
            "emit('voice_status', { status }); }",
            status,
        )
        assert await page.locator("#mtg-start").is_enabled()

        await page.evaluate(
            "async () => { const { emit } = await import('/static/ui/bus.js'); "
            "emit('voice_status', { status: null }); }"
        )
        assert await page.locator("#mtg-start").is_disabled()
        assert "temporarily unavailable" in (await page.locator("#mtg-availability").text_content())
        assert "Provider status is unavailable" in (
            await page.locator("#mtg-privacy").text_content()
        )

        await page.evaluate(
            "async status => { const { emit } = await import('/static/ui/bus.js'); "
            "emit('voice_status', { status }); }",
            status,
        )
        assert await page.locator("#mtg-start").is_enabled()
        assert errors == []
    finally:
        await context.close()


async def _assert_late_render_cannot_replace_newer_route(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate("location.hash = '#chat'")
        await page.wait_for_selector("#chat-input")
        await page.evaluate(
            "() => { window.__deferVoiceStatus = true; location.hash = '#meetings'; }"
        )
        await page.wait_for_function("window.__voiceStatusResolvers.length === 1")
        assert await page.locator("#chat-input").count() == 0
        assert await page.locator("#mtg-start").is_disabled()
        assert await page.locator("#mtg-availability").text_content() == (
            "Checking voice provider and microphone availability…"
        )
        await page.evaluate("location.hash = '#chat'")
        await page.wait_for_selector("#chat-input")
        await page.evaluate(
            "window.__voiceStatusResolvers[0](new Response(JSON.stringify({ "
            "enabled: true, listening: 'idle', meeting: 'idle', meeting_recording: false, "
            "meeting_recording_epoch: 'workbench-process', "
            "meeting_revision: 0, meeting_recording_revision: 0, meeting_available: true, "
            "meeting_reason: '', reason: '', stt: 'local', "
            "playback: false "
            "}), { status: 200, headers: { 'content-type': 'application/json' } }))"
        )
        await page.wait_for_timeout(100)
        assert await page.locator("#chat-input").is_visible()
        assert await page.locator("#mtg-start").count() == 0
        assert await page.evaluate("location.hash") == "#chat"
        assert errors == []
    finally:
        await context.close()


async def _assert_newer_screen_status_wins_over_older_poll(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate("window.__voiceStatusDeferrals = 2")
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "kind: 'orchestration_completed' }) })"
        )
        await page.wait_for_function("window.__voiceStatusResolvers.length === 1")
        await page.evaluate(
            "async () => { const meetings = await import('/static/screens/meetings.js'); "
            "const { api } = await import('/static/app.js'); "
            "window.__meetingRenderPromise = meetings.render("
            "document.getElementById('screen'), api); }"
        )
        await page.wait_for_function("window.__voiceStatusResolvers.length === 2")

        # P began first and returns an older snapshot. R began later for the visible Meetings
        # screen and must own both its local workflow and the global microphone chrome.
        await page.evaluate(
            "window.__voiceStatusResolvers[0](new Response(JSON.stringify({ "
            "enabled: true, listening: 'idle', meeting: 'idle', meeting_revision: 0, "
            "meeting_recording: false, meeting_recording_epoch: 'workbench-process', "
            "meeting_recording_revision: 0, "
            "meeting_available: true, meeting_reason: '', reason: '', stt: 'local', "
            "playback: false "
            "}), { status: 200, headers: { 'content-type': 'application/json' } }))"
        )
        await page.evaluate(
            "window.__voiceStatusResolvers[1](new Response(JSON.stringify({ "
            "enabled: true, listening: 'idle', meeting: 'recording', meeting_revision: 1, "
            "meeting_recording: true, meeting_recording_epoch: 'workbench-process', "
            "meeting_recording_revision: 1, "
            "meeting_available: true, meeting_reason: '', reason: '', stt: 'local', "
            "playback: false "
            "}), { status: 200, headers: { 'content-type': 'application/json' } }))"
        )
        await page.evaluate("window.__meetingRenderPromise")
        assert await page.locator("#mtg-state").text_content() == (
            "Listening through the workstation microphone…"
        )
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        assert errors == []
    finally:
        await context.close()


async def _assert_replacement_workspace_clears_local_meeting_phase(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "type: 'workspace', workspace_id: 'workbench', "
            "meeting_recording_epoch: 'workbench-process', session_id: 5, project_id: 1, "
            "context_revision: 1 "
            "}) })"
        )
        await page.wait_for_timeout(100)
        await _meeting_state(page, "recording")
        assert await page.locator("#mtg-state").text_content() == (
            "Listening through the workstation microphone…"
        )
        assert not await page.locator("#rec-dot").evaluate(
            "node => node.classList.contains('show')"
        )
        await page.evaluate("window.__voiceStatusDeferrals = 2")
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "type: 'workspace', workspace_id: 'replacement-workspace-id-0001', "
            "meeting_recording_epoch: 'workbench-process', session_id: 88, project_id: 2, "
            "context_revision: 1 "
            "}) })"
        )
        await page.wait_for_function("window.__voiceStatusResolvers.length >= 1")
        assert await page.locator("#mtg-state").text_content() == ("Ready for a short spoken note")
        assert await page.locator("#mtg-availability").text_content() == (
            "Checking voice provider and microphone availability…"
        )
        assert await page.locator("#mtg-start").is_disabled()
        assert errors == []
    finally:
        await context.close()


async def _assert_handshake_cannot_reopen_stale_global_revision(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate("window.__voiceStatusFails = true")
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "kind: 'meeting_recording', active: true, epoch: 'workbench-process', "
            "revision: 10 }) })"
        )
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "type: 'workspace', workspace_id: 'workbench', "
            "meeting_recording_epoch: 'workbench-process', session_id: 5, project_id: 1, "
            "context_revision: 1 "
            "}) })"
        )
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "kind: 'meeting_recording', active: false, epoch: 'workbench-process', "
            "revision: 9 }) })"
        )
        await page.wait_for_timeout(100)
        assert await page.locator("#rec-dot").evaluate("node => node.classList.contains('show')")
        assert await page.locator("#meeting-recording-status").text_content() == (
            "Workstation microphone is recording a meeting note."
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_same_workspace_handshake_preserves_local_revision(
    browser: object, base: str
) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.evaluate("window.__voiceStatusFails = true")
        workspace_frame = (
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "type: 'workspace', workspace_id: 'workbench', "
            "meeting_recording_epoch: 'workbench-process', session_id: 5, project_id: 1, "
            "context_revision: 1 "
            "}) })"
        )
        await page.evaluate(workspace_frame)
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "kind: 'meeting_state', state: 'recording', revision: 10, "
            "workspace_id: 'workbench', session_id: 5, project_id: 1, "
            "context_revision: 1 }) })"
        )
        assert await page.locator("#mtg-state").text_content() == (
            "Listening through the workstation microphone…"
        )
        await page.evaluate(workspace_frame)
        await page.evaluate(
            "() => window.__WB_SOCKET__._onmessage({ data: JSON.stringify({ "
            "kind: 'meeting_state', state: 'idle', revision: 9, "
            "workspace_id: 'workbench', session_id: 5, project_id: 1, "
            "context_revision: 1 }) })"
        )
        await page.wait_for_timeout(100)
        assert await page.locator("#mtg-state").text_content() == (
            "Listening through the workstation microphone…"
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_invalid_canonical_receipt_falls_back(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        legacy_id = "11111111-1111-4111-8111-111111111111"
        await page.evaluate(
            "id => { sessionStorage.setItem('kira:meeting-capture:project-1', 'invalid'); "
            "sessionStorage.setItem('kairo:meeting-capture:project-1', id); }",
            legacy_id,
        )
        await page.evaluate("window.__meetingMode = 'network'")
        await page.locator("#mtg-consent").check()
        await page.locator("#mtg-start").click()
        await page.wait_for_function(
            "document.getElementById('mtg-out').textContent.includes('before Kairo could confirm')"
        )
        first_id = await page.evaluate("window.__meetingPosts[0].capture_id")
        assert first_id == legacy_id
        assert (
            await page.evaluate("sessionStorage.getItem('kira:meeting-capture:project-1')")
            == first_id
        )
        assert (
            await page.evaluate("sessionStorage.getItem('kairo:meeting-capture:project-1')")
            == first_id
        )
        assert errors == []
    finally:
        await context.close()


async def _assert_receipt_survives_reload(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        canonical_id = "22222222-2222-4222-8222-222222222222"
        stale_legacy_id = "33333333-3333-4333-8333-333333333333"
        await page.evaluate(
            "ids => { sessionStorage.setItem('kira:meeting-capture:project-1', ids[0]); "
            "sessionStorage.setItem('kairo:meeting-capture:project-1', ids[1]); }",
            [canonical_id, stale_legacy_id],
        )
        await page.evaluate("window.__meetingMode = 'network'")
        await page.locator("#mtg-consent").check()
        await page.locator("#mtg-start").click()
        await page.wait_for_function(
            "document.getElementById('mtg-out').textContent.includes('before Kairo could confirm')"
        )
        first_id = await page.evaluate("window.__meetingPosts[0].capture_id")
        assert first_id == canonical_id
        assert (
            await page.evaluate("sessionStorage.getItem('kira:meeting-capture:project-1')")
            == canonical_id
        )
        assert (
            await page.evaluate("sessionStorage.getItem('kairo:meeting-capture:project-1')")
            == canonical_id
        )

        await page.reload(wait_until="load")
        await page.wait_for_function("window.__READY__ === true")
        await page.evaluate(_INTERCEPT)
        await page.evaluate("window.__meetingMode = 'recovered'")
        await page.locator("#mtg-consent").check()
        await page.locator("#mtg-start").click()
        await page.wait_for_function("window.__meetingPosts.length === 1")
        await page.wait_for_function(
            "document.getElementById('mtg-out').textContent.includes('source #92')"
        )
        assert await page.evaluate("window.__meetingPosts[0].capture_id") == first_id
        assert (
            await page.evaluate("sessionStorage.getItem('kira:meeting-capture:project-1')") is None
        )
        assert (
            await page.evaluate("sessionStorage.getItem('kairo:meeting-capture:project-1')") is None
        )
        assert errors == []
    finally:
        await context.close()


async def main() -> int:
    from playwright.async_api import async_playwright

    work = Path(tempfile.mkdtemp(prefix="meeting-dod-"))
    try:
        root = work / "site"
        shutil.copytree(STATIC_DIR, root / "static")
        index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        body = index.split("<body>")[-1].rsplit("<script", 1)[0]
        (root / "__wb.html").write_text(HARNESS.replace("%BODY%", body), encoding="utf-8")
        (root / "__wb_meetings.json").write_text(
            json.dumps(_seed_for("meetings")), encoding="utf-8"
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
                    await _assert_success_and_single_flight(browser, base)
                    await _assert_failures_recover(browser, base)
                    await _assert_stale_success_cannot_hide_recording(browser, base)
                    await _assert_stale_poll_cannot_corrupt_concurrent_render(browser, base)
                    await _assert_late_http_cannot_hide_newer_recording(browser, base)
                    await _assert_capability_and_context_recover(browser, base)
                    await _assert_late_render_cannot_replace_newer_route(browser, base)
                    await _assert_newer_screen_status_wins_over_older_poll(browser, base)
                    await _assert_replacement_workspace_clears_local_meeting_phase(browser, base)
                    await _assert_handshake_cannot_reopen_stale_global_revision(browser, base)
                    await _assert_same_workspace_handshake_preserves_local_revision(browser, base)
                    await _assert_invalid_canonical_receipt_falls_back(browser, base)
                    await _assert_receipt_survives_reload(browser, base)
                finally:
                    await browser.close()
        finally:
            server.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(
        "GREEN: meeting consent, lifecycle, receipt, recovery, source, failure, and mobile "
        "checks passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
