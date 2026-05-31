# T7 — `org/parse.py` decisions

`parse_note(text: str, path: str) -> Note` turns a raw org-roam v2 buffer into
the internal `Note` model (see `models.py`). `text` is the full buffer; `path`
is repo-relative. All char offsets are 0-based codepoint indices into `text`,
matching the engine's anchor convention (design §11).

## Heading index convention (shared with the chunker, T10)

Headings are numbered **`1, 2, 3, …` in document order**. Index **`0` is
reserved** for the pre-first-heading *preamble* segment, which is the content
from the start of the buffer to the first heading. The preamble is **not** an
`OrgHeading` — the parser emits only real headings, so the smallest `index` it
ever produces is `1`. The chunker assigns `heading_index = 0` to preamble chunks.
This keeps `heading_index` consistent across parse and chunk.

## `char_start` / `char_end` semantics

For an `OrgHeading`:

- `char_start` = offset of the heading line's first `*`.
- `char_end` = `char_start` of the **next heading line of ANY level**, or
  `len(text)` at EOF.

So `[char_start, char_end)` is the heading's **directly-owned segment span**: it
includes the heading line and all body text up to (but not including) the next
heading, regardless of nesting. A parent heading's span therefore stops at its
first child, not at its next sibling — matching design §5's "each heading owns
the body text from it to the next heading (of any level)". The chunker derives a
heading's body by stripping the heading line itself.

For an `OrgLink`, `[char_start, char_end)` is the span of the **entire**
`[[…]]` construct (both brackets included).

## Title fallback

Title comes from the first `#+title:` line (`#\+title:`, **case-insensitive**).
If absent or empty, it is derived from the **filename basename**: drop a
trailing `.org`, then replace `_` and `-` with spaces (e.g.
`my_cool_note-v2.org` → `my cool note v2`).

## File `:ID:` fallback

The file-level `:ID:` and `:ROAM_ALIASES:` are read from the top
`:PROPERTIES:…:END:` drawer **only if `:PROPERTIES:` is the first non-blank line
of the buffer** (org-roam places it above `#+title:`). A missing `:ID:` yields
`id=""` (empty string, not `None`) — `Note.id` is typed `str`. Missing aliases
yield `[]`.

### `:ROAM_ALIASES:` parsing

Space-separated tokens; a **double-quoted** token may contain spaces and is kept
verbatim (quotes stripped). Example:
`:ROAM_ALIASES: "Large Language Model" RLHF` → `["Large Language Model", "RLHF"]`.
Captured for the future mention path; unused in v1 (design §4).

## Tag / TODO stripping in heading text

Heading `text` is cleaned by:

1. Stripping a single **leading `TODO ` / `DONE `** keyword (the two standard
   org keywords).
2. Stripping **trailing tag cookies** `:tag1:tag2:` (regex
   `[ \t]+(?::[\w@#%]+)+:[ \t]*$` — tags share colons; chars are word chars plus
   `@ # %`). E.g. `DONE Second Heading :tagone:tagtwo:` → `Second Heading`.

The cleaned text is what populates `OrgHeading.text` and the `ancestor_path`
entries.

## Heading own `:ID:`

A heading's own `:ID:` is captured only when a `:PROPERTIES:` drawer is the
first non-blank line **immediately following** the heading line (org-roam's
placement). Absent → `id=None`.

## `ancestor_path`

A stack tracks open headings; on each heading, entries with level `>=` the
current level are popped, and the remaining (lower-level) headings' cleaned
texts form `ancestor_path` (outermost first). Index/level come from the star
count.

## Link regex handled

`\[\[id:([^\]:]+)(?:::([^\]]*))?\](?:\[[^\]]*\])?\]` matches all four forms:

- `[[id:UUID]]`
- `[[id:UUID][desc]]`
- `[[id:UUID::search]]`
- `[[id:UUID::*Heading][desc]]`

`search_string` is the part after `::` (e.g. `*Heading` or a search string),
or `None` when absent/empty. Only `id:`-type links are extracted; other link
types (`pdf:`, `https:`, …) are intentionally ignored. Note `[1++0.00]`-style
suffixes inside non-id links and `[[…]]`-wrapped `pdf:` links are not matched
because they are not `id:` links.
