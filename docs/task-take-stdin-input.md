# Task — current note via stdin; `:ID:` from buffer; file arg optional

**Status:** open · **Owner:** engine branch · **Raised from:** emacs UI design
(see [`emacsui-design.md`](./emacsui-design.md) §7).

## Why

The emacs frontend always sends the **whole current-note buffer on stdin** so
the tool works *while writing*, including **unsaved / brand-new buffers** that
have no file on disk. It passes a file path only when the buffer is visiting a
real file under the corpus.

This contradicts the current engine design.

## The contradiction (with `design.md`)

`design.md` §11 currently states the current-note **file path is a required
argument** and is where the engine **reads the note's `:ID:` from disk**:

> **Input:** … the current-note **file path** is an argument (repo-relative
> `source.file`, and to read the note's `:ID:`).

> **Commands:** `suggest <file> [--corpus DIR]` (buffer on stdin)

For an unsaved / new buffer there is no file to read, and even for a saved file
the *in-progress* identity and content live in the stdin buffer, not on disk.

## Required engine change

1. **Parse the current note's `:ID:` (and `#+title:`) from the stdin buffer
   text**, not from the file on disk.
2. **Make the file argument optional.** When given, use it only for
   `source.file` (repo-relative display) and to assist self / cyclic-link
   exclusion by path. When absent, derive identity solely from the stdin `:ID:`;
   `source.file` may be null.
3. **Self / cyclic exclusion keys on the stdin `:ID:`** (plus the path when
   provided) — never on re-reading the current file from disk.

## Edge case — note with no `:ID:`

A brand-new note may not yet have a file-level `:ID:`. Then:

- The engine cannot self-exclude by id. If a file path was provided, exclude by
  path; otherwise there is nothing to exclude (a new note has no existing links
  anyway).
- `source.id` is **null** for that run.

This means **`json-format.md`'s `source.id` must be relaxed to nullable**
(it currently says "always present"). Flag this as a related contract tweak.

## Suggested `design.md` edits (do NOT apply here — documentation phase)

- **§11 Invocation** — replace "the current-note file path is an argument …
  and to read the note's `:ID:`" with: *file path is **optional**; used for
  `source.file` and self/cyclic exclusion only. The note's `:ID:` and title are
  parsed from the **stdin buffer**.*
- **§11 Commands** — `suggest [file] [--corpus DIR]` (file optional; buffer on
  stdin).
- **§4 Corpus & link model** — note that the *current* note's identity is taken
  from the **stdin buffer's** file-level `:ID:`, falling back to null when the
  buffer has none.
- **§8 Retrieval** — self-exclusion (`note_uuid != current`) uses the stdin
  `:ID:`; when absent, fall back to path-based exclusion or none.

## Related contract tweak

- `json-format.md` — `source.id`: change "always present" → **nullable**
  (null when the current buffer has no file-level `:ID:`).
