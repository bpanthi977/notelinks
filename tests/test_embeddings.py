"""Tests for providers/embeddings.py (SWAP POINT 1).

Monkeypatch the client's ``embeddings.create`` with a fake that records calls
and returns deterministic vectors. Asserts batching, order preservation, and
one-vector-per-input.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from notelinks.config import Settings
from notelinks.providers import embeddings as emb


class _FakeEmbeddings:
    """Stand-in for ``client.embeddings`` recording each batch it receives."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def create(self, *, model: str, input: list[str], dimensions: int):
        self.calls.append(list(input))
        # Encode the input's identity into the vector so order is checkable:
        # first component = len(text), rest = zeros up to `dimensions`.
        data = [
            SimpleNamespace(index=i, embedding=[float(len(t))] + [0.0] * (dimensions - 1))
            for i, t in enumerate(input)
        ]
        return SimpleNamespace(data=data)


def _fake_client(dim: int) -> SimpleNamespace:
    return SimpleNamespace(embeddings=_FakeEmbeddings(dim))


def test_empty_input_returns_empty_and_makes_no_call():
    settings = Settings()
    client = _fake_client(settings.embedding_dim)
    assert emb.embed_texts([], settings, client=client) == []
    assert client.embeddings.calls == []


def test_batching_splits_large_input(monkeypatch):
    monkeypatch.setattr(emb, "_BATCH_SIZE", 128)
    settings = Settings()
    client = _fake_client(settings.embedding_dim)

    n = 300  # spans 3 batches at size 128: 128 + 128 + 44
    texts = [f"t{i}" for i in range(n)]
    vecs = emb.embed_texts(texts, settings, client=client)

    assert len(vecs) == n  # one vector per input
    assert [len(b) for b in client.embeddings.calls] == [128, 128, 44]


def test_order_preserved_across_and_within_batches(monkeypatch):
    monkeypatch.setattr(emb, "_BATCH_SIZE", 2)
    settings = Settings()
    client = _fake_client(settings.embedding_dim)

    # Distinct lengths so each vector's first component identifies its input.
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    vecs = emb.embed_texts(texts, settings, client=client)

    assert len(vecs) == len(texts)
    assert [v[0] for v in vecs] == [float(len(t)) for t in texts]
    # Each vector has the configured dimensionality.
    assert all(len(v) == settings.embedding_dim for v in vecs)


def test_out_of_order_provider_response_is_realigned(monkeypatch):
    """If the provider returns data out of index order, we re-sort by index."""
    settings = Settings()

    class _ShuffledEmbeddings(_FakeEmbeddings):
        def create(self, *, model, input, dimensions):
            self.calls.append(list(input))
            data = [
                SimpleNamespace(index=i, embedding=[float(i)] + [0.0] * (dimensions - 1))
                for i in range(len(input))
            ]
            return SimpleNamespace(data=list(reversed(data)))

    client = SimpleNamespace(embeddings=_ShuffledEmbeddings(settings.embedding_dim))
    vecs = emb.embed_texts(["x", "y", "z"], settings, client=client)
    assert [v[0] for v in vecs] == [0.0, 1.0, 2.0]


def test_make_client_requires_api_key():
    settings = Settings(openrouter_api_key="")
    with pytest.raises(ValueError):
        emb.make_client(settings)


def test_retry_recovers_from_transient_error(monkeypatch):
    monkeypatch.setattr(emb, "_BASE_DELAY", 0.0)  # no real sleeping
    settings = Settings()

    class _FlakyEmbeddings(_FakeEmbeddings):
        def __init__(self, dim):
            super().__init__(dim)
            self.attempts = 0

        def create(self, *, model, input, dimensions):
            self.attempts += 1
            if self.attempts == 1:
                raise emb.APIConnectionError(request=None)  # type: ignore[arg-type]
            return super().create(model=model, input=input, dimensions=dimensions)

    client = SimpleNamespace(embeddings=_FlakyEmbeddings(settings.embedding_dim))
    vecs = emb.embed_texts(["a", "bb"], settings, client=client)
    assert client.embeddings.attempts == 2
    assert [v[0] for v in vecs] == [1.0, 2.0]


@pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY"),
    reason="live smoke test; requires OPENROUTER_API_KEY",
)
def test_live_smoke_returns_correct_dim():
    settings = Settings()
    vecs = emb.embed_texts(["hello world", "second text"], settings)
    assert len(vecs) == 2
    assert all(len(v) == settings.embedding_dim for v in vecs)
