"""Memory tool tests: roundtrip, availability gating, permission defaults."""

from __future__ import annotations

from pathlib import Path

from kira.config import MemoryConfig
from kira.memory.embeddings import FakeEmbedder
from kira.memory.service import MemoryService
from kira.memory.store import MemoryStore
from kira.persistence.db import connect
from kira.tools import Permission, ToolContext, ToolRegistry, ToolResult
from kira.tools.builtin.memory import ForgetTool, RecallTool, RememberTool

MEMORY_TOOLS = ("remember", "recall", "forget")


def _content(r: object) -> str:
    return r.content if isinstance(r, ToolResult) else str(r)


def _is_error(r: object) -> bool:
    return isinstance(r, ToolResult) and r.is_error


async def _ctx(tmp_path: Path, *, embedder=None) -> tuple[ToolContext, MemoryService]:
    store = MemoryStore(await connect(tmp_path / "m.db"))
    svc = MemoryService(store=store, embedder=embedder or FakeEmbedder(), config=MemoryConfig())
    return ToolContext(memory=svc), svc


# --- roundtrip -------------------------------------------------------------


async def test_remember_recall_forget_roundtrip(tmp_path: Path) -> None:
    ctx, svc = await _ctx(tmp_path)
    try:
        out = _content(
            await RememberTool(ctx).run(
                RememberTool.Params(content="my favorite editor is neovim", type="preference")
            )
        )
        assert "Remembered" in out

        recalled = _content(await RecallTool(ctx).run(RecallTool.Params(query="favorite editor")))
        assert "neovim" in recalled

        mid = (await svc.store.all_live())[0].id
        forgotten = _content(await ForgetTool(ctx).run(ForgetTool.Params(memory_id=mid)))
        assert "Forgot" in forgotten
        assert await svc.store.all_live() == []
    finally:
        await svc.store.db.close()


async def test_recall_with_no_matches(tmp_path: Path) -> None:
    ctx, svc = await _ctx(tmp_path)
    try:
        out = _content(await RecallTool(ctx).run(RecallTool.Params(query="nothing stored yet")))
        assert "No relevant memories" in out
    finally:
        await svc.store.db.close()


async def test_forget_unknown_id_is_error(tmp_path: Path) -> None:
    ctx, svc = await _ctx(tmp_path)
    try:
        r = await ForgetTool(ctx).run(ForgetTool.Params(memory_id=999))
        assert _is_error(r)
    finally:
        await svc.store.db.close()


# --- availability gating ---------------------------------------------------


def test_memory_tools_unavailable_without_service() -> None:
    empty = ToolContext()
    assert RememberTool.is_available(empty) is False
    assert RecallTool.is_available(empty) is False
    assert ForgetTool.is_available(empty) is False


def test_memory_tools_available_with_service() -> None:
    ctx = ToolContext(memory=object())  # any non-None service
    assert RememberTool.is_available(ctx) is True


def test_registry_skips_memory_tools_without_service() -> None:
    reg = ToolRegistry()
    reg.discover("kira.tools.builtin", ToolContext(config=None, memory=None))
    for name in MEMORY_TOOLS:
        assert name not in reg
    assert "read_file" in reg  # phase-1 tools still register


async def test_registry_registers_memory_tools_with_service(tmp_path: Path) -> None:
    ctx, svc = await _ctx(tmp_path)
    try:
        reg = ToolRegistry()
        reg.discover("kira.tools.builtin", ctx)
        for name in MEMORY_TOOLS:
            assert name in reg
    finally:
        await svc.store.db.close()


# --- permission defaults ---------------------------------------------------


def test_memory_tool_permission_defaults() -> None:
    # remember ASKS (anti-injection); recall is read-only; forget asks.
    assert RememberTool.permission_default is Permission.ASK
    assert RecallTool.permission_default is Permission.ALLOW
    assert ForgetTool.permission_default is Permission.ASK


# --- system prompt ---------------------------------------------------------


def test_system_prompt_gains_memory_guidance_only_when_enabled() -> None:
    from kira.core.prompts import MEMORY_GUIDANCE, build_system

    assert MEMORY_GUIDANCE not in build_system()  # Phase-1 identity unchanged
    assert MEMORY_GUIDANCE in build_system(memory_enabled=True)


# --- degradation -----------------------------------------------------------


class _BoomEmbedder:
    model = "boom"

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("backend down")

    async def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("backend down")


async def test_recall_tool_reports_embedder_failure(tmp_path: Path) -> None:
    ctx, svc = await _ctx(tmp_path, embedder=_BoomEmbedder())
    try:
        r = await RecallTool(ctx).run(RecallTool.Params(query="x"))
        assert _is_error(r)
        assert "recall failed" in _content(r)
    finally:
        await svc.store.db.close()
