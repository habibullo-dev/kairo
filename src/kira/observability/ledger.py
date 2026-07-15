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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

import aiosqlite

from kira.core.client import ModelResponse
from kira.models.context_reuse import ContextReuseMode, capability, estimated_cache_savings
from kira.observability import get_logger
from kira.observability.cost import PricingTable, Usage

_COLUMNS = (
    "id, ts, trace_id, session_id, project_id, orchestration_run_id, agent_role, purpose, "
    "provider, model, effort, input_tokens, output_tokens, cache_write_tokens, "
    "cache_read_tokens, tool_call_count, latency_ms, cost_usd, pricing_version, created_at, "
    "team, stage, "
    # S7 Context Reuse (normalized, cross-provider; NULL = not reported / not cached):
    "cached_input_tokens, provider_cache_mode, provider_cache_hit_tokens, "
    "estimated_cache_savings_usd, stable_prefix_hash, "
    # Phase 15.6: how the model was chosen — 'auto'|'manual' (NULL = no router / legacy path).
    "routing_mode"
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
    team: str | None = None  # Phase 10B: orchestration team
    stage: str | None = None  # Phase 10B: council|synthesis|execution|review|verdict
    trace_id: str | None = None
    mode: str | None = None  # Phase 15.6: routing mode — 'auto'|'manual' (None = no router)


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
        self.log = get_logger("kira.cost")
        self._degraded_since: str | None = None  # A5: set when a write fails, cleared on success
        self._unrecorded = 0
        # Monotonic: a later successful row clears the *live* degradation signal, but it must not
        # erase evidence that a prior row was lost while an orchestration run was accumulating.
        self._failure_generation = 0

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
        cached_input_tokens: int | None = None,
        provider_cache_mode: str | None = None,
        provider_cache_hit_tokens: int | None = None,
        estimated_cache_savings_usd: float | None = None,
        stable_prefix_hash: str | None = None,
    ) -> None:
        """Insert one call's metadata. Unpriced ⇒ cost_usd NULL + a warning (never 0.0). A DB
        failure flips ``ledger_degraded`` and is swallowed — the model call must never break.

        The S7 cache fields are normalized + optional (NULL when caching is off or a provider does
        not report them — never a fabricated 0): the caller fills them from the context-reuse
        policy at the enable-step. Metadata only — token counts + a mode label + a prefix hash."""
        cost = self.pricing.cost(provider, model, usage)
        if cost is None:
            self.log.warning("pricing_unknown", provider=provider, model=model)
        try:
            async with self.lock:
                await self.db.execute(
                    f"INSERT INTO model_calls ({_COLUMNS}) VALUES "
                    "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?)",
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
                        ctx.team,
                        ctx.stage,
                        cached_input_tokens,
                        provider_cache_mode,
                        provider_cache_hit_tokens,
                        estimated_cache_savings_usd,
                        stable_prefix_hash,
                        ctx.mode,  # Phase 15.6 routing mode (auto|manual|NULL)
                    ),
                )
                await self.db.commit()
            self._clear_degraded()
        except Exception as exc:  # noqa: BLE001 - A5: a ledger failure is visible, never fatal
            self._mark_degraded()
            self.log.warning("ledger_write_failed", error=str(exc), purpose=ctx.purpose)

    async def record_failure(
        self,
        *,
        provider: str,
        model: str,
        latency_ms: float,
        error: Exception,
        ctx: CostContext,
    ) -> None:
        """Persist a failed model request as safe telemetry, then let the original error stand.

        Failed requests deliberately live outside ``model_calls``: no completion/tokens/cost were
        produced, and a ``NULL`` cost row there would look like an unpriced completion.  The
        exception *message* is never recorded because providers and adapters may include request
        fragments in it; the bounded class name is sufficient for aggregate health diagnostics.
        A failed telemetry write shares the normal fail-soft A5 contract.
        """
        error_class = type(error).__name__
        if not error_class.isidentifier() or len(error_class) > 120:
            error_class = "ModelRequestError"
        try:
            async with self.lock:
                await self.db.execute(
                    "INSERT INTO model_failures ("
                    "ts, trace_id, session_id, project_id, orchestration_run_id, agent_role, "
                    "purpose, provider, model, latency_ms, error_class, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        max(0.0, latency_ms),
                        error_class,
                        _now(),
                    ),
                )
                await self.db.commit()
            self._clear_degraded()
        except Exception as exc:  # noqa: BLE001 - telemetry must never replace the model failure
            self._mark_degraded()
            self.log.warning(
                "model_failure_ledger_write_failed", error=str(exc), purpose=ctx.purpose
            )

    def _mark_degraded(self) -> None:
        if self._degraded_since is None:
            self._degraded_since = _now()
        self._unrecorded += 1
        self._failure_generation += 1

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
            "failure_generation": self._failure_generation,
            "pricing_version": self.pricing.version,
        }

    def failure_generation(self) -> int:
        """A monotonic generation for fail-closed scoped cost rollups.

        It intentionally never resets on recovery. Consumers can snapshot it before a run and
        decline to present an exact total if any ledger row was lost before the run completed.
        """
        return self._failure_generation

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


_SERVICE_COLUMNS = (
    "id, ts, trace_id, project_id, orchestration_run_id, team, agent_role, stage, service, "
    "operation, units, est_cost_usd, pricing_version, created_at"
)


class ServiceLedger:
    """Records non-LLM service invocations (Semgrep/Gitleaks/Playwright/… — 10B) to the
    ``service_calls`` table. Metadata only: service, operation, units, cost — NEVER a matched
    secret value or a scanned body. Unknown/unpriced cost ⇒ NULL (fail-closed, never 0.0); a
    known-free local tool records a real 0.0. Shares the A5 degradation contract."""

    def __init__(
        self, db: aiosqlite.Connection, lock: asyncio.Lock, pricing_version: str = "?"
    ) -> None:
        self.db = db
        self.lock = lock
        self.pricing_version = pricing_version
        self.log = get_logger("kira.cost")
        self._degraded_since: str | None = None
        self._unrecorded = 0

    async def record(
        self,
        *,
        service: str,
        operation: str | None = None,
        units: float | None = None,
        est_cost_usd: float | None = None,
        ctx: CostContext | None = None,
    ) -> None:
        """Insert one service invocation's metadata. A DB failure flips degraded, never fatal."""
        c = ctx or cost_context.get()
        try:
            async with self.lock:
                await self.db.execute(
                    f"INSERT INTO service_calls ({_SERVICE_COLUMNS}) VALUES "
                    "(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _now(),
                        c.trace_id,
                        c.project_id,
                        c.orchestration_run_id,
                        c.team,
                        c.agent_role,
                        c.stage,
                        service,
                        operation,
                        units,
                        est_cost_usd,
                        self.pricing_version,
                        _now(),
                    ),
                )
                await self.db.commit()
            if self._degraded_since is not None:
                self._degraded_since = None
                self._unrecorded = 0
        except Exception as exc:  # noqa: BLE001 - a ledger failure is visible, never fatal
            if self._degraded_since is None:
                self._degraded_since = _now()
            self._unrecorded += 1
            self.log.warning("service_ledger_write_failed", error=str(exc), service=service)

    def status(self) -> dict:
        return {"degraded": self._degraded_since is not None, "unrecorded": self._unrecorded}

    async def spent(self, *, run_id: int | None = None, since: str | None = None) -> float:
        """Summed ``est_cost_usd`` over service_calls, scoped by orchestration run and/or a start
        timestamp (ISO, lexically comparable). NULL (unpriced) rows contribute 0 to the sum — the
        cap is about known spend; unpriced services are already blocked at registration."""
        clauses: list[str] = []
        params: list[object] = []
        if run_id is not None:
            clauses.append("orchestration_run_id = ?")
            params.append(run_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = await self.db.execute(
            f"SELECT COALESCE(SUM(est_cost_usd), 0.0) FROM service_calls {where}", tuple(params)
        )
        row = await cur.fetchone()
        return float((row[0] if row else 0.0) or 0.0)


def _day_start() -> str:
    """Start of the current UTC day, ISO — the per-day cap window (lexically comparable to ts)."""
    return _dt.datetime.now(_dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


@dataclass(frozen=True)
class ServiceBudget:
    """Pre-invocation service-cost caps (Phase 13). A metered call is refused BEFORE it is sent
    when the run-to-date (in an orchestration run) or day-to-date service spend plus the next
    call's cost would breach a cap — the anti-runaway for crawl/search/image. Caps come from
    ``ServicesConfig``; 0/None disables a cap. The check reads the :class:`ServiceLedger`, so it
    counts every prior metered call this run/day, not just the current tool's."""

    max_usd_per_run: float | None = None
    max_usd_per_day: float | None = None

    async def refusal(
        self, ledger: ServiceLedger, ctx: CostContext, next_cost: float | None
    ) -> str | None:
        """A clear refusal string if ``next_cost`` would breach a cap, else None. A fixed-zero /
        unpriced (0.0 / None) next cost never breaches."""
        if not next_cost:  # 0.0 or None ⇒ nothing to cap
            return None
        if self.max_usd_per_run and ctx.orchestration_run_id is not None:
            spent = await ledger.spent(run_id=ctx.orchestration_run_id)
            if spent + next_cost > self.max_usd_per_run:
                return (
                    f"service cost cap for this run reached (${spent:.4f} spent + ${next_cost:.4f} "
                    f"next > ${self.max_usd_per_run:.2f} cap) — not sent."
                )
        if self.max_usd_per_day:
            spent = await ledger.spent(since=_day_start())
            if spent + next_cost > self.max_usd_per_day:
                return (
                    f"daily service cost cap reached (${spent:.4f} + ${next_cost:.4f} > "
                    f"${self.max_usd_per_day:.2f}) — not sent."
                )
        return None


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
        start = time.perf_counter()
        try:
            resp = await self._inner.create(**kwargs)  # type: ignore[attr-defined]
        except Exception as exc:
            requested_model = kwargs.get("model")
            model = requested_model if isinstance(requested_model, str) else "unknown"
            await self._ledger.record_failure(
                provider=self._provider,
                model=model,
                latency_ms=(time.perf_counter() - start) * 1000.0,
                error=exc,
                ctx=cost_context.get(),
            )
            raise
        tool_calls = sum(1 for b in resp.content_blocks if b.get("type") == "tool_use")
        cache = self._cache_fields(resp)
        # Record the effort actually requested: a per-call override (the UI's per-model effort
        # selector) wins over the client's configured default, so the ledger's effort column is
        # honest about what drove this call.
        call_effort = kwargs.get("effort") or self._effort
        await self._ledger.record(
            provider=self._provider,
            model=resp.model,
            effort=call_effort,
            usage=resp.usage,
            latency_ms=resp.latency_ms,
            tool_call_count=tool_calls,
            ctx=cost_context.get(),
            **cache,
        )
        return resp

    def _cache_fields(self, resp: ModelResponse) -> dict:
        """The S7 normalized context-reuse columns for one call, derived from the response usage +
        the provider's capability. Every field is NULL when nothing was cached (fail-closed, never
        a fabricated 0): both wired providers report cache HITS in ``usage.cache_read_input_tokens``
        (Anthropic cache_read; OpenAI cached_tokens, mapped there by the adapter), so the hit count
        is provider-normalized already. ``cached_input_tokens`` is the automatic-prefix providers'
        (OpenAI) view of the same hit; ``provider_cache_mode`` is the provider's caching style,
        recorded only when there was real cache activity."""
        hit = resp.usage.cache_read_input_tokens or None
        write = resp.usage.cache_creation_input_tokens or None
        prefix_hash = getattr(resp, "stable_prefix_hash", None)
        cap = capability(self._provider)
        active = bool(hit or write or prefix_hash)
        mode = cap.mode.value if (cap.supported and active) else None
        cached_input = hit if cap.mode is ContextReuseMode.AUTOMATIC_PREFIX else None
        savings = None
        if hit:
            price = self._ledger.pricing.price_for(self._provider, resp.model)
            if price is not None:
                savings = estimated_cache_savings(self._provider, hit, price.input / 1_000_000)
        return {
            "cached_input_tokens": cached_input,
            "provider_cache_mode": mode,
            "provider_cache_hit_tokens": hit,
            "estimated_cache_savings_usd": savings,
            "stable_prefix_hash": prefix_hash,
        }
