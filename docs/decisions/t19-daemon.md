# T19 — Daemon / REST API (`notelinks serve`)

## Goal

Run the engine as a long-lived local server so a client (the org-roam frontend)
gets suggestions over HTTP from a warm process — instead of paying cold-start +
a full corpus walk on every query. The architecture was built for this (design
§2: `api.py` as a thin adapter over the same long-lived `Engine` the CLI drives).

## Decisions

### `suggest()` becomes a pure query (no auto-refresh)

`Engine.suggest` no longer calls `self.refresh()` — it reads the index as it
stands and runs parse → chunk → embed → retrieve → judge → finalize → `Envelope`,
still under the per-run `observability.root_span`. Freshness is now the caller's
responsibility:

* **CLI `suggest`** (one-shot) refreshes FIRST, then queries — preserving the
  original behaviour exactly (`engine.refresh(); engine.suggest(buffer)`).
* **The server** keeps the index fresh out-of-band: an initial refresh at
  startup + a file watcher (below).

Why: a query that silently refreshes would serialize a corpus walk (a *write*)
into every *read*, making the server's lock model far harder to reason about
(every read would need the write lock) and adding latency to the hot path. A
pure query reads a consistent index under a shared lock and never races a write.

Tests that relied on the old auto-refresh now call `engine.refresh()` first:
`tests/test_engine.py::test_suggest_end_to_end`, and in `tests/test_integration.py`
`test_suggest_end_to_end_happy_path`, `test_already_linked_target_is_never_suggested`,
the two retrieval/dedup/cap scenarios, and `test_suggest_emits_one_unified_trace`
(refresh placed OUTSIDE the asserted spans, so the single-root-trace assertion
still holds — refresh, with embeddings faked, emits no spans). Assertions are
unchanged.

### FastAPI app + lifespan (`api.py`)

`build_app(settings) -> FastAPI`. The **lifespan**:

1. constructs ONE `Engine(settings)`, held in `app.state` (wrapped in a small
   `_State` carrying the engine, the lock, and `last_refresh`);
2. does an **initial `refresh()`** (off the event loop via
   `anyio.to_thread.run_sync`) so the first query hits a warm index;
3. starts a `watchfiles.awatch(corpus_dir)` asyncio task;
4. on shutdown, cancels the watcher task.

### watchfiles watcher + debounce

The background task iterates `awatch(corpus_dir, debounce=400)`. `awatch` already
coalesces a burst of filesystem events into one batch; the 400 ms debounce caps
how often we walk the corpus when many files change at once (e.g. a git checkout).
For each batch we skip if no `.org` path changed, else run the refresh **off the
event loop** via `anyio.to_thread.run_sync(state.refresh)`. Failures are logged
and swallowed so a transient error (a half-written file) never kills the watcher.

### Single-lock concurrency

One `threading.Lock` (`_State.lock`) serializes ALL engine access — the watcher's
refresh (write), `POST /refresh` (write), and `POST /suggest` (read). Because
`suggest` is now pure, a read just needs the index to be internally consistent,
which the lock guarantees against a concurrent refresh.

Endpoints are plain `def` (sync), so FastAPI runs them in its threadpool — the
*blocking* `lock.acquire()` therefore never stalls the event loop. The watcher
runs `refresh()` via `anyio.to_thread.run_sync`, so it, too, takes the lock on a
worker thread, off the loop. (A single global lock is the right call for a
single-user local tool; a read/write lock would be premature.)

### Endpoints + shapes

| Method | Path       | Request body                | Response                                            |
|--------|------------|-----------------------------|-----------------------------------------------------|
| GET    | `/health`  | —                           | `{"status": "ok"}`                                  |
| GET    | `/status`  | —                           | `{"notes": int, "chunks": int, "last_refresh": str\|null}` |
| POST   | `/refresh` | `{"rebuild": false}`        | build stats `{"indexed","reindexed","skipped","deleted","chunks"}` |
| POST   | `/suggest` | `{"buffer": "<org text>"}`  | the `Envelope` (identical to CLI stdout / `json-format.md`) |

`/suggest` returns the pydantic `Envelope` directly — FastAPI serializes it, so
the wire contract is byte-identical to the CLI. `/status` reads note count from
`store.all_manifest_paths()` and chunk count from the chunk collection.

### Localhost, no auth

`cli.serve` binds `127.0.0.1:8765` by default. This is a single-user local tool,
so there is deliberately no authentication layer (matching the design's stance).

### Optional `[server]` extra

`[project.optional-dependencies] server = ["fastapi", "uvicorn[standard]",
"watchfiles"]` (pinned to the installed `fastapi 0.136.3`, `uvicorn 0.48.0`,
`watchfiles 1.2.0`). Same pattern as the `trace` extra: `api.py` is imported only
lazily — from `cli.serve` and from `tests/test_api.py` (guarded by
`pytest.importorskip("fastapi")`) — so the **core install and the default test
run never import FastAPI/uvicorn/watchfiles**. `serve` exits with an actionable
message (`uv sync --extra server`) when the extra is absent.

## Manual run + curl recipe

```bash
# 1. Install the server extra.
uv sync --extra server

# 2. Set provider access + corpus (or pass --corpus). Embeddings always need
#    OPENROUTER_API_KEY (see .env / T18).
export OPENROUTER_API_KEY=sk-or-...
export NOTELINKS_CORPUS_DIR=/path/to/org-roam-notes

# 3. Start the daemon (binds 127.0.0.1:8765; does an initial refresh + watches
#    the corpus for .org changes).
uv run notelinks serve            # --host/--port/--corpus/--verbose/--trace

# 4. Query it.
curl -s localhost:8765/health
curl -s localhost:8765/status
curl -s -X POST localhost:8765/refresh -H 'content-type: application/json' \
     -d '{"rebuild": false}'
curl -s -X POST localhost:8765/suggest -H 'content-type: application/json' \
     -d "{\"buffer\": $(jq -Rs . < some-note.org)}"
```

The watcher keeps the index fresh automatically as you edit `.org` files, so in
practice you only call `/suggest`; `/refresh` is a manual override.

## Tests

`tests/test_api.py` (skipped without the extra, `pytest.importorskip("fastapi")`)
uses FastAPI's `TestClient` with both provider boundaries mocked (the fake
embedder + a canned `complete_structured`) against a temp corpus/index. It asserts
`GET /health` ok; `POST /refresh` indexes (3 notes, chunks > 0); `POST /suggest`
returns a valid, confidence-sorted `Envelope` with components-only targets; and
that **`suggest` does not itself refresh** — a fresh `_State` engine returns an
empty envelope until `state.refresh()` runs, after which the immune target
surfaces.

Verification: `uv run ruff check` clean; `uv run pytest` is green WITHOUT the
extra (108 passed, 3 trace-skipped — the refactor + CLI change keep all existing
tests passing); WITH `--extra server` the API tests pass (112 passed, 3 skipped).
