# T4 — `src/notelinks/models.py`

All pydantic v2 models for notelinks. Two families: **output** models that mirror
the wire contract, and **internal** models used only by the pipeline.

## Model inventory

### A) Output models (mirror `docs/json-format.md` EXACTLY)

| Model | Fields |
|-------|--------|
| `ConnectionType` (StrEnum) | `elaborates`, `analogous-mechanism`, `contradicts`, `instance-of`, `generalizes`, `mention` |
| `Source` | `file: str`, `title: str`, `id: str`, `queried_at: str`, `content_hash: str` |
| `SourceChunk` | `text: str`, `heading: str \| None`, `char_start: int`, `char_end: int` |
| `TargetHeading` | `text: str`, `id: str \| None`, `level: int` |
| `Target` | `file: str`, `title: str`, `file_id: str`, `heading: TargetHeading \| None` |
| `SourceAnchor` | `char_start: int`, `char_end: int`, `expect: str`, `before: str`, `after: str`, `template: str`, `link_description: str` |
| `Suggestion` | `id: str`, `type: ConnectionType`, `confidence: int (1–5)`, `why: str`, `source_chunk: SourceChunk`, `target_excerpt: str`, `target: Target`, `source_anchor: SourceAnchor` |
| `Envelope` | `version: int`, `source: Source`, `suggestions: list[Suggestion]` |

### B) Internal models (pipeline only — NOT the contract; design §4–§8)

| Model | Fields |
|-------|--------|
| `OrgLink` | `target_uuid: str`, `search_string: str \| None`, `char_start: int`, `char_end: int` |
| `OrgHeading` | `text: str`, `level: int`, `id: str \| None`, `char_start: int`, `char_end: int`, `index: int`, `ancestor_path: list[str]` |
| `Note` | `id: str`, `path: str`, `title: str`, `aliases: list[str]`, `headings: list[OrgHeading]`, `links: list[OrgLink]`, `text: str` |
| `Chunk` | `note_uuid`, `note_path`, `note_title`, `heading_path`, `heading_text: str\|None`, `heading_id: str\|None`, `heading_level: int\|None`, `heading_index: int`, `chunk_in_heading: int`, `ordinal: int`, `char_start: int`, `char_end: int`, `text: str`, `embed_text: str`; computed `chunk_id -> "{note_uuid}:{ordinal}"` |
| `Candidate` | `source_chunk: Chunk`, `target_chunk: Chunk`, `score: float` |

### C) Judge I/O (MINIMAL skeleton — `pipeline/judge.py` owns/extends these)

| Model | Fields |
|-------|--------|
| `JudgeAnchor` | `mode: Literal["wrap","insert"]`, `expect: str`, `insert_text: str \| None` |
| `RawJudgeSuggestion` | `target_chunk_id: str`, `type: ConnectionType`, `confidence: int (1–5)`, `why: str`, `anchor: JudgeAnchor`, `target_is_note: bool` |
| `JudgeResponse` | `suggestions: list[RawJudgeSuggestion]` |

## Naming / typing choices

- **`ConnectionType` is a `StrEnum`** (Python 3.12, required by `pyproject.toml`),
  not `class C(str, Enum)`. Ruff's `UP042` flags the latter; `StrEnum` serializes
  to the same hyphenated string values under `model_dump(mode="json")`. Verified:
  `type == "analogous-mechanism"`.
- **`TargetHeading`** is the name for the nested `target.heading` object. The
  contract key stays `heading` (the field on `Target`); `TargetHeading` is just
  the class name, avoiding a clash with the bare `heading: str | None` string
  field on `SourceChunk`. design §12 calls it `Heading`; renamed to
  `TargetHeading` for clarity since two different `heading`-shaped things exist.
- **`X | None`** everywhere (modern union syntax), never `Optional`.
- **`confidence` bounds** enforced with `Field(ge=1, le=5)` on both the output
  `Suggestion` and the judge `RawJudgeSuggestion`. Out-of-range values raise
  `ValidationError` (tested 0 and 6).
- **`Chunk.chunk_id`** is a pydantic `@computed_field` `@property`, so it appears
  in `model_dump()` and is derived, never stored — single source of truth for the
  `"{uuid}:{ordinal}"` chunk id (design §7).
- **Collection fields** (`aliases`, `headings`, `links`, `ancestor_path`) use
  `Field(default_factory=list)` so a `Note`/`OrgHeading` can be built incrementally.
- **Judge models** use `ConfigDict(extra="forbid")` since they back a JSON-schema
  `response_format`; rejecting unknown keys keeps the judge contract tight. They
  are deliberately lean and flagged in-code as owned by the later `judge.py` task.
- **Import-light:** only `enum`, `typing`, and `pydantic`. No chromadb / openai.

## Contract-mirroring note

`Envelope(...).model_dump(mode="json")` reproduces the `json-format.md` example
key structure precisely. A round-trip smoke test constructs a full `Envelope`
from the example values and asserts every level's key set:

- top level → `{version, source, suggestions}`
- `source` → `{file, title, id, queried_at, content_hash}`
- `suggestion` → `{id, type, confidence, why, source_chunk, target_excerpt, target, source_anchor}`
- `source_chunk` → `{text, heading, char_start, char_end}`
- `target` → `{file, title, file_id, heading}`; `target.heading` → `{text, id, level}` (or `null`)
- `source_anchor` → `{char_start, char_end, expect, before, after, template, link_description}`

`ruff check` passes and the smoke test passes.

## Deferred: JudgeAnchor occurrence ambiguity

`JudgeAnchor` currently carries only `mode` + `expect` (the verbatim text to
wrap, or the sentence to insert after). **This is ambiguous when the same
`expect` string occurs more than once in the buffer** — the engine can't tell
which occurrence the judge meant. We **deliberately deferred** solving this in
v1; the model stays minimal. To revisit when implementing `pipeline/judge.py`
(T12) and the engine's anchor→offset resolution (design §9).

Context that shrinks the problem (already true by design):

- The judge runs **one call per source chunk**, and every `Chunk` carries its
  `[char_start, char_end)`. So the engine should search for `expect` **only
  within that source chunk's span** (≈256–400 tokens), not the whole note. That
  removes all cross-buffer collisions; the only residual case is the same string
  appearing twice *inside one chunk*.

Proposed solutions for when we pick this up (not implemented):

1. **Context snippet + link_text (recommended).** Judge returns `context` = a
   verbatim, locally-unique snippet from the chunk (≈a clause/sentence) that
   *contains* the link span, plus `link_text` = the exact phrase within `context`
   to wrap (or `insert_text` placed after `context` for insert mode). Engine
   finds `context` (unique in chunk) → finds `link_text` within it → offsets,
   then computes `before`/`after` itself. Separates the *locator* from the *link
   span*; the LLM only has to copy contiguous spans.
2. **expect + before/after hint words.** Judge returns `expect` plus a few
   verbatim words of `before`/`after`; engine matches `expect` framed by the
   hints within the chunk. Mirrors the wire format but needs three accurate
   verbatim copies.
3. **Require `expect` to be unique.** Keep a single `expect` but instruct the
   judge to make it long enough to occur exactly once in the chunk. Simplest,
   but conflates link text with locator (forces wrapping a whole sentence even
   when only a short phrase should be the link).

Regardless of choice, the wire-level `before`/`after` (~40 chars) should be
**computed by the engine from the buffer**, not copied by the LLM, since models
paraphrase long spans.
