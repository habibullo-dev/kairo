"""Browser-backed safety and UX DoD for the DOM-only conversation renderer.

Run with ``uv run python tests/ui/message_dod.py``. It asserts that hostile Markdown stays inert,
safe links are opt-in user navigation only, fenced code copies only its code text, and a long code
line does not create mobile horizontal overflow.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import shutil
import socket
import tempfile
import threading
from pathlib import Path

from jarvis.ui.screenshots import OVERLAP_PROBE_JS, analyze_overlap
from jarvis.ui.server import STATIC_DIR


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/kairo.css"></head><body>
<main><section class="chat-shell"><div class="chat-thread" id="messages"></div></section></main>
<script type="module">
  import { renderConversation } from "/static/screens/conversation.js";
  window.__pwned = 0; window.__copied = "";
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: (text) => { window.__copied = text; return Promise.resolve(); },
  }});
  const hostile = "<scr" + "ipt>window.__pwned = 1</scr" + "ipt>\n" +
    "<img src=x onerror=\"window.__pwned=2\">\n" +
    "<svg onload=\"window.__pwned=3\"></svg>\n[bad](javascript:evil)\n![image](https://example.test/x.png)";
  const longCode = "x".repeat(900);
  const answer = [
    "## A readable answer", "A paragraph with **important text**, `inline code` and a [safe link](https://example.com/docs).",
    "- first\n- second", "1. one\n2. two", "> quoted text",
    `\`\`\`js\nconst veryLong = "${longCode}";\n\`\`\``, hostile,
  ].join("\n\n");
  const state = { chat: [{ role: "assistant", text: answer }] };
  renderConversation(document.getElementById("messages"), state);
  document.querySelector(".message-code-copy").click();
  window.__RESULT__ = {
    pwned: window.__pwned,
    scripts: document.querySelectorAll("script").length,
    images: document.querySelectorAll("img").length,
    svgs: document.querySelectorAll("svg").length,
    unsafeLinks: [...document.querySelectorAll(".message-link")].filter((a) => !a.href.startsWith("https://")).length,
    safeLinks: document.querySelectorAll(".message-link").length,
    headings: document.querySelectorAll(".message-heading").length,
    strong: document.querySelectorAll(".message-strong").length,
    copied: window.__copied,
    code: document.querySelector(".message-code code").textContent,
  };
  window.__READY__ = true;
</script></body></html>"""


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def main() -> int:
    from playwright.async_api import async_playwright

    work = Path(tempfile.mkdtemp(prefix="message-dod-"))
    try:
        static = work / "static"
        shutil.copytree(STATIC_DIR, static)
        (work / "__message.html").write_text(HTML, encoding="utf-8")
        port = _free_port()
        handler = functools.partial(_QuietHandler, directory=str(work))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(f"http://127.0.0.1:{port}/__message.html", wait_until="load")
                await page.wait_for_function("window.__READY__ === true")
                result = await page.evaluate("window.__RESULT__")
                metrics = await page.evaluate(OVERLAP_PROBE_JS)
                problems = [p for p in analyze_overlap(metrics) if "element <code>" not in p]
                code_scrolls = await page.evaluate(
                    "(() => { const pre = document.querySelector('.message-code'); "
                    "return pre.scrollWidth > pre.clientWidth; })()"
                )
                await browser.close()
        finally:
            server.shutdown()
        assert result["pwned"] == 0
        assert result["scripts"] == 1  # only this harness module; hostile text created no node
        assert result["images"] == result["svgs"] == result["unsafeLinks"] == 0
        assert result["safeLinks"] == 1
        assert result["headings"] == 1 and result["strong"] == 1
        assert result["copied"] == result["code"] and "veryLong" in result["copied"]
        assert code_scrolls
        assert not problems, problems
        print("GREEN: hostile Markdown inert; code copy and 390px layout verified")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
