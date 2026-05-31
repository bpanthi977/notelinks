# T13 — `src/notelinks/engine.py` + `src/notelinks/cli.py`

The integration capstone: the transport-agnostic core (`Engine`) that wires the
whole pipeline together, plus the thin typer CLI adapter that drives it. Design
§2 (core/adapter split), §8–§11; output contract per `docs/json-format.md`.

## `Engine` — state & lifecycle (design §2)

`Engine(settings)` owns the only long-lived state in the system:

- **ONE shared `OpenAI` client.** Built once via `providers.llm.make_client`
  (embeddings and the judge share the identical OpenRouter-backed client) and
  injected into every embedding + judge call. It is constructed **lazily** on
  first real provider use (a `client` property), so an `Engine` can be built —
  and `refresh()`/`suggest()` driven with monkeypatched providers in tests —
  without an API key. The key is validated at call time by `make_client`, never
  at import.
- **ONE long-lived `Store`.** Opened once in `__init__`, reused across all
  `refresh()`/`suggest()` calls — never build-and-teardown per request.

`pipeline/` steps stay stateless functions; the engine threads the shared
client + store through them. Adapters (CLI now, a REST `api.py` later) construct
ONE `Engine` and reuse it.

## `refresh()`

Delegates to `index.build.refresh(self.store, self.settings, client=self.client,
rebuild=...)` and returns its stats dict (`indexed`/`reindexed`/`skipped`/
`deleted`/`chunks`). Incremental and mtime-gated, so cheap when nothing changed.

## `suggest(buffer_text, file_path)` flow

1. **Guard**: error clearly if `settings.corpus_dir is None`.
2. **Auto-refresh first** (design H): `suggest` calls `self.refresh()` so the
   corpus index is current. Incremental, so it is cheap when nothing changed.
   This also guarantees the on-disk copy of the current note is in the index —
   but it is excluded from retrieval by uuid (step 5), so it never self-matches.
3. **Buffer is the query, not the on-disk file.** We compute `rel_path` =
   `file_path` relative to `corpus_dir` (posix; falls back to the path as-is if
   it is not under the root) and `parse_note(buffer_text, rel_path)`. Parsing
   the BUFFER means unsaved edits count: the query chunks come from what the user
   is currently typing, while the index still holds the last-saved copy. The
   on-disk copy is excluded by `note.id`, so the two never collide.
4. **Embed** the buffer's chunk `embed_text`s with the shared client.
5. **Retrieve** candidates, excluding the current note's own chunks
   (`current_note_uuid=note.id`) and any candidate whose heading the source
   already links (`existing_links=note.links`, filtered heading-level by
   `pipeline/exclude.py` — see design §8).
6. **Judge** the candidates into raw `Suggestion`s.
7. **Rank / dedup / cap / invariants** (`_finalize`, design §10 — see below).
8. **Envelope**: `version=1`, `Source(file=rel_path, title, id, queried_at=<UTC
   ISO-8601>, content_hash="sha256:"+sha256(buffer_text))`, plus the finalized
   suggestions. `content_hash` covers the buffer verbatim (the thing actually
   queried), matching `source.file`/`source.id` from the parsed buffer.

## Ranking / dedup / cap / invariants (`_finalize`, design §10)

Applied in this order:

1. **Drop invalid**: any suggestion with `target.file_id == note.id` (no
   self-links) or one that would duplicate an existing link
   (`suggestion_duplicates_link`, heading-level — design §8). Retrieval already
   filters already-linked headings, so this is belt-and-suspenders against a
   judge promoting a non-excluded chunk to a whole-note `target_is_note` that
   duplicates a bare link.
2. **Dedup**: keep at most one suggestion per `(target.file_id, heading text or
   None)`, keeping the highest `confidence`. Iterating in incoming order means a
   tie keeps the first-seen (earlier-ranked) one.
3. **Sort** by `confidence` descending, **stable**. `Suggestion` carries no
   retrieval score, so the incoming order — already retrieval-rank-ordered by
   the judge — is the tie-break. This is a deliberate simplification: rather than
   thread a score through to the wire model purely to break confidence ties, we
   lean on Python's stable sort preserving the judge/candidate rank order.
4. **Cap** to `settings.top_n`, then **reassign** ids `s01`, `s02`, … in final
   order (the judge's per-run ids are discarded — only the final order is
   stable/meaningful on the wire).

## CLI surface (`cli.py`) — design §2, §11

A THIN typer adapter: build `Settings()` (loads env / `.env`), optionally
override `corpus_dir` with `--corpus`, construct one `Engine`, drive it, print
JSON. All real work + long-lived state lives in the engine.

- `suggest <file> [--corpus DIR]` — reads the buffer from **stdin**
  (`sys.stdin.read()`), runs `Engine.suggest`, prints
  `envelope.model_dump_json(indent=2)` to stdout.
- `index [--rebuild] [--corpus DIR]` — runs `Engine.refresh(rebuild=...)`, prints
  the stats dict as JSON.

Options use the `typing.Annotated[...]` style (e.g. `Annotated[Path | None,
typer.Option(...)]`) rather than `param = typer.Option(...)` defaults. This is
typer's modern idiom and sidesteps ruff `B008` (call-in-default) without
disabling the lint, while `X | None` satisfies `UP045`.

### Entry point

`pyproject.toml` `[project.scripts]` → `notelinks = "notelinks.cli:app"`, so
`notelinks suggest …` / `notelinks index …` work after install.

## Tests

- **`tests/test_engine.py`** — fully offline integration. A real temp corpus
  (current note + a resonant "immune" note + an unrelated "baking" note) and a
  real `Store`, with BOTH embedding boundaries faked (`index.build.embed_texts`
  for the corpus build and `engine.embed_texts` for the query) plus
  `providers.llm.complete_structured` returning a canned `JudgeResponse`. The
  fake embedder keys vectors by topic so the buffer lands nearest the immune
  note. Asserts: the `Envelope` validates and round-trips, is confidence-sorted,
  ids are `s01…` in order, no `target.file_id == note.id` (the on-disk self copy
  is excluded), the target carries components only, and `source_anchor` offsets
  bracket the verbatim `expect` in the buffer. A second test asserts `refresh()`
  indexes all three notes.
- **`tests/test_cli.py`** — offline CLI smoke via `typer.testing.CliRunner`:
  `index` (asserts stats JSON, exit 0) and `suggest` (feeds the buffer on stdin,
  asserts envelope JSON parses, exit 0), with the same faked boundaries.

Both set a dummy `OPENROUTER_API_KEY` so the shared client constructs; no request
is ever made (providers are faked and a `_no_network` fixture blocks sockets).
