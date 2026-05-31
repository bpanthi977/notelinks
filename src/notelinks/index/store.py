"""Long-lived Chroma store: chunk collection + manifest collection (design §7).

The `Store` is a **long-lived handle** owned by the `Engine` (design §2): open
once at startup, reuse across `refresh()`/`suggest()` calls — never
build-and-teardown per call.

Two persistent Chroma collections share one `PersistentClient`:

* **chunks** — one row per index chunk, distance = cosine. Embeddings are always
  supplied by us (`providers/embeddings.py`); the collection is created
  **without** Chroma's built-in embedding function so Chroma never downloads its
  bundled ONNX model.
* **manifest** — one row per note, keyed by file *path*, with a placeholder
  embedding `[0.0]` and `{uuid, content_hash, last_indexed_mtime}` metadata. Read
  via `get`, never queried; lets change-detection stay stat-only on the fast
  path.

Chroma metadata is **scalar-only and rejects ``None``** — optional fields whose
value is ``None`` (``heading_text`` / ``heading_id`` / ``heading_level``) are
*omitted* on write and reconstructed as ``None`` on read.
"""

from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from notelinks.config import Settings
from notelinks.models import Chunk

# Per-chunk metadata keys (design §7). Order here is purely documentary.
_CHUNK_META_KEYS = (
    "note_uuid",
    "note_path",
    "note_title",
    "heading_path",
    "heading_text",
    "heading_id",
    "heading_level",
    "heading_index",
    "chunk_in_heading",
    "ordinal",
    "char_start",
    "char_end",
)

# Optional chunk fields that may be None: omitted on write, restored on read.
_OPTIONAL_CHUNK_KEYS = ("heading_text", "heading_id", "heading_level")


class Store:
    """Long-lived Chroma handle for the chunk + manifest collections."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # anonymized_telemetry=False: no network beacons. The store is fully
        # offline — we only ever write/query explicit vectors.
        self._client = chromadb.PersistentClient(
            path=str(settings.index_dir),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        # embedding_function=None: created WITHOUT Chroma's built-in EF, so
        # Chroma never instantiates/downloads its ONNX model. We always supply
        # explicit embeddings on add/query.
        self._chunks = self._client.get_or_create_collection(
            "chunks",
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )
        self._manifest = self._client.get_or_create_collection(
            "manifest",
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )

    # -- chunks ------------------------------------------------------------

    def upsert_chunks(self, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        """Upsert chunks with explicit embeddings.

        ``None``-valued optional metadata keys are omitted (Chroma rejects
        ``None``). ``ids`` use the chunk's ``"{note_uuid}:{ordinal}"`` id.
        """
        if not chunks:
            return
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks/embeddings length mismatch: {len(chunks)} != {len(embeddings)}"
            )
        ids: list[str] = []
        metadatas: list[dict[str, Any]] = []
        documents: list[str] = []
        for chunk in chunks:
            ids.append(chunk.chunk_id)
            documents.append(chunk.text)
            meta: dict[str, Any] = {}
            for key in _CHUNK_META_KEYS:
                value = getattr(chunk, key)
                if value is None:  # Chroma metadata is scalar-only, rejects None
                    continue
                meta[key] = value
            metadatas.append(meta)
        self._chunks.upsert(
            ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents
        )

    def delete_note(self, note_uuid: str) -> None:
        """Delete every chunk belonging to one note (by file-level uuid)."""
        self._chunks.delete(where={"note_uuid": note_uuid})

    def query_chunks(
        self,
        query_embedding: list[float],
        k: int,
        exclude_note_uuid: str | None = None,
        exclude_target_uuids: list[str] | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Top-k nearest chunks for a query vector, best-first.

        FIXED CONTRACT (``pipeline/retrieve.py`` depends on this signature).

        ``exclude_note_uuid`` drops the query note's own chunks
        (``note_uuid $ne``); ``exclude_target_uuids`` drops already-linked
        targets (``note_uuid $nin [...]``). Both combine with ``$and``. Returns
        ``(Chunk, similarity)`` where ``similarity = 1 - cosine_distance``,
        sorted best-first.
        """
        if k <= 0:
            return []
        where = self._build_exclusion_where(exclude_note_uuid, exclude_target_uuids)
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": k,
            "include": ["metadatas", "documents", "distances"],
        }
        if where is not None:
            kwargs["where"] = where
        result = self._chunks.query(**kwargs)

        # Single-query call -> first (and only) row of each list.
        metadatas = (result.get("metadatas") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        out: list[tuple[Chunk, float]] = []
        for meta, doc, dist in zip(metadatas, documents, distances, strict=True):
            chunk = self._chunk_from_record(meta, doc)
            similarity = 1.0 - float(dist)
            out.append((chunk, similarity))
        # Chroma already returns ascending distance == descending similarity, but
        # sort defensively so the best-first contract never depends on backend order.
        out.sort(key=lambda pair: pair[1], reverse=True)
        return out

    def get_chunk(
        self, note_uuid: str, heading_index: int, chunk_in_heading: int
    ) -> Chunk | None:
        """Fetch one chunk by (note, heading segment, position) — neighbour context.

        Used by the judge to pull an adjacent chunk within the same heading
        (design §7/§9). Returns ``None`` if no such chunk exists.
        """
        where = {
            "$and": [
                {"note_uuid": note_uuid},
                {"heading_index": heading_index},
                {"chunk_in_heading": chunk_in_heading},
            ]
        }
        result = self._chunks.get(where=where, include=["metadatas", "documents"])
        ids = result.get("ids") or []
        if not ids:
            return None
        metadatas = result.get("metadatas") or []
        documents = result.get("documents") or []
        return self._chunk_from_record(metadatas[0], documents[0])

    # -- manifest ----------------------------------------------------------

    def get_manifest(self, path: str) -> dict | None:
        """Manifest row for a file path, or ``None`` if not indexed.

        Returns ``{uuid, content_hash, last_indexed_mtime}``.
        """
        result = self._manifest.get(ids=[path], include=["metadatas"])
        metadatas = result.get("metadatas") or []
        if not metadatas:
            return None
        meta = metadatas[0]
        return {
            "uuid": meta["uuid"],
            "content_hash": meta["content_hash"],
            "last_indexed_mtime": meta["last_indexed_mtime"],
        }

    def upsert_manifest(self, path: str, uuid: str, content_hash: str, mtime: float) -> None:
        """Upsert one manifest row, keyed by file path (placeholder embedding)."""
        self._manifest.upsert(
            ids=[path],
            embeddings=[[0.0]],
            metadatas=[
                {
                    "uuid": uuid,
                    "content_hash": content_hash,
                    "last_indexed_mtime": mtime,
                }
            ],
        )

    def all_manifest_paths(self) -> list[str]:
        """Every indexed file path (manifest row ids)."""
        result = self._manifest.get(include=[])
        return list(result.get("ids") or [])

    def delete_manifest(self, path: str) -> None:
        """Delete one manifest row by file path."""
        self._manifest.delete(ids=[path])

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _build_exclusion_where(
        exclude_note_uuid: str | None, exclude_target_uuids: list[str] | None
    ) -> dict[str, Any] | None:
        """Build the chunk-query where filter from the two exclusion inputs.

        ``$ne`` for the self note; ``$nin`` for already-linked targets; combine
        with ``$and`` only when both are present (Chroma requires a ``$and`` list
        to hold at least two clauses).
        """
        clauses: list[dict[str, Any]] = []
        if exclude_note_uuid is not None:
            clauses.append({"note_uuid": {"$ne": exclude_note_uuid}})
        if exclude_target_uuids:
            clauses.append({"note_uuid": {"$nin": list(exclude_target_uuids)}})
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    @staticmethod
    def _chunk_from_record(meta: dict[str, Any], document: str) -> Chunk:
        """Reconstruct a `Chunk` from a metadata dict + stored document.

        Omitted optional keys (`heading_text`/`heading_id`/`heading_level`) are
        filled back as ``None``. ``text`` comes from the stored document;
        ``embed_text`` is not persisted (the breadcrumb is derivable and the
        index never needs it again) so it is left empty on read.
        """
        return Chunk(
            note_uuid=meta["note_uuid"],
            note_path=meta["note_path"],
            note_title=meta["note_title"],
            heading_path=meta["heading_path"],
            heading_text=meta.get("heading_text"),
            heading_id=meta.get("heading_id"),
            heading_level=meta.get("heading_level"),
            heading_index=meta["heading_index"],
            chunk_in_heading=meta["chunk_in_heading"],
            ordinal=meta["ordinal"],
            char_start=meta["char_start"],
            char_end=meta["char_end"],
            text=document,
            embed_text="",
        )
