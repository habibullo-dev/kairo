"""Project memory export/import (Phase 10 Task 4 / A1 security).

Proves the round-trip preserves content + type, and — the security rules — import FORCES
the target scope + source='import', ignoring any project_id/source a file claims; the path
jail rejects a filename that tries to escape; foreign front-matter keys survive a re-export."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.config import MemoryConfig
from jarvis.knowledge.wiki import WikiPathError, safe_wiki_path
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.service import MemoryService
from jarvis.memory.store import MemoryStore
from jarvis.persistence.db import connect
from jarvis.projects import ProjectStore
from jarvis.projects.export import (
    export_project_memories,
    import_project_memories,
)

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _service(tmp_path: Path) -> MemoryService:
    db = await connect(tmp_path / "m.db")
    _OPEN.append(db)
    projects = ProjectStore(db)
    await projects.create(name="Source")  # id 1
    await projects.create(name="Target")  # id 2
    return MemoryService(store=MemoryStore(db), embedder=FakeEmbedder(), config=MemoryConfig())


async def test_export_writes_one_file_per_project_memory(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    await svc.remember("alpha fact one", "fact", project_id=1)
    await svc.remember("alpha fact two", "preference", project_id=1)
    await svc.remember("a global memory", "fact")  # global — NOT part of project 1's export

    out = tmp_path / "export"
    n = await export_project_memories(svc.store, 1, out)
    files = sorted(out.glob("*.md"))
    assert n == 2 and len(files) == 2
    bodies = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert "alpha fact one" in bodies and "a global memory" not in bodies


async def test_round_trip_into_another_project_forces_scope(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    await svc.remember("the widget spec is frozen", "fact", project_id=1)
    out = tmp_path / "export"
    await export_project_memories(svc.store, 1, out)

    # Import into project 2 (Target). Content preserved; scope FORCED to 2; source='import'.
    report = await import_project_memories(svc, 2, out)
    assert report.created == 1
    in_target = await svc.store.all_live(project_id=2, include_global=False)
    assert any("widget spec is frozen" in m.content for m in in_target)
    imported = next(m for m in in_target if "widget spec" in m.content)
    assert imported.source == "import" and imported.project_id == 2
    # The original project-1 memory is untouched (import created a new scoped copy).
    in_source = await svc.store.all_live(project_id=1, include_global=False)
    assert any("widget spec is frozen" in m.content for m in in_source)


async def test_import_ignores_inbound_project_and_source(tmp_path: Path) -> None:
    # A hand-crafted file claiming a foreign project_id and a TRUSTED source must not be
    # honored — import forces project_id=target and source='import' (pre-mortem #13).
    svc = await _service(tmp_path)
    in_dir = tmp_path / "malicious"
    in_dir.mkdir()
    (in_dir / "evil.md").write_text(
        "---\n"
        "id: 9\n"
        "type: fact\n"
        "source: user\n"  # claims a trusted source
        "project_id: 1\n"  # claims a different project
        "---\n\n"
        "attacker planted memory\n",
        encoding="utf-8",
    )
    await import_project_memories(svc, 2, in_dir)
    # It landed in project 2 (the target), as source='import' — never project 1 / source user.
    p2 = await svc.store.all_live(project_id=2, include_global=False)
    planted = next(m for m in p2 if "attacker planted" in m.content)
    assert planted.source == "import" and planted.project_id == 2
    p1 = await svc.store.all_live(project_id=1, include_global=False)
    assert all("attacker planted" not in m.content for m in p1)  # never leaked to project 1


async def test_import_missing_dir_is_empty_report(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    report = await import_project_memories(svc, 2, tmp_path / "does-not-exist")
    assert report.created == 0 and report.duplicate == 0 and report.skipped == []


async def test_foreign_frontmatter_keys_preserved_on_reexport(tmp_path: Path) -> None:
    svc = await _service(tmp_path)
    mid = (await svc.remember("keep my tags", "fact", project_id=1)).memory_id
    out = tmp_path / "export"
    await export_project_memories(svc.store, 1, out)
    # A user adds an Obsidian key by hand, then a re-export must preserve it.
    f = out / f"{mid}.md"
    text = f.read_text(encoding="utf-8")
    f.write_text(text.replace("---\n\n", "tags: [pinned]\n---\n\n", 1), encoding="utf-8")
    await export_project_memories(svc.store, 1, out)  # re-export over the edited file
    assert "tags:" in f.read_text(encoding="utf-8")  # foreign key survived


def test_path_jail_rejects_escape(tmp_path: Path) -> None:
    # The export filename jail (reused from the wiki) rejects an escaping name.
    with pytest.raises(WikiPathError):
        safe_wiki_path(tmp_path / "export", "../../etc/passwd.md")


async def test_export_import_are_not_tools() -> None:
    # These are human rituals — never exposed to the model. Assert the functions live in the
    # projects package and are not registered as tools (a tool would make them reachable).
    from jarvis.tools import ToolContext, ToolRegistry

    reg = ToolRegistry()
    reg.discover("jarvis.tools.builtin", ToolContext())
    names = {spec["name"] for spec in reg.specs()}
    assert not any("export" in n or "import" in n for n in names)
