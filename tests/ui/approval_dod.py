"""Browser-backed DoD for the nonce-bound attended approval dialog.

Run with ``uv run python tests/ui/approval_dod.py``. The harness imports the shipped shell and
stubs only its transport boundaries so the checks exercise the real approval DOM/controller.
"""

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


async def _open_page(browser: object, base: str) -> tuple[object, object, list[str]]:
    context = await browser.new_context(viewport={"width": 1024, "height": 768})
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(f"{base}/__wb.html?state=chat-fresh&theme=noir", wait_until="load")
    await page.wait_for_function("window.__READY__ === true")
    await page.evaluate(
        r"""() => {
          const socket = window.__WB_SOCKET__;
          socket.readyState = 1;
          window.__approvalPosts = [];
          window.__approvalGets = 0;
          window.__socketMessages = [];
          window.__approvalFetchMode = "success";
          window.__approvalStillPending = true;
          window.__serverPending = new Set();
          socket.send = (raw) => window.__socketMessages.push(JSON.parse(raw));
          const originalFetch = window.fetch;
          window.fetch = (url, options = {}) => {
            const value = typeof url === "string" ? url : url.url;
            const path = value.split("?")[0].replace(location.origin, "");
            if (/^\/api\/approvals\/[^/]+\/resolve$/.test(path)) {
              window.__approvalPosts.push({ path, body: JSON.parse(options.body || "{}") });
              if (window.__approvalFetchMode === "deferred-network") {
                return new Promise((_resolve, reject) => { window.__rejectApproval = reject; });
              }
              if (window.__approvalFetchMode === "network") {
                return Promise.reject(new TypeError("approval transport unavailable"));
              }
              const ok = window.__approvalFetchMode === "success";
              if (ok) window.__serverPending.delete(path.split("/")[3]);
              const body = ok
                ? { ok: true, message: "resolved" }
                : { ok: false, message: "invalid or replayed nonce" };
              return Promise.resolve(new Response(JSON.stringify(body), {
                status: ok ? 200 : 409,
                headers: { "content-type": "application/json" },
              }));
            }
            if (path === "/api/approvals") {
              window.__approvalGets += 1;
              const pending = window.__approvalStillPending
                ? [...window.__serverPending].map((decision_id) => ({ decision_id }))
                : [];
              return Promise.resolve(new Response(JSON.stringify({ pending }), {
                status: 200,
                headers: { "content-type": "application/json" },
              }));
            }
            return originalFetch(url, options);
          };
          socket._onopen();
          socket._onmessage({ data: JSON.stringify({
            type: "workspace", workspace_id: "approval-dod", session_id: 5, project_id: 1,
          }) });
          document.getElementById("rail-search").focus();
        }"""
    )
    return context, page, errors


async def _message(page: object, message: dict[str, object]) -> None:
    if message.get("type") == "approval":
        await page.evaluate(
            "decisionId => window.__serverPending.add(decisionId)", message["decision_id"]
        )
    await page.evaluate(
        "message => window.__WB_SOCKET__._onmessage({ data: JSON.stringify(message) })", message
    )


def _approval(decision_id: str) -> dict[str, object]:
    return {
        "type": "approval",
        "decision_id": decision_id,
        "kind": "turn",
        "tool": "run_shell",
        "input": {"command": "printf safe", "arguments": [f"item-{i}" for i in range(80)]},
        "reason": "The model requested a command.",
        "persistable": True,
        "session_id": 5,
        "project_id": 1,
    }


def _nonce(decision_id: str, value: str) -> dict[str, object]:
    return {
        "type": "approval_nonce",
        "decision_id": decision_id,
        "nonce": value,
        "session_id": 5,
        "project_id": 1,
    }


async def _assert_pre_nonce_and_focus(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await page.set_viewport_size({"width": 390, "height": 600})
        await page.locator('[data-screen="chat"]').focus()
        await _message(page, _approval("pre-nonce"))
        await page.wait_for_selector("#overlay.show")
        assert await page.evaluate("document.activeElement.id") == "approval-dialog"
        assert await page.evaluate(
            "document.querySelector('nav').inert && document.querySelector('main').inert"
        )
        assert await page.locator("#overlay button:disabled").count() == 3
        await page.evaluate("document.getElementById('ap-deny').click()")
        assert await page.evaluate("window.__approvalPosts.length") == 0

        await page.locator("#ap-details summary").click()
        assert await page.locator(".approve-card .bd").evaluate(
            "node => node.scrollHeight > node.clientHeight"
        )
        assert await page.locator(".approve-card .actions").evaluate(
            "node => { const r = node.getBoundingClientRect(); "
            "return r.top >= 0 && r.bottom <= innerHeight; }"
        )

        for _ in range(6):
            await page.keyboard.press("Tab")
            assert await page.evaluate(
                "document.getElementById('approval-dialog').contains(document.activeElement)"
            )
        await page.keyboard.press("Shift+Tab")
        assert await page.evaluate(
            "document.getElementById('approval-dialog').contains(document.activeElement)"
        )
        await page.wait_for_selector("#ap-retry:not([hidden])", timeout=8000)
        before_gets = await page.evaluate("window.__approvalGets")
        await page.evaluate(
            "() => { const retry = document.getElementById('ap-retry'); "
            "retry.click(); retry.click(); }"
        )
        await page.wait_for_function("document.getElementById('ap-retry').hidden")
        await page.wait_for_function("before => window.__approvalGets > before", arg=before_gets)
        assert await page.evaluate("window.__approvalGets") == before_gets + 1
        await page.wait_for_function(
            "window.__socketMessages.filter(m => m.type === 'approval_shown').length >= 2"
        )
        await page.keyboard.press("Escape")
        assert not await page.locator("#overlay").evaluate(
            "node => node.classList.contains('show')"
        )
        assert not await page.evaluate(
            "document.querySelector('nav').inert || document.querySelector('main').inert"
        )
        assert await page.evaluate("document.activeElement.dataset.screen") == "chat"
        assert errors == []
    finally:
        await context.close()


async def _assert_rejected_and_network_failures(browser: object, base: str) -> None:
    for mode, action, expected in (
        ("rejected", "ap-deny", "invalid or replayed nonce"),
        ("network", "ap-approve", "could not reach the approval service"),
    ):
        context, page, errors = await _open_page(browser, base)
        try:
            decision = f"failure-{mode}"
            await _message(page, _approval(decision))
            await _message(page, _nonce(decision, f"nonce-{mode}"))
            await page.locator(f"#{action}").wait_for(state="visible")
            await page.evaluate(f"window.__approvalFetchMode = '{mode}'")
            await page.locator(f"#{action}").click()
            await page.wait_for_function(
                "expected => document.getElementById('ap-waiting').textContent.includes(expected)",
                arg=expected,
            )
            assert await page.locator("#overlay.show").count() == 1
            assert await page.locator("#overlay button:disabled").count() == 3
            assert await page.locator("#gate-badge").text_content() == "1"
            assert await page.evaluate("window.__approvalPosts.length") == 1
            assert await page.evaluate("window.__approvalPosts[0].body.action") == (
                "deny" if action == "ap-deny" else "approve"
            )
            assert errors == []
        finally:
            await context.close()


async def _assert_success_races_and_reconnect(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _message(page, _approval("first"))
        await _message(page, _approval("second"))
        await _message(page, _nonce("second", "too-early"))
        await _message(page, _nonce("first", "first-old"))
        assert await page.locator("#ap-approve").is_enabled()

        # A new socket invalidates cached credentials and demands a new visible-screen nonce.
        await page.evaluate("window.__WB_SOCKET__._onopen()")
        assert await page.locator("#overlay button:disabled").count() == 3
        await _message(page, _nonce("first", "first-fresh"))

        # Two synchronous clicks still produce exactly one nonce-bound request.
        await page.evaluate(
            "() => { const button = document.getElementById('ap-approve'); "
            "button.click(); button.click(); }"
        )
        await page.wait_for_function(
            "document.getElementById('overlay').dataset.decision === 'second'"
        )
        assert await page.evaluate("window.__approvalPosts.length") == 1
        assert await page.locator("#gate-badge").text_content() == "1"
        assert await page.locator("#overlay button:disabled").count() == 3
        assert await page.evaluate("document.activeElement.id") == "approval-dialog"

        await _message(page, _nonce("second", "second-fresh"))
        await page.locator("#ap-always").click()
        await page.wait_for_function(
            "!document.getElementById('overlay').classList.contains('show')"
        )
        assert await page.locator("#gate-badge").text_content() == "0"
        assert await page.evaluate("document.activeElement.id") == "rail-search"
        assert await page.evaluate("window.__approvalPosts.length") == 2
        assert await page.evaluate("window.__approvalPosts[1].body.action") == "always"
        assert errors == []
    finally:
        await context.close()


async def _assert_ambiguous_response_reconciles(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _message(page, _approval("response-lost"))
        await _message(page, _nonce("response-lost", "maybe-consumed"))
        await page.evaluate(
            "window.__approvalFetchMode = 'network'; window.__approvalStillPending = false"
        )
        await page.locator("#ap-approve").click()
        await page.wait_for_function(
            "!document.getElementById('overlay').classList.contains('show')"
        )
        assert await page.locator("#gate-badge").text_content() == "0"
        assert await page.evaluate("window.__approvalPosts.length") == 1
        assert errors == []
    finally:
        await context.close()


async def _assert_escape_during_inflight_recovers(browser: object, base: str) -> None:
    context, page, errors = await _open_page(browser, base)
    try:
        await _message(page, _approval("escape-inflight"))
        await _message(page, _nonce("escape-inflight", "first-attempt"))
        await page.evaluate("window.__approvalFetchMode = 'deferred-network'")
        await page.locator("#ap-approve").click()
        await page.wait_for_function("typeof window.__rejectApproval === 'function'")
        await page.keyboard.press("Escape")
        assert await page.locator("#overlay.show").count() == 0
        assert not await page.evaluate("document.querySelector('main').inert")

        await page.evaluate(
            "window.__rejectApproval(new TypeError('response lost')); "
            "window.__rejectApproval = null"
        )
        await page.wait_for_function(
            "document.getElementById('overlay').dataset.decision === 'escape-inflight' "
            "&& document.getElementById('overlay').classList.contains('show')"
        )
        assert await page.locator("#gate-badge").text_content() == "1"
        assert await page.locator("#overlay button:disabled").count() == 3
        assert await page.evaluate("document.activeElement.id") == "approval-dialog"
        assert await page.evaluate("document.querySelector('main').inert")

        await _message(page, _nonce("escape-inflight", "fresh-attempt"))
        await page.evaluate("window.__approvalFetchMode = 'success'")
        await page.locator("#ap-deny").click()
        await page.wait_for_function(
            "!document.getElementById('overlay').classList.contains('show')"
        )
        assert await page.locator("#gate-badge").text_content() == "0"
        assert errors == []
    finally:
        await context.close()


async def main() -> int:
    from playwright.async_api import async_playwright

    work = Path(tempfile.mkdtemp(prefix="approval-dod-"))
    try:
        root = work / "site"
        shutil.copytree(STATIC_DIR, root / "static")
        index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        body = index.split("<body>")[-1].rsplit("<script", 1)[0]
        (root / "__wb.html").write_text(HARNESS.replace("%BODY%", body), encoding="utf-8")
        (root / "__wb_chat-fresh.json").write_text(
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
                    await _assert_pre_nonce_and_focus(browser, base)
                    await _assert_rejected_and_network_failures(browser, base)
                    await _assert_success_races_and_reconnect(browser, base)
                    await _assert_ambiguous_response_reconciles(browser, base)
                    await _assert_escape_during_inflight_recovers(browser, base)
                finally:
                    await browser.close()
        finally:
            server.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("GREEN: approval nonce, failure, race, reconnect, and focus checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
