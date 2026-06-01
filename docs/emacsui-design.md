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
- An **ispell-style bottom side window** (`*notelinks-review*`) shows the
  `a/r/n/p/j/q` key legend for the duration of the session — *keys only*. The
  current suggestion's metainfo lives in a posframe (§4). It closes on quit /
  when no suggestions remain.

## 4. Metainfo — target-info posframe

The current suggestion's details render **into a posframe anchored just past the
suggestion's overlay**, separate from the bottom key legend (§3). The bottom side
window is a fixed two-line legend; the posframe floats next to the suggestion
under review, so the details sit where the eye already is rather than at the
bottom of the frame.

- Placement never covers the span (which may be multi-line): by default it is
  anchored at the **overlay's end line** and opens **downward**, so the whole
  span stays above it. Only when the end is too near the window bottom for the
  posframe to fit below is it anchored at the **overlay's beginning line** and
  opened **upward** (keeping the span below it). Fit is estimated from the window
  body height and the info's line count (`notelinks--info-fits-below-p`).
- Anchors use the **beginning of the line**, not the span's column, so the frame
  is left-aligned and a span near the right margin never pushes it off-frame.

- Posframe content: **type**, **confidence (1–3)**, **why**, **target** (note
  title + heading), and a snippet of the **target excerpt** (truncated to stay
  bounded) — i.e. `notelinks--describe`.
- It tracks point via a buffer-local `post-command-hook` (and an explicit update
  after programmatic moves), re-rendering only when the suggestion under point
  changes. Off a suggestion the posframe is **hidden**.
- On a non-graphical frame (TTY) `posframe-workable-p` is nil, so the info falls
  back to the **echo area** (`message`); the key legend still shows.
- The legend buffer uses a small `notelinks-panel-mode`; pressing **`q`** there
  closes the panel and **quits the review** (in its source buffer), so the panel
  is also a valid exit point.

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

Two interchangeable transports, selected by `notelinks-backend`; **both feed the
identical result pipeline** (§8) — only how the `Envelope` JSON arrives differs.

**`cli` (default) — subprocess.** `make-process` runs `notelinks suggest
[--corpus DIR]`; the **whole buffer** goes to stdin, stdout is collected, and
the **sentinel** parses the JSON on exit. Each query pays cold-start + a full
incremental corpus walk. (No separate initial-build command — the first run is
just slower, covered by the same status indicator.)

**`http` — daemon.** POST the buffer to a running `notelinks serve` daemon:
`POST {base}/suggest` with body `{"buffer": <org text>}`, parsed in the
`url-retrieve` callback. The daemon holds a **warm** `Engine` and watches the
corpus, so a query is a pure read — much faster, no per-keystroke index walk.
Convenience commands hit the other endpoints: `notelinks-server-status`
(`GET /status`) and `notelinks-server-refresh` (`POST /refresh`).

Common to both:

- **No file argument.** Only the buffer is sent; the engine parses the note's
  `:ID:` (identity + self/cyclic exclusion) from it, so **unsaved / new buffers**
  work. (`source.file` was dropped from the contract — see
  [`task-take-stdin-input.md`](./task-take-stdin-input.md).)
- **Fully async**, with a mode-line status indicator while in flight.
- **Editing during the call is allowed** — results are re-anchored against the
  buffer's *current* text on receipt (§8); anything that won't resolve becomes
  unanchorable (§9).
- **Errors** (nonzero exit / connection failure / non-2xx / bad JSON) surface in
  a `*notelinks-error*` buffer with the detail.

### Configuration

- `notelinks-backend` — `cli` (default) | `http`.
- `notelinks-command` — list, default `("notelinks")`; e.g. `("uv" "run"
  "notelinks")`. (`cli` backend.)
- `notelinks-corpus-dir` — passed as `--corpus`; falls back to the engine's
  `NOTELINKS_CORPUS_DIR` env when nil. (`cli` backend.)
- `notelinks-server-url` — daemon base URL, default `http://127.0.0.1:8765`.
  (`http` backend.)

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

- **Transport error** (nonzero CLI exit, HTTP connection failure / non-2xx) or
  **malformed JSON** → show the detail in `*notelinks-error*`; abort the session.
- **Zero suggestions** → echo-area message, no session.
- **Unanchorable** suggestions and **overlap-discarded** suggestions → echo-area
  count plus a `*notelinks*` listing (type, why, target) so the user can act on
  them manually if desired.

## 10. Session lifecycle

```
M-x notelinks-suggest
  → query engine (cli subprocess or http daemon), buffer → engine; status indicator
  → on result:
      drop suggestions below notelinks-min-confidence (reported as a count)
      resolve anchors → markers
      insert pending insert-templates
      create overlays (face + keymap + help-echo)
      enable notelinks-review-mode (buffer-local; post-command-hook drives the info posframe)
      show bottom key-legend panel
      jump point to first suggestion
  → user accepts/rejects per suggestion (auto-advance; info posframe tracks point)
  → when none remain, or on quit:
      finalize/revert, clear overlays + markers
      disable notelinks-review-mode
      close key-legend panel + delete info posframe
```

## 11. Configuration variables (summary)

| var | default | meaning |
|---|---|---|
| `notelinks-backend` | `cli` | transport: `cli` \| `http` |
| `notelinks-command` | `("notelinks")` | engine executable + args (`cli`) |
| `notelinks-corpus-dir` | `nil` | `--corpus`; nil ⇒ engine env (`cli`) |
| `notelinks-server-url` | `http://127.0.0.1:8765` | daemon base URL (`http`) |
| `notelinks-navigation-order` | `buffer` | `buffer` \| `confidence` |
| `notelinks-min-confidence` | `2` | drop suggestions below this confidence (1–3) before review |

Commands: `notelinks-suggest` (review), `notelinks-server-status`,
`notelinks-server-refresh` (`http` backend).

## 12. Deferred / out of scope (v1 UI)

- Multiple target candidates per span (cycle targets in the popup).
- Exposing engine knobs (`top_n`, similarity floor, type filter) from elisp —
  rely on engine config for now.
