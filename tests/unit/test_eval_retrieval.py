"""Retrieval-harness tests — metric math, aggregation, determinism, and an end-to-end
seed+search+score over a real MemoryStore, all keyless (FakeEmbedder is bag-of-words,
so the synthetic corpus uses word overlap; the shipped golden sets are for live Voyage).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.evals import retrieval
from tests.evals.retrieval import (
    aggregate_metrics,
    check_determinism,
    evaluate_golden,
    load_golden,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    sweep_min_similarity,
)

from jarvis.config import load_config
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory import MemoryStore
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.persistence.db import connect


def _qr(ranked: list[str], relevant: list[str], kind: str = "normal") -> retrieval.QueryResult:
    return retrieval.QueryResult("q", kind, ranked, set(relevant))


# --- metric math -----------------------------------------------------------


def test_reciprocal_rank() -> None:
    assert reciprocal_rank(["a", "b", "c"], {"b"}) == 0.5
    assert reciprocal_rank(["a", "b"], {"a"}) == 1.0
    assert reciprocal_rank(["a", "b"], {"z"}) == 0.0


def test_recall_and_precision_at_k() -> None:
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"a", "c"}, 3) == 1.0  # both in top 3
    assert recall_at_k(ranked, {"a", "d"}, 2) == 0.5  # only a in top 2
    assert precision_at_k(ranked, {"a"}, 2) == 0.5  # 1 of 2 relevant
    assert recall_at_k(ranked, set(), 3) == 0.0  # empty relevant is not a divide error


def test_dedupe_preserves_order() -> None:
    assert retrieval._dedupe(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


# --- aggregation -----------------------------------------------------------


def test_aggregate_metrics_mrr_restraint_and_by_kind() -> None:
    results = [
        _qr(["a", "x"], ["a"], kind="normal"),  # RR 1.0
        _qr(["y", "b"], ["b"], kind="paraphrase"),  # RR 0.5
        _qr([], [], kind="unanswerable"),  # restraint 1.0 (returned nothing)
        _qr(["z"], [], kind="unanswerable"),  # restraint 0.0 (returned something)
    ]
    agg = aggregate_metrics(results)
    assert agg["n_answerable"] == 2 and agg["n_unanswerable"] == 2
    assert agg["mrr"] == 0.75  # mean(1.0, 0.5)
    assert agg["restraint"] == 0.5  # 1 of 2 unanswerables restrained
    assert agg["mrr_by_kind"]["normal"] == 1.0
    assert agg["mrr_by_kind"]["paraphrase"] == 0.5


# --- determinism (FakeEmbedder is deterministic) ---------------------------


async def test_determinism_self_check_passes_for_deterministic_embedder() -> None:
    ok, cos = await check_determinism(FakeEmbedder())
    assert ok and abs(cos - 1.0) < 1e-6


# --- end-to-end over a real MemoryStore ------------------------------------

_DOCS = [
    {"id": "d_rust", "text": "favorite programming language is rust"},
    {"id": "d_python", "text": "python work data pipelines etl"},
    {"id": "d_coffee", "text": "oat milk flat white every morning"},
    {"id": "d_prog_fun", "text": "programming is fun and rewarding"},  # near-distractor
]
_GOLDEN = {
    "documents": _DOCS,
    "queries": [
        {"q": "favorite programming language rust", "kind": "normal", "relevant": ["d_rust"]},
        {"q": "python work data pipelines", "kind": "normal", "relevant": ["d_python"]},
        {"q": "quantum chromodynamics seminar", "kind": "unanswerable", "relevant": []},
    ],
}


async def _seeded(tmp_path: Path) -> tuple[MemoryStore, FakeEmbedder]:
    store = MemoryStore(await connect(tmp_path / "memory.db"))
    embedder = FakeEmbedder()
    await retrieval.seed_memory(store, embedder, _DOCS)
    return store, embedder


async def test_evaluate_golden_ranks_relevant_first(tmp_path: Path) -> None:
    store, embedder = await _seeded(tmp_path)
    results = await evaluate_golden(
        _GOLDEN, store.search, embedder, top_k=5, min_similarity=0.3, id_of=retrieval.memory_id_of
    )
    agg = aggregate_metrics(results)
    assert agg["mrr"] == 1.0  # each answerable query's relevant doc ranks first
    assert agg["recall_at_k"][1] == 1.0
    assert agg["restraint"] == 1.0  # the unanswerable query returned nothing above the floor


async def test_sweep_admits_fewer_distractors_as_floor_rises(tmp_path: Path) -> None:
    store, embedder = await _seeded(tmp_path)
    rows = await sweep_min_similarity(
        _GOLDEN, store.search, embedder, thresholds=(0.0, 0.3, 0.6), id_of=retrieval.memory_id_of
    )
    assert [r["min_similarity"] for r in rows] == [0.0, 0.3, 0.6]
    assert all("mrr" in r and "nonrelevant_admitted" in r for r in rows)
    # a higher floor can only admit fewer (or equal) non-relevant docs
    assert rows[0]["nonrelevant_admitted"] >= rows[-1]["nonrelevant_admitted"]


# --- shipped golden sets are well-formed -----------------------------------


@pytest.mark.parametrize("name", ["memory.yaml", "kb.yaml"])
def test_shipped_golden_sets_are_well_formed(name: str) -> None:
    golden = load_golden(name)
    assert golden.get("embedding_model")  # the space labels were validated in
    ids = {d["id"] for d in golden["documents"]}
    assert len(ids) == len(golden["documents"])  # unique ids
    kinds = {q.get("kind", "normal") for q in golden["queries"]}
    assert {"hard_negative", "unanswerable"} <= kinds  # the trap-avoiding kinds ship
    for q in golden["queries"]:
        assert set(q.get("relevant", [])) <= ids  # every label points at a real doc
        if q.get("kind") == "unanswerable":
            assert q["relevant"] == []  # correct answer is empty


# --- live path skips cleanly without a key ---------------------------------


async def test_run_retrieval_skips_without_voyage_key(tmp_path: Path, capsys) -> None:
    config = load_config(root=tmp_path, env_file=None)  # no VOYAGE_API_KEY
    rc = await retrieval.run_retrieval(config)
    assert rc == 0
    assert "skipped" in capsys.readouterr().out.lower()


async def test_seed_kb_uses_valid_created_by(tmp_path: Path) -> None:
    # Regression: the kb_sources table CHECKs created_by IN ('user','agent'); seeding
    # with anything else raises IntegrityError only at live-run time (caught in Task 8).
    cfg = load_config(root=tmp_path, env_file=None)
    store = KnowledgeStore(await connect(tmp_path / "kira.db"))
    svc = KnowledgeService(
        store, FakeEmbedder(), cfg.knowledge, knowledge_dir=cfg.knowledge_dir, root=tmp_path
    )
    svc.ensure_dirs()
    await retrieval.seed_kb(svc, [{"id": "doc-a", "text": "deployment tooling knowledge content"}])
    assert "doc-a" in {s.title for s in await store.list_sources()}
