"""Suggestion pipeline (Phase 15 Task 4). The safety rules are enforced in code, not the model:
trust = worst evidence, evidence is pointers-only (no raw body), suggestions land QUARANTINED with a
single pending state, and there is NO auto-approve path. The extractor is injected so the pipeline
is tested without a live model. Keyless."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kira.graph import GraphStore
from kira.graph.suggest import Material, gather_material, suggest, utility_extractor
from kira.orchestration import OrchestrationStore
from kira.persistence.db import connect
from kira.persistence.fts import query_domain
from kira.projects import ProjectStore
from kira.ui.server import STATIC_DIR  # noqa: F401 - ensures package import path is set

SUGGEST_SRC = (Path(__file__).resolve().parents[2] / "src/kira/graph/suggest.py").read_text(
    encoding="utf-8"
)

_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close():
    yield
    while _OPEN:
        await _OPEN.pop().close()


async def _store(tmp_path: Path) -> GraphStore:
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    return GraphStore(db, asyncio.Lock())


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


class _Client:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def create(self, **_kw):
        self.calls += 1
        return _Resp(self.text)


async def test_trust_is_the_worst_evidence(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    materials = [
        Material("chat", "1", "local note", "trusted_local"),
        Material("source", "2", "web excerpt", "untrusted_external"),
    ]

    async def fake(_m):
        return [
            {"kind": "memory", "payload": {"content": "mixed"}, "evidence": [0, 1]},  # touches web
            {"kind": "memory", "payload": {"content": "clean"}, "evidence": [0]},  # local only
        ]

    a, b = await suggest(store, materials, fake, project_id=None, extractor_model="m")
    # one untrusted evidence item taints the whole proposal; the local-only one stays trusted
    assert (await store.get_suggestion(a)).trust_class == "untrusted_external"
    assert (await store.get_suggestion(b)).trust_class == "trusted_local"


async def test_evidence_is_pointers_only_no_raw_body(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    materials = [Material("source", "9", "SECRET-MATERIAL-BODY leaked?", "untrusted_external")]

    async def fake(_m):
        return [{"kind": "memory", "payload": {"content": "a fact"}, "evidence": [0]}]

    (sid,) = await suggest(store, materials, fake, project_id=None, extractor_model="m")
    s = await store.get_suggestion(sid)
    assert s.evidence == [{"kind": "source", "id": "9"}]  # POINTER, not the text
    assert "SECRET-MATERIAL-BODY" not in str({"evidence": s.evidence, "payload": s.payload})


async def test_off_vocabulary_proposals_are_ignored(tmp_path: Path) -> None:
    store = await _store(tmp_path)

    async def fake(_m):
        return [{"kind": "spell", "payload": {}}, {"kind": "memory", "payload": {"content": "ok"}}]

    ids = await suggest(store, [], fake, project_id=None, extractor_model="m")
    assert len(ids) == 1  # only the memory proposal is kept


async def test_suggestions_stay_quarantined(tmp_path: Path) -> None:
    store = await _store(tmp_path)

    async def fake(_m):
        return [{"kind": "node", "payload": {"title": "Zorblax"}, "evidence": []}]

    await suggest(store, [], fake, project_id=None, extractor_model="m")
    # A pending suggestion is not an asserted node ⇒ never in the entities FTS, never retrievable.
    assert await query_domain(store.db, "entities", "Zorblax") == []
    assert len(await store.list_suggestions(status="pending")) == 1


async def test_no_auto_approve_path_exists() -> None:
    # Structural: the pipeline only ADDs suggestions; it never calls the resolve/materialize path
    # (that lives behind the human review route, Task 5). Prose mentioning "auto-approved" is fine.
    assert "add_suggestion" in SUGGEST_SRC
    assert "resolve_suggestion" not in SUGGEST_SRC
    assert ".resolve(" not in SUGGEST_SRC and "materialize" not in SUGGEST_SRC


async def test_gather_material_is_bounded_and_model_generated(tmp_path: Path) -> None:
    db = await connect(tmp_path / "g.db")
    _OPEN.append(db)
    lock = asyncio.Lock()
    await ProjectStore(db, lock).create(name="P")  # id 1
    orch = OrchestrationStore(db, lock)
    for i in range(3):
        rid = await orch.begin_run(project_id=1, workflow="research", title=f"R{i}",
                                   config={"team": "research"}, context_manifest=[],
                                   estimated_cost_usd=0.1, budget_usd=1.0)
        await db.execute("UPDATE orchestration_runs SET synthesis_summary=? WHERE id=?",
                         (f"summary {i}", rid))
        # Close this direct fixture write before begin_run opens its next explicit transaction.
        await db.commit()
    store = GraphStore(db, lock)
    mats = await gather_material(store, 1, limit=2)
    assert len(mats) == 2  # bounded by limit
    assert all(m.kind == "run" and m.trust_class == "model_generated" for m in mats)


async def test_utility_extractor_parses_and_tolerates_garbage(tmp_path: Path) -> None:
    good = _Client('here you go: [{"kind":"memory","payload":{"content":"Z"},"evidence":[0]}] done')
    extract = utility_extractor(good, "claude-sonnet-5")
    out = await extract([Material("run", "1", "text", "model_generated")])
    assert out == [{"kind": "memory", "payload": {"content": "Z"}, "evidence": [0]}]
    garbage = utility_extractor(_Client("not json at all"), "m")
    assert await garbage([Material("run", "1", "t", "x")]) == []  # unparseable ⇒ no proposals
    assert await extract([]) == []  # no material ⇒ no model call, no proposals
