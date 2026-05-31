"""Tests for the judge: pure anchor resolution + orchestration (design §9).

No network: ``llm.complete_structured`` is monkeypatched to return a canned
``JudgeResponse``, and a fake store returns a canned same-heading neighbour.
"""

from __future__ import annotations

import notelinks.pipeline.judge as judge_mod
from notelinks.config import Settings
from notelinks.models import (
    Candidate,
    Chunk,
    JudgeAnchor,
    JudgeResponse,
    Note,
    RawJudgeSuggestion,
)
from notelinks.pipeline.judge import judge_candidates, resolve_anchor


def make_chunk(
    note_uuid: str,
    ordinal: int,
    text: str = "body",
    *,
    char_start: int = 0,
    char_end: int | None = None,
    heading_text: str | None = None,
    heading_id: str | None = None,
    heading_level: int | None = None,
    heading_index: int = 0,
    chunk_in_heading: int = 0,
    note_path: str | None = None,
    note_title: str | None = None,
    heading_path: str = "",
) -> Chunk:
    return Chunk(
        note_uuid=note_uuid,
        note_path=note_path or f"{note_uuid}.org",
        note_title=note_title or note_uuid,
        heading_path=heading_path,
        heading_text=heading_text,
        heading_id=heading_id,
        heading_level=heading_level,
        heading_index=heading_index,
        chunk_in_heading=chunk_in_heading,
        ordinal=ordinal,
        char_start=char_start,
        char_end=char_end if char_end is not None else len(text),
        text=text,
        embed_text=text,
    )


# ---------------------------------------------------------------------------
# Piece 1: resolve_anchor (pure core).
# ---------------------------------------------------------------------------


def test_resolve_anchor_wrap_offsets_and_context() -> None:
    buffer = "Prediction error is the driving signal of belief updating here."
    #          0123456789...      ^ "driving signal" begins at index 24
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    anchor = JudgeAnchor(mode="wrap", expect="driving signal")

    out = resolve_anchor(anchor, src, buffer)

    assert out is not None
    start = buffer.index("driving signal")
    assert out.char_start == start
    assert out.char_end == start + len("driving signal")
    assert out.expect == "driving signal"
    assert out.template == "{{link}}"
    assert out.link_description == "driving signal"
    assert out.before == buffer[max(0, start - 40) : start]
    assert out.after == buffer[start + len("driving signal") : start + len("driving signal") + 40]


def test_resolve_anchor_insert_empty_region_and_template() -> None:
    buffer = "The system minimizes surprise. It does so continuously."
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    sentence = "The system minimizes surprise."
    anchor = JudgeAnchor(
        mode="insert",
        expect=sentence,
        insert_text=" See also {{link}}.",
    )

    out = resolve_anchor(anchor, src, buffer)

    assert out is not None
    end = buffer.index(sentence) + len(sentence)
    # Empty region at the END of the matched expect span.
    assert out.char_start == end
    assert out.char_end == end
    assert out.expect == ""
    assert out.template == " See also {{link}}."
    assert out.before == buffer[max(0, end - 40) : end]
    assert out.after == buffer[end : end + 40]


def test_resolve_anchor_not_found_returns_none() -> None:
    buffer = "Nothing matching here."
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    anchor = JudgeAnchor(mode="wrap", expect="absent phrase")

    assert resolve_anchor(anchor, src, buffer) is None


def test_resolve_anchor_chunk_scoped_ignores_outside_span() -> None:
    # "target" appears before the chunk span; search must ignore it.
    buffer = "target appears early. CHUNKBODY without it."
    span_start = buffer.index("CHUNKBODY")
    src = make_chunk("S", 0, char_start=span_start, char_end=len(buffer))
    anchor = JudgeAnchor(mode="wrap", expect="target")

    # "target" only exists outside the chunk span -> not found.
    assert resolve_anchor(anchor, src, buffer) is None


def test_resolve_anchor_duplicate_in_chunk_picks_first() -> None:
    buffer = "echo and echo again, two echo tokens."
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    anchor = JudgeAnchor(mode="wrap", expect="echo")

    out = resolve_anchor(anchor, src, buffer)

    assert out is not None
    assert out.char_start == buffer.index("echo")  # the FIRST occurrence
    assert out.char_start == 0


# ---------------------------------------------------------------------------
# Piece 2: judge_candidates (orchestration), with monkeypatched LLM + fake store.
# ---------------------------------------------------------------------------


class FakeStore:
    """Canned ``get_chunk`` returning a single neighbour for any lookup."""

    def __init__(self, neighbour: Chunk | None) -> None:
        self._neighbour = neighbour
        self.calls: list[tuple[str, int, int]] = []

    def get_chunk(
        self, note_uuid: str, heading_index: int, chunk_in_heading: int
    ) -> Chunk | None:
        self.calls.append((note_uuid, heading_index, chunk_in_heading))
        return self._neighbour


SOURCE_BUFFER = (
    "Prediction error is the driving signal of belief updating. "
    "The brain corrects its model against incoming sensation."
)


def _make_note() -> Note:
    return Note(id="SRC-UUID", path="notes/cur.org", title="Current", text=SOURCE_BUFFER)


def _make_candidate() -> Candidate:
    source = make_chunk(
        "SRC-UUID",
        0,
        text=SOURCE_BUFFER,
        char_start=0,
        char_end=len(SOURCE_BUFFER),
        heading_text="Error as signal",
        heading_index=1,
        note_path="notes/cur.org",
        note_title="Current",
    )
    target = make_chunk(
        "TGT-UUID",
        2,
        text="The immune system tunes itself by selecting high-affinity clones.",
        char_start=10,
        char_end=80,
        heading_text="Affinity maturation",
        heading_id="HEAD-ID",
        heading_level=2,
        heading_index=3,
        chunk_in_heading=1,
        note_path="notes/immune.org",
        note_title="Immune Memory",
        heading_path="Immune Memory > Affinity maturation",
    )
    return Candidate(source_chunk=source, target_chunk=target, score=0.71)


def test_judge_candidates_assembles_suggestion(monkeypatch) -> None:
    candidate = _make_candidate()
    neighbour = make_chunk("TGT-UUID", 1, text="Prior exposure primes the response.")
    store = FakeStore(neighbour)

    canned = JudgeResponse(
        suggestions=[
            RawJudgeSuggestion(
                target_chunk_id="TGT-UUID:2",
                type="analogous-mechanism",
                confidence=4,
                why="Both cast error-correction as the driver of updating.",
                anchor=JudgeAnchor(mode="wrap", expect="driving signal"),
                target_is_note=False,
            )
        ]
    )

    captured: dict = {}

    def fake_complete_structured(messages, settings, response_model, *, client=None):
        captured["messages"] = messages
        captured["response_model"] = response_model
        return canned

    monkeypatch.setattr(judge_mod.llm, "complete_structured", fake_complete_structured)

    out = judge_candidates([candidate], _make_note(), store, Settings(), client=None)

    assert len(out) == 1
    sug = out[0]
    assert sug.id == "s01"
    assert sug.type == "analogous-mechanism"
    assert sug.confidence == 4
    assert sug.why.startswith("Both cast error-correction")

    # Target components from the matched target chunk (heading-level target).
    assert sug.target.file == "notes/immune.org"
    assert sug.target.title == "Immune Memory"
    assert sug.target.file_id == "TGT-UUID"
    assert sug.target.heading is not None
    assert sug.target.heading.text == "Affinity maturation"
    assert sug.target.heading.id == "HEAD-ID"
    assert sug.target.heading.level == 2

    # Source chunk display fields.
    assert sug.source_chunk.heading == "Error as signal"
    assert sug.source_chunk.char_start == 0
    assert sug.source_chunk.char_end == len(SOURCE_BUFFER)

    # SourceAnchor resolved against the buffer.
    start = SOURCE_BUFFER.index("driving signal")
    assert sug.source_anchor.char_start == start
    assert sug.source_anchor.char_end == start + len("driving signal")
    assert sug.source_anchor.expect == "driving signal"
    assert sug.source_anchor.template == "{{link}}"

    # target_excerpt is the (possibly truncated) target chunk text.
    assert sug.target_excerpt == candidate.target_chunk.text

    # The response model was JudgeResponse, and the cached prefix was used.
    assert captured["response_model"] is JudgeResponse
    user_msg = captured["messages"][1]
    assert isinstance(user_msg["content"], list)
    assert user_msg["content"][0].get("cache_control") == {"type": "ephemeral"}
    assert SOURCE_BUFFER in user_msg["content"][0]["text"]

    # Neighbour fetch was attempted (prev preferred since chunk_in_heading == 1).
    assert ("TGT-UUID", 3, 0) in store.calls


def test_judge_candidates_target_is_note_yields_no_heading(monkeypatch) -> None:
    candidate = _make_candidate()
    store = FakeStore(None)
    canned = JudgeResponse(
        suggestions=[
            RawJudgeSuggestion(
                target_chunk_id="TGT-UUID:2",
                type="generalizes",
                confidence=3,
                why="Note-wide resonance.",
                anchor=JudgeAnchor(mode="wrap", expect="belief updating"),
                target_is_note=True,
            )
        ]
    )
    monkeypatch.setattr(
        judge_mod.llm, "complete_structured", lambda *a, **k: canned
    )

    out = judge_candidates([candidate], _make_note(), store, Settings())

    assert len(out) == 1
    assert out[0].target.heading is None  # whole-note target


def test_judge_candidates_insert_uses_target_title_description(monkeypatch) -> None:
    candidate = _make_candidate()
    store = FakeStore(None)
    canned = JudgeResponse(
        suggestions=[
            RawJudgeSuggestion(
                target_chunk_id="TGT-UUID:2",
                type="elaborates",
                confidence=5,
                why="Adds an example.",
                anchor=JudgeAnchor(
                    mode="insert",
                    expect="The brain corrects its model against incoming sensation.",
                    insert_text=" Compare {{link}}.",
                ),
                target_is_note=False,
            )
        ]
    )
    monkeypatch.setattr(
        judge_mod.llm, "complete_structured", lambda *a, **k: canned
    )

    out = judge_candidates([candidate], _make_note(), store, Settings())

    assert len(out) == 1
    anchor = out[0].source_anchor
    assert anchor.expect == ""  # insert => empty region
    assert anchor.template == " Compare {{link}}."
    assert anchor.link_description == "Immune Memory"  # defaulted to target title


def test_judge_candidates_empty_response_yields_no_suggestions(monkeypatch) -> None:
    candidate = _make_candidate()
    store = FakeStore(None)
    monkeypatch.setattr(
        judge_mod.llm,
        "complete_structured",
        lambda *a, **k: JudgeResponse(suggestions=[]),
    )

    out = judge_candidates([candidate], _make_note(), store, Settings())

    assert out == []


def test_judge_candidates_drops_unanchorable_suggestion(monkeypatch) -> None:
    candidate = _make_candidate()
    store = FakeStore(None)
    canned = JudgeResponse(
        suggestions=[
            RawJudgeSuggestion(
                target_chunk_id="TGT-UUID:2",
                type="contradicts",
                confidence=4,
                why="Paraphrased anchor not in chunk.",
                anchor=JudgeAnchor(mode="wrap", expect="phrase that is absent"),
                target_is_note=False,
            )
        ]
    )
    monkeypatch.setattr(
        judge_mod.llm, "complete_structured", lambda *a, **k: canned
    )

    out = judge_candidates([candidate], _make_note(), store, Settings())

    assert out == []  # dropped because expect was not found verbatim


def test_judge_candidates_groups_by_source_chunk(monkeypatch) -> None:
    # Two candidates sharing one source chunk + one from a second source chunk
    # => two judge calls (one per source chunk).
    c1 = _make_candidate()
    c2 = _make_candidate()
    # second target on the same source chunk
    c2.target_chunk = make_chunk(
        "TGT2-UUID",
        0,
        text="Another target.",
        heading_text="Other",
        heading_level=1,
        note_path="notes/other.org",
        note_title="Other Note",
    )
    src2 = make_chunk(
        "SRC-UUID",
        9,
        text="Second source chunk text only.",
        char_start=0,
        char_end=30,
        heading_index=2,
    )
    c3 = Candidate(source_chunk=src2, target_chunk=c1.target_chunk, score=0.5)

    store = FakeStore(None)
    calls: list = []

    def fake(messages, settings, response_model, *, client=None):
        calls.append(messages)
        return JudgeResponse(suggestions=[])

    monkeypatch.setattr(judge_mod.llm, "complete_structured", fake)

    judge_candidates([c1, c2, c3], _make_note(), store, Settings())

    # c1 and c2 share source SRC-UUID:0; c3 is SRC-UUID:9 => exactly 2 calls.
    assert len(calls) == 2
