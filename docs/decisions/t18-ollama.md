# T18 — Run the judge on a local Ollama model (`.env`-only)

## Goal

Let the JUDGE run against a local model served by Ollama, configured entirely
from the environment (no new CLI flag), while EMBEDDINGS stay on OpenRouter so
the Chroma index is untouched (no rebuild, no dim change). Default behaviour
(OpenRouter judge with Anthropic prompt caching) is byte-for-byte unchanged.

Ollama exposes an OpenAI-compatible API at `http://localhost:11434/v1`, so this
is a config + client-routing change, not new SDK code.

## Decisions

### `.env`-only config (no CLI flag)

Three settings drive everything, all read from the environment:

* `judge_provider: Literal["openrouter", "ollama"] = "openrouter"` — env
  `NOTELINKS_JUDGE_PROVIDER` (matches the `NOTELINKS_*` `validation_alias`
  pattern). Default keeps OpenRouter.
* `ollama_base_url: str = "http://localhost:11434/v1"` — env `OLLAMA_BASE_URL`.
* `judge_model` (existing, env `JUDGE_MODEL`) is **reused** for the local tag —
  no separate `ollama_model` field and no hardcoded gemma default. The user sets
  `JUDGE_MODEL=gemma3n:e2b` when `judge_provider=ollama`.

Embeddings config is unchanged (always OpenRouter).

### Per-role client split (was one shared client)

Previously the `Engine` built ONE OpenRouter `OpenAI` client and injected it into
both `embed_texts` and `judge_candidates` (both providers happened to share the
OpenRouter client). With a local judge those roles diverge, so the single
`Engine.client` property is split into two lazy properties:

* `embedding_client` → `embeddings.make_client(settings)` — ALWAYS OpenRouter.
* `judge_client` → `llm.make_judge_client(settings)` — OpenRouter or Ollama.

`make_judge_client` is new in `providers/llm.py`:

* `judge_provider == "ollama"` → `OpenAI(base_url=settings.ollama_base_url,
  api_key="ollama")`. The dummy key satisfies the SDK (which requires a
  non-empty key) without needing a real one — local Ollama ignores auth. Crucially
  it does **not** require `OPENROUTER_API_KEY` for the judge in this mode.
* otherwise → the existing `make_client` (OpenRouter, key-validated).

`make_client` is kept as the OpenRouter constructor. `complete_structured`'s
lazy default switched from `make_client` to `make_judge_client` so a throwaway
client honours the provider too.

### Provider-aware request shape (flatten + drop `cache_control`)

The judge request sends the full current note as a CACHED prefix: list-of-parts
`content` with an Anthropic `cache_control` ephemeral breakpoint (an
Anthropic/OpenRouter-ism — design §9). OpenRouter forwards that untouched; a
generic OpenAI-compatible endpoint like Ollama can't use it and may reject the
part shape.

So in `complete_structured`, when `judge_provider != "openrouter"`, messages are
normalized by `_normalize_for_non_anthropic`:

* list-of-parts `content` is flattened to a single plain string by concatenating
  the `text` of its parts;
* `cache_control` is dropped (it carried only caching metadata, no content);
* plain-string `content` is left as-is.

The normalizer returns NEW dicts and does not mutate the caller's messages. When
`judge_provider == "openrouter"`, messages are passed through exactly as before —
unchanged behaviour, verified by test.

### json_object / `_extract_json` reliance for small models

The existing structured-output fallback (T16) covers small local models for free:
strict `json_schema` → `{"type": "json_object"}` + schema-hint → `_extract_json`.
If Ollama rejects strict `json_schema`, the `APIStatusError` triggers the
`json_object` fallback. Small models (gemma) often emit prose and/or fenced JSON;
`_extract_json` already strips markdown fences and slices to the outermost braces.
No new structured-output code was needed.

### Why embeddings stay remote

Embeddings define the vector space of the Chroma index. Swapping the embedding
provider/model/dim would invalidate the existing index and force a rebuild. Since
this task is judge-only, embeddings remain on OpenRouter and the index is
completely untouched. Consequence: **`OPENROUTER_API_KEY` is still required for
embeddings even with a local judge** (documented in `.env.example` and the
`embedding_client` docstring).

## Scope

Touched: `config.py`, `providers/llm.py`, `engine.py`, `.env.example`, tests,
this doc. NOT touched: index / chunk / retrieve modules.

## Tests (offline — no Ollama, no key)

`tests/test_ollama_judge.py`:

* config: `NOTELINKS_JUDGE_PROVIDER=ollama` → `judge_provider == "ollama"`;
  default is `openrouter`; `OLLAMA_BASE_URL` default + override.
* `make_judge_client`: ollama → ollama `base_url`, constructs WITHOUT an
  OpenRouter key; openrouter → OpenRouter `base_url` and requires the key
  (asserted via `str(client.base_url)`).
* `complete_structured` normalization: ollama flattens content + drops
  `cache_control` (and does not mutate the input); openrouter preserves
  list-of-parts + `cache_control` (unchanged).
* engine: ollama → `judge_client.base_url` is the ollama URL while
  `embedding_client.base_url` is OpenRouter's.

Full suite + `ruff check` pass; default behaviour is unchanged.

## Manual usage recipe

```bash
ollama serve                     # start the local server
ollama pull gemma3n:e2b          # pull the model you want as the judge

# In .env (or exported):
export OPENROUTER_API_KEY=sk-or-...        # STILL needed: embeddings are remote
export NOTELINKS_JUDGE_PROVIDER=ollama
export OLLAMA_BASE_URL=http://localhost:11434/v1   # optional; this is the default
export JUDGE_MODEL=gemma3n:e2b

notelinks suggest --corpus /path/to/notes < note.org
```

The judge calls hit the local model; embeddings/index still use OpenRouter.
