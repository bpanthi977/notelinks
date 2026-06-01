"""Offline integration test for the `Engine` (design §2, §8-§11).

Fully offline. We build a real temp corpus + a real :class:`Store`, but stub the
two provider boundaries:

* embeddings — both ``notelinks.index.build.embed_texts`` (used when refresh
  indexes the corpus) and ``notelinks.engine.embed_texts`` (used to embed the
  query buffer) are monkeypatched to one deterministic fake. Vectors are keyed
  so the buffer's resonant sentence lands nearest a SPECIFIC target note.
* the judge LLM — ``notelinks.providers.llm.complete_structured`` is patched to
  return a canned :class:`JudgeResponse` that points at a real retrieved target
  chunk with an ``expect`` copied verbatim from the buffer.

We then drive ``Engine(settings).suggest(buffer)`` end to end and assert
the envelope validates, is confidence-sorted, has no self-link, carries target
COMPONENTS only, and that ``source_anchor`` offsets land on the buffer.
"""

import socket

import pytest

from notelinks.config import Settings
from notelinks.engine import Engine
from notelinks.index import build as build_mod
from notelinks.models import Envelope, JudgeAnchor, JudgeResponse, RawJudgeSuggestion
from notelinks.providers import llm

# The buffer (current note) the user is editing — not yet on disk.
BUFFER = """\
:PROPERTIES:
:ID:       uuid-current
:END:
#+title: Active Inference

* Error as signal

Prediction error is the signal that drives belief updating in the brain.
The mismatch between expectation and input is what propagates corrections.
"""

# A target whose body talks about the SAME mechanism (the one we want surfaced).
IMMUNE = """\
:PROPERTIES:
:ID:       uuid-immune
:END:
#+title: Immune Memory

* Affinity maturation

The immune system tunes its antibodies by selecting for better-fitting clones,
a feedback loop that corrects mismatch between receptor and antigen over time.
"""

# A clearly-unrelated target that should not be the nearest neighbour.
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


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly on any outbound connection — proves the run is offline."""

    def _blocked(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("network access attempted during an offline engine test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def _fake_embed(texts, settings, *, client=None):
    """Deterministic 3-D vectors keyed by topic so retrieval is predictable.

    Anything mentioning the error/correction mechanism (the current note AND the
    immune note's body) embeds toward axis 0; baking embeds toward axis 1; a
    weak default keeps every other chunk far from the query.
    """
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp corpus + settings with both embed boundaries faked."""
    corpus_dir = tmp_path / "corpus"
    _write(corpus_dir / "active-inference.org", BUFFER)  # on-disk copy of current note
    _write(corpus_dir / "immune-memory.org", IMMUNE)
    _write(corpus_dir / "bread-baking.org", COOKING)

    # Both embedding entry points used during suggest(): corpus build + query.
    monkeypatch.setattr(build_mod, "embed_texts", _fake_embed)
    monkeypatch.setattr("notelinks.engine.embed_texts", _fake_embed)

    settings = Settings(
        # A dummy key: the shared client is constructed but never makes a request
        # (embeddings + judge are faked, and network is blocked by _no_network).
        openrouter_api_key="test-key",
        corpus_dir=corpus_dir,
        index_dir=tmp_path / "index",
        embedding_dim=3,
        sim_floor=0.1,
    )
    return corpus_dir, settings


def test_refresh_indexes_the_corpus(env, monkeypatch):
    corpus_dir, settings = env
    engine = Engine(settings)
    stats = engine.refresh()
    assert stats["indexed"] == 3
    assert stats["chunks"] > 0
    assert set(engine.store.all_manifest_paths()) == {
        "active-inference.org",
        "immune-memory.org",
        "bread-baking.org",
    }


def test_suggest_end_to_end(env, monkeypatch):
    corpus_dir, settings = env

    captured: dict[str, str] = {}

    def fake_complete_structured(messages, settings_, response_model, *, client=None):
        """Accept the nearest immune target, anchoring on a verbatim buffer span."""
        # Find the immune candidate's number (as labelled) in the suffix so we
        # point at a real offered candidate (the engine maps unknown ids away).
        suffix = messages[-1]["content"][-1]["text"]
        chosen = None
        current = None
        for line in suffix.splitlines():
            if line.startswith("candidate_id:"):
                current = int(line.split(":", 1)[1].strip())
            elif "Immune Memory" in line and current is not None:
                chosen = current
                break
        if chosen is None:
            chosen = 1
        captured["candidate_id"] = chosen
        return JudgeResponse(
            suggestions=[
                RawJudgeSuggestion(
                    candidate_id=chosen,
                    type="analogous-mechanism",
                    confidence=2,
                    why="Both cast mismatch-correction as the driver of updating.",
                    anchor=JudgeAnchor(
                        mode="wrap",
                        # verbatim span lifted from BUFFER's source chunk.
                        expect="propagates corrections",
                    ),
                    target_is_note=False,
                )
            ]
        )

    monkeypatch.setattr(llm, "complete_structured", fake_complete_structured)

    engine = Engine(settings)
    # suggest() is now a pure query (T19): refresh the corpus first so the index
    # is populated before querying (the CLI/server adapters own freshness).
    engine.refresh()
    envelope = engine.suggest(BUFFER)

    # Envelope validates and round-trips through the wire schema.
    assert isinstance(envelope, Envelope)
    Envelope.model_validate(envelope.model_dump(mode="json"))

    assert envelope.version == 1
    assert envelope.source.id == "uuid-current"
    assert "file" not in envelope.source.model_dump()  # source.file was removed
    assert envelope.source.content_hash.startswith("sha256:")

    assert envelope.suggestions, "expected at least one surfaced suggestion"

    # Confidence-sorted (desc).
    confs = [s.confidence for s in envelope.suggestions]
    assert confs == sorted(confs, reverse=True)

    # ids reassigned in final order.
    assert [s.id for s in envelope.suggestions] == [
        f"s{i:02d}" for i in range(1, len(envelope.suggestions) + 1)
    ]

    top = envelope.suggestions[0]
    # No self-link: the on-disk copy of the current note (same uuid) is excluded.
    for s in envelope.suggestions:
        assert s.target.file_id != "uuid-current"
    # The resonant immune note was the one surfaced (nearest neighbour).
    assert top.target.file_id == "uuid-immune"

    # Target carries link COMPONENTS only — no finished [[id:...]] string.
    assert top.target.file == "immune-memory.org"
    assert top.target.file_id == "uuid-immune"
    if top.target.heading is not None:
        assert top.target.heading.text == "Affinity maturation"

    # source_anchor offsets land on the buffer and bracket the verbatim expect.
    anchor = top.source_anchor
    assert BUFFER[anchor.char_start : anchor.char_end] == anchor.expect == "propagates corrections"
    assert anchor.template == "{{link}}"
