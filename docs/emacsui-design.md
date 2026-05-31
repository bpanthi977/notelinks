# Emacs UI design (notelinks frontend)

Emacs is the **only** frontend. The engine is a CLI that emits a single JSON
object per the authoritative contract in [`json-format.md`](./json-format.md);
this elisp client consumes it, presents suggestions as **inline overlays** in the
current note's buffer, and applies accepts directly to the **live buffer**.

This document records the settled UI decisions. See
[`json-format.md`](./json-format.md) for the data contract and
[`design.md`](./design.md) for the engine. The stdin/`:ID:`/file-arg change this
UI assumes is tracked in
[`task-take-stdin-input.md`](./task-take-stdin-input.md).

## 1. Presentation — inline overlays

Each suggestion is rendered as a **highlighted region** in the buffer, marked
with a dedicated face (`notelinks-suggestion`) so it reads as "special".

- **wrap-span**: the existing span (`expect`) is highlighted in place. The text
  is left untouched until accept.
- **insert**: there is no existing text, so the filled template (the prose with
  the assembled `{{link}}` substituted in) is **inserted** into the buffer and
  highlighted. Visually it is identical to a wrap-span region — just newly
  inserted text rather than pre-existing text.

So at the presentation layer there is a single concept — *a highlighted region
you act on* — and the wrap/insert distinction only changes what accept/reject do
(§2).

## 2. Pending → accept / reject model

On entering review, every suggestion becomes a **pending** highlighted region.
Acting on it finalizes or reverts that one region:

| | wrap-span | insert |
|---|---|---|
| **pending (entry)** | highlight the existing span; text unchanged | insert filled template; highlight it |
| **accept** | wrap the span into `[[id:…][desc]]`; drop highlight | keep inserted text as-is; drop highlight |
| **reject** | drop highlight; text untouched | delete the inserted region |

- `accept` finalizes **and advances point to the next suggestion** (keeps the
  modal flow going).
- `reject` reverts that region and advances similarly.
- `quit` = reject all remaining suggestions, then end the session.

Rationale for keeping wrap-span as a *bare highlight until accept* (rather than
pre-wrapping into a link): minimal buffer churn, and the description of a
wrap-span link is the span text itself, so the visible text doesn't change on
accept anyway — only the link face appears.

## 3. Interaction model — contextual-modal

Normal cursor movement and editing are **always free**. The single-key modal
controls are active **only while point is inside a suggestion overlay**, via the
overlay's own `keymap` property:

| key | action |
|---|---|
| `a` | accept current, advance to next |
| `r` | reject current, advance to next |
| `n` | next suggestion |
| `p` | previous suggestion |
| `q` | quit — reject all remaining, end session |

- A buffer-local minor mode `notelinks-review-mode` is active for the whole
  session. It binds only **non-self-insert** keys (`C-c C-n` next, `C-c C-p`
  prev, `C-c C-q` quit) so the user can navigate/quit from *anywhere* in the
  buffer without shadowing ordinary typing.
- The fast single keys (`a r n p q`) live on each **overlay keymap**, so they
  only fire when point is on a suggestion — elsewhere those letters type
  normally.
- Entry jumps point to the first suggestion; `a`/`r` auto-advance, so the user
  stays "on" suggestions throughout normal flow. If they wander off to edit,
  they return by moving onto a suggestion or via the `C-c` bindings.
- An **ispell-style legend buffer** (small window) shows the `a/r/n/p/q` keys
  with one-line descriptions for the duration of the session; it closes on quit
  / when no suggestions remain.

## 4. Metainfo popup

Uses **eldoc / `help-echo`** — no posframe.

- Each overlay carries a `help-echo` function returning a formatted block:
  **type**, **confidence (1–5)**, **why**, **target** (note title + heading), and
  a snippet of the **target excerpt**.
- Surfaced via `help-at-pt` on point-idle (echo area) and as a tooltip on mouse
  hover; integrates with eldoc where available.

## 5. Navigation order

`next`/`prev` walk suggestions in **buffer order** by default. Configurable via
`notelinks-navigation-order` (`buffer` | `confidence`), default `buffer`.
Confidence is always shown (face intensity and/or popup) regardless of order.

## 6. Overlap handling

The UI **assumes the engine emits non-overlapping source anchors**. Defensive
fallback only: if, at anchor-resolve time, a suggestion's region overlaps one
already placed, keep the **higher-confidence** suggestion and **discard** the
lower-confidence one (reported as discarded, §9).

> **Known design gap:** non-overlap *should* be enforced engine-side (one clean
> region per accepted suggestion). We deliberately do **not** implement that
> engine constraint in v1 — the UI's discard-lower-confidence fallback is the
> only guard. To be called out explicitly in the documentation phase.

## 7. Engine invocation

- **Mechanism:** `make-process`; write the **whole buffer** to the process
  stdin, close it, collect stdout; the process **sentinel** parses the JSON on
  exit. Fully **async** — a spinner / mode-line indicator runs while the call is
  in flight.
- **Editing during the call is allowed.** Results are re-anchored against the
  buffer's *current* text on receipt (§8), so drift is fine; anything that won't
  resolve becomes unanchorable (§9).
- **No special initial-build path.** `suggest` auto-refreshes the corpus; the
  first ever run is simply slower and is covered by the same spinner. (No
  separate "index" command in the UI.)
- **File argument:** pass the repo-relative path **only when** the buffer visits
  a file under the corpus (for `source.file` + self/cyclic exclusion); **omit**
  for unsaved / new buffers. The note's `:ID:` is parsed by the engine from the
  stdin buffer. This depends on the engine change in
  [`task-take-stdin-input.md`](./task-take-stdin-input.md).
- **Configuration:**
  - `notelinks-command` — list, default `("notelinks")`; e.g. set to
    `("uv" "run" "notelinks")`.
  - `notelinks-corpus-dir` — passed as `--corpus`; falls back to the engine's
    `NOTELINKS_CORPUS_DIR` env when nil.

## 8. Anchor resolution & markers

(Restating the relevant part of `json-format.md` from the UI's side.)

On receiving the JSON, resolve **each** suggestion exactly once:

1. Search near `char_start` for `expect` framed by `before`/`after` (empty
   region → find where `before`/`after` meet). This pins the true location
   regardless of drift since the buffer was sent.
2. For **insert**, insert the filled `template` at that point.
3. Drop **markers** at the region's start and end (single marker for an empty
   region). From here on the markers — not the offsets — define the region, so
   accepting/rejecting earlier suggestions automatically keeps later ones
   correct (no re-search).
4. If no confident match is found, the suggestion is **unanchorable** (§9) — it
   gets no overlay and is never applied via blind `goto-char`.

On accept, a final `expect`-still-matches check guards the marked region against
any unexpected edit.

`content_hash` (echoing the buffer the engine saw) is available to detect heavy
drift and warn; re-anchoring still relies on `expect`/`before`/`after`.

## 9. Errors, empty results, unanchorable

- **Process error** (nonzero exit) or **malformed JSON** → show stderr in a
  dedicated error buffer; abort the session.
- **Zero suggestions** → echo-area message, no session.
- **Unanchorable** suggestions and **overlap-discarded** suggestions → echo-area
  count plus a `*notelinks*` listing (type, why, target) so the user can act on
  them manually if desired.

## 10. Session lifecycle

```
M-x notelinks-suggest
  → spawn engine (buffer → stdin), spinner
  → on result:
      resolve anchors → markers
      insert pending insert-templates
      create overlays (face + keymap + help-echo)
      enable notelinks-review-mode (buffer-local)
      show legend buffer
      jump point to first suggestion
  → user accepts/rejects per suggestion (auto-advance)
  → when none remain, or on quit:
      finalize/revert, clear overlays + markers
      disable notelinks-review-mode
      close legend buffer
```

## 11. Configuration variables (summary)

| var | default | meaning |
|---|---|---|
| `notelinks-command` | `("notelinks")` | engine executable + args |
| `notelinks-corpus-dir` | `nil` | `--corpus`; nil ⇒ engine env |
| `notelinks-navigation-order` | `buffer` | `buffer` \| `confidence` |

## 12. Deferred / out of scope (v1 UI)

- Multiple target candidates per span (cycle targets in the popup).
- Exposing engine knobs (`top_n`, similarity floor, type filter) from elisp —
  rely on engine config for now.
- posframe / graphical popups.
