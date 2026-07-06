"""Knowledge tool tests: roundtrip, availability gating, permission + policy defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvis.config import KnowledgeConfig, load_config
from jarvis.core import AgentLoop, FakeClient, ToolCall, text_message, tool_use_message
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.permissions import PermissionGate, load_policy
from jarvis.persistence.db import connect
from jarvis.tools import Permission, ToolContext, ToolRegistry
from jarvis.tools.builtin.knowledge import (
    IngestSourceTool,
    LintKnowledgeBaseTool,
    QueryKnowledgeBaseTool,
    WriteWikiPageTool,
)
from jarvis.tools.executor import ToolExecutor

KB_TOOLS = ("ingest_source", "query_knowledge_base", "lint_knowledge_base", "write_wiki_page")
_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


async def _svc(tmp_path: Path) -> KnowledgeService:
    store = KnowledgeStore(await connect(tmp_path / "kb.db"))
    _OPEN_DBS.append(store.db)
    svc = KnowledgeService(
        store,
        FakeEmbedder(),
        KnowledgeConfig(min_similarity=0.0),
        knowledge_dir=tmp_path / "knowledge",
        root=tmp_path,
    )
    svc.ensure_dirs()
    return svc


def _content(r) -> str:
    return r.content if hasattr(r, "content") else str(r)


# --- tool behavior ---------------------------------------------------------


async def test_ingest_then_query_roundtrip(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    ctx = ToolContext(knowledge=svc)
    out = _content(
        await IngestSourceTool(ctx).run(
            IngestSourceTool.Params(text="The Eiffel Tower is in Paris.", title="fact")
        )
    )
    assert "source #1" in out
    result = _content(
        await QueryKnowledgeBaseTool(ctx).run(
            QueryKnowledgeBaseTool.Params(query="Eiffel Tower Paris")
        )
    )
    assert "Paris" in result and "[source #1" in result


async def test_write_wiki_page_tool(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    ctx = ToolContext(knowledge=svc)
    out = _content(
        await WriteWikiPageTool(ctx).run(
            WriteWikiPageTool.Params(page="topics/x.md", content="# X\n\nbody")
        )
    )
    assert "Wrote wiki page x.md" in out
    assert (svc.wiki_dir / "topics" / "x.md").exists()


async def test_lint_tool(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    out = _content(
        await LintKnowledgeBaseTool(ToolContext(knowledge=svc)).run(LintKnowledgeBaseTool.Params())
    )
    assert "clean" in out


def test_ingest_exactly_one_source_validation() -> None:
    with pytest.raises(ValidationError):
        IngestSourceTool.Params(title="none given")
    with pytest.raises(ValidationError):
        IngestSourceTool.Params(path="a.txt", url="https://x")


# --- availability gating ---------------------------------------------------


def test_tools_unavailable_without_service() -> None:
    empty = ToolContext()
    for tool in (
        IngestSourceTool,
        QueryKnowledgeBaseTool,
        LintKnowledgeBaseTool,
        WriteWikiPageTool,
    ):
        assert tool.is_available(empty) is False


def test_registry_skips_kb_tools_without_service() -> None:
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=None, knowledge=None))
    for name in KB_TOOLS:
        assert name not in reg
    assert "read_file" in reg  # earlier-phase tools still register


async def test_registry_registers_kb_tools_with_service(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext(config=None, knowledge=svc))
    for name in KB_TOOLS:
        assert name in reg


# --- permission + policy defaults ------------------------------------------


def test_permission_defaults() -> None:
    assert IngestSourceTool.permission_default is Permission.ASK
    assert QueryKnowledgeBaseTool.permission_default is Permission.ALLOW
    assert LintKnowledgeBaseTool.permission_default is Permission.ALLOW
    assert WriteWikiPageTool.permission_default is Permission.ASK


def test_policy_defaults() -> None:
    policy = load_policy(Path("config/permissions.yaml"))
    assert policy.tools["ingest_source"] is Permission.ASK
    assert policy.tools["write_wiki_page"] is Permission.ASK
    assert policy.tools["query_knowledge_base"] is Permission.ALLOW
    assert policy.tools["lint_knowledge_base"] is Permission.ALLOW


def test_system_prompt_gains_kb_guidance_only_when_enabled() -> None:
    from jarvis.core.prompts import KNOWLEDGE_GUIDANCE, build_system

    assert KNOWLEDGE_GUIDANCE not in build_system()
    assert KNOWLEDGE_GUIDANCE in build_system(knowledge_enabled=True)


# --- through a real AgentLoop ----------------------------------------------


async def test_ingest_through_agent_loop(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    config = load_config(root=tmp_path, env_file=None)
    registry = ToolRegistry()
    registry.discover("jarvis.tools.builtin", ToolContext(config=config, knowledge=svc))
    gate = PermissionGate(load_policy(Path("config/permissions.yaml")), tmp_path)

    async def _allow(_call, _decision):
        return Permission.ALLOW

    loop = AgentLoop(
        client=FakeClient(
            [
                tool_use_message(
                    [
                        ToolCall(
                            "c1", "ingest_source", {"text": "Mercury is a planet.", "title": "p"}
                        )
                    ]
                ),
                text_message("Ingested."),
            ]
        ),
        registry=registry,
        executor=ToolExecutor(timeout=30, max_result_chars=24_000),
        gate=gate,
        config=config,
        approver=_allow,
    )
    result = await loop.run_turn([{"role": "user", "content": "ingest a fact"}])
    assert result.stop_reason == "end_turn"
    assert len(await svc.store.list_sources()) == 1  # the ingest actually happened


def test_call_summary_branches() -> None:
    from jarvis.cli.repl import _call_summary

    ingest = _call_summary(ToolCall("c", "ingest_source", {"url": "https://example.test/post"}))
    assert "https://example.test/post" in ingest
    wiki = _call_summary(
        ToolCall(
            "c", "write_wiki_page", {"page": "p.md", "content": "# H\n\nbody", "source_ids": [3]}
        )
    )
    assert "p.md" in wiki and "[3]" in wiki
