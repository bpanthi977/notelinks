"""T18: local-Ollama judge routing, configured entirely from the environment.

Covers the four moving parts of running the judge on a local Ollama model while
keeping embeddings on OpenRouter:

* config: ``NOTELINKS_JUDGE_PROVIDER`` selects the provider (default openrouter).
* ``make_judge_client``: ollama → the Ollama base_url, built WITHOUT an OpenRouter
  key; openrouter → the OpenRouter base_url.
* ``complete_structured`` message normalization: ollama flattens list-of-parts
  content and drops ``cache_control``; openrouter preserves it byte-for-byte.
* engine: the per-role client split routes judge → ollama, embeddings →
  OpenRouter.

Everything runs offline — no Ollama, no API key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from notelinks.config import Settings
from notelinks.engine import Engine
from notelinks.models import JudgeResponse
from notelinks.providers import llm

_VALID_JUDGE_JSON = json.dumps(
    {
        "suggestions": [
            {
                "target_chunk_id": "uuid-1:3",
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
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _FakeCompletions:
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


# --- config -----------------------------------------------------------------


def test_judge_provider_defaults_to_openrouter(monkeypatch: Any) -> None:
    monkeypatch.delenv("NOTELINKS_JUDGE_PROVIDER", raising=False)
    assert Settings().judge_provider == "openrouter"


def test_judge_provider_from_env_ollama(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTELINKS_JUDGE_PROVIDER", "ollama")
    assert Settings().judge_provider == "ollama"


def test_ollama_base_url_default_and_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert Settings().ollama_base_url == "http://localhost:11434/v1"
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example:1234/v1")
    assert Settings().ollama_base_url == "http://example:1234/v1"


# --- make_judge_client ------------------------------------------------------


def test_make_judge_client_ollama_no_key_required() -> None:
    # No OpenRouter key set: ollama must still build (local needs none).
    settings = Settings(openrouter_api_key="", judge_provider="ollama")
    client = llm.make_judge_client(settings)
    assert str(client.base_url).rstrip("/") == "http://localhost:11434/v1"


def test_make_judge_client_ollama_respects_custom_url() -> None:
    settings = Settings(
        openrouter_api_key="",
        judge_provider="ollama",
        ollama_base_url="http://host.docker.internal:11434/v1",
    )
    client = llm.make_judge_client(settings)
    assert str(client.base_url).rstrip("/") == "http://host.docker.internal:11434/v1"


def test_make_judge_client_openrouter() -> None:
    settings = Settings(openrouter_api_key="test-key", judge_provider="openrouter")
    client = llm.make_judge_client(settings)
    assert "openrouter.ai" in str(client.base_url)


def test_make_judge_client_openrouter_requires_key() -> None:
    settings = Settings(openrouter_api_key="", judge_provider="openrouter")
    with pytest.raises(RuntimeError, match="openrouter_api_key is empty"):
        llm.make_judge_client(settings)


# --- complete_structured message normalization ------------------------------


def _cached_messages() -> list[dict]:
    prefix = llm.cached_text("FULL CURRENT NOTE TEXT")
    return [
        {"role": "system", "content": "system prompt"},
        {
            "role": "user",
            "content": [prefix, {"type": "text", "text": "candidate targets..."}],
        },
    ]


def test_ollama_flattens_content_and_drops_cache_control() -> None:
    settings = Settings(judge_provider="ollama", judge_model="gemma3n:e2b")
    client, fake = _client_with([_VALID_JUDGE_JSON])

    llm.complete_structured(_cached_messages(), settings, JudgeResponse, client=client)

    sent = fake.calls[0]["messages"]
    assert fake.calls[0]["model"] == "gemma3n:e2b"
    # System message left as a plain string.
    assert sent[0]["content"] == "system prompt"
    # User message: list-of-parts flattened to one string, no cache_control.
    user_content = sent[1]["content"]
    assert isinstance(user_content, str)
    assert user_content == "FULL CURRENT NOTE TEXTcandidate targets..."
    assert "cache_control" not in json.dumps(sent)


def test_openrouter_preserves_cache_control() -> None:
    settings = Settings(openrouter_api_key="test-key", judge_provider="openrouter")
    client, fake = _client_with([_VALID_JUDGE_JSON])

    llm.complete_structured(_cached_messages(), settings, JudgeResponse, client=client)

    sent = fake.calls[0]["messages"]
    sent_content = sent[1]["content"]
    # Unchanged behaviour: list-of-parts + cache_control survive intact.
    assert isinstance(sent_content, list)
    assert sent_content[0]["cache_control"] == {"type": "ephemeral"}
    assert sent_content[0]["text"] == "FULL CURRENT NOTE TEXT"
    assert sent_content[1] == {"type": "text", "text": "candidate targets..."}


def test_ollama_does_not_mutate_input_messages() -> None:
    settings = Settings(judge_provider="ollama", judge_model="gemma3n:e2b")
    client, _ = _client_with([_VALID_JUDGE_JSON])
    messages = _cached_messages()

    llm.complete_structured(messages, settings, JudgeResponse, client=client)

    # Caller's original messages are untouched (still list-of-parts + cache_control).
    assert isinstance(messages[1]["content"], list)
    assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


# --- engine per-role client split -------------------------------------------


def test_engine_routes_judge_to_ollama_embeddings_to_openrouter(tmp_path: Any) -> None:
    settings = Settings(
        openrouter_api_key="test-key",
        judge_provider="ollama",
        corpus_dir=tmp_path,
    )
    engine = Engine(settings)
    assert str(engine.judge_client.base_url).rstrip("/") == "http://localhost:11434/v1"
    assert "openrouter.ai" in str(engine.embedding_client.base_url)
