"""ArtifactStore (schema v9): dedupe (origin + hash), XOR, path confinement + sensitive-path
refusal at registration, content_path resolution, and pin/label metadata. All keyless."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kira.persistence.artifacts import ArtifactPathError, ArtifactStore
from kira.persistence.db import connect
from kira.projects.store import ProjectStore

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _setup(tmp_path: Path):
    db = await connect(tmp_path / "art.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    projects = ProjectStore(db, lock)
    data_dir = tmp_path / "data"
    roots = {
        "artifacts": data_dir / "artifacts",
        "wiki": data_dir / "knowledge" / "wiki",
        "evals": data_dir / "evals",
    }
    for r in roots.values():
        r.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(db, lock, data_dir=data_dir, managed_roots=roots)
    return store, projects, roots, data_dir


async def test_register_local_and_resolve_content_path(tmp_path: Path) -> None:
    store, projects, roots, data_dir = await _setup(tmp_path)
    pid = await projects.create(name="Alpha")
    f = roots["artifacts"] / "note.md"
    f.write_text("hello", encoding="utf-8")

    aid = await store.register(
        origin_type="wiki", origin_id="w1", kind="wiki", title="Note",
        local_path=f, content_hash="h1", created_by="agent", project_id=pid,
    )
    art = await store.get(aid)
    assert art is not None
    assert art.local_path == "artifacts/note.md"  # stored relative to the data dir
    assert art.external_uri is None
    assert store.content_path(art) == f.resolve()  # re-confined, resolves back to the file


async def test_xor_local_xor_external_enforced(tmp_path: Path) -> None:
    store, _, roots, _ = await _setup(tmp_path)
    f = roots["artifacts"] / "x.md"
    with pytest.raises(ArtifactPathError):
        await store.register(origin_type="t", kind="k", title="T", created_by="user",
                             local_path=f, external_uri="https://example.com/x")
    with pytest.raises(ArtifactPathError):
        await store.register(origin_type="t", kind="k", title="T", created_by="user")


async def test_sensitive_path_refused_even_under_managed_root(tmp_path: Path) -> None:
    store, _, roots, _ = await _setup(tmp_path)
    # A .env file physically under a managed root is still refused by is_sensitive_path.
    with pytest.raises(ArtifactPathError):
        await store.register(origin_type="t", kind="k", title="T", created_by="user",
                             local_path=roots["artifacts"] / ".env")


async def test_path_escaping_managed_roots_refused(tmp_path: Path) -> None:
    store, _, _, _ = await _setup(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ArtifactPathError):
        await store.register(origin_type="t", kind="k", title="T", created_by="user",
                             local_path=outside)
    # And a traversal attempt that resolves outside is likewise refused.
    with pytest.raises(ArtifactPathError):
        await store.register(origin_type="t", kind="k", title="T", created_by="user",
                             local_path=roots_escape(tmp_path))


def roots_escape(tmp_path: Path) -> Path:
    return tmp_path / "data" / "artifacts" / ".." / ".." / ".." / "secret.md"


async def test_external_uri_artifact_has_no_content(tmp_path: Path) -> None:
    store, _, _, _ = await _setup(tmp_path)
    aid = await store.register(
        origin_type="google_stitch", origin_id="g1", kind="design", title="Mock",
        external_uri="https://stitch.example/mock", created_by="agent",
        provenance_class="untrusted_model_generated",
    )
    art = await store.get(aid)
    assert art is not None and art.local_path is None
    assert store.content_path(art) is None


async def test_dedupe_by_origin_updates_in_place(tmp_path: Path) -> None:
    store, projects, roots, _ = await _setup(tmp_path)
    pid = await projects.create(name="Alpha")
    (roots["wiki"] / "page.md").write_text("v1", encoding="utf-8")
    first = await store.register(
        origin_type="wiki", origin_id="wiki/page.md", kind="wiki", title="Old",
        local_path=roots["wiki"] / "page.md", content_hash="hash-v1",
        created_by="agent", project_id=pid,
    )
    # Same origin, new version (edited page) → same row, updated fields.
    second = await store.register(
        origin_type="wiki", origin_id="wiki/page.md", kind="wiki", title="New",
        local_path=roots["wiki"] / "page.md", content_hash="hash-v2",
        created_by="agent", project_id=pid,
    )
    assert first == second
    art = await store.get(first)
    assert art is not None and art.title == "New" and art.content_hash == "hash-v2"
    assert len(await store.list()) == 1


async def test_dedupe_by_content_hash_when_no_origin(tmp_path: Path) -> None:
    store, _, roots, _ = await _setup(tmp_path)
    (roots["evals"] / "r.md").write_text("x", encoding="utf-8")
    a1 = await store.register(origin_type="eval", kind="eval_report", title="R",
                              local_path=roots["evals"] / "r.md", content_hash="same",
                              created_by="system")
    a2 = await store.register(origin_type="eval", kind="eval_report", title="R again",
                              local_path=roots["evals"] / "r.md", content_hash="same",
                              created_by="system")
    assert a1 == a2  # identical content fingerprint → deduped
    assert len(await store.list()) == 1


async def test_pin_label_and_list_scoping(tmp_path: Path) -> None:
    store, projects, roots, _ = await _setup(tmp_path)
    a = await projects.create(name="Alpha")
    (roots["artifacts"] / "p.md").write_text("x", encoding="utf-8")
    (roots["artifacts"] / "g.md").write_text("y", encoding="utf-8")
    scoped = await store.register(origin_type="t", origin_id="p", kind="digest", title="Scoped",
                                  local_path=roots["artifacts"] / "p.md", created_by="agent",
                                  project_id=a)
    glob = await store.register(origin_type="t", origin_id="g", kind="digest", title="Global",
                                local_path=roots["artifacts"] / "g.md", created_by="agent")

    assert await store.set_pinned(scoped, True) is True
    assert await store.set_labels(scoped, ["needs-review", "coding"]) is True
    art = await store.get(scoped)
    assert art is not None and art.pinned is True and art.labels == ("needs-review", "coding")

    pinned = await store.list(pinned=True)
    assert [x.id for x in pinned] == [scoped]

    project_only = await store.list(project_id=a, include_global=False)
    assert [x.id for x in project_only] == [scoped]
    with_global = {x.id for x in await store.list(project_id=a, include_global=True)}
    assert with_global == {scoped, glob}


async def test_update_rejects_unknown_field(tmp_path: Path) -> None:
    store, _, roots, _ = await _setup(tmp_path)
    (roots["artifacts"] / "u.md").write_text("x", encoding="utf-8")
    aid = await store.register(origin_type="t", origin_id="u", kind="digest", title="U",
                               local_path=roots["artifacts"] / "u.md", created_by="agent")
    with pytest.raises(ValueError, match="unknown artifact field"):
        await store.update(aid, bogus="x")
    assert await store.update(aid, title="renamed") is True
    art = await store.get(aid)
    assert art is not None and art.title == "renamed"


async def test_identical_content_hash_across_origins_does_not_crash(tmp_path: Path) -> None:
    # content_hash is a NON-UNIQUE fingerprint, so re-registering an origin with content
    # byte-identical to another artifact updates it in place (no IntegrityError).
    store, _, roots, _ = await _setup(tmp_path)
    (roots["wiki"] / "a.md").write_text("x", encoding="utf-8")
    (roots["wiki"] / "b.md").write_text("y", encoding="utf-8")
    a = await store.register(origin_type="wiki", origin_id="a", kind="wiki", title="A",
                             local_path=roots["wiki"] / "a.md", content_hash="H1",
                             created_by="agent")
    b = await store.register(origin_type="wiki", origin_id="b", kind="wiki", title="B",
                             local_path=roots["wiki"] / "b.md", content_hash="H2",
                             created_by="agent")
    again = await store.register(origin_type="wiki", origin_id="a", kind="wiki", title="A2",
                                 local_path=roots["wiki"] / "a.md", content_hash="H2",
                                 created_by="agent")
    assert again == a and b != a
    art = await store.get(a)
    assert art is not None and art.content_hash == "H2" and art.title == "A2"
    assert len(await store.list()) == 2


async def test_update_null_on_not_null_column_refused_without_poisoning(tmp_path: Path) -> None:
    # update(title=None) must be refused BEFORE it hits the DB, so it can't leave the shared
    # connection mid-transaction; a subsequent write on the same connection still works.
    store, _, roots, _ = await _setup(tmp_path)
    (roots["artifacts"] / "n.md").write_text("x", encoding="utf-8")
    aid = await store.register(origin_type="t", origin_id="n", kind="digest", title="N",
                               local_path=roots["artifacts"] / "n.md", created_by="agent")
    with pytest.raises(ValueError, match="cannot be null"):
        await store.update(aid, title=None)
    with pytest.raises(ValueError, match="cannot be null"):
        await store.update(aid, kind=None)
    # The connection is not poisoned — a following register() (which opens a transaction) works.
    (roots["artifacts"] / "n2.md").write_text("y", encoding="utf-8")
    ok = await store.register(origin_type="t", origin_id="n2", kind="digest", title="N2",
                              local_path=roots["artifacts"] / "n2.md", created_by="agent")
    assert ok and await store.get(ok) is not None
