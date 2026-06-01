"""API tests for the FastAPI daemon (T19).

Skipped entirely when the optional ``[server]`` extra is absent — the import of
``fastapi`` (and the ``notelinks.api`` module that needs it) is guarded by
:func:`pytest.importorskip`, so the default test run (core install, no extra)
never imports FastAPI / uvicorn / watchfiles.

Fully offline: the two provider boundaries are monkeypatched exactly as in the
engine integration tests — embeddings (both ``build`` and ``engine`` entry
points) and the judge LLM (``complete_structured``) — against a temp corpus +
index. We drive the app via FastAPI's ``TestClient`` and assert:

* ``GET /health`` → ``{"status": "ok"}``;
* ``POST /refresh`` indexes the corpus (returns build stats);
* ``POST /suggest`` returns a valid, confidence-sorted ``Envelope`` whose targets
  carry link COMPONENTS only;
* ``suggest`` does NOT itself refresh — a fresh engine with no prior refresh (we
  bypass the lifespan's initial refresh) returns no corpus-based suggestions
  until ``/refresh`` runs.

The lifespan's watcher uses real ``watchfiles`` but we never trigger file events
in these tests; ``TestClient`` runs the lifespan (initial refresh + watcher
start/stop) around the ``with`` block.
"""

import socket

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from notelinks.config import Settings  # noqa: E402
from notelinks.index import build as build_mod  # noqa: E402
from notelinks.models import (  # noqa: E402
    JudgeAnchor,
    JudgeResponse,
    RawJudgeSuggestion,
)
from notelinks.providers import llm  # noqa: E402

# A note buffer the user is editing (matches the engine-test fixtures' shape).
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
    """Deterministic 3-D vectors keyed by topic (mirror of the engine tests)."""
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


def _fake_complete_structured(messages, settings_, response_model, *, client=None):
    """Accept the nearest immune target, anchoring on a verbatim buffer span."""
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
    return JudgeResponse(
        suggestions=[
            RawJudgeSuggestion(
                candidate_id=chosen,
                type="analogous-mechanism",
                confidence=2,
                why="Both cast mismatch-correction as the driver of updating.",
                anchor=JudgeAnchor(mode="wrap", expect="propagates corrections"),
                target_is_note=False,
            )
        ]
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Block real outbound connections so the API run is provably offline."""

    def _blocked(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("network access attempted during an offline API test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp corpus + settings with both provider boundaries faked."""
    corpus_dir = tmp_path / "corpus"
    _write(corpus_dir / "active-inference.org", BUFFER)
    _write(corpus_dir / "immune-memory.org", IMMUNE)
    _write(corpus_dir / "bread-baking.org", COOKING)

    monkeypatch.setattr(build_mod, "embed_texts", _fake_embed)
    monkeypatch.setattr("notelinks.engine.embed_texts", _fake_embed)
    monkeypatch.setattr(llm, "complete_structured", _fake_complete_structured)

    settings = Settings(
        openrouter_api_key="test-key",
        corpus_dir=corpus_dir,
        index_dir=tmp_path / "index",
        embedding_dim=3,
        sim_floor=0.1,
    )
    return settings


def test_health(env):
    from notelinks.api import build_app

    with TestClient(build_app(env)) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_refresh_indexes_and_status_reports(env):
    from notelinks.api import build_app

    with TestClient(build_app(env)) as client:
        # The lifespan already ran an initial refresh; an explicit refresh is a
        # cheap incremental no-op-ish call but still returns the stats dict.
        r = client.post("/refresh", json={"rebuild": True})
        assert r.status_code == 200
        stats = r.json()
        assert stats["indexed"] + stats["reindexed"] == 3
        assert stats["chunks"] > 0

        s = client.get("/status").json()
        assert s["notes"] == 3
        assert s["chunks"] > 0
        assert s["last_refresh"] is not None


def test_suggest_returns_valid_envelope(env):
    from notelinks.api import build_app

    with TestClient(build_app(env)) as client:
        # Lifespan did the initial refresh, so the index is warm.
        r = client.post("/suggest", json={"buffer": BUFFER})
        assert r.status_code == 200
        env_json = r.json()

        assert env_json["version"] == 1
        assert env_json["source"]["id"] == "uuid-current"
        assert "file" not in env_json["source"]  # source.file was removed
        assert env_json["source"]["content_hash"].startswith("sha256:")

        suggestions = env_json["suggestions"]
        assert suggestions, "expected at least one surfaced suggestion"

        # Confidence-sorted (desc).
        confs = [s["confidence"] for s in suggestions]
        assert confs == sorted(confs, reverse=True)

        # ids reassigned in final order.
        assert [s["id"] for s in suggestions] == [
            f"s{i:02d}" for i in range(1, len(suggestions) + 1)
        ]

        # No self-link; nearest resonant note surfaced; target = COMPONENTS only.
        for s in suggestions:
            assert s["target"]["file_id"] != "uuid-current"
        top = suggestions[0]
        assert top["target"]["file_id"] == "uuid-immune"
        assert top["target"]["file"] == "immune-memory.org"
        assert "[[id:" not in str(top["target"])  # no finished link string


def test_suggest_does_not_itself_refresh(env, monkeypatch):
    """A fresh engine with no prior refresh yields no corpus suggestions.

    We bypass the lifespan's initial refresh by hitting the endpoint functions
    against a hand-built ``_State`` whose engine was never refreshed — proving
    ``suggest`` is a pure query (T19): it returns an empty envelope until a
    ``/refresh`` populates the index, then returns the corpus-based suggestion.
    """
    from notelinks.api import _State
    from notelinks.engine import Engine

    state = _State(Engine(env))

    # No refresh yet: the index is empty, so suggest finds no candidates.
    envelope = state.suggest(BUFFER)
    assert envelope.suggestions == [], "suggest must not auto-refresh the corpus"

    # After an explicit refresh, the same query surfaces the immune target.
    state.refresh()
    envelope2 = state.suggest(BUFFER)
    assert envelope2.suggestions
    assert envelope2.suggestions[0].target.file_id == "uuid-immune"
