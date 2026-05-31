# Design

How the tool turns a *current note* into a ranked set of suggested links to
other notes. This document records the settled design decisions; see
[`objective.md`](./objective.md) for *what* we're building and
[`json-format.md`](./json-format.md) for the authoritative engine↔frontend
output contract.

## 1. Overview

v1 surfaces **idea-resonance** links: a passage in the current note conceptually
echoes a passage in another note (analogy, shared mechanism, contradiction,
instance-of, generalization), even with no shared vocabulary. The unit of
connection is a **passage**, not a whole note.

Two-stage pipeline:

1. **Retrieve** — embed structural chunks of every note into a vector store; for
   each chunk of the current note, retrieve nearest-neighbour chunks from other
   notes as candidates. High recall, cheap.
2. **Judge + explain** — an LLM decides which candidates are genuine,
   worth-linking connections, classifies the type, writes a rationale, and
   produces the link anchor. High precision, the rationale.

The explicit *mention* path (detecting that the note names a concept that has
its own note) is deferred; when added it slots in as another candidate source
feeding the same judge, changing nothing downstream.

## 2. Runtime architecture (core / adapter split)

v1 ships as a CLI, but a later phase runs as a **daemon that keeps the index
fresh and serves suggestions over REST**. The code is structured so that switch
is a thin adapter swap:

- **`engine.py` — transport-agnostic core.** An `Engine(config)` owns the
  long-lived state (Chroma client/collections, embedding + judge provider
  clients, the manifest) and exposes:
  - `refresh()` — incremental corpus refresh.
  - `suggest(buffer_text, file_path) -> Envelope` — full pipeline for one note,
    returning the pydantic `Envelope` (serializes identically for CLI stdout or
    a REST body).
  - `watch()` *(future)* — continuous refresh for the daemon.
- **Adapters are thin**, holding state only through `Engine`:
  - `cli.py` (typer) constructs an `Engine`, calls `refresh()` + `suggest()`,
    prints JSON.
  - `api.py` / `daemon.py` *(future)* construct one `Engine` at startup, reuse
    it across requests, drive `refresh()`/`watch()` on file events.
- **Constraints this imposes now:** no per-call global singletons; Chroma and
  provider clients are constructed once and injected; `index/store` is a
  long-lived handle (open once, reuse), not build-and-teardown per call;
  `pipeline/` steps are **stateless functions**. Future REST/watch deps are not
  added in v1.

### Module layout

```
src/notelinks/
  config.py    pydantic-settings: model names, base_url, paths, tuning knobs
  models.py    output models (mirror json-format.md) + internal models
  engine.py    Engine(config): refresh(), suggest() -> Envelope   (CORE)
  cli.py       typer adapter -> Engine (v1 transport)
  providers/
    embeddings.py  embed_texts(list[str]) -> list[vector]   (SWAP POINT 1)
    llm.py         judge(...) -> structured (+ prompt cache)  (SWAP POINT 2)
  org/
    parse.py   .org -> Note(id, title, aliases, headings[], links[])
    chunk.py   heading-segment + recursive-split chunking with char offsets
  index/
    store.py   long-lived Chroma handle: chunk collection + manifest collection
    build.py   incremental refresh (mtime-gated, hash-confirmed)
  pipeline/
    retrieve.py  stateless: candidate generation + selection
    judge.py     stateless: group-by-source-chunk judge calls
tests/         sample .org fixtures + unit/e2e tests
```

## 3. Tech stack

- **Python**, managed with **uv**.
- **Provider access:** OpenAI Python SDK pointed at OpenRouter
  (`base_url=https://openrouter.ai/api/v1`, one `OPENROUTER_API_KEY` for both
  embeddings and chat). Isolated behind two swap points: `providers/embeddings.py`
  and `providers/llm.py`.
- **Embedding model:** `openai/text-embedding-3-small` (1536-dim). Retrieval
  needs recall; the judge supplies precision, so the smaller/cheaper model is
  the right trade.
- **Judge model:** latest Claude **Sonnet** via OpenRouter.
- **Vector DB:** **ChromaDB**, embedded/persistent.
- **Org parsing:** fully custom (own the char offsets + link spans).
- **Tokeniser:** `tiktoken` (`cl100k_base`).
- **CLI:** `typer`.

## 4. Corpus & link model

- **org-roam v2.** One note = one `.org` file with a file-level `:ID:` (UUID, in
  the top property drawer) and `#+title:`. Optional `:ROAM_ALIASES:` (captured
  for the future mention path; unused in v1).
- **Note identity = the file-level `:ID:` UUID.** The index maps UUID ↔ file
  path ↔ title.
- **Headings** are addressed *within* a file by the file's UUID + a search
  string (not their own ID).
- **Link syntax (parse and emit):**
  - To a note: `[[id:UUID][description]]`
  - To a heading: `[[id:UUID::*Heading text][description]]`
  - The general `[[id:UUID::search string]]` form is *parsed*; we *emit* the
    `*Heading` form for heading targets.
- **The engine emits link *components only*** (`file_id`, optional
  `heading.{text,id,level}`), never a finished `[[id:...]]` string — the elisp
  frontend assembles it (see the table in `json-format.md`). A heading with its
  own org-id is linked directly by that id.

### Parser must extract

File-level ID, title, ROAM_ALIASES; all headings (text, level, own ID if
present, char range, ordinal index); all existing `[[id:...]]` links (target
UUID + optional search string + position). Drawers (`:PROPERTIES:`,
`:LOGBOOK:`, …), `#+keyword:` lines, and `# comments` are stripped.

## 5. Chunking

No special-casing — everything substantive inside a heading is chunked.

1. **Strip** drawers, `#+keyword:` lines, `# comments`. Keep everything else
   **including code blocks, tables, lists, paragraphs**.
2. **Segment by heading:** each heading owns the body text from it to the next
   heading (of any level); content before the first heading is the
   root/preamble segment owned by the note. Nesting is tracked to build each
   heading's ancestor path.
3. **Recursively split** each segment body into size-bounded chunks with a
   separator hierarchy preferring **paragraph (`\n\n`) > line (`\n`) > sentence
   > word (` `)**. Chunks never cross a heading boundary.

- **Sizing** (token-based, `tiktoken`): target ~256 tokens, max ~400. Sub-min
  tail chunks merge into the previous chunk of the same heading; empty segments
  yield no chunk.
- **Overlap:** ~32 tokens **only when splitting prose** (preserves idea
  continuity across a mid-paragraph cut). **No overlap** at list-item or
  table-row boundaries (structured items are self-contained).
- All sizes tunable in `config.py`.

## 6. Embedded text

Each chunk is embedded as **breadcrumb + body**:

```
<note title> > <ancestor heading> > … > <chunk heading>

<chunk body>
```

Root/preamble chunks use the note title alone. The breadcrumb is for embedding
context only — char-offset anchors still point at the body span in the file, and
the judge receives fuller context separately (§9).

## 7. Vector index & incremental refresh

- **Index location:** the Chroma store is **one index per corpus**, defaulting to
  `<corpus_dir>/dbs/notelinks_index` (derived from `NOTELINKS_CORPUS_DIR`). An
  explicit `NOTELINKS_INDEX_DIR` overrides it; the dir (and parents) is created
  on open. There is **no shared default** — with neither a corpus nor an explicit
  index dir, opening the store raises rather than silently picking a path.
- **Chunk collection** (Chroma): embeddings supplied by us; created **without**
  Chroma's built-in embedding function and always added with explicit
  embeddings, so Chroma never downloads its bundled ONNX model. Distance =
  **cosine**.
- **Chunk id:** `"{file_uuid}:{ordinal}"`. On change, delete the note's chunks
  via `where={"note_uuid": uuid}` and re-add.
- **Per-chunk metadata:** `note_uuid`, `note_path` (repo-relative),
  `note_title`, `heading_path`, `heading_text`, `heading_id` (if any),
  `heading_level`, `heading_index` (heading/segment ordinal; 0 = preamble),
  `chunk_in_heading`, `char_start`, `char_end`.
  - `heading_index` + `chunk_in_heading` let the judge's target-side context
    fetch an adjacent chunk **only within the same heading**: prev exists iff
    `chunk_in_heading > 0`; next = look up `(heading_index, chunk_in_heading+1)`.
  - `heading_text` / `heading_id` / `heading_level` feed the `target.heading`
    link components.
- **Manifest:** a **second Chroma collection**, one row per note, **keyed by
  file path**, placeholder embedding `[0.0]`, metadata
  `{uuid, content_hash, last_indexed_mtime}`. Read via `get`, never queried.
  Keyed by path so change-detection is **stat-only** on the fast path. (Chroma
  metadata is scalar-only.)

**Incremental refresh — mtime-gated, hash-confirmed**, per `.org` file:

```
mtime = stat(path).st_mtime
row   = manifest.get(path)
if row is None:                         # new file
    index(path); manifest.upsert(path, hash=H, last_indexed=mtime)
elif mtime > row.last_indexed_mtime:    # touched since last check
    H = sha256(read(path))
    if H != row.content_hash: reindex(path)
    manifest.upsert(path, hash=H, last_indexed=mtime)   # advance even if hash matched
else:
    skip                                # untouched, no file read
# deletions: manifest paths absent on disk → delete chunks + manifest row
```

Advancing the watermark even when the hash matched means a touched-but-unchanged
file isn't re-hashed on future runs.

## 8. Retrieval & candidate selection

- **Queries** = every chunk of the current note (its own index entry refreshed
  first).
- **Query filter:** exclude self (`note_uuid != current`) at the vector query.
- **Already-linked exclusion is heading-level** (`pipeline/exclude.py`), applied
  as a post-query filter on candidate chunks (not a note-level `$nin` on the
  query, which would over-exclude headings the source hasn't linked). A candidate
  target chunk is dropped when the source already references *its* heading:
  - `[[id:U::*H]]` → drops only chunks of note `U` under heading text `H`; other
    headings of `U` stay eligible.
  - `[[id:HID]]` (a heading's own org-id) → drops the chunk carrying `heading_id == HID`.
  - `[[id:U]]` (bare whole-note link) → drops only `U`'s **preamble**
    (`heading_index == 0`); headed sections stay eligible.

  The output stage repeats this at link-identity granularity
  (`suggestion_duplicates_link`) as belt-and-suspenders: a suggestion is dropped
  if it would assemble to a link the note already has (e.g. the judge promoting a
  non-excluded chunk to a whole-note target that duplicates a bare link).
- **top_k = 8** neighbours per source chunk.
- A **candidate** = `(source_chunk, target_chunk, score)`. Duplicate
  `(source, target)` pairs dedup; a target chunk hit by multiple sources keeps
  the best-scoring pairing.
- **Selection (combined per-source + global), in order:**
  1. Drop pairs below a cosine-similarity **floor**.
  2. Per source chunk, keep its **top-N (N=3)** targets.
  3. **Round-robin by rank** across source chunks — all rank-1s (score-ordered),
     then rank-2s, then rank-3s — appending until the **global cap M=40** is hit.

  Every source chunk's best is represented before any chunk's second, and cost
  stays bounded. `top_k`, N, M, and floor are all configurable.

## 9. Judge

- **Structured output** via JSON-schema `response_format`, validated by
  pydantic. The judge may **reject** (return zero suggestions) — essential for
  precision.
- **Batching: one call per source chunk**, presenting that chunk's up-to-N
  candidate targets together (so it can choose among competing targets and avoid
  over-linking one passage).
- **Context per call:**
  - *Source side:* the **full current note**, with the source chunk marked.
    Because this prefix repeats across calls, it is sent as a **cached prefix**
    (Anthropic prompt caching via OpenRouter `cache_control`) to control cost.
  - *Target side (per candidate):* target chunk + breadcrumb + note title + up
    to **1 adjacent chunk** of context, only if it exists **without crossing a
    heading boundary**.
- **Target granularity:** default to the target chunk's owning **heading**; the
  judge may instead choose the whole **note** when the connection is note-wide.
- **Anchor — judge returns verbatim text, engine computes offsets.** The judge
  returns either the exact substring to **wrap**, or **insert** prose (carrying
  a `{{link}}` slot) plus the verbatim sentence it attaches to. The engine
  locates that text in the buffer and fills the `source_anchor` fields
  (`char_start`/`char_end`, `expect`, ~40-char `before`/`after`, `template`,
  `link_description`). If not found verbatim, fall back to the chunk span or
  drop. wrap vs insert falls out of empty-vs-non-empty region — no mode flag.

## 10. Ranking & output

- **Output** = a single JSON object on stdout per [`json-format.md`](./json-format.md).
- **Ranking:** sorted by `confidence` descending (tie-break by retrieval score),
  capped to **top_n** (configurable, default ~12).
- **Cross-target dedup:** at most **one suggestion per (target note + heading)**,
  keeping the highest confidence; the same note may still appear under different
  headings.
- **Engine-enforced invariants:** no self-links; no suggestion that would
  duplicate an existing link (heading-level — see §8); output sorted + capped.
- **Connection type enum:** `elaborates`, `analogous-mechanism`, `contradicts`,
  `instance-of`, `generalizes`, `mention`.

## 11. Invocation

- **Input:** the current note is provided as **buffer text via stdin** (so it
  works *while writing*, including unsaved edits). **No file path is taken** —
  the note is identified by its own `:ID:` parsed from the buffer, which is also
  what drives self-exclusion; the frontend already knows which buffer it queried.
  `char_*` offsets are 0-based Unicode codepoints into that buffer. Corpus root
  comes from `NOTELINKS_CORPUS_DIR`, overridable by `--corpus`.
- `content_hash` = sha256 of the stdin buffer; `queried_at` = current UTC.
- The envelope's `source` therefore has **no `file`** field — identity is
  `source.id` (uuid) + `source.title`. (`target.file` is unaffected: it comes
  from corpus indexing.)
- **Commands:**
  - `suggest [--corpus DIR]` (buffer on stdin): incremental refresh of the whole
    corpus → query the current buffer → emit JSON.
  - `index [--rebuild] [--corpus DIR]`: first/forced full build.

## 12. Data models & config

- **`models.py`** mirrors `json-format.md` exactly for output (`Envelope`,
  `Source`, `Suggestion`, `SourceChunk`, `Target`, `Heading`, `SourceAnchor`)
  plus internal models (`Note`, `OrgHeading`, `Chunk`, `Candidate`, judge
  in/out).
- **`config.py`** (`pydantic-settings`, env-overridable) defaults:
  `OPENROUTER_BASE_URL=https://openrouter.ai/api/v1`;
  `embedding_model=openai/text-embedding-3-small`, `embedding_dim=1536`; judge =
  current OpenRouter Sonnet slug; `corpus_dir`, `index_dir` (defaults to
  `<corpus_dir>/dbs/notelinks_index`; see §7); chunk
  `target_tokens=256`, `max_tokens=400`, `overlap_tokens=32`, `min_tokens`;
  retrieval `top_k=8`, `per_source_n=3`, `global_cap_m=40`, `sim_floor`; output
  `top_n=12`; tiktoken `cl100k_base`.

## 13. Out of scope (v1)

- Explicit mention path as its own candidate source (architecture leaves room).
- Daemon + REST transport (core/adapter split prepares for it; deps not added).
- Surprise/novelty weighting, few-shot calibration to existing links, and
  evaluation methodology.
