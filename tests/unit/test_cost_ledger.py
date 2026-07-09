"""The cost ledger + LedgeredClient + cost_scope (Phase 10 Task 7).

Pins: a wrapped completion writes one metadata-only row with the right purpose/scope; an
unpriced model records cost_usd NULL (never a silent 0.0); cost_scope overrides the purpose
and resets; a ledger WRITE failure flips ledger_degraded (A5) and never breaks the call;
LedgeredClient is transparent (proxies attributes). Keyless via FakeClient + tmp SQLite."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.core.client import FakeClient, text_message
from jarvis.observability.cost import Usage, load_pricing
from jarvis.observability.ledger import (
    CostContext,
    CostLedger,
    LedgeredClient,
    cost_context,
    cost_scope,
)
from jarvis.persistence.db import connect

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _ledger(tmp_path: Path) -> CostLedger:
    db = await connect(tmp_path / "l.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    # model_calls.project_id has a FK to projects — create a few so scoped rows resolve
    # (the FK enforcement is the point: a ledger row can't reference a nonexistent project).
    from jarvis.projects import ProjectStore

    projects = ProjectStore(db, lock)
    for name in ("One", "Two", "Three"):  # ids 1, 2, 3
        await projects.create(name=name)
    return CostLedger(db, lock, load_pricing(None))


async def _rows(ledger: CostLedger) -> list[dict]:
    cur = await ledger.db.execute(
        "SELECT purpose, provider, model, project_id, cost_usd, input_tokens, tool_call_count "
        "FROM model_calls ORDER BY id"
    )
    cols = ("purpose", "provider", "model", "project_id", "cost_usd", "input_tokens", "tools")
    return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]


async def test_ledgered_client_records_one_row(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path)
    inner = FakeClient([text_message("hi", usage=Usage(input_tokens=100, output_tokens=20))])
    client = LedgeredClient(inner, ledger=ledger, provider="anthropic", effort="high")
    token = cost_context.set(CostContext(purpose="turn", project_id=3))
    try:
        await client.create(
            model="claude-opus-4-8", system="s", messages=[], tools=[], max_tokens=10
        )
    finally:
        cost_context.reset(token)
    rows = await _rows(ledger)
    assert len(rows) == 1
    assert rows[0]["purpose"] == "turn" and rows[0]["project_id"] == 3
    assert rows[0]["provider"] == "anthropic" and rows[0]["input_tokens"] == 100
    assert rows[0]["cost_usd"] is not None and rows[0]["cost_usd"] > 0


async def test_records_routing_mode(tmp_path: Path) -> None:
    # Phase 15.6: the ledger records HOW the model was chosen — 'auto'|'manual' from the router,
    # NULL on the legacy path (no router: REPL / sub-agents / evals).
    ledger = await _ledger(tmp_path)
    client = LedgeredClient(
        FakeClient([text_message("a", model="gemini-2.5-flash"), text_message("b", model="x")]),
        ledger=ledger,
        provider="gemini",
        effort="high",
    )
    token = cost_context.set(CostContext(purpose="turn", mode="auto"))
    try:
        await client.create(model="m", system="s", messages=[], tools=[], max_tokens=10)
    finally:
        cost_context.reset(token)
    cur = await ledger.db.execute(
        "SELECT provider, model, routing_mode FROM model_calls ORDER BY id DESC LIMIT 1"
    )
    assert tuple(await cur.fetchone()) == ("gemini", "gemini-2.5-flash", "auto")
    # No mode in context ⇒ routing_mode NULL (byte-identical legacy path).
    await client.create(model="m", system="s", messages=[], tools=[], max_tokens=10)
    cur = await ledger.db.execute("SELECT routing_mode FROM model_calls ORDER BY id DESC LIMIT 1")
    assert (await cur.fetchone())[0] is None


async def test_unpriced_model_records_null_not_zero(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path)
    inner = FakeClient([text_message("x", usage=Usage(input_tokens=500))])
    client = LedgeredClient(inner, ledger=ledger, provider="openai", effort=None)
    # code-fallback table has no openai models ⇒ unpriced ⇒ cost_usd NULL.
    await client.create(model="mystery-model", system="s", messages=[], tools=[], max_tokens=10)
    rows = await _rows(ledger)
    assert rows[0]["cost_usd"] is None  # NULL, never a silent 0.0
    # And the "no silent $0" invariant: zero rows carry a 0.0 cost for the unpriced model.
    cur = await ledger.db.execute(
        "SELECT COUNT(*) FROM model_calls WHERE model='mystery-model' AND cost_usd = 0.0"
    )
    assert (await cur.fetchone())[0] == 0
    total = await ledger.total()
    assert total["unpriced"] == 1  # surfaced as unknown, never hidden


async def test_cost_scope_overrides_purpose_and_resets(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path)
    inner = FakeClient(
        [
            text_message("a", usage=Usage(input_tokens=10)),
            text_message("b", usage=Usage(input_tokens=10)),
        ]
    )
    client = LedgeredClient(inner, ledger=ledger, provider="anthropic", effort="high")
    token = cost_context.set(CostContext(purpose="turn", project_id=1))
    try:
        with cost_scope(purpose="compaction"):
            await client.create(
                model="claude-sonnet-5", system="s", messages=[], tools=[], max_tokens=10
            )
        # outside the scope, purpose is back to "turn"
        await client.create(
            model="claude-sonnet-5", system="s", messages=[], tools=[], max_tokens=10
        )
    finally:
        cost_context.reset(token)
    rows = await _rows(ledger)
    assert [r["purpose"] for r in rows] == ["compaction", "turn"]
    assert all(r["project_id"] == 1 for r in rows)  # scope preserved across the override


async def test_tool_call_count_recorded(tmp_path: Path) -> None:
    from jarvis.core.client import ToolCall, tool_use_message

    ledger = await _ledger(tmp_path)
    inner = FakeClient([tool_use_message([ToolCall("t1", "read_file", {"path": "x"})])])
    client = LedgeredClient(inner, ledger=ledger, provider="anthropic", effort="high")
    await client.create(model="claude-opus-4-8", system="s", messages=[], tools=[], max_tokens=10)
    assert (await _rows(ledger))[0]["tools"] == 1


async def test_ledger_failure_flips_degraded_and_never_raises(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path)
    await ledger.db.close()  # force every write to fail
    _OPEN.remove(ledger.db)
    inner = FakeClient([text_message("x")])
    client = LedgeredClient(inner, ledger=ledger, provider="anthropic", effort="high")
    # The create still returns (the model call must never break on a ledger fault)...
    resp = await client.create(
        model="claude-opus-4-8", system="s", messages=[], tools=[], max_tokens=10
    )
    assert resp.text == "x"
    # ...and the failure is VISIBLE (A5), not silent.
    status = ledger.status()
    assert status["degraded"] is True and status["unrecorded"] == 1 and status["since"] is not None


async def test_ledgered_client_is_transparent(tmp_path: Path) -> None:
    # Proxies attributes of the wrapped client (e.g. AnthropicClient.thinking) so callers /
    # the factory cache checks keep working through the wrap.
    ledger = await _ledger(tmp_path)

    class _Inner:
        thinking = False

        async def create(self, **kw):
            return text_message("x")

    client = LedgeredClient(_Inner(), ledger=ledger, provider="anthropic", effort="high")
    assert client.thinking is False


async def test_contextvar_isolation_across_parallel(tmp_path: Path) -> None:
    # Each concurrent task sets its own context inside the coroutine (pre-mortem #8); the
    # ledger rows carry each task's own purpose, not a shared one.
    ledger = await _ledger(tmp_path)

    async def one(purpose: str, pid: int) -> None:
        inner = FakeClient([text_message("x", usage=Usage(input_tokens=1))])
        client = LedgeredClient(inner, ledger=ledger, provider="anthropic", effort="high")
        token = cost_context.set(CostContext(purpose=purpose, project_id=pid))
        try:
            await client.create(
                model="claude-opus-4-8", system="s", messages=[], tools=[], max_tokens=1
            )
        finally:
            cost_context.reset(token)

    await asyncio.gather(one("subagent", 1), one("orchestration", 2), one("turn", 3))
    rows = await _rows(ledger)
    by_purpose = {r["purpose"]: r["project_id"] for r in rows}
    assert by_purpose == {"subagent": 1, "orchestration": 2, "turn": 3}


# --- end-to-end wiring: a real interactive turn records a row --------------


async def test_repl_turn_records_ledger_row(tmp_path: Path) -> None:
    # The repl.py wiring: a Repl built with a cost_ledger wraps its client, so an interactive
    # turn writes a purpose="turn" ledger row. Proves the tap is live end-to-end (not just the
    # LedgeredClient unit).
    import io

    from rich.console import Console

    from jarvis.cli.repl import Repl
    from jarvis.config import load_config
    from jarvis.persistence import SessionStore

    db = await connect(tmp_path / "repl.db")
    _OPEN.append(db)
    store = SessionStore(db)
    ledger = CostLedger(db, store.lock, load_pricing(None))
    repl = Repl(
        load_config(root=tmp_path, env_file=None),
        client=FakeClient([text_message("done", usage=Usage(input_tokens=42, output_tokens=8))]),
        console=Console(file=io.StringIO()),
        store=store,
        session_id=await store.create_session(),
        cost_ledger=ledger,
    )
    repl.messages = [{"role": "user", "content": "hello"}]
    await repl.run_turn()
    rows = await _rows(ledger)
    assert len(rows) == 1 and rows[0]["purpose"] == "turn" and rows[0]["input_tokens"] == 42
