"""Playwright-localhost adapter — the ``playwright_inspect`` tool (Phase 10B Task 16, B3).

**Localhost-only AND inspect-only, both enforced in the adapter, before any driver runs:**

* URL allowlist — the host MUST be loopback (``127.0.0.1`` / ``localhost`` / ``::1``); optionally
  narrowed to the project's configured dev ports. This makes the tool **non-egress by
  construction** (it can only reach the local dev app).
* Verb allowlist — exactly ``{navigate, screenshot, dom_inspect, a11y_check, visual_diff}``.
  There is no click / type / submit / eval verb to invoke; arbitrary interaction is a
  separately-planned, separately-gated future step. This must never become a generic browser.

Execution-stage, ASK-gated (derived from the ServiceSpec). The browser driver is injected
(``set_driver``) so the safety logic is testable with no Playwright install.
"""

from __future__ import annotations

from typing import ClassVar, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from kira.observability import get_logger
from kira.services.tooling import ServiceTool, frame_output
from kira.tools.base import ToolResult

log = get_logger("kira.services.playwright")

#: The ONLY verbs this tool exposes. click/type/submit/eval are deliberately absent.
INSPECT_VERBS: frozenset[str] = frozenset(
    {"navigate", "screenshot", "dom_inspect", "a11y_check", "visual_diff"}
)
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class PlaywrightParams(BaseModel):
    verb: str = Field(description=f"One of: {', '.join(sorted(INSPECT_VERBS))}.")
    url: str = Field(description="A localhost URL (127.0.0.1/localhost) of the local dev app.")
    selector: str = Field(default="", description="Optional CSS selector for dom_inspect.")


class InspectDriver(Protocol):
    async def inspect(self, verb: str, url: str, selector: str) -> str: ...


class _NotInstalledDriver:
    async def inspect(self, verb: str, url: str, selector: str) -> str:
        raise RuntimeError(
            "playwright is not installed — inspect-only browser QA needs the playwright extra"
        )


_driver: InspectDriver = _NotInstalledDriver()


def set_driver(driver: InspectDriver) -> None:
    """Inject the browser driver (tests + the real Playwright wiring). Default is a stub that
    errors, so the tool is safe to register even without the Playwright extra."""
    global _driver
    _driver = driver


def url_is_localhost(url: str, *, allow_ports: list[int] | None = None) -> bool:
    """True iff ``url`` targets loopback (http/https) — and, if ``allow_ports`` is non-empty, a
    listed port. This is the non-egress guarantee (B3): only the local dev app is reachable."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return False
    if allow_ports:
        return p.port in allow_ports
    return True


class PlaywrightInspectTool(ServiceTool):
    service_name = "playwright_local"
    name = "playwright_inspect"
    description = (
        "Inspect a LOCAL dev app (localhost only) with Playwright — read-only: navigate, "
        "screenshot, dom_inspect, a11y_check, visual_diff. No clicking, typing, form submission, "
        "or script eval. Non-localhost URLs are refused."
    )
    Params = PlaywrightParams
    #: Not read-only-spawnable: execution-stage only, so it never enters the council/review floor.
    execution_only: ClassVar[bool] = True

    async def run(self, params: PlaywrightParams) -> ToolResult:  # type: ignore[override]
        verb = params.verb.strip().lower()
        if verb not in INSPECT_VERBS:
            return ToolResult(
                content=(
                    f"unsupported verb {params.verb!r}; playwright_inspect is inspect-only: "
                    f"{sorted(INSPECT_VERBS)} (no click/type/submit/eval)"
                ),
                is_error=True,
            )
        allow_ports = list(
            getattr(self.context.config.services, "playwright_allow_ports", []) or []
        )
        if not url_is_localhost(params.url, allow_ports=allow_ports):
            return ToolResult(
                content=f"refusing a non-localhost URL: {params.url} (localhost-only, non-egress)",
                is_error=True,
            )
        try:
            output = await _driver.inspect(verb, params.url, params.selector)
        except Exception as exc:  # noqa: BLE001 - a driver failure is a tool error, not a crash
            return ToolResult(content=f"playwright_inspect failed: {exc}", is_error=True)
        await self._record_call(verb, est_cost_usd=0.0)  # fixed_zero (local)
        assert self.spec is not None
        return ToolResult(content=frame_output(self.spec, output))
