"""Unit tests for the chat/judge provider (SWAP POINT 2).

The OpenAI client's ``chat.completions.create`` is monkeypatched with a fake
that (a) captures the kwargs it was called with and (b) returns a canned
completion whose content is a JSON string. We assert:

* ``complete_structured(..., JudgeResponse)`` returns a validated
  ``JudgeResponse``.
* ``response_format`` is the strict json_schema for the model.
* ``cache_control`` breakpoints in message content parts are forwarded
  unmodified.
* On a transient error the call is retried.
* A 4xx ``APIStatusError`` on the strict path falls back to ``json_object``.

A live smoke test runs only when ``OPENROUTER_API_KEY`` is set.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest
from openai import APIConnectionError, APIStatusError

from notelinks.config import Settings
from notelinks.models import JudgeResponse
from notelinks.providers import llm

_VALID_JUDGE_JSON = json.dumps(
    {
        "suggestions": [
            {
                "candidate_id": 1,
                "type": "analogous-mechanism",
                "confidence": 4,
                "why": "Both describe feedback loops that stabilize a system.",
                "anchor": {
                    "mode": "wrap",
                    "expect": "negative feedback keeps the system in check",
                    "insert_text": None,
                },
                "target_is_note": False,
            }
        ]
    }
)


def _fake_completion(content: str) -> Any:
    """Mimic the shape of an OpenAI ChatCompletion (only the bits we read)."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _FakeCompletions:
    """Records every create() call; returns canned content per scripted attempt."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return _fake_completion(result)


def _client_with(responses: list[Any]) -> tuple[Any, _FakeCompletions]:
    fake = _FakeCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return client, fake


@pytest.fixture
def settings() -> Settings:
    return Settings(openrouter_api_key="test-key", judge_model="anthropic/claude-sonnet-4.5")


def test_returns_validated_judge_response(settings: Settings) -> None:
    client, fake = _client_with([_VALID_JUDGE_JSON])
    messages = [{"role": "user", "content": "judge this"}]

    result = llm.complete_structured(messages, settings, JudgeResponse, client=client)

    assert isinstance(result, JudgeResponse)
    assert len(result.suggestions) == 1
    assert result.suggestions[0].type.value == "analogous-mechanism"
    assert result.suggestions[0].anchor.mode == "wrap"

    # Strict json_schema response_format was requested for the right model.
    call = fake.calls[0]
    assert call["model"] == "anthropic/claude-sonnet-4.5"
    rf = call["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "JudgeResponse"
    assert rf["json_schema"]["strict"] is True
    assert "properties" in rf["json_schema"]["schema"]


def test_cache_control_is_forwarded_unmodified(settings: Settings) -> None:
    client, fake = _client_with([_VALID_JUDGE_JSON])
    prefix = llm.cached_text("FULL CURRENT NOTE TEXT")
    assert prefix["cache_control"] == {"type": "ephemeral"}

    messages = [
        {
            "role": "user",
            "content": [prefix, {"type": "text", "text": "candidate targets..."}],
        }
    ]

    llm.complete_structured(messages, settings, JudgeResponse, client=client)

    sent = fake.calls[0]["messages"]
    sent_content = sent[0]["content"]
    # The cache breakpoint survived intact.
    assert sent_content[0]["cache_control"] == {"type": "ephemeral"}
    assert sent_content[0]["text"] == "FULL CURRENT NOTE TEXT"
    assert sent_content[1] == {"type": "text", "text": "candidate targets..."}


def test_retries_on_transient_error(settings: Settings, monkeypatch: Any) -> None:
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    transient = APIConnectionError(request=None)  # type: ignore[arg-type]
    client, fake = _client_with([transient, _VALID_JUDGE_JSON])

    result = llm.complete_structured(
        [{"role": "user", "content": "x"}], settings, JudgeResponse, client=client
    )

    assert isinstance(result, JudgeResponse)
    assert len(fake.calls) == 2  # one failed attempt + one success


def test_falls_back_to_json_object_on_strict_rejection(
    settings: Settings, monkeypatch: Any
) -> None:
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    rejection = APIStatusError(
        "strict json_schema not supported",
        response=SimpleNamespace(status_code=400, headers={}, request=None),  # type: ignore[arg-type]
        body=None,
    )
    client, fake = _client_with([rejection, _VALID_JUDGE_JSON])

    result = llm.complete_structured(
        [{"role": "user", "content": "x"}], settings, JudgeResponse, client=client
    )

    assert isinstance(result, JudgeResponse)
    # First call used json_schema, second used json_object + schema-hint system msg.
    assert fake.calls[0]["response_format"]["type"] == "json_schema"
    assert fake.calls[1]["response_format"] == {"type": "json_object"}
    assert fake.calls[1]["messages"][0]["role"] == "system"
    assert "JSON schema" in fake.calls[1]["messages"][0]["content"]


def test_strict_schema_strips_unsupported_numeric_constraints(settings: Settings) -> None:
    """Anthropic via OpenRouter 400s on minimum/maximum for integer types.

    Pydantic emits ``minimum``/``maximum`` for ``confidence: int = Field(ge=1, le=5)``.
    The strict json_schema we SEND must have those stripped (the constraint is still
    enforced when we validate the response through the pydantic model).
    """
    client, fake = _client_with([_VALID_JUDGE_JSON])
    llm.complete_structured(
        [{"role": "user", "content": "x"}], settings, JudgeResponse, client=client
    )
    schema = fake.calls[0]["response_format"]["json_schema"]["schema"]
    schema_text = json.dumps(schema)
    assert "minimum" not in schema_text
    assert "maximum" not in schema_text
    # Structure is otherwise intact.
    assert "properties" in schema


def test_extracts_json_from_markdown_fence(settings: Settings) -> None:
    """The json_object fallback often returns prose + a ```json fenced block.

    Claude wraps its JSON in reasoning text and/or a markdown code fence; the
    content must still validate. (Observed live behaviour on the json_object route.)"""
    fenced = (
        "Let me analyze each candidate.\n\n```json\n" + _VALID_JUDGE_JSON + "\n```\n"
    )
    client, fake = _client_with([fenced])
    result = llm.complete_structured(
        [{"role": "user", "content": "x"}], settings, JudgeResponse, client=client
    )
    assert isinstance(result, JudgeResponse)
    assert len(result.suggestions) == 1


def test_make_client_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="openrouter_api_key is empty"):
        llm.make_client(Settings(openrouter_api_key=""))


@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="live smoke test; set OPENROUTER_API_KEY to run",
)
def test_live_smoke() -> None:
    settings = Settings()  # reads OPENROUTER_API_KEY from env
    messages = [
        {
            "role": "user",
            "content": [
                llm.cached_text(
                    "Source note: negative feedback loops keep systems stable."
                ),
                {
                    "type": "text",
                    "text": (
                        "Candidate target chunk uuid-1:3 (heading 'Homeostasis'): "
                        "the body regulates temperature via feedback. "
                        "Suggest at most one link or none."
                    ),
                },
            ],
        }
    ]
    result = llm.complete_structured(messages, settings, JudgeResponse)
    assert isinstance(result, JudgeResponse)
