"""Cost-aware embedding indexer for the memory graph (Phase 15 Task 6).

Embeds asserted graph entities (and the ``unindexed`` memories that suggestion-approval created) so
they are reachable by semantic search. Two cost rules:

* **Fail closed on an unpriced model.** :class:`CostAwareEmbedder` prices every embed call against
  the pricing table; if the (provider, model) has no row it REFUSES (``require_priced``) rather than
  silently spend an unmeasurable amount — the memory-graph indexer never runs unpriced. (Voyage rows
  were added to pricing.yaml this phase.)
* **Re-embed only what changed.** Each node/memory carries a ``content_hash``; unchanged text is
  skipped, so ``kira graph reindex`` is cheap to re-run and reports its projected/actual spend.

Voyage discards token usage, so cost is estimated from text length — good enough for the ledger +
caps; the exact bill is Voyage's invoice.
"""

from __future__ import annotations

import hashlib

from jarvis.graph.review import UNINDEXED
from jarvis.graph.store import GraphStore, _to_blob
from jarvis.observability.cost import PricingTable, Usage


class UnpricedEmbedderError(RuntimeError):
    """The embedding model has no pricing row — the indexer refuses to run (fail closed)."""


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class CostAwareEmbedder:
    """Wraps an ``Embedder``; prices every call, fails closed on an unpriced model, accumulates
    ``spent_usd``, and records to an optional ``record(model, tokens, cost)`` sink."""

    def __init__(self, inner, pricing: PricingTable, *, provider: str = "voyage",
                 require_priced: bool = True, record=None) -> None:
        self.inner = inner
        self.model = inner.model
        self.pricing = pricing
        self.provider = provider
        self.require_priced = require_priced
        self.record = record
        self.spent_usd = 0.0
        self.calls = 0

    def _charge(self, texts: list[str]) -> None:
        tokens = sum(max(1, len(t) // 4) for t in texts)  # estimate — Voyage discards usage
        usage = Usage(input_tokens=tokens, output_tokens=0)
        cost = self.pricing.cost(self.provider, self.model, usage)
        if cost is None:
            if self.require_priced:
                raise UnpricedEmbedderError(
                    f"{self.provider}/{self.model} is unpriced — refusing to index (fail closed)")
            return
        self.spent_usd += cost
        self.calls += 1
        if self.record is not None:
            self.record(self.model, tokens, cost)

    async def embed_query(self, text: str):
        self._charge([text])
        return await self.inner.embed_query(text)

    async def embed_documents(self, texts: list[str]):
        if texts:
            self._charge(texts)
        return await self.inner.embed_documents(texts)


async def reindex(store: GraphStore, embedder: CostAwareEmbedder, *, dry_run: bool = False) -> dict:
    """(Re)embed asserted entities + unindexed memories whose content changed. Returns a report
    (embedded / skipped / spent). ``dry_run`` counts what WOULD be embedded without spending."""
    entities_embedded = memories_embedded = skipped = 0

    # Entities: re-embed when title+summary changed (content_hash) or never embedded.
    for node in await store.list_nodes():
        text = f"{node.title}\n{node.summary}".strip()
        h = content_hash(text)
        if node.content_hash == h and node.embedding is not None:
            skipped += 1
            continue
        if not dry_run:
            vec = await embedder.embed_documents([text])
            await store.set_embedding(node.id, vec[0], embedder.model, h)
        entities_embedded += 1

    # Memories that suggestion-approval left with the 'unindexed' sentinel.
    rows = await (await store.db.execute(
        "SELECT id, content FROM memories WHERE status='live' AND embedding_model=?", (UNINDEXED,)
    )).fetchall()
    for mid, content in rows:
        if not dry_run:
            vec = await embedder.embed_documents([content or ""])
            async with store.lock:
                await store.db.execute(
                    "UPDATE memories SET embedding=?, embedding_model=? WHERE id=?",
                    (_to_blob(vec[0]), embedder.model, mid))
                await store.db.commit()
        memories_embedded += 1

    return {
        "entities_embedded": entities_embedded, "memories_embedded": memories_embedded,
        "skipped": skipped, "spent_usd": round(embedder.spent_usd, 6),
    }
