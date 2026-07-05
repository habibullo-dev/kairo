"""Embeddings: turn text into vectors for similarity search.

The :class:`Embedder` protocol is the seam (mirroring ``LLMClient``): the memory
service depends on the interface, so the whole system unit-tests offline against
:class:`FakeEmbedder` and goes live with :class:`VoyageEmbedder` unchanged.

Two calls, not one: ``embed_documents`` (for stored memories) and ``embed_query``
(for a lookup) — Voyage models are trained with an ``input_type`` distinction, and
using it measurably improves retrieval. Every embedder exposes ``.model`` so the
store can tag each vector with its space (and refuse to mix spaces later).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from jarvis.config import Config


@runtime_checkable
class Embedder(Protocol):
    """What the memory layer needs from an embedding backend."""

    model: str

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed texts for *storage* (Voyage ``input_type='document'``)."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed one text for *lookup* (Voyage ``input_type='query'``)."""
        ...


class VoyageEmbedder:
    """Live :class:`Embedder` over the Voyage API. Inject ``client`` in tests."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "voyage-3-large",
        client: object | None = None,
    ) -> None:
        if client is None:
            import voyageai

            client = voyageai.AsyncClient(api_key=api_key)
        self._client = client
        self.model = model

    @classmethod
    def from_config(cls, config: Config) -> VoyageEmbedder:
        config.require("voyage")
        return cls(api_key=config.secrets.voyage_api_key, model=config.models.embedding)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        result = await self._client.embed(texts, model=self.model, input_type="document")  # type: ignore[attr-defined]
        return result.embeddings

    async def embed_query(self, text: str) -> list[float]:
        result = await self._client.embed([text], model=self.model, input_type="query")  # type: ignore[attr-defined]
        return result.embeddings[0]


@dataclass
class FakeEmbedder:
    """Deterministic, offline embedder for tests.

    A bag-of-words hash: each token bumps a fixed dimension, so texts that share
    words land close in cosine space and unrelated texts stay far apart — enough
    structure to exercise recall/dedup thresholds without a network or a key. The
    hash is ``hashlib``-based (not builtin ``hash``) so it's stable across processes
    regardless of ``PYTHONHASHSEED``.
    """

    model: str = "fake-embedder"
    dim: int = 64

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            digest = hashlib.md5(token.encode("utf-8")).hexdigest()
            vec[int(digest, 16) % self.dim] += 1.0
        return vec
