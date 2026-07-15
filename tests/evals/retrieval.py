"""Retrieval-quality evals — is the *right* thing recalled, and where is the floor?

Two failure modes this measures that the scenario suite can't: silent recall
regressions (a memory/KB change that quietly stops surfacing the relevant note) and
mis-set similarity floors (memory 0.35 / KB 0.30 were config guesses — "tune from real
recall logs" that never existed). It drives ``MemoryStore.search`` /
``KnowledgeStore.search`` *directly* (structured ``ScoredMemory``/``ScoredChunk`` with
``.score`` — no string parsing, no ``recall()`` access-stat side effect).

Design (PLAN-5 §D5):

* **Determinism first ⇒ N=1.** Voyage embeddings are effectively deterministic, so
  :func:`check_determinism` embeds one query twice and asserts cosine ≈ 1.0; with that
  established, the budget buys corpus size, not repeats.
* **Authoring is separated from labeling.** Golden queries are written blind; relevance
  is labeled independently (by the judge model, then human-adjudicated); provenance
  rides in the yaml. This defeats the trap of an author who unconsciously writes
  queries that only the intended memory could match.
* **Rank-sensitive metrics.** MRR and recall@1/@3 are primary (headroom at small corpus
  size); recall@k / precision@k for k∈{1,3,5,8} are recorded. Query kinds — paraphrase,
  **hard_negative** (same topic, different answer), **unanswerable** (correct result =
  empty) — are scored separately; unanswerables score *restraint* (nothing returned).
* **The floor sweep is data, not a knob.** :func:`sweep_min_similarity` reports metrics
  across 0.20–0.45 with an explicit decision rule: move a floor only if lowering admits
  a labeled distractor or raising drops a labeled relevant. Without graduated
  distractors *between* the floors, the sweep is theater — the golden sets ship them.

Live-only: real signal needs Voyage (the FakeEmbedder is bag-of-words and can't model
paraphrase). ``run_retrieval`` skips cleanly with a message when ``VOYAGE_API_KEY`` is
unset, so it never fails a keyless CI. The *harness* (metrics, sweep, seeding) is
unit-tested keyless against the FakeEmbedder with a word-overlap corpus.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import statistics
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from jarvis.config import ConfigError, load_config
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.store import KnowledgeStore
from jarvis.memory import MemoryStore, VoyageEmbedder
from jarvis.memory.embeddings import Embedder
from jarvis.persistence.db import connect

GOLDEN_DIR = Path(__file__).parent / "golden"
KS = (1, 3, 5, 8)
DEFAULT_THRESHOLDS = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45)
# The search function contract shared by both stores: (query_vec, model, *, top_k,
# min_similarity) -> list of scored results.
SearchFn = Callable[..., Awaitable[list]]


# --- metric math (pure; keyless-tested) ------------------------------------


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    """1/rank of the first relevant hit (0 if none in the list)."""
    for i, doc in enumerate(ranked, start=1):
        if doc in relevant:
            return 1.0 / i
    return 0.0


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Fraction of the relevant set found in the top k."""
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Fraction of the top k that are relevant (denominator k, not len(ranked))."""
    if k <= 0:
        return 0.0
    return len(set(ranked[:k]) & relevant) / k


def _mean(xs: list[float]) -> float | None:
    return round(statistics.mean(xs), 4) if xs else None


# --- per-query results + aggregation ---------------------------------------


@dataclass
class QueryResult:
    query: str
    kind: str  # normal | paraphrase | hard_negative | unanswerable
    ranked: list[str]  # doc ids, best-first, deduped
    relevant: set[str]


def _dedupe(seq: list[str]) -> list[str]:
    """Order-preserving dedupe — KB chunks collapse to their parent doc id."""
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x is not None and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def aggregate_metrics(results: list[QueryResult]) -> dict:
    """MRR + recall/precision@k over answerable queries; a separate *restraint* rate
    over unanswerable ones (correct = returned nothing); per-kind MRR breakdown."""
    answerable = [r for r in results if r.relevant]
    unanswerable = [r for r in results if not r.relevant]
    by_kind: dict[str, list[float]] = {}
    for r in answerable:
        by_kind.setdefault(r.kind, []).append(reciprocal_rank(r.ranked, r.relevant))
    return {
        "n_answerable": len(answerable),
        "n_unanswerable": len(unanswerable),
        "mrr": _mean([reciprocal_rank(r.ranked, r.relevant) for r in answerable]),
        "recall_at_k": {
            k: _mean([recall_at_k(r.ranked, r.relevant, k) for r in answerable]) for k in KS
        },
        "precision_at_k": {
            k: _mean([precision_at_k(r.ranked, r.relevant, k) for r in answerable]) for k in KS
        },
        "restraint": _mean([1.0 if not r.ranked else 0.0 for r in unanswerable]),
        "mrr_by_kind": {kind: _mean(rrs) for kind, rrs in sorted(by_kind.items())},
    }


# --- running the harness over a store --------------------------------------


async def retrieve(
    search_fn: SearchFn,
    embedder: Embedder,
    query: str,
    *,
    top_k: int,
    min_similarity: float,
    id_of: Callable[[object], str | None],
) -> list[str]:
    """Embed the query and return the deduped ranked doc ids the store surfaces."""
    qvec = await embedder.embed_query(query)
    results = await search_fn(qvec, embedder.model, top_k=top_k, min_similarity=min_similarity)
    return _dedupe([id_of(r) for r in results])


async def evaluate_golden(
    golden: dict,
    search_fn: SearchFn,
    embedder: Embedder,
    *,
    top_k: int,
    min_similarity: float,
    id_of: Callable[[object], str | None],
) -> list[QueryResult]:
    out: list[QueryResult] = []
    for q in golden["queries"]:
        ranked = await retrieve(
            search_fn, embedder, q["q"], top_k=top_k, min_similarity=min_similarity, id_of=id_of
        )
        out.append(QueryResult(q["q"], q.get("kind", "normal"), ranked, set(q.get("relevant", []))))
    return out


async def sweep_min_similarity(
    golden: dict,
    search_fn: SearchFn,
    embedder: Embedder,
    *,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
    top_k: int = 8,
    id_of: Callable[[object], str | None],
) -> list[dict]:
    """Metrics at each floor + non-relevant admissions — the data the floor decision is
    read from (never auto-applied). ``nonrelevant_admitted`` counts labeled distractors
    that leak above the floor; ``relevant_recall`` is recall@k at that floor."""
    rows: list[dict] = []
    for t in thresholds:
        results = await evaluate_golden(
            golden, search_fn, embedder, top_k=top_k, min_similarity=t, id_of=id_of
        )
        leaked = sum(len([d for d in r.ranked if d not in r.relevant]) for r in results)
        agg = aggregate_metrics(results)
        rows.append(
            {
                "min_similarity": t,
                "mrr": agg["mrr"],
                "recall_at_3": agg["recall_at_k"][3],
                "restraint": agg["restraint"],
                "nonrelevant_admitted": leaked,
            }
        )
    return rows


async def check_determinism(
    embedder: Embedder, *, text: str = "retrieval determinism probe", tol: float = 1e-4
) -> tuple[bool, float]:
    """Embed one text twice; cosine must be ~1.0 (justifies N=1). Returns (ok, cosine)."""
    a = np.asarray(await embedder.embed_query(text), dtype=float)
    b = np.asarray(await embedder.embed_query(text), dtype=float)
    cos = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
    return abs(1.0 - cos) <= tol, round(cos, 6)


# --- store seeding (source/title carries the golden doc id) ----------------


async def seed_memory(store: MemoryStore, embedder: Embedder, documents: list[dict]) -> None:
    """Insert each golden doc as a live memory whose ``source`` is the golden id, so a
    ScoredMemory maps straight back to its label."""
    vecs = await embedder.embed_documents([d["text"] for d in documents])
    for d, v in zip(documents, vecs, strict=True):
        await store.add(
            type="fact",
            content=d["text"],
            embedding=v,
            embedding_model=embedder.model,
            source=d["id"],
        )


def memory_id_of(scored: object) -> str | None:
    return scored.memory.source  # type: ignore[attr-defined]


async def seed_kb(knowledge: KnowledgeService, documents: list[dict]) -> None:
    """Ingest each golden doc through the real service (chunk + embed + store) with its
    golden id as the title, then mark it reviewed so search returns it."""
    for d in documents:
        await knowledge.ingest(text=d["text"], title=d["id"], created_by="user")
    for src in await knowledge.store.list_sources(review_status="unreviewed"):
        await knowledge.store.set_review_status(src.id, "reviewed")


def kb_id_of(scored: object) -> str | None:
    return scored.source_title  # type: ignore[attr-defined]


# --- golden loading + reporting --------------------------------------------


def load_golden(name: str) -> dict:
    return yaml.safe_load((GOLDEN_DIR / name).read_text(encoding="utf-8"))


DECISION_RULE = (
    "Floor decision rule: move a floor ONLY if lowering it admits a labeled distractor "
    "(nonrelevant_admitted rises) or raising it drops a labeled relevant (recall falls). "
    "Absent graduated distractors between the floors, treat the sweep as data collection."
)


def _print_eval(title: str, results: list[QueryResult], sweep: list[dict]) -> None:
    agg = aggregate_metrics(results)
    print(f"\n=== {title} ===")
    print(f"  MRR={agg['mrr']}  recall@1={agg['recall_at_k'][1]}  recall@3={agg['recall_at_k'][3]}")
    print(f"  restraint(unanswerable)={agg['restraint']}  by-kind MRR={agg['mrr_by_kind']}")
    print("  min_sim sweep (floor -> mrr / recall@3 / restraint / nonrelevant_admitted):")
    for row in sweep:
        print(
            f"    {row['min_similarity']:.2f} -> {row['mrr']} / {row['recall_at_3']} / "
            f"{row['restraint']} / {row['nonrelevant_admitted']}"
        )
    print(f"  {DECISION_RULE}")


async def run_retrieval(config, *, top_k: int = 8) -> int:
    """Live retrieval eval over both golden corpora. Skips cleanly (exit 0) when Voyage
    isn't configured — keyless CI must never fail here."""
    if not config.secrets.voyage_api_key:
        print("Retrieval eval skipped: set VOYAGE_API_KEY in .env to run it.")
        return 0

    embedder = VoyageEmbedder.from_config(config)
    ok, cos = await check_determinism(embedder)
    print(f"Determinism self-check: cosine={cos} ({'OK' if ok else 'FAIL — N>1 needed!'})")

    # Memory corpus (doc == memory; id via the `source` field).
    mem_golden = load_golden("memory.yaml")
    mem_db = Path(tempfile.mkdtemp(prefix="jarvis-ret-mem-")) / "memory.db"
    mem_store = MemoryStore(await connect(mem_db))
    await seed_memory(mem_store, embedder, mem_golden["documents"])
    mem_results = await evaluate_golden(
        mem_golden, mem_store.search, embedder, top_k=top_k, min_similarity=0.0, id_of=memory_id_of
    )
    mem_sweep = await sweep_min_similarity(
        mem_golden, mem_store.search, embedder, id_of=memory_id_of
    )
    _print_eval("memory retrieval", mem_results, mem_sweep)

    # KB corpus (doc -> chunks; id via source_title, unreviewed included after seeding).
    kb_golden = load_golden("kb.yaml")
    kb_root = Path(tempfile.mkdtemp(prefix="jarvis-ret-kb-"))
    kb_config = config.model_copy(update={"root": kb_root})
    kb_store = KnowledgeStore(await connect(kb_root / "kira.db"))
    knowledge = KnowledgeService(
        kb_store, embedder, kb_config.knowledge, knowledge_dir=kb_config.knowledge_dir, root=kb_root
    )
    knowledge.ensure_dirs()
    await seed_kb(knowledge, kb_golden["documents"])
    kb_search = functools.partial(kb_store.search, include_unreviewed=True)
    kb_results = await evaluate_golden(
        kb_golden, kb_search, embedder, top_k=top_k, min_similarity=0.0, id_of=kb_id_of
    )
    kb_sweep = await sweep_min_similarity(kb_golden, kb_search, embedder, id_of=kb_id_of)
    _print_eval("kb retrieval", kb_results, kb_sweep)
    return 0


def main() -> None:
    argparse.ArgumentParser(description="Jarvis retrieval evals (live Voyage).").parse_args()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        sys.exit(1)
    sys.exit(asyncio.run(run_retrieval(config)))


if __name__ == "__main__":
    main()
