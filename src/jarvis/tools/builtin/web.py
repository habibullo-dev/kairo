"""Web tools: search (Tavily) and fetch+extract (httpx + trafilatura).

The network calls live in module-level helpers (``_tavily_search``, ``_fetch_html``)
so tests can monkeypatch them without hitting the network. The Tavily API key is
pulled from the injected ToolContext's config, not from globals.
"""

from __future__ import annotations

import asyncio

import httpx
import trafilatura
from pydantic import BaseModel, Field

from jarvis.tools.base import Permission, Tool, ToolResult

_TAVILY_URL = "https://api.tavily.com/search"


async def _tavily_search(api_key: str, query: str, max_results: int) -> dict:
    """POST to Tavily and return the parsed JSON. Isolated for mocking in tests."""
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",  # quality-first
        "include_answer": True,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_TAVILY_URL, json=payload)
        resp.raise_for_status()
        return resp.json()


async def _fetch_html(url: str, timeout_seconds: float) -> str:
    """GET a URL and return the raw body. Isolated for mocking in tests."""
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Jarvis/0.1 (+assistant)"})
        resp.raise_for_status()
        return resp.text


class WebSearchParams(BaseModel):
    query: str = Field(description="Search query.")
    max_results: int = Field(default=5, description="Number of results to return.")


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web and return an answer summary plus ranked source snippets."
    Params = WebSearchParams
    # Network egress asks by default: the query leaves the machine (and could carry
    # sensitive context). "Always allow" at the prompt persists a tool-level allow.
    permission_default = Permission.ASK

    async def run(self, params: WebSearchParams) -> ToolResult | str:
        cfg = self.context.config
        api_key = cfg.secrets.tavily_api_key if cfg else ""
        if not api_key:
            return ToolResult(
                content="web_search is not configured (set TAVILY_API_KEY in .env).",
                is_error=True,
            )
        data = await _tavily_search(api_key, params.query, params.max_results)
        results = data.get("results", [])
        lines: list[str] = []
        if data.get("answer"):
            lines.append(f"Answer: {data['answer']}\n")
        for i, r in enumerate(results, 1):
            snippet = (r.get("content") or "").strip()[:500]
            lines.append(
                f"{i}. {r.get('title', '(no title)')}\n   {r.get('url', '')}\n   {snippet}"
            )
        return "\n".join(lines) if lines else "No results found."


class WebFetchParams(BaseModel):
    url: str = Field(description="URL to fetch.")
    timeout_seconds: float = Field(default=30.0, description="Request timeout.")


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch a web page and return its main text content (boilerplate stripped)."
    Params = WebFetchParams
    # Asks by default: fetching a URL is an outbound request to an arbitrary host
    # (SSRF / exfiltration surface). The human sees the target URL before approving.
    permission_default = Permission.ASK

    async def run(self, params: WebFetchParams) -> ToolResult | str:
        html = await _fetch_html(params.url, params.timeout_seconds)
        text = await asyncio.to_thread(trafilatura.extract, html)
        if not text:
            return ToolResult(
                content=f"Could not extract readable content from {params.url}.", is_error=True
            )
        return f"# Source: {params.url}\n\n{text}"
