"""Embeddings tests: FakeEmbedder determinism + VoyageEmbedder input_type wiring."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from jarvis.memory.embeddings import Embedder, FakeEmbedder, VoyageEmbedder


def _cos(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a), np.asarray(b)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    return float(va @ vb / (na * nb)) if na and nb else 0.0


# --- FakeEmbedder ----------------------------------------------------------


async def test_fake_embedder_is_deterministic() -> None:
    e = FakeEmbedder()
    assert await e.embed_query("hello world") == await e.embed_query("hello world")


async def test_fake_embedder_word_overlap_raises_similarity() -> None:
    e = FakeEmbedder()
    base = await e.embed_query("the user prefers dark mode")
    close = await e.embed_query("the user prefers dark themes")  # 4/5 words shared
    far = await e.embed_query("compile the rust binary")  # no overlap
    assert _cos(base, close) > _cos(base, far)


async def test_fake_embedder_satisfies_protocol() -> None:
    assert isinstance(FakeEmbedder(), Embedder)


async def test_fake_embed_documents_batches() -> None:
    e = FakeEmbedder()
    vecs = await e.embed_documents(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == e.dim for v in vecs)


# --- VoyageEmbedder (injected client) --------------------------------------


class _RecordingVoyage:
    """Stand-in for voyageai.AsyncClient that records how it was called."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def embed(self, texts, model, input_type):
        self.calls.append({"texts": texts, "model": model, "input_type": input_type})
        return SimpleNamespace(embeddings=[[float(len(t)), 1.0] for t in texts])


async def test_voyage_embed_documents_uses_document_input_type() -> None:
    client = _RecordingVoyage()
    emb = VoyageEmbedder(model="voyage-3-large", client=client)
    out = await emb.embed_documents(["hello", "hi"])
    assert out == [[5.0, 1.0], [2.0, 1.0]]
    assert client.calls[0]["input_type"] == "document"
    assert client.calls[0]["model"] == "voyage-3-large"


async def test_voyage_embed_query_uses_query_input_type_and_unwraps() -> None:
    client = _RecordingVoyage()
    emb = VoyageEmbedder(client=client)
    out = await emb.embed_query("hello")
    assert out == [5.0, 1.0]  # single vector, not a list of one
    assert client.calls[0]["input_type"] == "query"
    assert client.calls[0]["texts"] == ["hello"]


async def test_voyage_embed_documents_empty_shortcircuits() -> None:
    client = _RecordingVoyage()
    emb = VoyageEmbedder(client=client)
    assert await emb.embed_documents([]) == []
    assert client.calls == []  # no API call for an empty batch
