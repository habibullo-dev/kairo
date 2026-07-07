"""The per-model-call cost ledger (Phase 10 Task 7).

Every LLM completion writes one metadata-only ``model_calls`` row — tokens, latency, model,
purpose, scope — NEVER prompts or bodies. Attribution rides a :class:`CostContext`
contextvar set by each call site (turn / subagent / compaction / reflection / memory_dedup /
digest / orchestration); :class:`LedgeredClient` wraps any ``LLMClient`` and records after
each ``create`` from whatever context is current.

Two rules from the amendments/pre-mortem:
* **Fail-closed pricing** — an unpriced (provider, model) records ``cost_usd = NULL`` + a
  ``pricing_unknown`` warning, never a silent 0.0.
* **Visible degradation (A5)** — a ledger *write* failure never breaks the model call, but it
  flips a ``ledger_degraded`` status (surfaced on the NoticeBoard / status strip / Hub) so
  cost tracking can't silently disappear; it clears on the next successful write.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

import aiosqlite

from jarvis.core.client import ModelResponse
from jarvis.observability import get_logger
from jarvis.observability.cost import PricingTable, Usage

_COLUMNS = (
    "id, ts, trace_id, session_id, project_id, orchestration_run_id, agent_role, purpose, "
    "provider, model, effort, input_tokens, output_tokens, cache_write_tokens, "
    "cache_read_tokens, tool_call_count, latency_ms, cost_usd, pricing_version, created_at"
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


@dataclass(frozen=True)
class CostContext:
    """Who a model call was for. All optional; ``purpose`` defaults to a plain interactive
    turn. Set inside each call site (and inside each child coroutine for parallel work, so a
    gather can't share one role across agents — pre-mortem #8)."""

    purpose: str = "turn"
    project_id: int | None = None
    session_id: int | None = None
    orchestration_run_id: int | None = None
    agent_role: str | None = None
    trace_id: str | None = None


# The default is a FROZEN CostContext (immutable), so a single shared default instance is
# safe — noqa B039, which can't tell a frozen dataclass from a mutable structure.
cost_context: ContextVar[CostContext] = ContextVar(
    "cost_context",
    default=CostContext(),  # noqa: B039 - CostContext is frozen (immutable); a shared default is safe
)


@contextmanager
def cost_scope(**overrides: object) -> Iterator[None]:
    """Temporarily override fields of the current :data:`cost_context` (merging, so a nested
    scope keeps the outer project_id/trace_id and just changes e.g. ``purpose``). Reset on
    exit. Wrap a single ``await client.create(...)`` — the contextvar stays set across the
    await within the same task."""
    current = cost_context.get()
    token = cost_context.set(replace(current, **overrides))  # type: ignore[arg-type]
    try:
        yield
    finally:
        cost_context.reset(token)


class CostLedger:
    """Writes ``model_calls`` rows on the shared connection + lock. Metadata only."""

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock, pricing: PricingTable) -> None:
        self.db = db
        self.lock = lock
        self.pricing = pricing
        self.log = get_logger("jarvis.cost")
        self._degraded_since: str | None = None  # A5: set when a write fails, cleared on success
        self._unrecorded = 0

    async def record(
        self,
        *,
        provider: str,
        model: str,
        effort: str | None,
        usage: Usage,
        latency_ms: float | None,
        tool_call_count: int,
        ctx: CostContext,
    ) -> None:
        """Insert one call's metadata. Unpriced ⇒ cost_usd NULL + a warning (never 0.0). A DB
        failure flips ``ledger_degraded`` and is swallowed — the model call must never break."""
        cost = self.pricing.cost(provider, model, usage)
        if cost is None:
            self.log.warning("pricing_unknown", provider=provider, model=model)
        try:
            async with self.lock:
                await self.db.execute(
                    f"INSERT INTO model_calls ({_COLUMNS}) VALUES "
                    "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _now(),
                        ctx.trace_id,
                        ctx.session_id,
                        ctx.project_id,
                        ctx.orchestration_run_id,
                        ctx.agent_role,
                        ctx.purpose,
                        provider,
                        model,
                        effort,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cache_creation_input_tokens,
                        usage.cache_read_input_tokens,
                        tool_call_count,
                        latency_ms,
                        cost,  # None ⇒ SQL NULL (fail-closed; never a silent 0.0)
                        self.pricing.version,
                        _now(),
                    ),
                )
                await self.db.commit()
            self._clear_degraded()
        except Exception as exc:  # noqa: BLE001 - A5: a ledger failure is visible, never fatal
            self._mark_degraded()
            self.log.warning("ledger_write_failed", error=str(exc), purpose=ctx.purpose)

    def _mark_degraded(self) -> None:
        if self._degraded_since is None:
            self._degraded_since = _now()
        self._unrecorded += 1

    def _clear_degraded(self) -> None:
        if self._degraded_since is not None:
            self.log.info("ledger_recovered", unrecorded=self._unrecorded)
        self._degraded_since = None
        self._unrecorded = 0

    def status(self) -> dict:
        """The A5 degradation status for the Hub / status strip. ``degraded`` True means cost
        tracking is failing to persist — surfaced, never silent."""
        return {
            "degraded": self._degraded_since is not None,
            "since": self._degraded_since,
            "unrecorded": self._unrecorded,
            "pricing_version": self.pricing.version,
        }

    async def total(self, *, project_id: int | None = None) -> dict:
        """A minimal rollup (Task 8 adds the periodised views): summed cost + call counts,
        optionally scoped to a project. ``unpriced`` counts NULL-cost rows so they're never
        silently read as $0."""
        if project_id is not None:
            where, params = "WHERE project_id = ?", (project_id,)
        else:
            where, params = "", ()
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0), COUNT(*), "
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) "
            f"FROM model_calls {where}",
            params,
        )
        row = await cur.fetchone()
        return {"cost_usd": row[0], "calls": row[1], "unpriced": row[2] or 0}


class LedgeredClient:
    """Wraps an ``LLMClient`` and records each ``create`` to the ledger. Transparent — any
    attribute the wrapped client exposes (e.g. ``thinking``) proxies through."""

    def __init__(
        self, inner: object, *, ledger: CostLedger, provider: str, effort: str | None = None
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._provider = provider
        self._effort = effort

    def __getattr__(self, name: str) -> object:
        # Proxy anything we don't define (e.g. AnthropicClient.thinking) to the wrapped client.
        return getattr(self._inner, name)

    async def create(self, **kwargs: object) -> ModelResponse:
        resp = await self._inner.create(**kwargs)  # type: ignore[attr-defined]
        tool_calls = sum(1 for b in resp.content_blocks if b.get("type") == "tool_use")
        await self._ledger.record(
            provider=self._provider,
            model=resp.model,
            effort=self._effort,
            usage=resp.usage,
            latency_ms=resp.latency_ms,
            tool_call_count=tool_calls,
            ctx=cost_context.get(),
        )
        return resp
