"""Offline unit tests for the Chroma `Store` (design §7).

Everything here is fully offline: hand-made unit vectors, no network, and no
ONNX. The store is created with `embedding_function=None`, so importing/using it
must never download Chroma's bundled embedding model. We additionally hard-block
all socket connections to *prove* no network access is needed.
"""

import socket

import pytest

from notelinks.config import Settings
from notelinks.index.store import Store
from notelinks.models import Chunk


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if any code attempts an outbound network connection."""

    def _blocked(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("network access attempted during an offline Store test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def store(tmp_path):
    settings = Settings(index_dir=tmp_path / "index")
    return Store(settings)


def _chunk(
    *,
    note_uuid: str,
    ordinal: int,
    text: str,
    heading_text: str | None = "Heading",
    heading_id: str | None = "hid",
    heading_level: int | None = 1,
    heading_index: int = 1,
    chunk_in_heading: int = 0,
) -> Chunk:
    return Chunk(
        note_uuid=note_uuid,
        note_path=f"notes/{note_uuid}.org",
        note_title=f"Note {note_uuid}",
        heading_path=f"Note {note_uuid} > {heading_text}",
        heading_text=heading_text,
        heading_id=heading_id,
        heading_level=heading_level,
        heading_index=heading_index,
        chunk_in_heading=chunk_in_heading,
        ordinal=ordinal,
        char_start=ordinal * 100,
        char_end=ordinal * 100 + len(text),
        text=text,
        embed_text=f"{text} (embed)",
    )


# Orthonormal basis vectors in 4-D — exact cosine distances are easy to reason about.
E0 = [1.0, 0.0, 0.0, 0.0]
E1 = [0.0, 1.0, 0.0, 0.0]
E2 = [0.0, 0.0, 1.0, 0.0]
E3 = [0.0, 0.0, 0.0, 1.0]


def _seed(store: Store):
    """A small mixed corpus across three notes; returns (chunks, embeddings)."""
    chunks = [
        _chunk(note_uuid="A", ordinal=0, text="alpha zero"),
        _chunk(note_uuid="A", ordinal=1, text="alpha one", chunk_in_heading=1),
        # Note B chunk with all optional heading_* fields None (preamble-like).
        _chunk(
            note_uuid="B",
            ordinal=0,
            text="beta zero",
            heading_text=None,
            heading_id=None,
            heading_level=None,
            heading_index=0,
        ),
        _chunk(note_uuid="C", ordinal=0, text="gamma zero"),
    ]
    embeddings = [E0, E1, E2, E3]
    store.upsert_chunks(chunks, embeddings)
    return chunks, embeddings


def test_query_best_first_and_similarity(store):
    _seed(store)
    # Query == E0 exactly -> A:0 first with similarity 1.0; others orthogonal -> 0.0.
    results = store.query_chunks(E0, k=4)
    assert [c.chunk_id for c, _ in results][0] == "A:0"
    top_chunk, top_sim = results[0]
    assert top_chunk.chunk_id == "A:0"
    assert top_sim == pytest.approx(1.0)
    # Orthogonal vectors -> cosine distance 1.0 -> similarity 0.0.
    for _, sim in results[1:]:
        assert sim == pytest.approx(0.0)
    # Sorted best-first (non-increasing similarity).
    sims = [sim for _, sim in results]
    assert sims == sorted(sims, reverse=True)


def test_query_similarity_partial(store):
    """A 45-degree query between E0 and E1 yields similarity ~0.707 for both."""
    _seed(store)
    q = [1.0, 1.0, 0.0, 0.0]  # store may normalize; cosine is scale-invariant
    results = store.query_chunks(q, k=2)
    ids = {c.chunk_id for c, _ in results}
    assert ids == {"A:0", "A:1"}
    for _, sim in results:
        assert sim == pytest.approx(0.70710678, abs=1e-4)


def test_none_optional_fields_round_trip(store):
    """Chunk B:0 was written with heading_* = None; must come back as None."""
    _seed(store)
    results = store.query_chunks(E2, k=1)
    chunk, sim = results[0]
    assert chunk.chunk_id == "B:0"
    assert sim == pytest.approx(1.0)
    assert chunk.heading_text is None
    assert chunk.heading_id is None
    assert chunk.heading_level is None
    assert chunk.heading_index == 0
    assert chunk.text == "beta zero"


def test_exclude_note_uuid(store):
    _seed(store)
    results = store.query_chunks(E0, k=4, exclude_note_uuid="A")
    note_uuids = {c.note_uuid for c, _ in results}
    assert "A" not in note_uuids
    assert note_uuids == {"B", "C"}


def test_exclude_target_uuids(store):
    _seed(store)
    results = store.query_chunks(E0, k=4, exclude_target_uuids=["B", "C"])
    note_uuids = {c.note_uuid for c, _ in results}
    assert note_uuids == {"A"}


def test_exclude_both_combined(store):
    _seed(store)
    # Exclude self A and linked target C -> only B remains.
    results = store.query_chunks(E0, k=4, exclude_note_uuid="A", exclude_target_uuids=["C"])
    note_uuids = {c.note_uuid for c, _ in results}
    assert note_uuids == {"B"}


def test_delete_note(store):
    _seed(store)
    store.delete_note("A")
    results = store.query_chunks(E0, k=10)
    assert all(c.note_uuid != "A" for c, _ in results)
    # The other notes survive.
    assert {c.note_uuid for c, _ in results} == {"B", "C"}


def test_get_chunk_fetches_right_neighbour(store):
    _seed(store)
    # A has two chunks in heading_index=1: chunk_in_heading 0 and 1.
    c0 = store.get_chunk("A", heading_index=1, chunk_in_heading=0)
    c1 = store.get_chunk("A", heading_index=1, chunk_in_heading=1)
    assert c0 is not None and c0.chunk_id == "A:0"
    assert c1 is not None and c1.chunk_id == "A:1"
    assert c1.text == "alpha one"
    # Non-existent neighbour -> None.
    assert store.get_chunk("A", heading_index=1, chunk_in_heading=2) is None
    assert store.get_chunk("Z", heading_index=1, chunk_in_heading=0) is None


def test_query_empty_and_k_zero(store):
    _seed(store)
    assert store.query_chunks(E0, k=0) == []


def test_manifest_round_trip(store):
    assert store.get_manifest("notes/a.org") is None
    store.upsert_manifest("notes/a.org", uuid="A", content_hash="sha256:aaa", mtime=111.5)
    store.upsert_manifest("notes/b.org", uuid="B", content_hash="sha256:bbb", mtime=222.0)

    row = store.get_manifest("notes/a.org")
    assert row == {"uuid": "A", "content_hash": "sha256:aaa", "last_indexed_mtime": 111.5}

    assert set(store.all_manifest_paths()) == {"notes/a.org", "notes/b.org"}

    # Upsert overwrites in place (keyed by path).
    store.upsert_manifest("notes/a.org", uuid="A", content_hash="sha256:zzz", mtime=999.0)
    row2 = store.get_manifest("notes/a.org")
    assert row2 == {"uuid": "A", "content_hash": "sha256:zzz", "last_indexed_mtime": 999.0}
    assert set(store.all_manifest_paths()) == {"notes/a.org", "notes/b.org"}

    store.delete_manifest("notes/a.org")
    assert store.get_manifest("notes/a.org") is None
    assert store.all_manifest_paths() == ["notes/b.org"]


def test_upsert_empty_is_noop(store):
    store.upsert_chunks([], [])
    assert store.query_chunks(E0, k=5) == []


def test_upsert_length_mismatch_raises(store):
    with pytest.raises(ValueError):
        store.upsert_chunks([_chunk(note_uuid="A", ordinal=0, text="x")], [E0, E1])
