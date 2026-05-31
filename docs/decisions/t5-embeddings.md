# T5 — Embedding provider (SWAP POINT 1)

`src/notelinks/providers/embeddings.py` is the only module aware of the
embedding backend (OpenRouter via the OpenAI SDK). See design §2 (core/adapter
split), §3 (stack), §6 (what gets embedded).

## Function contract

```python
def make_client(settings: Settings) -> OpenAI
def embed_texts(texts: list[str], settings: Settings, *, client: OpenAI | None = None) -> list[list[float]]
```

- `embed_texts` returns exactly **one vector per input text, in input order**.
- **Empty input → empty list**, and no HTTP request is made.
- Model = `settings.embedding_model`; dimensionality = `settings.embedding_dim`
  (passed as the `dimensions` arg, which `text-embedding-3-*` supports).
- `make_client` validates `openrouter_api_key` is non-empty **at call time**
  (not import time) per `config.py`, raising `ValueError` if missing.

## Batching

Inputs are split into batches of `_BATCH_SIZE = 128` per `/embeddings` request.
A corpus can be thousands of chunks; one request per chunk wastes round-trips,
and one request for everything risks payload/token limits. 128 is a modest
middle ground. Order is preserved two ways: batches are processed in slice
order, and within each batch the response `data` is **sorted by `.index`**
before extraction — so a provider returning data out of order can never
scramble alignment (covered by a test).

## Retry

Modest manual backoff (no extra dependency): up to `_MAX_ATTEMPTS = 4` attempts
with exponential delay `0.5 · 2^n` seconds, retrying only on transient errors
— `RateLimitError`, `APIConnectionError`, `APIError` (5xx / network). The last
exception is re-raised on exhaustion. `tenacity` is present transitively but we
avoid depending on a transitive dep; the manual loop is tiny and testable
(`_BASE_DELAY` is monkeypatched to 0 in tests).

## Inject-client design

The `client` keyword exists so the long-lived `Engine` (design §2) builds one
shared `OpenAI` client at startup and injects it into every `embed_texts` call,
matching the "construct provider clients once, inject, no per-call singletons"
constraint. When omitted (tests, one-off scripts) a client is built lazily from
`settings`.

## How the swap point is isolated

Everything outside `providers/` speaks only `list[str] -> list[list[float]]`.
The OpenAI SDK, `base_url`, model slug, batching, and retry all live here.
Swapping to another embedding backend means reimplementing this one file; the
index builder, retrieval, and engine are untouched. The package docstring in
`providers/__init__.py` states this invariant.

## Tests

`tests/test_embeddings.py` monkeypatches `client.embeddings.create` with a fake
returning deterministic vectors (first component = `len(text)`), asserting:
empty input makes no call; a 300-input request splits into 128/128/44; order is
preserved across and within batches; out-of-order provider data is realigned;
`make_client` rejects an empty key; and a transient error is retried then
succeeds. A live smoke test (`test_live_smoke_returns_correct_dim`) runs only
when `OPENROUTER_API_KEY` is set, asserting `len(vec) == settings.embedding_dim`.
