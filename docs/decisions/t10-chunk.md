# T10 — `org/chunk.py` decisions

`chunk_note(note: Note, settings: Settings) -> list[Chunk]` segments a parsed
note by heading, strips non-content, recursively token-splits each segment body,
and emits `Chunk`s with char offsets back into `note.text`. Implements design
§5 (chunking) and §6 (embedded text). All char offsets are 0-based codepoint
indices into `note.text` (the engine's anchor convention, design §11).

## Segmentation

The note is decomposed into ordered **segments**:

- **Preamble** (`heading_index = 0`): `note.text[0 : first_heading.char_start)`,
  or the whole buffer if the note has no headings. `heading_text` / `heading_id`
  / `heading_level` are `None`. Breadcrumb = the note title alone.
- **Each `OrgHeading`** (`heading_index = OrgHeading.index`, the parser's 1-based
  ordinal): the body is `note.text[<just past the heading line> : heading.char_end)`.
  We derive the body start by finding the heading line's terminating newline
  (`char_start → first '\n'`) and starting one past it; if the heading is the
  last line with no newline before `char_end`, the body is empty. We rely on the
  parser's `char_end` = start of the next heading of ANY level (t7-parse.md), so
  a parent heading's body stops at its first child — no double-counting of nested
  content.

Segments are processed in document order, so `ordinal` is a contiguous 0-based
**global** sequence and `chunk_in_heading` is 0-based **within each segment**.
`heading_index` is carried straight through and stays consistent with the parser
(0 = preamble; gaps are possible when intermediate headings have empty bodies —
that is expected and harmless).

## Stripping + the offset map (the load-bearing part)

`_clean_segment(raw_body, base)` walks the segment line-by-line
(`splitlines(keepends=True)` so newlines are preserved and offsets stay exact)
and drops:

- **Drawers**: a bare `:NAME:` opener line (e.g. `:PROPERTIES:`, `:LOGBOOK:`)
  through its matching `:END:` (inclusive).
- **`#+keyword:` lines** (`#+title:`, `#+attr_html:`, `#+date:`, …).
- **`# comment` lines** (`#` then space/EOL, but not `#+`).

**Kept verbatim**: code blocks (`#+begin_…` … `#+end_…`, including any
`#+keyword`-looking lines inside them — guarded by an `in_block` flag),
tables, lists, paragraphs.

As it keeps characters, it records `offmap`: a list where `offmap[j]` is the
original `note.text` offset of cleaned-text codepoint `j`, with one extra
trailing entry (the original offset just past the last kept char). A half-open
cleaned span `[a, b)` therefore maps to original `[offmap[a], offmap[b])`. This
is how every emitted `Chunk.char_start`/`char_end` are real offsets **into
`note.text`** — we never emit offsets into the throwaway cleaned string.

### Interior-stripped-line caveat

Because a chunk's `[char_start, char_end)` is computed from the *first* and
*last* kept cleaned positions of the chunk, the original span can enclose an
interior line that was stripped (e.g. a `#+keyword:` sitting between two kept
paragraphs that landed in the same chunk). This is intentional and harmless: the
later verbatim anchor search (design §9) is **chunk-scoped**, so an extra
stripped line inside the span never produces a wrong anchor. `Chunk.text` itself
is the cleaned body (no stripped lines).

## Recursive splitter

`_recursive_pieces` splits a cleaned segment into **atomic pieces** each
`<= chunk_max_tokens`, using the separator hierarchy
**paragraph (`\n\n`) > line (`\n`) > sentence > word (` `)** (design §5.3).
Tokens are counted with `tiktoken.get_encoding(settings.tokenizer_encoding)`.
Separators are absorbed into the preceding piece so concatenating pieces
reproduces the cleaned text exactly. If a run is unsplittable even at word level
(e.g. one giant token-dense line), `_hard_cut` slices it by token budget as a
last resort. Each piece carries a **boundary kind** (`para`/`line`/`sentence`/
`word`/`hard`/`start`) describing the separator at its left edge.

`_pack_pieces` then greedily accumulates consecutive pieces toward
`chunk_target_tokens` (allowing growth up to `chunk_max_tokens` only while still
below target), yielding target-sized chunks while never exceeding max. Each
packed chunk keeps the boundary kind of its **leading** piece.

## Overlap rule

`chunk_overlap_tokens` of context is prepended to a chunk **only** when its left
edge is a **mid-prose** cut — leading boundary kind `sentence` or `word` — and
the line at that position does **not** begin a structured item (list bullet
`-`/`+`/`*`, ordered `N.`/`N)`, or table row `|`). Paragraph/line/hard
boundaries and structured-item / table-row boundaries get **no** overlap, since
those items are self-contained (design §5.3). Overlap is realised by walking
back `overlap_tokens` from the chunk's start (`_overlap_start`, via tiktoken
decode of the trailing tokens) and lowering only `char_start` / `text` — the map
keeps offsets honest. The segment's first chunk never gets overlap.

## Merge / min handling

`_merge_small_tails` folds any chunk under `chunk_min_tokens` into the **previous
chunk of the same segment** (extending that chunk's end, keeping its boundary
kind so a tail merged back into prose stays prose). A leading sub-min chunk with
no predecessor is folded **forward** into the next chunk instead. Empty
segments (cleaned body blank after stripping) yield **no** chunk.

## heading_path / breadcrumb choices

`heading_path` (and the `embed_text` prefix) is the breadcrumb:

- Preamble → the **note title alone** (chosen over `""` so preamble chunks still
  embed with useful context per design §6).
- A heading → `"<note title> > <ancestor_path…> > <heading.text>"`, joining the
  parser's `ancestor_path` (outermost first) with the heading text. Empty parts
  are dropped from the join.

`embed_text` is `f"{breadcrumb}\n\n{body}"` (design §6), so it always starts with
the breadcrumb followed by a blank line and then the cleaned body.

## Tests

`tests/test_chunk.py` builds a crafted note (preamble + nested headings + a
property drawer + a `:LOGBOOK:` drawer + a `#+keyword` + a `# comment` + a long
prose paragraph + a self-contained list) and asserts: drawers/keywords/comments
are stripped; `note.text[char_start:char_end]` contains every chunk body line
(offset round-trip); `heading_index`/`chunk_in_heading`/`ordinal` correctness;
prose splits carry overlap while list splits do not; token bounds are respected;
`embed_text` starts with the breadcrumb; empty drawer-only segments emit no
chunk; and a real `notes/mamba.org` chunks without error.
