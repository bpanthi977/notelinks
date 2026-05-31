"""Offline unit tests for incremental refresh (design §7).

Fully offline: a real :class:`Store` on a temp dir + a MOCK ``embed_texts`` (so
no network, no provider). We monkeypatch ``notelinks.index.build.embed_texts``
to return deterministic fake vectors and spy on its call count to prove the
mtime/hash gate actually avoids re-embedding.
"""

import os
import socket

import pytest

from notelinks.config import Settings
from notelinks.index import build
from notelinks.index.store import Store


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if any code attempts an outbound network connection."""

    def _blocked(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("network access attempted during an offline build test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


class EmbedSpy:
    """Deterministic fake embedder; records how many texts it was asked to embed.

    ``calls`` counts invocations; ``embedded`` counts total texts. Vectors are
    fixed-dimension and content-derived, so they are stable but never network.
    """

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.calls = 0
        self.embedded = 0

    def __call__(self, texts, settings, *, client=None):
        self.calls += 1
        self.embedded += len(texts)
        out = []
        for i, _ in enumerate(texts):
            vec = [0.0] * self.dim
            vec[i % self.dim] = 1.0
            out.append(vec)
        return out


@pytest.fixture
def spy(monkeypatch):
    s = EmbedSpy()
    monkeypatch.setattr(build, "embed_texts", s)
    return s


def _note_text(uuid: str, title: str, heading: str, body: str) -> str:
    return (
        ":PROPERTIES:\n"
        f":ID:       {uuid}\n"
        ":END:\n"
        f"#+title: {title}\n\n"
        f"Preamble line for {title}.\n\n"
        f"* {heading}\n\n"
        f"{body}\n"
    )


def _write(path, text, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.fixture
def corpus(tmp_path):
    """A temp corpus with three small notes; returns (corpus_dir, settings)."""
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    _write(
        corpus_dir / "alpha.org",
        _note_text("uuid-alpha", "Alpha", "First", "Alpha body paragraph here."),
    )
    _write(
        corpus_dir / "sub" / "beta.org",
        _note_text("uuid-beta", "Beta", "Second", "Beta body paragraph here."),
    )
    _write(
        corpus_dir / "gamma.org",
        _note_text("uuid-gamma", "Gamma", "Third", "Gamma body paragraph here."),
    )
    settings = Settings(corpus_dir=corpus_dir, index_dir=tmp_path / "index")
    return corpus_dir, settings


def _chunk_count(store: Store) -> int:
    """Total chunk rows currently in the store."""
    return store._chunks.count()


def _note_chunk_ids(store: Store, uuid: str) -> list[str]:
    return store._chunks.get(where={"note_uuid": uuid}, include=[]).get("ids") or []


def test_first_refresh_indexes_all(corpus, spy):
    corpus_dir, settings = corpus
    store = Store(settings)

    stats = build.refresh(store, settings)

    assert stats["indexed"] == 3
    assert stats["reindexed"] == 0
    assert stats["skipped"] == 0
    assert stats["deleted"] == 0
    assert stats["chunks"] > 0
    # Chunks landed in the store, manifest has all three (relative, posix) paths.
    assert _chunk_count(store) == stats["chunks"]
    assert set(store.all_manifest_paths()) == {"alpha.org", "sub/beta.org", "gamma.org"}
    # Each manifest row carries the parsed uuid + a sha256: hash.
    row = store.get_manifest("alpha.org")
    assert row["uuid"] == "uuid-alpha"
    assert row["content_hash"].startswith("sha256:")


def test_rerun_reembeds_nothing(corpus, spy):
    corpus_dir, settings = corpus
    store = Store(settings)
    build.refresh(store, settings)

    spy.calls = 0
    spy.embedded = 0
    stats = build.refresh(store, settings)

    assert spy.calls == 0  # nothing re-embedded
    assert stats == {"indexed": 0, "reindexed": 0, "skipped": 3, "deleted": 0, "chunks": 0}


def test_edit_reembeds_only_that_file(corpus, spy):
    corpus_dir, settings = corpus
    store = Store(settings)
    build.refresh(store, settings)
    base_mtime = (corpus_dir / "alpha.org").stat().st_mtime

    # Change alpha's content AND bump its mtime past the watermark.
    _write(
        corpus_dir / "alpha.org",
        _note_text("uuid-alpha", "Alpha", "First", "Alpha body REWRITTEN entirely."),
        mtime=base_mtime + 100,
    )

    spy.calls = 0
    stats = build.refresh(store, settings)

    assert stats["reindexed"] == 1
    assert stats["skipped"] == 2
    assert stats["indexed"] == 0
    assert spy.calls == 1  # only the edited file was re-embedded
    # alpha still indexed under the same uuid; hash advanced.
    assert _note_chunk_ids(store, "uuid-alpha")


def test_touch_advances_watermark_without_reindex(corpus, spy):
    corpus_dir, settings = corpus
    store = Store(settings)
    build.refresh(store, settings)
    before = store.get_manifest("gamma.org")

    # Bump mtime but keep identical content.
    new_mtime = (corpus_dir / "gamma.org").stat().st_mtime + 500
    os.utime(corpus_dir / "gamma.org", (new_mtime, new_mtime))

    spy.calls = 0
    stats = build.refresh(store, settings)

    assert spy.calls == 0  # no re-embed: hash matched
    assert stats["reindexed"] == 0
    assert stats["skipped"] == 3
    after = store.get_manifest("gamma.org")
    assert after["content_hash"] == before["content_hash"]
    assert after["last_indexed_mtime"] == new_mtime  # watermark advanced
    assert after["last_indexed_mtime"] > before["last_indexed_mtime"]

    # And a subsequent run with no mtime change re-hashes nothing either.
    spy.calls = 0
    stats2 = build.refresh(store, settings)
    assert spy.calls == 0
    assert stats2["skipped"] == 3


def test_delete_removes_chunks_and_manifest(corpus, spy):
    corpus_dir, settings = corpus
    store = Store(settings)
    build.refresh(store, settings)
    assert _note_chunk_ids(store, "uuid-beta")

    (corpus_dir / "sub" / "beta.org").unlink()

    stats = build.refresh(store, settings)

    assert stats["deleted"] == 1
    assert store.get_manifest("sub/beta.org") is None
    assert "sub/beta.org" not in store.all_manifest_paths()
    assert _note_chunk_ids(store, "uuid-beta") == []  # chunks gone too


def test_rebuild_reindexes_everything(corpus, spy):
    corpus_dir, settings = corpus
    store = Store(settings)
    build.refresh(store, settings)

    spy.calls = 0
    stats = build.refresh(store, settings, rebuild=True)

    assert stats["reindexed"] == 3  # all forced, ignoring mtime/hash
    assert stats["indexed"] == 0
    assert stats["skipped"] == 0
    assert spy.calls == 3
    assert stats["chunks"] > 0


def test_rebuild_still_handles_deletions(corpus, spy):
    corpus_dir, settings = corpus
    store = Store(settings)
    build.refresh(store, settings)

    (corpus_dir / "gamma.org").unlink()
    stats = build.refresh(store, settings, rebuild=True)

    assert stats["reindexed"] == 2
    assert stats["deleted"] == 1
    assert store.get_manifest("gamma.org") is None
