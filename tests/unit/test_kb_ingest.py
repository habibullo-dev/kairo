"""Ingest pipeline tests: artifacts, dedup, supersede, quarantine, crash-ordering.

Keyless — FakeEmbedder + a mocked URL fetch; .md files use the in-process passthrough
path, so no network and no live converter subprocess is required for most cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from kira.config import KnowledgeConfig
from kira.knowledge import converters
from kira.knowledge.service import IngestResult, KnowledgeError, KnowledgeService
from kira.knowledge.store import KnowledgeStore
from kira.memory.embeddings import FakeEmbedder
from kira.persistence.db import connect
from kira.projects import ProjectStore

_OPEN_DBS: list = []


@pytest.fixture(autouse=True)
async def _close_dbs():
    yield
    while _OPEN_DBS:
        await _OPEN_DBS.pop().close()


async def _service(tmp_path: Path, *, embedder=None, **cfg) -> KnowledgeService:
    store = KnowledgeStore(await connect(tmp_path / "kb.db"))
    _OPEN_DBS.append(store.db)
    svc = KnowledgeService(
        store,
        embedder or FakeEmbedder(),
        KnowledgeConfig(**cfg),
        knowledge_dir=tmp_path / "knowledge",
        root=tmp_path,
    )
    svc.ensure_dirs()
    return svc


class _RecordingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[str] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.documents.extend(texts)
        return await super().embed_documents(texts)


# --- file ingest -----------------------------------------------------------


async def test_ingest_file_writes_artifacts_and_chunks(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# Notes\n\nThe key number is 42.", encoding="utf-8")
    svc = await _service(tmp_path)
    result = await svc.ingest(path="notes.md", created_by="user")

    assert result.action == "ingested"
    assert result.review_status == "reviewed"
    assert result.chunks >= 1
    source = await svc.store.get_source(result.source_id)
    assert source.kind == "file"
    assert source.origin == str((tmp_path / "notes.md").resolve())
    assert source.created_by == "user"
    # immutable artifacts exist on disk
    assert (svc.knowledge_dir / source.raw_path).exists()
    assert (svc.knowledge_dir / source.markdown_path).read_text(encoding="utf-8").endswith("42.")
    # chunks embedded with the current model
    chunks = await svc.store.chunks_for_source(result.source_id)
    assert chunks and all(c.embedding_model == "fake-embedder" for c in chunks)


async def test_reingest_identical_bytes_is_noop(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("same content", encoding="utf-8")
    svc = await _service(tmp_path)
    first = await svc.ingest(path="a.md")
    second = await svc.ingest(path="a.md")
    assert second.action == "duplicate"
    assert second.source_id == first.source_id
    assert len(await svc.store.list_sources()) == 1  # no new row


async def test_reingest_changed_file_supersedes(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    f.write_text("version one", encoding="utf-8")
    svc = await _service(tmp_path)
    first = await svc.ingest(path="doc.md")
    f.write_text("version two, different content", encoding="utf-8")
    second = await svc.ingest(path="doc.md")

    assert second.action == "superseded"
    assert (await svc.store.get_source(first.source_id)).status == "superseded"
    assert (await svc.store.find_live_by_origin(str(f.resolve()))).id == second.source_id
    # old raw artifact stays on disk (immutability)
    assert (svc.knowledge_dir / (await svc.store.get_source(first.source_id)).raw_path).exists()


async def test_oversize_file_refused(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * 5000, encoding="utf-8")
    svc = await _service(tmp_path, max_ingest_bytes=1000)
    with pytest.raises(KnowledgeError, match="cap"):
        await svc.ingest(path="big.md")


async def test_sensitive_path_refused(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    svc = await _service(tmp_path)
    with pytest.raises(KnowledgeError, match="sensitive"):
        await svc.ingest(path=".env")


async def test_browser_upload_uses_the_existing_ingest_pipeline_without_retaining_staging(
    tmp_path: Path,
) -> None:
    svc = await _service(tmp_path)
    result = await svc.ingest_uploaded(
        "design-notes.md", b"# Design notes\n\nKeep the approval screen visible."
    )
    source = await svc.store.get_source(result.source_id)
    assert result.action == "ingested" and source.project_id is None
    assert source.origin == "chat-upload:global:design-notes.md"
    assert (svc.knowledge_dir / source.raw_path).exists()
    staging = svc.knowledge_dir / "staging"
    assert not staging.exists() or not list(staging.iterdir())


async def test_browser_upload_refuses_unknown_type_before_any_converter(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    with pytest.raises(KnowledgeError, match="unsupported upload type"):
        await svc.ingest_uploaded("payload.exe", b"not a document")


async def test_suspected_secret_is_redacted_before_chunks_and_embedding(tmp_path: Path) -> None:
    embedder = _RecordingEmbedder()
    svc = await _service(tmp_path, embedder=embedder)
    secret = "realvalue123456789"
    result = await svc.ingest_uploaded(
        "config.yaml",
        f"api_key: {secret}\nservice: local\n".encode(),
    )
    assert result.suspected_secret_hits == 1
    assert result.suspected_secret_rules == ("credential_assignment",)
    assert embedder.documents and all(secret not in text for text in embedder.documents)
    chunks = await svc.store.chunks_for_source(result.source_id)
    assert chunks and all(secret not in chunk.text for chunk in chunks)
    assert "[REDACTED_SECRET:credential_assignment]" in chunks[0].text
    source = await svc.store.get_source(result.source_id)
    assert source is not None
    # The immutable local artifact remains faithful; only derived/cloud-bound text is redacted.
    assert secret in (svc.knowledge_dir / source.markdown_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["src/main.py", "pyproject.toml", "web/site.css", "ops/run.ps1"])
async def test_browser_upload_accepts_project_code_and_config_as_safe_text(
    tmp_path: Path, name: str
) -> None:
    svc = await _service(tmp_path)
    result = await svc.ingest_uploaded(name, b"# local project source\n", relative_path=name)
    source = await svc.store.get_source(result.source_id)
    assert source is not None and source.title == name
    assert source.converter == "passthrough"


async def test_detached_folder_can_be_reattached_and_identical_files_keep_paths(
    tmp_path: Path,
) -> None:
    svc = await _service(tmp_path)
    project_id = await ProjectStore(svc.store.db, svc.store.lock).create(name="Project")
    first = await svc.ingest_uploaded(
        "a.py", b"same boilerplate", project_id=project_id, relative_path="wrong/a.py"
    )
    second = await svc.ingest_uploaded(
        "b.py", b"same boilerplate", project_id=project_id, relative_path="wrong/b.py"
    )
    assert first.source_id != second.source_id
    detached = await svc.store.reject_project_folder_import(project_id=project_id, root="wrong")
    assert detached.sources_rejected == 2
    assert (await svc.store.get_source(first.source_id)).status == "rejected"

    replacement = await svc.ingest_uploaded(
        "a.py", b"same boilerplate", project_id=project_id, relative_path="right/a.py"
    )
    replacement_source = await svc.store.get_source(replacement.source_id)
    assert replacement_source is not None and replacement_source.status == "live"
    assert replacement_source.title == "right/a.py"


async def test_exactly_one_source_required(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    with pytest.raises(KnowledgeError, match="exactly one"):
        await svc.ingest()
    with pytest.raises(KnowledgeError, match="exactly one"):
        await svc.ingest(path="a.md", text="b")


# --- note + url ------------------------------------------------------------


async def test_ingest_note(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    result = await svc.ingest(text="# Idea\n\nA freeform note.", title="my idea")
    source = await svc.store.get_source(result.source_id)
    assert source.kind == "note"
    assert result.chunks >= 1


async def test_ingest_url_stores_raw_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch(url, **kw):
        return b"<html><body><h1>Web Doc</h1><p>web body</p></body></html>", "text/html"

    monkeypatch.setattr(converters, "fetch_url", _fake_fetch)
    monkeypatch.setattr(converters.trafilatura, "extract", lambda *a, **k: "# Web Doc\n\nweb body")
    svc = await _service(tmp_path)
    result = await svc.ingest(url="https://example.test/post")
    source = await svc.store.get_source(result.source_id)
    assert source.kind == "url"
    assert source.origin == "https://example.test/post"
    assert b"<h1>Web Doc</h1>" in (svc.knowledge_dir / source.raw_path).read_bytes()
    assert "web body" in (svc.knowledge_dir / source.markdown_path).read_text(encoding="utf-8")


# --- unattended quarantine (D6) --------------------------------------------


async def test_unattended_ingest_is_unreviewed(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text("background research", encoding="utf-8")
    svc = await _service(tmp_path)
    svc.bound_unattended = True
    result = await svc.ingest(path="n.md", created_by="agent")
    assert result.review_status == "unreviewed"
    assert (await svc.store.get_source(result.source_id)).review_status == "unreviewed"


async def test_explicit_quarantine_is_per_ingest_not_shared_service_state(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    result = await svc.ingest(
        text="Untrusted meeting transcript",
        title="Meeting note",
        quarantine=True,
    )
    assert result.review_status == "unreviewed"
    assert svc.bound_unattended is False
    assert (await svc.store.get_source(result.source_id)).review_status == "unreviewed"


async def test_unattended_reingest_does_not_supersede_reviewed(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    f.write_text("trusted v1", encoding="utf-8")
    svc = await _service(tmp_path)
    first = await svc.ingest(path="doc.md")  # reviewed
    # an unattended re-ingest of changed content must NOT replace the trusted version
    svc.bound_unattended = True
    f.write_text("unattended v2 possibly poisoned", encoding="utf-8")
    second = await svc.ingest(path="doc.md", created_by="agent")

    assert second.action == "ingested"  # not 'superseded'
    assert (await svc.store.get_source(first.source_id)).status == "live"  # v1 still trusted
    assert (await svc.store.get_source(second.source_id)).review_status == "unreviewed"


# --- crash ordering --------------------------------------------------------


async def test_raw_artifact_written_before_db_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.md").write_text("content here", encoding="utf-8")
    svc = await _service(tmp_path)

    async def _boom(*_a, **_kw):
        raise RuntimeError("db died after the raw artifact was written")

    monkeypatch.setattr(svc.store, "add_source", _boom)
    with pytest.raises(RuntimeError):
        await svc.ingest(path="a.md")
    # the raw artifact exists (written first) — an orphan file, not a dangling row
    raw_files = list((svc.knowledge_dir / "raw").iterdir())
    assert raw_files, "raw artifact should be on disk before the DB row is attempted"
    assert await svc.store.list_sources() == []  # no row was created


async def test_ingest_result_dataclass_shape() -> None:
    r = IngestResult("ingested", 1, 3, "reviewed", "T")
    assert (r.action, r.source_id, r.chunks, r.review_status, r.title) == (
        "ingested",
        1,
        3,
        "reviewed",
        "T",
    )
