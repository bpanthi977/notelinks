# T9 — Retrieval & candidate selection

`src/notelinks/pipeline/retrieve.py` implements design §8 in two layers: a pure
selection core and a thin Store-driven query wrapper.

## `hits_by_source` shape

```python
list[tuple[Chunk, list[tuple[Chunk, float]]]]
#         source_chunk, ranked [(target_chunk, similarity)] desc
```

One entry per source (current-note) chunk. The inner list is the source's
nearest targets ordered by cosine similarity **descending** — i.e. its rank-1
hit is element `[0]`. `select_candidates` relies on this ordering and does not
re-sort within a source; it only re-sorts *across* sources within a rank.

## `select_candidates` semantics (pure, no infra)

Applied in order:

1. **Floor** — drop any hit with `similarity < sim_floor`.
2. **Per-source cap** — keep each source's top `per_source_n` surviving targets
   (slice of the already-desc list). Sources left with zero hits are dropped.
3. **Round-robin by rank** — for `rank = 0, 1, 2, …`, gather that rank's
   candidate from every source that still has one, sort *that rank slice* by
   score descending, and append. Stop appending once `global_cap_m` is reached.

This guarantees **every source chunk's best is represented before any chunk's
second** (rank-1s, score-ordered across sources, all precede rank-2s), keeping
cost bounded and coverage broad. Within a single rank, ties are broken by score.

## Dedup semantics (`retrieve_candidates` wrapper)

A target chunk hit by multiple source chunks keeps **only its single
best-scoring pairing** (design §8). Implemented as a `dict` keyed by
`target_chunk.chunk_id` (`"{note_uuid}:{ordinal}"`), retaining the
`(source_chunk, target_chunk, similarity)` with the highest similarity. The
surviving deduped hits are regrouped under their winning source chunk and
re-sorted desc, producing a fresh `hits_by_source` that feeds
`select_candidates`. So dedup happens *before* the per-source cap and
round-robin merge — a target only ever competes from its strongest source.

## Codes to the fixed Store contract

The wrapper depends only on the agreed signature (typed via a `Protocol`,
`_Store`); it does **not** implement the Store:

```python
store.query_chunks(
    embedding, k=settings.top_k,
    exclude_note_uuid=current_note_uuid,
) -> list[tuple[Chunk, float]]  # (target_chunk, cosine_similarity), best-first
```

Only **self-exclusion** is pushed into the query. **Already-linked exclusion is
heading-level** and applied as a post-query filter on candidate chunks via
`pipeline/exclude.py:chunk_already_linked` — a note-level `$nin` would
over-exclude headings the source hasn't linked (a link to one heading of a note
would suppress the whole note). See design §8 for the per-form rules
(heading-text link → that heading; heading own-id link → that heading; bare
whole-note link → preamble only). All four knobs (`top_k`, `per_source_n`,
`global_cap_m`, `sim_floor`) come from the injected `Settings`.

## Tests

`tests/test_retrieve.py` covers the floor dropping weak hits, the per-source cap,
round-robin ordering (asserting every rank-1 precedes any rank-2 and intra-rank
score ordering), global-cap truncation, and a `FakeStore`-backed
`retrieve_candidates` test verifying a target seen by two sources is deduped to
its best pairing and that the Store contract is called with the configured
knobs/filters.
