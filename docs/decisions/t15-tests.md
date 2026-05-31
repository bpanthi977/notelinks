# T15 — broader integration / e2e tests

`tests/test_integration.py` exercises the `Engine` end to end against a **real**
temp corpus + a **real** `Store`, mocking only the two provider boundaries. It
complements (does not duplicate) `test_build.py` (unit-level refresh gate) and
`test_engine.py` (a single happy-path e2e).

## Scenarios covered

**1. Refresh idempotency / incrementality (driven via `Engine.refresh`).**
- `test_first_refresh_indexes_all_notes` — a 4-note corpus: first refresh indexes
  all (`indexed == 4`, chunk counts match `store._chunks.count()`, manifest holds
  all four repo-relative paths).
- `test_second_refresh_reembeds_nothing` — a second refresh re-embeds NOTHING
  (embed spy `calls == 0`, `embedded == 0`; stats all-skipped).
- `test_editing_one_note_reembeds_only_that_note` — edit one note's content +
  bump mtime via `os.utime` → exactly that note re-embedded (`spy.calls == 1`,
  `reindexed == 1`, other three skipped).
- `test_deleting_a_note_removes_its_chunks` — delete a note on disk → manifest row
  and all its chunks removed from the store.

**2. End-to-end `suggest` happy path (real judge orchestration).** Only the LLM
transport (`complete_structured`) is faked, so `retrieve.py`, `judge.py`,
`resolve_anchor`, and `Engine._finalize` all run for real.
- Envelope validates and round-trips through `Envelope.model_validate(model_dump(...))`
  per `docs/json-format.md`.
- `suggestions` confidence-sorted descending; ids reassigned `s01..` in final order.
- `target` carries link COMPONENTS only (`file_id`, optional `heading`), never a
  finished `[[id:...]]` string.
- `source_anchor` offsets bracket the verbatim `expect` in the buffer
  (`BUFFER[char_start:char_end] == expect`).
- The resonant immune note (nearest neighbour by the deterministic embedding) is
  the surfaced top suggestion; no self-link present.

**3. Finalize invariants** (judge patched at the engine boundary for precise
control of the raw suggestion list; retrieval + parsing still run for real):
- (a) already-linked exclusion — a buffer linking `[[id:uuid-immune]]` never gets
  immune re-suggested, while a fresh target survives.
- (b) no self-link — the source note's own id is dropped as a target.
- (c) per-(target note + heading) dedup — duplicate (target, heading) collapses to
  the single highest-confidence one; same target under a different heading is kept.
- (d) `top_n` cap — judge emits 5 distinct targets, `top_n = 2` keeps the two
  highest-confidence, renumbered `s01`/`s02`.

## Mocking strategy (which boundaries are faked + why)

Two provider boundaries are the only fakes; everything else (Store, Chroma,
parse, chunk, retrieve, finalize) is exercised for real.

- **Embeddings** — patched at BOTH `notelinks.index.build.embed_texts` (corpus
  indexing during `refresh`) and `notelinks.engine.embed_texts` (buffer query).
  Both must be patched because `suggest()` auto-refreshes (uses the build
  boundary) and then embeds the buffer (uses the engine boundary); a single fake
  serves both. This removes the network/model dependency while keeping real
  vector storage + ANN query in Chroma.
- **The judge** — patched at one of two layers depending on intent:
  - *Happy path*: patch `notelinks.providers.llm.complete_structured` (the
    transport). This keeps the real `judge_candidates` orchestration, target-id
    resolution, and `resolve_anchor` in the test path, so anchoring/offset logic
    is genuinely exercised end to end.
  - *Invariant tests*: patch `notelinks.engine.judge_candidates` directly to
    return a hand-built raw `Suggestion` list. This is the cleanest way to feed
    `Engine._finalize` precise inputs (self-links, dup headings, > top_n
    suggestions) without contorting an LLM-shaped response, since `_finalize` is
    the unit under test there.
- **Network guard** — an autouse fixture blocks `socket.connect` /
  `socket.create_connection`, so any accidental real call fails loudly and proves
  the suite is fully offline.

## How determinism is achieved

- A single content-keyed fake embedder (`EmbedSpy`) maps text → a small hand-built
  3-D vector by topic: error/correction text → axis 0, baking → axis 1, else →
  axis 2. The buffer's resonant chunk and the immune/control bodies all land on
  axis 0, so cosine nearest-neighbour ordering is fixed and the immune note
  reliably surfaces first. `embedding_dim=3` and `sim_floor=0.1` in Settings match
  the fake vectors.
- The spy also counts `calls` / `embedded`, which is how the incrementality tests
  assert "re-embedded nothing" vs "re-embedded exactly one note".
- Notes carry fixed `:ID:` uuids, so target ids in assertions are stable.
- `judge_candidates`' assembly is already order-deterministic (sequential, by
  first-seen group order), and `_finalize` uses a stable sort, so suggestion
  ordering/ids are reproducible run to run.
