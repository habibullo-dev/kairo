"""REPL knowledge integration: tool wiring, `kb` commands, non-destructive rebuild."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from kira.cli.repl import Repl, _build_knowledge
from kira.config import KnowledgeConfig, load_config
from kira.core import FakeClient, text_message
from kira.knowledge.service import KnowledgeService
from kira.knowledge.store import KnowledgeStore
from kira.memory.embeddings import FakeEmbedder
from kira.persistence.db import connect
from kira.persistence.sessions import SessionStore

_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=200), buf


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


def _repl(tmp_path: Path, *, knowledge=None) -> tuple[Repl, io.StringIO]:
    config = load_config(root=tmp_path, env_file=None)
    console, buf = _console()
    repl = Repl(
        config, client=FakeClient([text_message("ok")]), console=console, knowledge=knowledge
    )
    return repl, buf


# --- wiring ----------------------------------------------------------------


async def test_kb_tools_registered_when_enabled(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    repl, _ = _repl(tmp_path, knowledge=svc)
    for name in ("ingest_source", "query_knowledge_base", "lint_knowledge_base", "write_wiki_page"):
        assert name in repl.registry


def test_kb_disabled_wires_nothing(tmp_path: Path) -> None:
    repl, _ = _repl(tmp_path, knowledge=None)
    for name in ("ingest_source", "query_knowledge_base", "write_wiki_page"):
        assert name not in repl.registry


async def test_kb_command_without_service(tmp_path: Path) -> None:
    repl, buf = _repl(tmp_path, knowledge=None)
    await repl._kb_command("")
    assert "not enabled" in buf.getvalue()


# --- kb commands -----------------------------------------------------------


async def test_kb_stats(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    await svc.ingest(text="a fact worth knowing", title="f")
    repl, buf = _repl(tmp_path, knowledge=svc)
    await repl._kb_command("")
    assert "1 sources" in buf.getvalue() and "chunks" in buf.getvalue()


async def test_kb_lint(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    repl, buf = _repl(tmp_path, knowledge=svc)
    await repl._kb_command("lint")
    assert "clean" in buf.getvalue()


async def test_kb_rebuild_is_non_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = await _svc(tmp_path)
    path = await svc.write_page("p.md", "# P\n\noriginal body")
    # simulate a hand edit in the vault
    edited = path.read_text(encoding="utf-8") + "\n\nhand-added paragraph\n"
    path.write_text(edited, encoding="utf-8")

    repl, _ = _repl(tmp_path, knowledge=svc)
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    await repl._kb_command("rebuild")

    # rebuild re-derives the chunk index but must NOT rewrite the page file
    assert path.read_text(encoding="utf-8") == edited
    assert "p.md" in await svc.store.wiki_paths_with_chunks()


async def test_kb_review_approve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = await _svc(tmp_path)
    svc.bound_unattended = True
    result = await svc.ingest(text="unattended research finding", title="r", created_by="agent")
    assert (await svc.store.get_source(result.source_id)).review_status == "unreviewed"

    repl, buf = _repl(tmp_path, knowledge=svc)
    monkeypatch.setattr("builtins.input", lambda *_a: "a")  # approve
    await repl._kb_command("review")
    assert (await svc.store.get_source(result.source_id)).review_status == "reviewed"
    assert "approved" in buf.getvalue()


async def test_kb_review_empty_queue(tmp_path: Path) -> None:
    svc = await _svc(tmp_path)
    repl, buf = _repl(tmp_path, knowledge=svc)
    await repl._kb_command("review")
    assert "No sources awaiting review" in buf.getvalue()


# --- degradation -----------------------------------------------------------


async def test_build_knowledge_degrades_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)  # no key in the ambient env
    config = load_config(root=tmp_path, env_file=None)
    db = await connect(tmp_path / "j.db")
    store = SessionStore(db)
    _OPEN_DBS.append(db)
    console, buf = _console()
    svc = _build_knowledge(config, db, store.lock, console, memory=None)
    assert svc is None
    assert "Knowledge base off" in buf.getvalue()


async def test_build_knowledge_disabled_by_config(tmp_path: Path) -> None:
    config = load_config(root=tmp_path, env_file=None)
    config.knowledge.enabled = False
    db = await connect(tmp_path / "j.db")
    _OPEN_DBS.append(db)
    console, _ = _console()
    assert _build_knowledge(config, db, SessionStore(db).lock, console, memory=None) is None
