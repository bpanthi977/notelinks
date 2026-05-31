"""Offline smoke tests for the typer CLI adapter (design §2, §11).

We drive the app through :class:`typer.testing.CliRunner` with the same offline
fakes as the engine integration test (embeddings + judge stubbed), assert exit
code 0, and that the printed output parses (JSON for both commands).
"""

import json
import socket

import pytest
from typer.testing import CliRunner

from notelinks.cli import app
from notelinks.index import build as build_mod
from notelinks.models import JudgeResponse
from notelinks.providers import llm

runner = CliRunner()

# Self-contained fixtures (tests/ is not a package, so no cross-test import).
BUFFER = """\
:PROPERTIES:
:ID:       uuid-current
:END:
#+title: Active Inference

* Error as signal

Prediction error is the signal that drives belief updating in the brain.
The mismatch between expectation and input is what propagates corrections.
"""

IMMUNE = """\
:PROPERTIES:
:ID:       uuid-immune
:END:
#+title: Immune Memory

* Affinity maturation

The immune system tunes its antibodies by selecting for better-fitting clones,
a feedback loop that corrects mismatch between receptor and antigen over time.
"""

COOKING = """\
:PROPERTIES:
:ID:       uuid-cooking
:END:
#+title: Bread Baking

* Hydration

Higher hydration doughs produce an open crumb, but they are harder to shape.
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fake_embed(texts, settings, *, client=None):
    out = []
    for t in texts:
        low = t.lower()
        if "mismatch" in low or "correct" in low or "prediction error" in low:
            out.append([1.0, 0.0, 0.0])
        elif "hydration" in low or "dough" in low or "crumb" in low:
            out.append([0.0, 1.0, 0.0])
        else:
            out.append([0.0, 0.0, 1.0])
    return out


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("network access attempted during an offline CLI test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    corpus_dir = tmp_path / "corpus"
    _write(corpus_dir / "active-inference.org", BUFFER)
    _write(corpus_dir / "immune-memory.org", IMMUNE)
    _write(corpus_dir / "bread-baking.org", COOKING)

    monkeypatch.setattr(build_mod, "embed_texts", _fake_embed)
    monkeypatch.setattr("notelinks.engine.embed_texts", _fake_embed)
    # Index dir lives under tmp; point Settings at it via env so Settings() picks
    # it up without a corpus override in the index command.
    monkeypatch.setenv("NOTELINKS_INDEX_DIR", str(tmp_path / "index"))
    # Dummy key so the shared client constructs; no request is ever made (faked).
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    # Judge never accepts — the smoke test only needs valid JSON, not links.
    monkeypatch.setattr(
        llm,
        "complete_structured",
        lambda *a, **k: JudgeResponse(suggestions=[]),
    )
    return corpus_dir


def test_index_command_prints_stats(corpus):
    result = runner.invoke(app, ["index", "--corpus", str(corpus)])
    assert result.exit_code == 0, result.output
    stats = json.loads(result.output)
    assert stats["indexed"] == 3
    assert stats["chunks"] > 0


def test_suggest_command_emits_envelope_json(corpus):
    result = runner.invoke(
        app, ["suggest", "active-inference.org", "--corpus", str(corpus)], input=BUFFER
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["version"] == 1
    assert envelope["source"]["id"] == "uuid-current"
    assert isinstance(envelope["suggestions"], list)
