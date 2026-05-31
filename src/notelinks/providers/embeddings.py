"""Embedding provider — SWAP POINT 1 (design §2, §3, §6).

The ONLY module that knows the embedding backend is OpenRouter-via-OpenAI-SDK.
Everything else asks for ``embed_texts(list[str]) -> list[vector]`` and stays
provider-agnostic. To swap providers, reimplement this file; nothing else
changes.

Contract::

    embed_texts(texts, settings, *, client=None) -> list[list[float]]

* One output vector per input text, in the SAME order as the input.
* Empty input -> empty output (no request made).
* Uses ``settings.embedding_model`` and ``settings.embedding_dim``.
* Inputs are batched (``_BATCH_SIZE`` per request) so a large corpus does not
  blow the per-request payload/limit; batch boundaries never reorder results.

The ``client`` parameter lets the long-lived ``Engine`` (design §2) construct
one shared :class:`OpenAI` client and inject it across all calls. When omitted
a client is built lazily from ``settings`` — convenient for tests / one-offs.
"""

from __future__ import annotations

import time

from openai import APIConnectionError, APIError, OpenAI, RateLimitError

from notelinks.config import Settings

# Max inputs per /embeddings request. text-embedding-3-* accepts large batches;
# 128 keeps payloads modest while amortising round-trips over a big corpus.
_BATCH_SIZE = 128

# Modest retry on transient/rate-limit failures (design: "keep it modest").
_MAX_ATTEMPTS = 4
_BASE_DELAY = 0.5  # seconds; exponential backoff: 0.5, 1.0, 2.0, ...
_RETRYABLE = (RateLimitError, APIConnectionError, APIError)


def make_client(settings: Settings) -> OpenAI:
    """Build an :class:`OpenAI` client pointed at the configured provider.

    Validates the API key at *call time* (not import time) per ``config.py``.
    """
    if not settings.openrouter_api_key:
        raise ValueError(
            "openrouter_api_key is empty; set OPENROUTER_API_KEY before embedding."
        )
    return OpenAI(
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
    )


def embed_texts(
    texts: list[str],
    settings: Settings,
    *,
    client: OpenAI | None = None,
) -> list[list[float]]:
    """Embed ``texts``, returning one vector per text in input order.

    Args:
        texts: input strings to embed.
        settings: config supplying the model name and dimensionality.
        client: optional shared client (injected by the ``Engine``). Built
            lazily from ``settings`` when omitted.

    Returns:
        ``list[list[float]]`` — one vector per input, same order. ``[]`` for
        empty input (no request is made).
    """
    if not texts:
        return []

    if client is None:
        client = make_client(settings)

    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[start : start + _BATCH_SIZE]
        response = _create_with_retry(client, batch, settings)
        # API guarantees one datum per input; sort by index defensively so a
        # provider returning out-of-order data can never scramble alignment.
        data = sorted(response.data, key=lambda d: d.index)
        vectors.extend(d.embedding for d in data)

    return vectors


def _create_with_retry(client: OpenAI, batch: list[str], settings: Settings):
    """Call the embeddings endpoint for one batch with modest backoff retry."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return client.embeddings.create(
                model=settings.embedding_model,
                input=batch,
                dimensions=settings.embedding_dim,
            )
        except _RETRYABLE as exc:  # transient: rate limit / connection / 5xx
            last_exc = exc
            if attempt == _MAX_ATTEMPTS - 1:
                break
            time.sleep(_BASE_DELAY * (2**attempt))
    assert last_exc is not None  # only reached after a retryable failure
    raise last_exc
