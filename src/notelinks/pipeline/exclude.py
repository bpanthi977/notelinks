"""Already-linked exclusion (design §8/§10) — **heading-level**.

Two pure predicates over a note's existing ``[[id:...]]`` links:

* :func:`chunk_already_linked` — RETRIEVAL side. Drops a candidate target
  *chunk* whose heading the source already links, so the judge never spends a
  call on a passage you've already connected. Heading-level: a link to one
  heading of a note suppresses only that heading — other headings of the same
  note stay eligible. A bare whole-note link suppresses only the note's
  **preamble** (``heading_index == 0``), per the design decision.
* :func:`suggestion_duplicates_link` — OUTPUT side. Drops a finished
  :class:`~notelinks.models.Suggestion` that would assemble to a link the note
  already has (belt-and-suspenders behind the retrieval filter, which is the
  authoritative one).

Link forms (design §4):

* ``[[id:U::*H]]`` → references heading text ``H`` of note ``U``.
* ``[[id:HID]]``   → ``HID`` is a heading's *own* org-id (search string absent).
* ``[[id:U]]``     → ``U`` is a file-level uuid: the whole note.

A bare ``[[id:X]]`` is one of the latter two; ``X`` is a file uuid XOR a heading
id (org-ids are unique), so testing both interpretations is unambiguous — at
most one can match a given chunk/target.
"""

from __future__ import annotations

from notelinks.models import Chunk, OrgLink, Target


def _heading_ref(search_string: str) -> str:
    """The heading text a ``::...`` search string references.

    We emit the ``*Heading`` form, so a leading ``*`` is stripped; the general
    ``::search string`` form (parsed but never emitted) is matched verbatim
    against the heading text.
    """
    s = search_string.strip()
    return s[1:].strip() if s.startswith("*") else s


def chunk_already_linked(chunk: Chunk, links: list[OrgLink]) -> bool:
    """True if the source already links the *heading* this target chunk lives in.

    Heading-level (design §8): linking one heading of a note suppresses only that
    heading; a bare whole-note link suppresses only the note's preamble.
    """
    for link in links:
        ss = link.search_string
        if ss is None:
            # Bare link: a heading by its own org-id, or a whole-note link
            # (which suppresses only the preamble — heading_index 0).
            if chunk.heading_id is not None and link.target_uuid == chunk.heading_id:
                return True
            if link.target_uuid == chunk.note_uuid and chunk.heading_index == 0:
                return True
        elif (
            link.target_uuid == chunk.note_uuid
            and chunk.heading_text is not None
            and chunk.heading_text == _heading_ref(ss)
        ):
            return True
    return False


def suggestion_duplicates_link(target: Target, links: list[OrgLink]) -> bool:
    """True if ``target`` would assemble to a link the note already has.

    Mirrors how the frontend builds a link from (file_id, heading): a note-wide
    target (``heading is None``) duplicates a bare ``[[id:U]]``; a heading target
    duplicates ``[[id:U::*H]]`` (text match) or a bare ``[[id:HID]]`` when the
    heading carries that own id.
    """
    heading = target.heading
    for link in links:
        ss = link.search_string
        if ss is None:
            if heading is None and target.file_id == link.target_uuid:
                return True
            if (
                heading is not None
                and heading.id is not None
                and heading.id == link.target_uuid
            ):
                return True
        elif (
            heading is not None
            and target.file_id == link.target_uuid
            and heading.text == _heading_ref(ss)
        ):
            return True
    return False
