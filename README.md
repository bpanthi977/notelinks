# notelinks

Suggests **idea-resonance** links between the note you're writing and the rest
of your org-roam corpus — analogies, shared mechanisms, contradictions,
instances of a general claim, generalizations of a specific one — even when the
two passages share no vocabulary. The unit of connection is a **passage**, not a
whole note. It surfaces ranked candidate links for you to review and accept; it
never edits your notes itself.

- **What** we're building: [`docs/objective.md`](docs/objective.md)
- **How** it works (settled design): [`docs/design.md`](docs/design.md)
- **Output contract** (engine ↔ frontend): [`docs/json-format.md`](docs/json-format.md)
- **Per-task decision log**: [`docs/decisions/`](docs/decisions/)
- **Walkthrough video**: [`video.md`](video.md)

## How it works

A two-stage pipeline (design §1):

1. **Retrieve** — every note is chunked structurally (org headings / paragraphs
   / list items), embedded, and stored in a local Chroma vector index. Each
   chunk of the *current* note becomes a query; nearest-neighbour chunks from
   *other* notes are candidate links. High recall, cheap.
2. **Judge + explain** — an LLM decides which candidates are genuine,
   worth-linking connections, classifies the type
   (`elaborates`, `analogous-mechanism`, `contradicts`, `instance-of`,
   `generalizes`, `mention`), writes a short rationale, and produces the link
   anchor in the current note. High precision.

The index is **incremental**: a refresh re-embeds only the notes that changed
(mtime-gated, hash-confirmed). The core (`engine.py`) is transport-agnostic, so
the same logic backs both the one-shot CLI and the long-lived daemon.

## Requirements

- Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/)
- An **OpenRouter** API key (used for embeddings, and — by default — the judge).
  The judge can instead run on a local Ollama model; see [Local judge](#local-judge-optional).

## Setup

```bash
uv sync                       # core install
cp .env.example .env          # then fill in OPENROUTER_API_KEY
```

Set the key (and, optionally, a default corpus) in `.env`:

```sh
OPENROUTER_API_KEY=sk-or-...
# NOTELINKS_CORPUS_DIR=/path/to/your/org-roam/notes   # else pass --corpus
```

A sample corpus of 21 org-roam notes ships under [`notes/`](notes/).

## Usage

The CLI exposes three commands (run via `uv run notelinks ...`):

### `index` — build / refresh the vector index

```bash
uv run notelinks index --corpus notes              # incremental refresh
uv run notelinks index --corpus notes --rebuild    # forced full rebuild
```

Prints build stats as JSON. The Chroma store lives at
`<corpus>/dbs/notelinks_index` by default (override with `NOTELINKS_INDEX_DIR`).

### `suggest` — get link suggestions for a note buffer

The current note is read from **stdin** (so it works while writing, including
unsaved edits) and identified by the `:ID:` in its buffer — no file path is
taken. `suggest` refreshes the index first (cheap when nothing changed), then
queries:

```bash
uv run notelinks suggest --corpus notes < notes/dreamer_v2.org
```

It prints a single JSON `Envelope` to stdout: a ranked, capped list of
suggestions, each with a connection `type`, a `source_anchor` (where in the
current note the link attaches), a `target` (note, optionally a heading), a
`why`, and a `confidence`. See [`docs/json-format.md`](docs/json-format.md) for
the full schema and how the frontend turns components into `[[id:...]]` links.

### `serve` — run the daemon (optional `[server]` extra)

A long-lived [FastAPI](https://fastapi.tiangolo.com/) app over one `Engine`,
keeping the index fresh via an initial refresh plus a file watcher:

```bash
uv sync --extra server
uv run notelinks serve --corpus notes        # binds 127.0.0.1:8765
```

Endpoints: `GET /health`, `GET /status`, `POST /refresh {"rebuild": false}`,
`POST /suggest {"buffer": "<org text>"}`. Localhost-only, single-user, no auth.

### Common flags

| Flag | Applies to | Effect |
|------|-----------|--------|
| `--corpus DIR` | all | Corpus root, overrides `NOTELINKS_CORPUS_DIR`. |
| `--rebuild` | `index` | Force a full reindex. |
| `--verbose` / `-v` | all | Timestamped DEBUG logging to stderr (stdout stays JSON). |
| `--trace` | all | Export OpenTelemetry traces to a local Phoenix collector (needs the `trace` extra). |
| `--host` / `--port` | `serve` | Bind address / TCP port. |

## Configuration

All tuning knobs live in [`src/notelinks/config.py`](src/notelinks/config.py)
(pydantic-settings) and are env-overridable. Key ones:

| Setting | Env var | Default |
|---------|---------|---------|
| Embedding model | `EMBEDDING_MODEL` | `openai/text-embedding-3-small` |
| Judge model | `JUDGE_MODEL` | `anthropic/claude-sonnet-4.5` |
| Corpus dir | `NOTELINKS_CORPUS_DIR` | — (or `--corpus`) |
| Index dir | `NOTELINKS_INDEX_DIR` | `<corpus>/dbs/notelinks_index` |
| Neighbours / source chunk | — | `top_k=8` |
| Candidates kept / source | — | `per_source_n=3` |
| Global candidate cap | — | `global_cap_m=40` |
| Final suggestions cap | — | `top_n=12` |

### Local judge (optional)

Run the judge on a local [Ollama](https://ollama.com) model instead of
OpenRouter. Embeddings still go through OpenRouter, so `OPENROUTER_API_KEY` is
still required. In `.env`:

```sh
NOTELINKS_JUDGE_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
JUDGE_MODEL=gemma3n:e2b
```

Then: `ollama serve`, `ollama pull gemma3n:e2b`.

## Development

```bash
uv run pytest        # full suite (offline — no API keys needed)
uv run ruff check    # lint
uv run ruff format   # format
```

The test suite is hermetic: it stubs providers and isolates from any developer
`.env`, so it runs without network access or secrets.

## Project layout

```
src/notelinks/
  config.py      pydantic-settings: models, paths, tuning knobs
  models.py      output contract models + internal models
  engine.py      Engine(config): refresh(), suggest() -> Envelope   (CORE)
  cli.py         typer adapter (suggest / index / serve)
  api.py         FastAPI daemon (optional [server] extra)
  providers/     embeddings.py, llm.py        (provider swap points)
  org/           parse.py (.org -> Note), chunk.py (structural chunking)
  index/         store.py (long-lived Chroma), build.py (incremental refresh)
  pipeline/      retrieve.py, judge.py        (stateless steps)
docs/            objective, design, json-format, per-task decisions
notes/           sample org-roam corpus (21 notes)
tests/           unit + offline e2e tests
```
