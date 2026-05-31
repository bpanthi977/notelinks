"""Tests for the retrieval selection core and query wrapper (design §8)."""

from notelinks.config import Settings
from notelinks.models import Chunk, OrgLink
from notelinks.pipeline.retrieve import retrieve_candidates, select_candidates


def make_chunk(
    note_uuid: str,
    ordinal: int,
    text: str = "body",
    *,
    heading_text: str | None = None,
    heading_id: str | None = None,
    heading_index: int = 0,
) -> Chunk:
    """Minimal synthetic Chunk; chunk_id = f'{note_uuid}:{ordinal}'."""
    return Chunk(
        note_uuid=note_uuid,
        note_path=f"{note_uuid}.org",
        note_title=note_uuid,
        heading_path="",
        heading_text=heading_text,
        heading_id=heading_id,
        heading_level=None if heading_text is None else 1,
        heading_index=heading_index,
        chunk_in_heading=0,
        ordinal=ordinal,
        char_start=0,
        char_end=len(text),
        text=text,
        embed_text=text,
    )


def _link(target_uuid: str, search_string: str | None = None) -> OrgLink:
    return OrgLink(
        target_uuid=target_uuid, search_string=search_string, char_start=0, char_end=0
    )


def test_floor_drops_weak_hits() -> None:
    src = make_chunk("S", 0)
    t_strong = make_chunk("A", 0)
    t_weak = make_chunk("B", 0)
    hits = [(src, [(t_strong, 0.9), (t_weak, 0.1)])]

    out = select_candidates(hits, per_source_n=5, global_cap_m=10, sim_floor=0.2)

    assert [c.target_chunk.chunk_id for c in out] == ["A:0"]


def test_per_source_cap_respected() -> None:
    src = make_chunk("S", 0)
    targets = [(make_chunk("T", i), 0.9 - i * 0.01) for i in range(5)]
    hits = [(src, targets)]

    out = select_candidates(hits, per_source_n=3, global_cap_m=10, sim_floor=0.0)

    assert len(out) == 3
    assert [c.target_chunk.chunk_id for c in out] == ["T:0", "T:1", "T:2"]


def test_round_robin_every_rank1_precedes_any_rank2() -> None:
    s1 = make_chunk("S1", 0)
    s2 = make_chunk("S2", 0)
    # s1 has a strong rank-1 (0.9) and a weak rank-2 (0.3);
    # s2 has a weaker rank-1 (0.5) and a rank-2 (0.4).
    hits = [
        (s1, [(make_chunk("A", 0), 0.9), (make_chunk("B", 0), 0.3)]),
        (s2, [(make_chunk("C", 0), 0.5), (make_chunk("D", 0), 0.4)]),
    ]

    out = select_candidates(hits, per_source_n=3, global_cap_m=10, sim_floor=0.0)
    ids = [c.target_chunk.chunk_id for c in out]

    # Both rank-1s (A=0.9 then C=0.5, score-ordered) come before any rank-2.
    assert ids == ["A:0", "C:0", "D:0", "B:0"]
    assert ids.index("A:0") < ids.index("B:0")
    assert ids.index("C:0") < ids.index("B:0")
    # Rank-2 hits among themselves are score-ordered (D=0.4 before B=0.3).
    assert ids.index("D:0") < ids.index("B:0")


def test_global_cap_truncates() -> None:
    s1 = make_chunk("S1", 0)
    s2 = make_chunk("S2", 0)
    hits = [
        (s1, [(make_chunk("A", i), 0.9 - i * 0.01) for i in range(3)]),
        (s2, [(make_chunk("C", i), 0.8 - i * 0.01) for i in range(3)]),
    ]

    out = select_candidates(hits, per_source_n=3, global_cap_m=2, sim_floor=0.0)

    assert len(out) == 2
    # Both rank-1s win the cap (A:0 @0.9, C:0 @0.8).
    assert [c.target_chunk.chunk_id for c in out] == ["A:0", "C:0"]


class FakeStore:
    """Canned ``query_chunks`` keyed by embedding (a 1-element list = tag)."""

    def __init__(self, responses: dict[float, list[tuple[Chunk, float]]]) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    def query_chunks(
        self,
        embedding: list[float],
        *,
        k: int,
        exclude_note_uuid: str,
    ) -> list[tuple[Chunk, float]]:
        self.calls.append({"k": k, "exclude_note_uuid": exclude_note_uuid})
        return self._responses[embedding[0]]


def test_retrieve_candidates_dedups_target_seen_by_two_sources() -> None:
    s1 = make_chunk("S", 0)
    s2 = make_chunk("S", 1)
    shared = make_chunk("T", 0)  # same target chunk_id "T:0" from both sources
    other = make_chunk("U", 0)

    store = FakeStore(
        {
            1.0: [(shared, 0.6)],  # s1 sees shared @ 0.6
            2.0: [(shared, 0.9), (other, 0.5)],  # s2 sees shared @ 0.9 (better)
        }
    )
    settings = Settings(top_k=8, per_source_n=3, global_cap_m=40, sim_floor=0.2)

    out = retrieve_candidates(
        store,
        [s1, s2],
        [[1.0], [2.0]],
        settings=settings,
        current_note_uuid="S",
        existing_links=[],
    )

    # The shared target appears exactly once, keeping its best pairing (s2 @ 0.9).
    shared_pairs = [c for c in out if c.target_chunk.chunk_id == "T:0"]
    assert len(shared_pairs) == 1
    assert shared_pairs[0].score == 0.9
    assert shared_pairs[0].source_chunk.chunk_id == "S:1"

    # The other target survives too; two distinct targets total.
    assert {c.target_chunk.chunk_id for c in out} == {"T:0", "U:0"}

    # Store contract was exercised with the configured knobs (self-note excluded
    # at the query; already-linked filtering is now heading-level, post-query).
    assert store.calls[0]["k"] == 8
    assert store.calls[0]["exclude_note_uuid"] == "S"


def test_retrieve_filters_already_linked_heading_only() -> None:
    """Heading-level exclusion: a heading link drops only that heading's chunk."""
    src = make_chunk("S", 0)
    # Note T, two headings: "Alpha" (already linked) and "Beta" (not linked).
    t_alpha = make_chunk("T", 0, heading_text="Alpha", heading_index=1)
    t_beta = make_chunk("T", 1, heading_text="Beta", heading_index=2)

    store = FakeStore({1.0: [(t_alpha, 0.9), (t_beta, 0.8)]})
    settings = Settings(top_k=8, per_source_n=3, global_cap_m=40, sim_floor=0.2)

    out = retrieve_candidates(
        store,
        [src],
        [[1.0]],
        settings=settings,
        current_note_uuid="S",
        existing_links=[_link("T", "*Alpha")],  # links heading Alpha of T
    )

    # Alpha is suppressed; the sibling heading Beta still surfaces.
    assert {c.target_chunk.chunk_id for c in out} == {"T:1"}


def test_retrieve_whole_note_link_excludes_only_preamble() -> None:
    """A bare ``[[id:T]]`` suppresses only T's preamble (heading_index 0)."""
    src = make_chunk("S", 0)
    t_pre = make_chunk("T", 0, heading_index=0)  # preamble
    t_head = make_chunk("T", 1, heading_text="Body", heading_index=1)

    store = FakeStore({1.0: [(t_pre, 0.9), (t_head, 0.8)]})
    settings = Settings(top_k=8, per_source_n=3, global_cap_m=40, sim_floor=0.2)

    out = retrieve_candidates(
        store,
        [src],
        [[1.0]],
        settings=settings,
        current_note_uuid="S",
        existing_links=[_link("T")],  # bare whole-note link
    )

    # Preamble dropped; the headed section stays eligible.
    assert {c.target_chunk.chunk_id for c in out} == {"T:1"}


def test_retrieve_heading_own_id_link_excludes_that_heading() -> None:
    """A bare ``[[id:HID]]`` (heading's own id) drops the chunk with that id."""
    src = make_chunk("S", 0)
    t_head = make_chunk("T", 0, heading_text="Body", heading_id="HID-1", heading_index=1)
    t_other = make_chunk("T", 1, heading_text="Other", heading_index=2)

    store = FakeStore({1.0: [(t_head, 0.9), (t_other, 0.8)]})
    settings = Settings(top_k=8, per_source_n=3, global_cap_m=40, sim_floor=0.2)

    out = retrieve_candidates(
        store,
        [src],
        [[1.0]],
        settings=settings,
        current_note_uuid="S",
        existing_links=[_link("HID-1")],  # links the heading by its own org-id
    )

    assert {c.target_chunk.chunk_id for c in out} == {"T:1"}
