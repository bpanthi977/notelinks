# T12 — `src/notelinks/pipeline/judge.py`

Turns retrieval `Candidate`s into `Suggestion`s (design §9). Stateless, two
layers mirroring `retrieve.py`: a pure anchor-resolution core
(`resolve_anchor`) and the orchestration (`judge_candidates`). Ranking, dedup,
`top_n`, and invariants are the engine's job (T13, §10) — NOT here.

## Grouping

`judge_candidates` groups candidates **by source chunk** (`chunk_id`), one judge
call per source chunk (design §9). Presenting a source chunk's competing targets
together lets the judge choose among them and avoid over-linking one passage.
First-seen order is preserved so output ids are stable within a run.

## Message layout (cached prefix + per-call suffix)

Two messages per call:

1. `{"role": "system", "content": _SYSTEM_PROMPT}` — the resonance-task
   instructions (static).
2. `{"role": "user", "content": [<part1>, <part2>]}`:
   - **part1 = cached prefix**: the FULL current note (`note.text`) wrapped via
     `llm.cached_text(...)`, which attaches an Anthropic `ephemeral`
     `cache_control` breakpoint. Because the same note text repeats across all
     of this note's per-source-chunk calls, this prefix is cached upstream
     (OpenRouter → Anthropic) — the cost-control win.
   - **part2 = per-call suffix** (cache-miss tail): the marked SOURCE chunk
     (heading + `[char_start, char_end)` + body), then the candidate target
     passages.

The system prompt is intentionally not in the cached user part; the heavy,
repeated payload is the full note, which is what we cache.

## Neighbour-context fetch

For each candidate target we include **at most one** adjacent same-heading chunk
via `store.get_chunk(note_uuid, heading_index, chunk_in_heading ± 1)`
(`_fetch_neighbour`): prefer the previous chunk (only when
`chunk_in_heading > 0`), else the next. `store.get_chunk` returns `None` when the
neighbour crosses the heading boundary or doesn't exist (design §7/§9 — never
cross a heading), and we simply omit it. It is labelled "for reference only" in
the prompt so the judge anchors against the source, not the neighbour.

## Anchor resolution + fallback

`resolve_anchor(anchor, source_chunk, buffer) -> SourceAnchor | None` is the pure
core:

- Search for `anchor.expect` **only within**
  `buffer[source_chunk.char_start : source_chunk.char_end]` (chunk-scoped, per
  the t4-models deferred-ambiguity note). This eliminates cross-buffer
  collisions.
- `mode="wrap"`: region = the matched `expect` span → `char_start`/`char_end` of
  that span in the buffer; `expect` = span text; `template="{{link}}"`;
  `link_description` = span text.
- `mode="insert"`: empty region (`char_start == char_end`) at the END of the
  matched `expect` span; `expect=""`; `template = anchor.insert_text` (prose
  carrying `{{link}}`); `link_description` defaults to the span text in the pure
  core, then `judge_candidates` **overrides it to the target note title** (a
  more sensible default for an inserted-prose link).
- `before`/`after` = up to ~40 chars of buffer flanking the region, **computed
  by the engine** from the buffer (never produced by the LLM — models paraphrase
  long spans).
- Not found in the chunk span → returns `None`.

**Fallback choice (drop, not whole-chunk wrap).** If `resolve_anchor` returns
`None`, `judge_candidates` **drops** the suggestion. Rationale: a non-matching
`expect` means the judge paraphrased or pointed outside the chunk, so the
anchor/rationale pairing is untrustworthy; dropping favours precision and is
consistent with the judge's reject-by-default stance. The whole-chunk-wrap
fallback remains a viable alternative (wrap `source_chunk.char_start..char_end`)
and could be enabled later if recall proves too low; it is deliberately not the
default.

## Duplicate-occurrence handling (deferred ambiguity)

When `expect` occurs more than once **inside one chunk span**, `resolve_anchor`
picks the **first** occurrence (`str.find`). This is the only residual case of
the t4-models deferred ambiguity (chunk-scoping removed the rest). `JudgeAnchor`
stays minimal (`mode`, `expect`, `insert_text`); we did not adopt the proposed
context-snippet/locator split in v1.

## Suggestion assembly

- `target` = `Target(file=note_path, title=note_title, file_id=note_uuid,
  heading=...)`. `heading` = `TargetHeading(text, id, level)` from the target
  chunk's `heading_text/heading_id/heading_level`, UNLESS `raw.target_is_note`
  or the chunk has no heading (preamble) → `None` (file-level target). `level`
  falls back to 1 if the chunk's `heading_level` is `None` but a heading text
  exists (defensive; shouldn't normally happen).
- `source_chunk` = `SourceChunk(text, heading=source heading_text or None,
  char_start, char_end)`.
- `target_excerpt` = target chunk text, truncated to `_TARGET_EXCERPT_MAX`
  (600 chars).
- `type`, `confidence`, `why` from the raw suggestion; `id` = `s01`, `s02`, …
  (stable within the run, counted only over *kept* suggestions).
- A judge-returned `target_chunk_id` not among the offered candidates is ignored
  defensively.

## Judge prompt — key instructions

`_SYSTEM_PROMPT` (kept tight):

- Defines the **idea-resonance** task (analogy / shared mechanism /
  contradiction / instance-of / generalization / elaboration, **even with no
  shared vocabulary**); unit = a passage.
- Requires classifying `type` from EXACTLY the enum (`elaborates`,
  `analogous-mechanism`, `contradicts`, `instance-of`, `generalizes`,
  `mention`), with a one-line gloss per value.
- Demands **precision over recall**: suggest only GENUINE, worth-linking
  connections; an **empty** list is valid and often correct; reject topical /
  keyword overlap.
- For each accepted candidate: return the offered `target_chunk_id`, a 1–5
  `confidence`, a tight one-sentence `why` (the shared idea, not a summary),
  `target_is_note` (false = link the chunk's heading, the default), and an
  `anchor` whose `expect` is **copied verbatim from the SOURCE chunk** (wrap a
  short phrase, or insert authored prose containing `{{link}}` after a verbatim
  sentence). Explicitly tells the model **not** to emit before/after offsets —
  the engine computes those.
- Output must match `JudgeResponse` / `RawJudgeSuggestion` (enforced by
  `complete_structured`'s strict json_schema response_format).

## Tests (`tests/test_judge.py`, no network)

- `resolve_anchor`: wrap (offsets + before/after), insert (empty region +
  template), not-found → `None`, chunk-scoped (ignores matches outside the
  span), duplicate-in-chunk picks first.
- `judge_candidates` with `llm.complete_structured` monkeypatched + a fake store
  returning a canned neighbour: full `Suggestion` assembly (Target components,
  SourceAnchor, type/confidence/why, cached-prefix breadcrumb), `target_is_note`
  → no heading, insert → target-title `link_description`, empty response → no
  suggestions, unanchorable → dropped, grouping → one call per source chunk.

`uv run ruff check` and `uv run pytest tests/test_judge.py` both pass.
