# T8 — `src/notelinks/index/store.py`

The long-lived Chroma handle (design §7). One `PersistentClient`, two
collections (`chunks`, `manifest`). Owned by the `Engine`: opened once, reused —
not built-and-torn-down per call (design §2).

## Collection setup & disabling the built-in EF / ONNX

- `chromadb.PersistentClient(path=str(settings.index_dir), settings=ChromaSettings(anonymized_telemetry=False))`.
  Telemetry off so there are no outbound beacons.
- **`index_dir` guard + create.** `settings.index_dir` is `None` when neither
  `NOTELINKS_INDEX_DIR` nor a `corpus_dir` to derive it from was set;
  constructing the `Store` then raises `ValueError` (no silent fallback —
  mirrors the `corpus_dir` guard). Otherwise the dir and its parents (e.g. the
  corpus's `dbs/`) are created with `mkdir(parents=True, exist_ok=True)` before
  opening the client, so the on-disk location is predictable.
- Both collections are created with
  `get_or_create_collection(name, metadata={"hnsw:space": "cosine"}, embedding_function=None)`.
- **`embedding_function=None` is the key.** In chromadb 1.5.9 the default value
  of that parameter is `DefaultEmbeddingFunction` (an ONNX MiniLM model that
  Chroma downloads on first use). Passing `None` explicitly means the collection
  has **no** embedding function, so Chroma never instantiates or downloads the
  ONNX model. We therefore *must* supply explicit vectors on every `add`/`upsert`
  and `query` — which we always do (vectors come from
  `providers/embeddings.py`). This keeps the store fully offline; the test suite
  asserts it by monkeypatching `socket.socket.connect` / `socket.create_connection`
  to raise.

## Chunk rows

- **id** = `Chunk.chunk_id` = `"{note_uuid}:{ordinal}"` (computed on the model).
- **documents** = `chunk.text` (the chunk body). `embed_text` is *not* persisted
  — the index never needs it again, and it is derivable (breadcrumb + body). On
  read it is reconstructed as `""`.
- **metadatas** = the design §7 fields: `note_uuid`, `note_path`, `note_title`,
  `heading_path`, `heading_text`, `heading_id`, `heading_level`,
  `heading_index`, `chunk_in_heading`, `ordinal`, `char_start`, `char_end`.

### None-metadata handling

Chroma metadata is **scalar-only and rejects `None`**. The optional fields
`heading_text` / `heading_id` / `heading_level` (None for preamble / id-less
headings) are **omitted** from the metadata dict on write, and filled back as
`None` on read via `meta.get(key)`. All required fields use `meta[key]`.

## Cosine distance → similarity

Collections use `hnsw:space = cosine`, so `query` returns a cosine **distance**
`d`. Callers want a **similarity**, computed as `similarity = 1 - d`. For unit
vectors this is the cosine similarity (identical → 1.0, orthogonal → 0.0). We
sort results best-first by similarity defensively, so the contract never relies
on Chroma's return ordering.

## Where-filter construction

Built in `_build_exclusion_where`:

- `exclude_note_uuid` → `{"note_uuid": {"$ne": uuid}}` (drop the query note's own
  chunks — design §8 self-exclusion).
- `exclude_target_uuids` → `{"note_uuid": {"$nin": [...]}}` (drop
  already-linked targets).
- **Combine with `$and` only when both clauses are present.** Chroma requires a
  `$and` list to hold **at least two** expressions, so a single clause is passed
  bare; no exclusions → no `where` at all.

`get_chunk` uses a 3-clause `$and` (`note_uuid` + `heading_index` +
`chunk_in_heading`) — always ≥ 2 clauses, so always valid.

## Fixed `query_chunks` contract

`pipeline/retrieve.py` depends on this exact signature:

```python
def query_chunks(
    self,
    query_embedding: list[float],
    k: int,
    exclude_note_uuid: str | None = None,
    exclude_target_uuids: list[str] | None = None,
) -> list[tuple[Chunk, float]]:  # (Chunk, similarity), best-first
```

`include=["metadatas", "documents", "distances"]`; each result row is
reconstructed into a `Chunk` (omitted optionals → `None`, `embed_text=""`) and
paired with `1 - distance`. `k <= 0` short-circuits to `[]`.

## Manifest layout (design §7)

Second collection, **keyed by file path** (id = path), placeholder embedding
`[0.0]`, metadata `{uuid, content_hash, last_indexed_mtime}`. Read via `get`,
never queried — keyed by path so change-detection is stat-only on the fast path.

- `get_manifest(path) -> {uuid, content_hash, last_indexed_mtime} | None`
- `upsert_manifest(path, uuid, content_hash, mtime)` — overwrites in place.
- `all_manifest_paths() -> list[str]` (for deletion sweep).
- `delete_manifest(path)`.

## Tests (`tests/test_store.py`)

Fully offline (network blocked via monkeypatched sockets). Orthonormal 4-D unit
vectors give exact, hand-checkable cosine similarities. Covers: best-first order
+ exact similarity (incl. a 45° query → ~0.707), None optional fields round-trip,
`exclude_note_uuid` / `exclude_target_uuids` / both combined, `delete_note`,
`get_chunk` neighbour fetch (and `None` misses), `k=0` and empty-upsert no-ops,
length-mismatch guard, and full manifest upsert/get/all/delete round-trip.
