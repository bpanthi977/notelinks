# T6 — `providers/llm.py` (SWAP POINT 2)

The only module aware of the chat/judge provider (OpenRouter via the OpenAI
SDK). Callers speak pydantic models + plain message dicts; everything
provider-specific lives here (design §2, §9).

## Contract

```python
def make_client(settings: Settings) -> OpenAI
def cached_text(text: str) -> dict          # an ephemeral-cached text content part
def complete_structured(
    messages: list[dict],
    settings: Settings,
    response_model: type[BaseModel],
    *, client: OpenAI | None = None,
) -> BaseModel                               # validated instance of response_model
```

`complete_structured` is generic over any pydantic `BaseModel`; the judge will
pass `JudgeResponse`, but nothing here is judge-specific.

## Structured output: strict json_schema, with a json_object fallback

Primary path requests:

```python
response_format = {
  "type": "json_schema",
  "json_schema": {
    "name": response_model.__name__,
    "strict": True,
    "schema": response_model.model_json_schema(),
  },
}
```

The returned `message.content` is JSON-parsed and validated via
`response_model.model_validate_json` — so the boundary is always a real pydantic
instance, never a loose dict.

**Why a fallback.** OpenRouter routes the request to whichever upstream provider
serves the slug; not all of them honor strict `json_schema`. If the strict path
raises an `APIStatusError` (route rejected the format) — or returns content that
fails to parse/validate — we retry once with
`response_format={"type": "json_object"}` and prepend a **system message that
embeds the JSON schema** as a hint, then validate the same way. This keeps the
function usable across routes while preferring the stronger guarantee when
available. The choice is logged at WARNING so route-level rejections are visible.

We do **not** use the SDK's `beta.chat.completions.parse` helper: it builds
strongly-typed message params that reject the extra `cache_control` key we need
for prompt caching (see below), and it hides the raw `response_format` we want
to control and fall back on.

## Prompt caching: pass content parts through unmodified

Anthropic prompt caching via OpenRouter uses `cache_control` breakpoints on
message **content parts**:

```python
{"type": "text", "text": "<full current note>", "cache_control": {"type": "ephemeral"}}
```

`cached_text(text)` returns exactly that part. The judge will make one call per
source chunk; the full-note prefix is identical across calls, so marking it with
an ephemeral breakpoint lets Anthropic serve it from cache, and only the
per-call target candidates (the tail after the breakpoint) are billed as input.

The OpenAI SDK's typed content-part models don't include `cache_control` and
would drop or reject it. We therefore pass `messages` as **raw `list[dict]`**
straight into `client.chat.completions.create(messages=...)`; the SDK serializes
unknown keys verbatim, so the breakpoint reaches OpenRouter → Anthropic intact.
A unit test asserts the captured kwargs still carry `cache_control` unchanged.
(If a future SDK version starts stripping unknown keys, the alternative is
`extra_body`; not needed today.)

## Injected client

`make_client(settings)` builds the long-lived `OpenAI` handle once and validates
`openrouter_api_key` **at call time** (not import time), per the config
contract. `complete_structured` takes an optional `client=`; the engine
constructs one client at start-up and injects it so pipeline functions stay
stateless (design §2). When omitted, the function builds a throwaway client —
this is the seam tests monkeypatch (they pass a fake client whose
`chat.completions.create` returns canned JSON and records kwargs).

## Retry behavior

`_call_with_retry` wraps each `create` call: up to 3 attempts on transient
errors only — `APIConnectionError`, `RateLimitError`, `InternalServerError` —
with exponential backoff (0.5s, 1.0s). Non-transient errors (auth, malformed
request) propagate immediately. The strict→json_object fallback is a separate
layer on top, so each `response_format` variant gets its own retry budget.

## Tests (`tests/test_llm.py`)

- returns a validated `JudgeResponse`; strict json_schema response_format and
  model slug are asserted from captured kwargs;
- `cache_control` content part forwarded unmodified;
- transient error → retried then succeeds;
- 4xx on strict path → falls back to `json_object` + schema-hint system message;
- `make_client` raises on empty API key;
- live smoke test, skipped unless `OPENROUTER_API_KEY` is set.

## Doc note

OpenRouter's docs pages are JS-rendered and weren't machine-fetchable during
this task; the `json_schema` / `cache_control` shapes used here are the
standard OpenAI-compatible `response_format` and Anthropic ephemeral-cache
conventions OpenRouter proxies. Confirm against live docs if a route misbehaves.
