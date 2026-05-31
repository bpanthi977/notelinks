"""Heading-level already-linked exclusion predicates (design §8/§10)."""

from notelinks.models import Chunk, OrgLink, Target, TargetHeading
from notelinks.pipeline.exclude import (
    chunk_already_linked,
    suggestion_duplicates_link,
)


def _link(target_uuid: str, search_string: str | None = None) -> OrgLink:
    return OrgLink(
        target_uuid=target_uuid, search_string=search_string, char_start=0, char_end=0
    )


def _chunk(
    note_uuid: str,
    *,
    heading_text: str | None = None,
    heading_id: str | None = None,
    heading_index: int = 0,
) -> Chunk:
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
        ordinal=0,
        char_start=0,
        char_end=1,
        text="x",
        embed_text="x",
    )


def _target(
    file_id: str,
    *,
    heading_text: str | None = None,
    heading_id: str | None = None,
) -> Target:
    heading = (
        None
        if heading_text is None
        else TargetHeading(text=heading_text, id=heading_id, level=1)
    )
    return Target(file=f"{file_id}.org", title=file_id, file_id=file_id, heading=heading)


# --- chunk_already_linked (retrieval side) ---------------------------------


def test_heading_text_link_matches_only_that_heading() -> None:
    links = [_link("T", "*Alpha")]
    assert chunk_already_linked(_chunk("T", heading_text="Alpha", heading_index=1), links)
    # Sibling heading of the same note is NOT excluded.
    assert not chunk_already_linked(
        _chunk("T", heading_text="Beta", heading_index=2), links
    )
    # Same heading text in a DIFFERENT note is not excluded.
    assert not chunk_already_linked(
        _chunk("U", heading_text="Alpha", heading_index=1), links
    )


def test_heading_link_strips_leading_star() -> None:
    # "*Alpha" and the heading text "Alpha" must match.
    assert chunk_already_linked(
        _chunk("T", heading_text="Alpha", heading_index=1), [_link("T", "*Alpha")]
    )


def test_whole_note_link_excludes_only_preamble() -> None:
    links = [_link("T")]  # bare [[id:T]]
    assert chunk_already_linked(_chunk("T", heading_index=0), links)  # preamble
    assert not chunk_already_linked(
        _chunk("T", heading_text="Body", heading_index=1), links
    )


def test_heading_own_id_link_excludes_that_heading() -> None:
    links = [_link("HID-1")]  # bare [[id:HID-1]] where HID-1 is a heading's own id
    assert chunk_already_linked(
        _chunk("T", heading_text="Body", heading_id="HID-1", heading_index=1), links
    )
    assert not chunk_already_linked(
        _chunk("T", heading_text="Other", heading_index=2), links
    )


def test_no_links_excludes_nothing() -> None:
    assert not chunk_already_linked(_chunk("T", heading_index=0), [])


# --- suggestion_duplicates_link (output side) ------------------------------


def test_note_wide_suggestion_duplicates_bare_note_link() -> None:
    # Judge promoted a (non-excluded) chunk to a whole-note target for a note the
    # source already links bare — that's an exact duplicate and must be dropped.
    assert suggestion_duplicates_link(_target("T"), [_link("T")])


def test_heading_suggestion_not_blocked_by_bare_note_link() -> None:
    # Bare note link must NOT block a heading-specific suggestion to that note.
    assert not suggestion_duplicates_link(
        _target("T", heading_text="Body"), [_link("T")]
    )


def test_heading_suggestion_duplicates_heading_text_link() -> None:
    assert suggestion_duplicates_link(
        _target("T", heading_text="Alpha"), [_link("T", "*Alpha")]
    )
    assert not suggestion_duplicates_link(
        _target("T", heading_text="Beta"), [_link("T", "*Alpha")]
    )


def test_heading_suggestion_duplicates_heading_own_id_link() -> None:
    assert suggestion_duplicates_link(
        _target("T", heading_text="Body", heading_id="HID-1"), [_link("HID-1")]
    )
