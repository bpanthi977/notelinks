"""Broader offline integration / e2e tests for the `Engine` (T15, design §2, §7-§11).

Fully offline. Every test uses a REAL temp ``corpus_dir`` + temp ``index_dir`` and a
REAL :class:`~notelinks.index.store.Store`. Only the two provider boundaries are
mocked:

* **embeddings** — both ``notelinks.index.build.embed_texts`` (corpus indexing) and
  ``notelinks.engine.embed_texts`` (buffer query) are monkeypatched to ONE
  deterministic, content-keyed fake. Vectors are hand-built per topic so
  nearest-neighbour ordering is fully predictable (no network, no real model).
* **the judge** — depending on the scenario we either patch
  ``notelinks.providers.llm.complete_structured`` (genuine end-to-end happy path,
  exercising the real ``judge_candidates`` + anchor resolution) OR patch
  ``notelinks.engine.judge_candidates`` directly (invariant tests, where we want
  precise control over the raw suggestions handed to ``Engine._finalize``).

These tests complement — and deliberately do NOT duplicate — ``test_build.py``
(unit-level refresh gate) and ``test_engine.py`` (a single happy-path e2e). Here we
cover refresh idempotency/incrementality AT THE ENGINE LEVEL, a richer e2e happy
path, and the four finalize invariants (already-linked exclusion, no self-link,
per-(target, heading) dedup, top_n cap).
"""

import os
import socket

import pytest

from notelinks.config import Settings
from notelinks.engine import Engine
from notelinks.index import build as build_mod
from notelinks.models import (
    Envelope,
    JudgeAnchor,
    JudgeResponse,
    RawJudgeSuggestion,
    SourceAnchor,
    SourceChunk,
    Suggestion,
    Target,
    TargetHeading,
)
from notelinks.providers import llm

# ---------------------------------------------------------------------------
# Corpus + buffer fixtures.
# ---------------------------------------------------------------------------

# The note the user is editing (its on-disk copy is also in the corpus). Its
# resonant sentence ("propagates corrections") talks about error-correction.
ACTIVE = """\
:PROPERTIES:
:ID:       uuid-active
:END:
#+title: Active Inference

* Error as signal

Prediction error is the signal that drives belief updating in the brain.
The mismatch between expectation and input is what propagates corrections.
"""

# Resonant target: the SAME error-correction mechanism in another domain.
IMMUNE = """\
:PROPERTIES:
:ID:       uuid-immune
:END:
#+title: Immune Memory

* Affinity maturation

The immune system tunes its antibodies by selecting for better-fitting clones,
a feedback loop that corrects mismatch between receptor and antigen over time.
"""

# A second resonant target (also error-correction themed) so retrieval surfaces
# more than one candidate — used by dedup / cap / multi-suggestion scenarios.
CONTROL = """\
:PROPERTIES:
:ID:       uuid-control
:END:
#+title: Control Theory

* Feedback loops

A controller corrects mismatch between a setpoint and a measurement,
propagates corrections back into the plant until the error shrinks.
"""

# Clearly-unrelated target: must never be the nearest neighbour.
COOKING = """\
:PROPERTIES:
:ID:       uuid-cooking
:END:
#+title: Bread Baking

* Hydration

Higher hydration doughs produce an open crumb, but they are harder to shape.
"""


def _write(path, text, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class EmbedSpy:
    """Deterministic, content-keyed fake embedder that also counts calls.

    Vectors are hand-built per topic so nearest-neighbour ordering is predictable:

    * error/correction text (the active note AND the immune + control bodies) ->
      axis 0, so the buffer's resonant chunk retrieves those targets first;
    * baking text -> axis 1 (far from the query);
    * everything else -> axis 2 (a weak default, also far from the query).

    ``calls`` counts invocations; ``embedded`` counts total texts embedded — both
    let a test prove the mtime/hash gate avoided re-embedding.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.embedded = 0

    def __call__(self, texts, settings, *, client=None):
        self.calls += 1
        self.embedded += len(texts)
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
    """Fail loudly on any outbound connection — proves the run is fully offline."""

    def _blocked(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError("network access attempted during an offline integration test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def spy(monkeypatch):
    """Install the deterministic embedder at BOTH boundaries; return the spy."""
    s = EmbedSpy()
    monkeypatch.setattr(build_mod, "embed_texts", s)
    monkeypatch.setattr("notelinks.engine.embed_texts", s)
    return s


@pytest.fixture
def env(tmp_path, spy):
    """A real temp 4-note corpus + Settings; embeddings already faked via ``spy``.

    Returns ``(corpus_dir, settings, spy)``.
    """
    corpus_dir = tmp_path / "corpus"
    _write(corpus_dir / "active-inference.org", ACTIVE)  # on-disk copy of the buffer
    _write(corpus_dir / "immune-memory.org", IMMUNE)
    _write(corpus_dir / "control-theory.org", CONTROL)
    _write(corpus_dir / "bread-baking.org", COOKING)

    settings = Settings(
        # Dummy key: the shared client is built lazily but never makes a request
        # (both providers are faked + network is blocked).
        openrouter_api_key="test-key",
        corpus_dir=corpus_dir,
        index_dir=tmp_path / "index",
        embedding_dim=3,
        sim_floor=0.1,
    )
    return corpus_dir, settings, spy


# ---------------------------------------------------------------------------
# Scenario 1: refresh idempotency / incrementality, driven via Engine.refresh.
# ---------------------------------------------------------------------------


def test_first_refresh_indexes_all_notes(env):
    corpus_dir, settings, spy = env
    engine = Engine(settings)

    stats = engine.refresh()

    assert stats["indexed"] == 4
    assert stats["reindexed"] == 0
    assert stats["skipped"] == 0
    assert stats["deleted"] == 0
    assert stats["chunks"] > 0
    # Every chunk written this run lives in the store.
    assert engine.store._chunks.count() == stats["chunks"]
    assert set(engine.store.all_manifest_paths()) == {
        "active-inference.org",
        "immune-memory.org",
        "control-theory.org",
        "bread-baking.org",
    }


def test_second_refresh_reembeds_nothing(env):
    corpus_dir, settings, spy = env
    engine = Engine(settings)
    engine.refresh()

    spy.calls = 0
    spy.embedded = 0
    stats = engine.refresh()

    # Idempotent: the mtime/hash gate avoids ALL re-embedding on an unchanged corpus.
    assert spy.calls == 0
    assert spy.embedded == 0
    assert stats == {
        "indexed": 0,
        "reindexed": 0,
        "skipped": 4,
        "deleted": 0,
        "chunks": 0,
    }


def test_editing_one_note_reembeds_only_that_note(env):
    corpus_dir, settings, spy = env
    engine = Engine(settings)
    engine.refresh()
    base_mtime = (corpus_dir / "immune-memory.org").stat().st_mtime

    # Change content AND bump mtime past the watermark so the gate re-hashes + re-embeds.
    edited = IMMUNE.replace("over time.", "over many rounds of selection.")
    _write(corpus_dir / "immune-memory.org", edited, mtime=base_mtime + 100)

    spy.calls = 0
    stats = engine.refresh()

    assert stats["reindexed"] == 1
    assert stats["indexed"] == 0
    assert stats["skipped"] == 3
    assert spy.calls == 1  # ONLY the edited note re-embedded
    # Same uuid, still indexed.
    immune_chunks = engine.store._chunks.get(where={"note_uuid": "uuid-immune"}, include=[])
    assert immune_chunks.get("ids")


def test_deleting_a_note_removes_its_chunks(env):
    corpus_dir, settings, spy = env
    engine = Engine(settings)
    engine.refresh()
    before = engine.store._chunks.get(where={"note_uuid": "uuid-cooking"}, include=[])
    assert before.get("ids"), "cooking note should be indexed before deletion"

    (corpus_dir / "bread-baking.org").unlink()
    stats = engine.refresh()

    assert stats["deleted"] == 1
    assert engine.store.get_manifest("bread-baking.org") is None
    assert "bread-baking.org" not in engine.store.all_manifest_paths()
    after = engine.store._chunks.get(where={"note_uuid": "uuid-cooking"}, include=[])
    assert after.get("ids") == []  # chunks gone too


# ---------------------------------------------------------------------------
# Scenario 2: end-to-end suggest happy path through the REAL judge orchestration.
# We patch only the LLM transport (complete_structured), so retrieve.py +
# judge.py + resolve_anchor + Engine._finalize all run for real.
# ---------------------------------------------------------------------------


def test_suggest_end_to_end_happy_path(env, monkeypatch):
    corpus_dir, settings, spy = env

    def fake_complete_structured(messages, settings_, response_model, *, client=None):
        """Accept the immune target, anchoring on a verbatim span from the buffer."""
        suffix = messages[-1]["content"][-1]["text"]
        # Prefer the immune target id; fall back to the first offered id otherwise.
        target_id = None
        for line in suffix.splitlines():
            if line.startswith("target_chunk_id:"):
                cid = line.split(":", 1)[1].strip()
                if cid.startswith("uuid-immune"):
                    target_id = cid
                    break
                target_id = target_id or cid
        return JudgeResponse(
            suggestions=[
                RawJudgeSuggestion(
                    target_chunk_id=target_id,
                    type="analogous-mechanism",
                    confidence=4,
                    why="Both cast mismatch-correction as the driver of updating.",
                    anchor=JudgeAnchor(mode="wrap", expect="propagates corrections"),
                    target_is_note=False,
                )
            ]
        )

    monkeypatch.setattr(llm, "complete_structured", fake_complete_structured)

    engine = Engine(settings)
    envelope = engine.suggest(ACTIVE)

    # Validates + round-trips against the json-format.md wire shape.
    assert isinstance(envelope, Envelope)
    dumped = envelope.model_dump(mode="json")
    Envelope.model_validate(dumped)
    assert dumped["version"] == 1
    assert dumped["source"]["id"] == "uuid-active"
    assert "file" not in dumped["source"]  # source.file was removed
    assert dumped["source"]["content_hash"].startswith("sha256:")

    assert envelope.suggestions, "expected at least one surfaced suggestion"

    # Confidence-sorted descending.
    confs = [s.confidence for s in envelope.suggestions]
    assert confs == sorted(confs, reverse=True)

    # ids reassigned s01.. in final order.
    assert [s.id for s in envelope.suggestions] == [
        f"s{i:02d}" for i in range(1, len(envelope.suggestions) + 1)
    ]

    top = envelope.suggestions[0]
    # No self-link anywhere.
    for s in envelope.suggestions:
        assert s.target.file_id != "uuid-active"
    # The resonant immune note was surfaced as the nearest neighbour.
    assert top.target.file_id == "uuid-immune"
    # Target carries link COMPONENTS only (file_id + optional heading) — no [[id:]] string.
    assert top.target.file == "immune-memory.org"
    assert top.target.title == "Immune Memory"
    if top.target.heading is not None:
        assert top.target.heading.text == "Affinity maturation"

    # source_anchor offsets bracket the verbatim expect within the buffer.
    anchor = top.source_anchor
    assert ACTIVE[anchor.char_start : anchor.char_end] == anchor.expect == "propagates corrections"
    assert anchor.template == "{{link}}"


# ---------------------------------------------------------------------------
# Scenario 3: finalize invariants. We patch judge_candidates at the engine so we
# control the exact raw Suggestion list reaching Engine._finalize, while retrieval
# and parsing still run for real over the temp corpus.
# ---------------------------------------------------------------------------


def _suggestion(
    *,
    sid: str,
    file_id: str,
    file: str,
    title: str,
    confidence: int,
    heading_text: str | None = None,
    heading_id: str | None = None,
    heading_level: int = 1,
    expect: str = "propagates corrections",
) -> Suggestion:
    """Build a minimal-but-valid `Suggestion` for the invariant tests.

    ``expect`` is a verbatim span from ``ACTIVE`` so the wire ``source_anchor`` is
    realistic; finalize itself only inspects ``target`` + ``confidence``, so the
    anchor values are otherwise inert here.
    """
    start = ACTIVE.find(expect)
    assert start != -1, "expect must be a verbatim ACTIVE span"
    end = start + len(expect)
    heading = (
        TargetHeading(text=heading_text, id=heading_id, level=heading_level)
        if heading_text is not None
        else None
    )
    return Suggestion(
        id=sid,
        type="analogous-mechanism",
        confidence=confidence,
        why="why",
        source_chunk=SourceChunk(
            text=expect, heading="Error as signal", char_start=start, char_end=end
        ),
        target_excerpt="excerpt",
        target=Target(file=file, title=title, file_id=file_id, heading=heading),
        source_anchor=SourceAnchor(
            char_start=start,
            char_end=end,
            expect=expect,
            before=ACTIVE[max(0, start - 10) : start],
            after=ACTIVE[end : end + 10],
            template="{{link}}",
            link_description=expect,
        ),
    )


def _patch_judge(monkeypatch, raw_suggestions):
    """Patch engine.judge_candidates to return a fixed raw `Suggestion` list."""

    def fake_judge(candidates, note, store, settings, *, client=None):
        return list(raw_suggestions)

    monkeypatch.setattr("notelinks.engine.judge_candidates", fake_judge)


def test_already_linked_target_is_never_suggested(env, monkeypatch):
    """(a) A buffer that already links a target -> that target is excluded."""
    corpus_dir, settings, spy = env

    # The buffer already links the immune note via [[id:uuid-immune]].
    linked_buffer = ACTIVE.replace(
        "propagates corrections.",
        "propagates corrections. See [[id:uuid-immune][Immune Memory]].",
    )

    # Judge tries to suggest BOTH immune (already linked) and control (fresh).
    raw = [
        _suggestion(
            sid="s01",
            file_id="uuid-immune",
            file="immune-memory.org",
            title="Immune Memory",
            confidence=5,
        ),
        _suggestion(
            sid="s02",
            file_id="uuid-control",
            file="control-theory.org",
            title="Control Theory",
            confidence=3,
        ),
    ]
    _patch_judge(monkeypatch, raw)

    engine = Engine(settings)
    envelope = engine.suggest(linked_buffer)

    target_ids = {s.target.file_id for s in envelope.suggestions}
    assert "uuid-immune" not in target_ids  # already-linked exclusion
    assert "uuid-control" in target_ids


def test_self_link_is_never_a_target(env, monkeypatch):
    """(b) The source note's own id is never a target."""
    corpus_dir, settings, spy = env

    raw = [
        _suggestion(
            sid="s01",
            file_id="uuid-active",  # self!
            file="active-inference.org",
            title="Active Inference",
            confidence=5,
        ),
        _suggestion(
            sid="s02",
            file_id="uuid-control",
            file="control-theory.org",
            title="Control Theory",
            confidence=4,
        ),
    ]
    _patch_judge(monkeypatch, raw)

    engine = Engine(settings)
    envelope = engine.suggest(ACTIVE)

    target_ids = {s.target.file_id for s in envelope.suggestions}
    assert "uuid-active" not in target_ids  # no self-link
    assert target_ids == {"uuid-control"}


def test_dedup_keeps_highest_confidence_per_target_and_heading(env, monkeypatch):
    """(c) Per-(target note + heading) dedup keeps the single highest-confidence one."""
    corpus_dir, settings, spy = env

    raw = [
        # Same (immune, "Affinity maturation") twice -> keep the conf=5 one.
        _suggestion(
            sid="s01",
            file_id="uuid-immune",
            file="immune-memory.org",
            title="Immune Memory",
            confidence=2,
            heading_text="Affinity maturation",
        ),
        _suggestion(
            sid="s02",
            file_id="uuid-immune",
            file="immune-memory.org",
            title="Immune Memory",
            confidence=5,
            heading_text="Affinity maturation",
        ),
        # Same target note, DIFFERENT heading -> kept separately.
        _suggestion(
            sid="s03",
            file_id="uuid-immune",
            file="immune-memory.org",
            title="Immune Memory",
            confidence=4,
            heading_text="Clonal selection",
        ),
    ]
    _patch_judge(monkeypatch, raw)

    engine = Engine(settings)
    envelope = engine.suggest(ACTIVE)

    # Two survivors: one per distinct heading.
    keys = sorted(
        (s.target.file_id, s.target.heading.text if s.target.heading else None)
        for s in envelope.suggestions
    )
    assert keys == [
        ("uuid-immune", "Affinity maturation"),
        ("uuid-immune", "Clonal selection"),
    ]
    # The kept "Affinity maturation" suggestion is the conf=5 one.
    aff = next(
        s
        for s in envelope.suggestions
        if s.target.heading and s.target.heading.text == "Affinity maturation"
    )
    assert aff.confidence == 5
    # Confidence-sorted desc overall.
    confs = [s.confidence for s in envelope.suggestions]
    assert confs == sorted(confs, reverse=True)


def test_top_n_cap_is_respected(env, monkeypatch):
    """(d) top_n cap respected even when the judge emits more than top_n."""
    corpus_dir, settings, spy = env
    settings.top_n = 2  # cap below the number we will emit

    # Five DISTINCT targets (distinct file_ids so dedup keeps them all), descending conf.
    raw = [
        _suggestion(
            sid=f"s{i:02d}",
            file_id=f"uuid-target-{i}",
            file=f"target-{i}.org",
            title=f"Target {i}",
            confidence=conf,
        )
        for i, conf in enumerate([5, 4, 3, 2, 1], start=1)
    ]
    _patch_judge(monkeypatch, raw)

    engine = Engine(settings)
    envelope = engine.suggest(ACTIVE)

    assert len(envelope.suggestions) == 2  # capped to top_n
    # The two highest-confidence survived, in order, renumbered s01/s02.
    assert [s.confidence for s in envelope.suggestions] == [5, 4]
    assert [s.id for s in envelope.suggestions] == ["s01", "s02"]


# ---------------------------------------------------------------------------
# Scenario 4: unified trace — root span + cross-thread context propagation.
#
# Proves T17's "one trace per suggest run" goal OFFLINE: a LOCAL otel
# TracerProvider with an InMemorySpanExporter (NOT the Phoenix OTLP exporter — no
# collector is running) is pointed at by ``observability._TRACER``, then a real
# ``Engine.suggest`` runs with the two provider boundaries monkeypatched. We then
# assert the manual spans we create — the root ``notelinks.suggest``, the
# ``retrieve_candidates`` RETRIEVER span (same thread), and EVERY
# ``judge_source_chunk`` span (created INSIDE ThreadPoolExecutor worker threads) —
# all share the root's ``trace_id``. The judge-group spans nesting under the root
# is the proxy that proves contextvars-based otel context was propagated across
# threads (the auto OpenAI spans need real calls, so they are out of scope here).
# ---------------------------------------------------------------------------


def test_suggest_emits_one_unified_trace(env, monkeypatch):
    # Skip cleanly when the optional ``trace`` extra is absent. The active trace
    # path (retrieval_span) lazily imports openinference.semconv, so require it
    # too — otherwise the test would fail rather than skip with a partial install.
    pytest.importorskip("opentelemetry.sdk.trace")
    pytest.importorskip("openinference.semconv.trace")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from notelinks import observability

    corpus_dir, settings, spy = env

    # Local, in-memory tracer — no network, no Phoenix collector.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Point the module tracer at our local provider (the autouse fixture in
    # test_observability resets _TRACER; here we set+restore around this test).
    saved_tracer = observability._TRACER
    observability._TRACER = provider.get_tracer("notelinks-test")

    # Judge accepts the immune target, anchoring on a verbatim buffer span.
    def fake_complete_structured(messages, settings_, response_model, *, client=None):
        suffix = messages[-1]["content"][-1]["text"]
        target_id = None
        for line in suffix.splitlines():
            if line.startswith("target_chunk_id:"):
                cid = line.split(":", 1)[1].strip()
                if cid.startswith("uuid-immune"):
                    target_id = cid
                    break
                target_id = target_id or cid
        return JudgeResponse(
            suggestions=[
                RawJudgeSuggestion(
                    target_chunk_id=target_id,
                    type="analogous-mechanism",
                    confidence=4,
                    why="Both cast mismatch-correction as the driver of updating.",
                    anchor=JudgeAnchor(mode="wrap", expect="propagates corrections"),
                    target_is_note=False,
                )
            ]
        )

    monkeypatch.setattr(llm, "complete_structured", fake_complete_structured)

    try:
        engine = Engine(settings)
        envelope = engine.suggest(ACTIVE)
    finally:
        observability._TRACER = saved_tracer

    # The run still produced real output (behaviour unchanged by tracing).
    assert isinstance(envelope, Envelope)
    assert envelope.suggestions

    spans = exporter.get_finished_spans()
    by_name: dict[str, list] = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)

    # The three manual span kinds are present.
    assert len(by_name.get("notelinks.suggest", [])) == 1, "exactly one root span"
    assert by_name.get("retrieve_candidates"), "retrieval span recorded"
    judge_spans = by_name.get("judge_source_chunk", [])
    assert judge_spans, "at least one per-source-chunk judge span (in a worker thread)"

    root = by_name["notelinks.suggest"][0]
    root_trace_id = root.context.trace_id

    # Root span carries the note id/title attributes the engine passed.
    assert root.attributes["notelinks.note_id"] == "uuid-active"
    assert root.attributes["notelinks.note_title"] == "Active Inference"

    # The retrieval span (same thread) shares the root trace.
    for s in by_name["retrieve_candidates"]:
        assert s.context.trace_id == root_trace_id

    # ALL judge-group spans — created inside ThreadPoolExecutor workers — share the
    # SAME trace_id as the root: proof the otel context crossed thread boundaries.
    for s in judge_spans:
        assert s.context.trace_id == root_trace_id, (
            "judge span split into its own trace — thread context not propagated"
        )
