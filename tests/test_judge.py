"""Tests for the judge: pure anchor resolution + orchestration (design §9).

No network: ``llm.complete_structured`` is monkeypatched to return a canned
``JudgeResponse``, and a fake store returns a canned same-heading neighbour.
"""

from __future__ import annotations

import threading
import time

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


def test_resolve_anchor_insert_inline_brace_anchor() -> None:
    # The judge marks the link anchor inline with {{...}}; the engine rewrites
    # that span to the wire {{link}} token and uses the words as the description.
    buffer = "The system minimizes surprise. It does so continuously."
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    sentence = "The system minimizes surprise."
    anchor = JudgeAnchor(
        mode="insert",
        expect=sentence,
        insert_text=" This connects to {{free energy}} more broadly.",
    )

    out = resolve_anchor(anchor, src, buffer)

    assert out is not None
    end = buffer.index(sentence) + len(sentence)
    # Empty region at the END of the matched expect span.
    assert out.char_start == end
    assert out.char_end == end
    assert out.expect == ""
    # Braced phrase -> link_description; the marker becomes the wire {{link}}.
    assert out.template == " This connects to {{link}} more broadly."
    assert out.link_description == "free energy"
    assert out.before == buffer[max(0, end - 40) : end]
    assert out.after == buffer[end : end + 40]


def test_resolve_anchor_insert_literal_link_token_back_compat() -> None:
    # Back-compat: insert_text with a literal {{link}} and no other marker keeps
    # working — template unchanged, description falls back to the expect sentence.
    buffer = "The system minimizes surprise. It does so continuously."
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    sentence = "The system minimizes surprise."
    anchor = JudgeAnchor(mode="insert", expect=sentence, insert_text=" See also {{link}}.")

    out = resolve_anchor(anchor, src, buffer)

    assert out is not None
    assert out.template == " See also {{link}}."
    assert out.link_description == sentence


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


def test_resolve_anchor_insert_tolerates_org_link_and_wrapped_newline() -> None:
    # The reported case: the buffer has an org link and a line-wrap newline, but
    # the judge renders the link to its visible text and joins the wrapped lines.
    buffer = (
        "Shannon [[id:40628C21-A838-45DA-836C-2FA6E9F3B4E6][entropy]] assigns "
        "amount of uncertainity\nto an probability distribution.\n\nNext para."
    )
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    anchor = JudgeAnchor(
        mode="insert",
        expect="Shannon entropy assigns amount of uncertainity to an probability distribution.",
        insert_text="The formula has a deeper {{connection}} in statistical mechanics.",
    )

    out = resolve_anchor(anchor, src, buffer)

    assert out is not None
    # Empty region right after the matched span's raw end ("distribution.").
    end = buffer.index("distribution.") + len("distribution.")
    assert out.char_start == end
    assert out.char_end == end
    assert out.expect == ""
    assert out.template == "The formula has a deeper {{link}} in statistical mechanics."
    assert out.link_description == "connection"
    # before/after are raw buffer slices, so the elisp client can re-anchor them.
    assert out.before == buffer[max(0, end - 40) : end]
    assert out.after == buffer[end : end + 40]


def test_resolve_anchor_fuzzy_tolerates_reworded_expect() -> None:
    # Judge "corrects" the note's typos when reproducing the sentence
    # ("uncertainity" -> "uncertainty", "an probability" -> "a probability") and
    # renders the link. Fuzzy matching still anchors it.
    buffer = (
        "Shannon [[id:X][entropy]] assigns amount of uncertainity\n"
        "to an probability distribution.\n\nNext."
    )
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    anchor = JudgeAnchor(
        mode="insert",
        expect="Shannon entropy assigns amount of uncertainty to a probability distribution.",
        insert_text="See {{link}}.",
    )

    out = resolve_anchor(anchor, src, buffer)

    assert out is not None
    end = buffer.index("distribution.") + len("distribution.")
    assert out.char_start == end  # insert lands right after the real sentence end
    assert out.before == buffer[max(0, end - 40) : end]


def test_resolve_anchor_fuzzy_rejects_absent_expect() -> None:
    # A sentence with no real counterpart in the note must NOT be force-matched
    # (precision-first: dropping beats anchoring in the wrong place).
    buffer = (
        "Shannon entropy assigns amount of uncertainty to a probability distribution."
    )
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    anchor = JudgeAnchor(
        mode="wrap", expect="quantum chromodynamics confines colour charge tightly"
    )

    assert resolve_anchor(anchor, src, buffer) is None


def test_resolve_anchor_wrap_through_link_uses_raw_span_text() -> None:
    # A wrap whose rendered expect crosses a link resolves to the RAW span text
    # (with markup), so the wire `expect` matches the buffer verbatim.
    buffer = "see [[id:X][entropy]] here"
    src = make_chunk("S", 0, text=buffer, char_start=0, char_end=len(buffer))
    anchor = JudgeAnchor(mode="wrap", expect="entropy here")

    out = resolve_anchor(anchor, src, buffer)

    assert out is not None
    raw = "[[id:X][entropy]] here"
    assert out.char_start == buffer.index(raw)
    assert out.char_end == buffer.index(raw) + len(raw)
    assert out.expect == raw  # raw span, not the judge's rendered "entropy here"
    assert buffer[out.char_start : out.char_end] == out.expect


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
                candidate_id=1,
                type="analogous-mechanism",
                confidence=3,
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
    assert sug.confidence == 3
    assert sug.why.startswith("Both cast error-correction")

    # Target components from the matched target chunk (heading-level target).
    assert sug.target.file == "notes/immune.org"
    assert sug.target.title == "Immune Memory"
    assert sug.target.file_id == "TGT-UUID"
    assert sug.target.heading is not None
    assert sug.target.heading.text == "Affinity maturation"
    assert sug.target.heading.id == "HEAD-ID"
    assert sug.target.heading.level == 2

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
                candidate_id=1,
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


def test_judge_candidates_insert_uses_inline_brace_description(monkeypatch) -> None:
    # Insert description comes from the judge's inline {{...}} marker — NOT the
    # target title (the old override was removed for naturally-flowing prose).
    candidate = _make_candidate()
    store = FakeStore(None)
    canned = JudgeResponse(
        suggestions=[
            RawJudgeSuggestion(
                candidate_id=1,
                type="elaborates",
                confidence=3,
                why="Adds an example.",
                anchor=JudgeAnchor(
                    mode="insert",
                    expect="The brain corrects its model against incoming sensation.",
                    insert_text=" This {{mirrors}} immune adaptation.",
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
    assert anchor.template == " This {{link}} immune adaptation."
    assert anchor.link_description == "mirrors"  # the inline-marked phrase


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
                candidate_id=1,
                type="contradicts",
                confidence=2,
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


def test_judge_candidates_groups_by_target_file(monkeypatch) -> None:
    # Two candidates hitting the SAME target note (different chunks of it) + one
    # candidate hitting a SECOND target note => exactly two judge calls (one per
    # target file), regardless of which source chunk each came from.
    c1 = _make_candidate()  # target note TGT-UUID
    # A second chunk of the SAME target note TGT-UUID (different ordinal).
    c2 = _make_candidate()
    c2.target_chunk = make_chunk(
        "TGT-UUID",
        7,
        text="A second passage from the same target note.",
        heading_text="Clonal selection",
        heading_level=2,
        heading_index=5,
        note_path="notes/immune.org",
        note_title="Immune Memory",
    )
    # A candidate from a different source chunk hitting a SECOND target note.
    src2 = make_chunk(
        "SRC-UUID",
        9,
        text="Second source chunk text only.",
        char_start=0,
        char_end=30,
        heading_index=2,
    )
    other_target = make_chunk(
        "TGT2-UUID",
        0,
        text="Another target.",
        heading_text="Other",
        heading_level=1,
        note_path="notes/other.org",
        note_title="Other Note",
    )
    c3 = Candidate(source_chunk=src2, target_chunk=other_target, score=0.5)

    store = FakeStore(None)
    calls: list = []

    def fake(messages, settings, response_model, *, client=None):
        calls.append(messages)
        return JudgeResponse(suggestions=[])

    monkeypatch.setattr(judge_mod.llm, "complete_structured", fake)

    judge_candidates([c1, c2, c3], _make_note(), store, Settings())

    # c1 and c2 share target TGT-UUID; c3 is TGT2-UUID => exactly 2 calls.
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Piece 3: parallelization (thread pool) — determinism, isolation, safety.
# ---------------------------------------------------------------------------

# A multi-source note whose three source chunks each contain a distinct,
# verbatim-findable phrase. Offsets are computed from this text so each source
# chunk's char span exactly covers its sentence.
MULTI_BUFFER = (
    "Alpha drives the alpha signal forward. "
    "Beta tunes the beta loop slowly. "
    "Gamma resolves the gamma tension fully."
)
_PHRASES = ["alpha signal", "beta loop", "gamma tension"]


def _multi_note() -> Note:
    return Note(id="SRC-UUID", path="notes/cur.org", title="Current", text=MULTI_BUFFER)


def _multi_candidates() -> list[Candidate]:
    """One candidate per distinct target file (3 groups), each anchorable.

    Each candidate also has its own source chunk whose phrase is verbatim-findable
    in the current note, so the whole-buffer anchor resolves for every group.
    """
    sentences = [
        "Alpha drives the alpha signal forward.",
        "Beta tunes the beta loop slowly.",
        "Gamma resolves the gamma tension fully.",
    ]
    candidates: list[Candidate] = []
    for i, sentence in enumerate(sentences):
        start = MULTI_BUFFER.index(sentence)
        source = make_chunk(
            "SRC-UUID",
            i,
            text=sentence,
            char_start=start,
            char_end=start + len(sentence),
            heading_text=f"H{i}",
            heading_index=i,
            note_path="notes/cur.org",
            note_title="Current",
        )
        target = make_chunk(
            f"TGT{i}-UUID",
            100 + i,
            text=f"Target body for group {i}.",
            char_start=0,
            char_end=20,
            heading_text=f"TgtH{i}",
            heading_id=f"TGTHEAD{i}",
            heading_level=2,
            heading_index=10 + i,
            chunk_in_heading=0,
            note_path=f"notes/t{i}.org",
            note_title=f"Target {i}",
        )
        candidates.append(Candidate(source_chunk=source, target_chunk=target, score=0.5))
    return candidates


def _canned_for(tid: str, candidates: list[Candidate]) -> JudgeResponse:
    """A per-target-file canned response wrapping that group's distinct phrase."""
    # tid is a target note uuid; map back to the matching candidate/phrase.
    cand = next(c for c in candidates if c.target_chunk.note_uuid == tid)
    idx = candidates.index(cand)
    return JudgeResponse(
        suggestions=[
            RawJudgeSuggestion(
                candidate_id=1,
                type="elaborates",
                confidence=3,
                why=f"resonance for group {idx}",
                anchor=JudgeAnchor(mode="wrap", expect=_PHRASES[idx]),
                target_is_note=False,
            )
        ]
    )


def _sequential_expectation(
    candidates: list[Candidate], store, monkeypatch
) -> list:
    """Run the judge with a plain (no-sleep) per-target mock = the reference."""

    def plain(messages, settings, response_model, *, client=None):
        tid = _tid_from_messages(messages, candidates)
        return _canned_for(tid, candidates)

    monkeypatch.setattr(judge_mod.llm, "complete_structured", plain)
    return judge_candidates(candidates, _multi_note(), store, Settings(), max_workers=4)


def _tid_from_messages(messages, candidates) -> str:
    """Recover which target file a built message set belongs to.

    The per-call suffix embeds the target passage text; match it back to a target
    note uuid so the mock routes the correct canned response to the correct group.
    """
    suffix = messages[1]["content"][1]["text"]
    for c in candidates:
        if c.target_chunk.text in suffix:
            return c.target_chunk.note_uuid
    raise AssertionError("could not route message to a target file")


def test_judge_parallel_stable_order_despite_out_of_order_completion(
    monkeypatch,
) -> None:
    candidates = _multi_candidates()
    store = FakeStore(None)

    # Reference: sequential expectation (stable group order).
    expected = _sequential_expectation(candidates, FakeStore(None), monkeypatch)
    assert [s.id for s in expected] == ["s01", "s02", "s03"]

    # Now a mock that makes the LATER groups finish FIRST: group 0 sleeps longest.
    n_groups = len(candidates)

    def out_of_order(messages, settings, response_model, *, client=None):
        tid = _tid_from_messages(messages, candidates)
        idx = next(
            i
            for i, c in enumerate(candidates)
            if c.target_chunk.note_uuid == tid
        )
        # Earlier groups sleep longer => completion order is reversed.
        time.sleep(0.02 * (n_groups - idx))
        return _canned_for(tid, candidates)

    monkeypatch.setattr(judge_mod.llm, "complete_structured", out_of_order)

    out = judge_candidates(
        candidates, _multi_note(), store, Settings(), max_workers=4
    )

    # Same suggestions, same order, same stable ids as the sequential reference.
    assert [s.id for s in out] == [s.id for s in expected]
    assert [s.why for s in out] == [s.why for s in expected]
    assert [s.target.title for s in out] == [s.target.title for s in expected]
    assert [s.source_anchor.expect for s in out] == [
        s.source_anchor.expect for s in expected
    ]
    # Concretely: ids are s01..s03 in first-seen group order.
    assert [s.id for s in out] == ["s01", "s02", "s03"]
    assert [s.target.title for s in out] == ["Target 0", "Target 1", "Target 2"]


def test_judge_parallel_calls_once_per_group(monkeypatch) -> None:
    candidates = _multi_candidates()
    store = FakeStore(None)
    count = {"n": 0}
    lock = threading.Lock()

    def fake(messages, settings, response_model, *, client=None):
        with lock:
            count["n"] += 1
        tid = _tid_from_messages(messages, candidates)
        return _canned_for(tid, candidates)

    monkeypatch.setattr(judge_mod.llm, "complete_structured", fake)
    judge_candidates(candidates, _multi_note(), store, Settings(), max_workers=4)

    # One call per distinct target file (3 groups).
    assert count["n"] == 3


def test_judge_parallel_failure_isolation(monkeypatch) -> None:
    candidates = _multi_candidates()
    store = FakeStore(None)
    failing_tid = candidates[1].target_chunk.note_uuid  # middle group fails

    def fake(messages, settings, response_model, *, client=None):
        tid = _tid_from_messages(messages, candidates)
        if tid == failing_tid:
            raise RuntimeError("boom in group 1")
        return _canned_for(tid, candidates)

    monkeypatch.setattr(judge_mod.llm, "complete_structured", fake)

    out = judge_candidates(
        candidates, _multi_note(), store, Settings(), max_workers=4
    )

    # The failing group contributes nothing; the other two still produce
    # suggestions and the run does not crash. ids stay stable/contiguous.
    assert [s.id for s in out] == ["s01", "s02"]
    assert [s.target.title for s in out] == ["Target 0", "Target 2"]
    assert [s.why for s in out] == ["resonance for group 0", "resonance for group 2"]


def test_judge_parallel_thread_safe_per_group_routing(monkeypatch) -> None:
    candidates = _multi_candidates()
    store = FakeStore(None)
    # Record (thread, sid, source_text) seen by each invocation to assert that
    # inputs are not crossed between concurrent calls.
    seen: list[tuple[str, str]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(candidates))

    def fake(messages, settings, response_model, *, client=None):
        tid = _tid_from_messages(messages, candidates)
        suffix = messages[1]["content"][1]["text"]
        cand = next(c for c in candidates if c.target_chunk.note_uuid == tid)
        # Force genuine concurrency: every call waits until all are in-flight.
        barrier.wait(timeout=5)
        with lock:
            seen.append((tid, suffix))
        # The suffix carried into this call must be this group's own target text.
        assert cand.target_chunk.text in suffix
        return _canned_for(tid, candidates)

    monkeypatch.setattr(judge_mod.llm, "complete_structured", fake)

    out = judge_candidates(
        candidates, _multi_note(), store, Settings(), max_workers=len(candidates)
    )

    # Every group routed exactly once, with its own input (no data races).
    assert sorted(tid for tid, _ in seen) == sorted(
        c.target_chunk.note_uuid for c in candidates
    )
    assert [s.id for s in out] == ["s01", "s02", "s03"]
