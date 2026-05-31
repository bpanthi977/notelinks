"""Chat / judge provider — SWAP POINT 2 (design §2, §9).

This is the ONLY module that knows the chat provider (OpenRouter via the OpenAI
SDK). Callers (``pipeline/judge.py``) speak pydantic models and plain message
dicts; everything provider-specific — structured-output plumbing, Anthropic
prompt caching, transient-error retries — lives here.

Key behaviours
--------------
* **Structured output.** We request ``response_format`` of type ``json_schema``
  with ``strict=True`` and the response model's JSON schema. If the chosen route
  rejects strict json_schema (some OpenRouter providers do), we transparently
  fall back to ``{"type": "json_object"}`` plus a schema hint injected into the
  message stream, then validate. Either way the returned content is parsed as
  JSON and validated through the caller's pydantic model.
* **Prompt caching.** Messages whose ``content`` is a *list of parts* are passed
  through to OpenRouter unmodified, so Anthropic ``cache_control`` breakpoints
  (``{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}``)
  reach the upstream model. The OpenAI SDK's typed params reject the extra
  ``cache_control`` key, so we always send messages as raw dicts via the SDK's
  pass-through (it accepts ``list[dict]``); unknown keys are forwarded verbatim.
* **Injected client.** ``make_client`` builds the long-lived ``OpenAI`` handle
  once (design §2: clients are constructed once and injected). Pipeline
  functions stay stateless and take the client (or let the function build a
  throwaway one, which tests monkeypatch).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, InternalServerError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from notelinks.config import Settings

logger = logging.getLogger(__name__)

# Errors worth a quick retry: transient network / rate-limit / 5xx.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    APIConnectionError,
    RateLimitError,
    InternalServerError,
)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 0.5


def make_client(settings: Settings) -> OpenAI:
    """Build the OpenRouter-backed ``OpenAI`` client from ``settings``.

    Validates the API key at call time (not import time), per the config
    contract. Construct once at engine start-up and inject into pipeline calls.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "openrouter_api_key is empty; set OPENROUTER_API_KEY before making LLM calls."
        )
    return OpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
    )


def cached_text(text: str) -> dict[str, Any]:
    """A text content-part carrying an Anthropic ``ephemeral`` cache breakpoint.

    The caller (judge.py) puts the full-current-note prefix in such a part so
    OpenRouter forwards the cache breakpoint to Anthropic. Everything after the
    breakpoint (per-call target candidates) is the cache-miss tail.
    """
    return {
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }


def _schema_for(response_model: type[BaseModel]) -> dict[str, Any]:
    return response_model.model_json_schema()


def _json_schema_response_format(response_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": response_model.__name__,
            "strict": True,
            "schema": _schema_for(response_model),
        },
    }


def _schema_hint_message(response_model: type[BaseModel]) -> dict[str, Any]:
    """A system message that pins the output shape for the json_object fallback."""
    schema = json.dumps(_schema_for(response_model), separators=(",", ":"))
    return {
        "role": "system",
        "content": (
            "Respond with a single JSON object and nothing else. It MUST validate "
            f"against this JSON schema:\n{schema}"
        ),
    }


def _extract_content(completion: Any) -> str:
    """Pull the text content out of a chat completion, raising if absent."""
    choice = completion.choices[0]
    content = choice.message.content
    if content is None:
        raise ValueError("LLM returned no content (message.content was None)")
    return content


def _call_with_retry(client: OpenAI, model: str, **kwargs: Any) -> Any:
    """One chat.completions.create call with light retry on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return client.chat.completions.create(model=model, **kwargs)
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            if attempt == _MAX_ATTEMPTS:
                break
            sleep_s = _BACKOFF_BASE_S * (2 ** (attempt - 1))
            logger.warning(
                "transient LLM error (attempt %d/%d): %s; retrying in %.1fs",
                attempt,
                _MAX_ATTEMPTS,
                exc,
                sleep_s,
            )
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc


def complete_structured(
    messages: list[dict],
    settings: Settings,
    response_model: type[BaseModel],
    *,
    client: OpenAI | None = None,
) -> BaseModel:
    """Run a structured chat completion and return a validated ``response_model``.

    ``messages`` is a list of raw message dicts. A message's ``content`` may be a
    plain string OR a list of content parts (e.g. from :func:`cached_text`);
    parts — including ``cache_control`` breakpoints — are forwarded to OpenRouter
    unmodified so Anthropic prompt caching works through the proxy.

    Structured output is requested via a strict ``json_schema`` response_format.
    If the route rejects that, we retry once with ``{"type": "json_object"}``
    plus a schema-hint system message. The returned content is JSON-parsed and
    validated through ``response_model``; the validated instance is returned.
    """
    client = client or make_client(settings)
    model = settings.judge_model

    # Pass raw dicts so unknown keys (cache_control on content parts) survive the
    # SDK's serialization untouched.
    base_messages: list[Any] = list(messages)

    # --- Attempt 1: strict json_schema -------------------------------------
    try:
        completion = _call_with_retry(
            client,
            model,
            messages=base_messages,
            response_format=_json_schema_response_format(response_model),
        )
        content = _extract_content(completion)
        return response_model.model_validate_json(content)
    except (APIStatusError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        # APIStatusError: route rejected strict json_schema (4xx).
        # ValidationError/JSONDecodeError/ValueError: malformed structured output.
        logger.warning(
            "strict json_schema path failed (%s: %s); falling back to json_object",
            type(exc).__name__,
            exc,
        )

    # --- Attempt 2: json_object + schema hint ------------------------------
    fallback_messages: list[Any] = [_schema_hint_message(response_model), *base_messages]
    completion = _call_with_retry(
        client,
        model,
        messages=fallback_messages,
        response_format={"type": "json_object"},
    )
    content = _extract_content(completion)
    return response_model.model_validate_json(content)
