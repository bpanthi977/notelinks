# Engine ↔ Emacs JSON contract

The engine (CLI) is the only producer; the emacs/elisp frontend is the only
consumer. One engine run = one **current note** (the query). Output is a single
JSON object on stdout.

## Division of labor

- **Engine** decides *what* to link, *where* it attaches (offsets + verification
  text), the connection *type*, the *why*, and the *template* of text that lands
  in the buffer (with a `{{link}}` slot). It emits **components only**, never a
  finished `[[id:...]]` string.
- **Elisp** assembles the actual org link from `target` components, applies the
  edit to the **live buffer**, re-anchoring via `expect`/`before`/`after`, and
  handles navigation/preview.

## One edit operation, two shapes

Every accept is the same operation: **replace the region `[char_start,
char_end)` with `template` (after filling `{{link}}`)**. There is no mode flag —
the shape falls out of the region:

- **wrap-span**: non-empty region. `expect` = the span text, `template` =
  `"{{link}}"`, `link_description` = the span text → the visible text is
  unchanged but becomes a link.
- **insert**: empty region (`char_end == char_start`, `expect == ""`).
  `template` is engine-authored prose carrying `{{link}}`; `before`/`after`
  pin the insertion point.

Elisp applies both with identical code.

## Position model

- `char_*` fields are **0-based Unicode codepoint offsets** into the buffer text
  the engine was given. Emacs `point` = `char + 1` for a UTF-8/LF buffer.
- Offsets are **search anchors, not addresses**. Truth is the text
  (`before`/`expect`/`after`).

Two distinct kinds of drift make raw offsets unsafe, and elisp handles both by
resolving to **markers** the moment results arrive:

1. **Async drift** — the engine runs while the user keeps typing, so offsets can
   already be stale before any edit is applied.
2. **Accept-cascade drift** — accepting one suggestion inserts/wraps text, which
   shifts every offset *after* it, invalidating the remaining suggestions.

**On receiving the result**, elisp resolves each suggestion exactly once:

- Search near `char_start` for `expect` framed by `before`/`after` (for an empty
  region, find the point where `before`/`after` meet). This pins the true
  location regardless of async drift.
- Drop **markers** at the resolved start and end (a single marker for an empty
  region). From here on the markers — not the offsets — define the region.
- If no confident match is found, that suggestion is marked unanchorable and
  reported; it is never applied via blind `goto-char`.

**On accept**, elisp replaces the marker region with the filled `template`.
Because markers move with the buffer, accepting earlier suggestions
automatically keeps later ones pointing at the right text — no re-search. A
final `expect`-still-matches check guards against any unexpected edit inside the
marked region.

Running the engine on the **current buffer contents** (elisp passes them in)
keeps async drift small; markers absorb the rest.

## Envelope

```jsonc
{
  "version": 1,
  "source": {
    "file": "notes/active-inference.org",   // repo-relative
    "title": "Active Inference",
    "id": "A1B2-...",                        // org-id of the note (always present)
    "queried_at": "2026-05-30T12:00:00Z",
    "content_hash": "sha256:..."             // of the buffer text at query time
  },
  "suggestions": [ /* ranked by confidence desc, capped to top-N */ ]
}
```

## Suggestion

```jsonc
{
  "id": "s01",                               // stable within this run
  "type": "analogous-mechanism",             // see enum below
  "confidence": 4,                           // 1–5
  "why": "Both cast error-correction as the driver of updating.",

  // ---- DISPLAY: what resonated (the user judges on these) ----
  "source_chunk": {
    "text": "Prediction error is the signal that...",
    "heading": "Error as signal",            // heading in the CURRENT note, or null
    "char_start": 1180, "char_end": 1402
  },
  "target_excerpt": "The immune system tunes itself by...",

  // ---- NAVIGATION + link assembly (components only) ----
  "target": {
    "file": "notes/immune-memory.org",
    "title": "Immune Memory",
    "file_id": "C3D4-...",                   // org-id of the note (always present)
    "heading": {                             // or null = file-level target
      "text": "Affinity maturation",
      "id": "E5F6-...",                      // org-id of the heading, or null
      "level": 2
    }
  },

  // ---- EDIT: replace [char_start,char_end) with filled template ----
  "source_anchor": {
    "char_start": 1190,
    "char_end": 1210,                        // == char_start for an insert
    "expect": "error-correction",            // text currently in the region ("" for insert)
    "before": "...driven by ",               // ~40 chars before, to disambiguate
    "after": " across both",                 // ~40 chars after
    "template": "{{link}}",                  // text to place; prose w/ {{link}} for insert
    "link_description": "error-correction"   // desc for the assembled link
  }
}
```

## Link assembly (elisp side)

Given `target` and the description `link_description`:

| case                                | link                                          |
|-------------------------------------|-----------------------------------------------|
| `heading == null`                   | `[[id:<file_id>][<desc>]]`                    |
| `heading.id` present                | `[[id:<heading.id>][<desc>]]`                 |
| `heading` present, `heading.id` nil | `[[id:<file_id>::*<heading.text>][<desc>]]`   |

Then substitute the result for `{{link}}` in `template`.

## Connection type enum

`elaborates`, `analogous-mechanism`, `contradicts`, `instance-of`,
`generalizes`, `mention`.

## Invariants (engine-enforced, not represented in output)

- No suggestion targets `source.file` (no self-links).
- No `mention` suggestion whose target the current note already links to.
- `suggestions` is sorted by `confidence` descending and capped to top-N.
