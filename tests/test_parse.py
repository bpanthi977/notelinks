"""Unit tests for ``notelinks.org.parse`` (T7)."""

from pathlib import Path

from notelinks.org.parse import parse_note

# A crafted note exercising: top drawer (ID + ROAM_ALIASES), title, nested
# headings, a heading with its own :ID: drawer, a TODO heading, a tagged
# heading, and several link forms.
CRAFTED = """:PROPERTIES:
:ID:       FILE-UUID-0001
:ROAM_ALIASES: "Large Language Model" RLHF
:END:
#+title: Crafted Note
#+date: <2026-05-31 Sun>

Preamble text linking to [[id:AAAA][a note]] and a bare [[id:BBBB]].

* First Heading
Body of first with a heading link [[id:CCCC::*Some Heading][see here]].

** Child Heading
:PROPERTIES:
:ID:       CHILD-UUID-9999
:END:
Child body referencing [[id:DDDD::search only]].

* DONE Second Heading                                              :tagone:tagtwo:
Tail body at EOF.
"""


def test_crafted_file_drawer_and_title():
    note = parse_note(CRAFTED, "notes/crafted.org")
    assert note.id == "FILE-UUID-0001"
    assert note.title == "Crafted Note"
    assert note.aliases == ["Large Language Model", "RLHF"]
    assert note.path == "notes/crafted.org"
    assert note.text == CRAFTED


def test_crafted_headings_levels_and_indices():
    note = parse_note(CRAFTED, "notes/crafted.org")
    texts = [(h.text, h.level, h.index) for h in note.headings]
    assert texts == [
        ("First Heading", 1, 1),
        ("Child Heading", 2, 2),
        ("Second Heading", 1, 3),  # TODO keyword + trailing tags stripped
    ]


def test_crafted_ancestor_paths():
    note = parse_note(CRAFTED, "notes/crafted.org")
    by_text = {h.text: h for h in note.headings}
    assert by_text["First Heading"].ancestor_path == []
    assert by_text["Child Heading"].ancestor_path == ["First Heading"]
    assert by_text["Second Heading"].ancestor_path == []


def test_crafted_heading_own_id():
    note = parse_note(CRAFTED, "notes/crafted.org")
    by_text = {h.text: h for h in note.headings}
    assert by_text["Child Heading"].id == "CHILD-UUID-9999"
    assert by_text["First Heading"].id is None
    assert by_text["Second Heading"].id is None


def test_crafted_char_offsets_land_on_substrings():
    note = parse_note(CRAFTED, "notes/crafted.org")
    by_text = {h.text: h for h in note.headings}

    first = by_text["First Heading"]
    # char_start points at the first '*' of the heading line.
    assert CRAFTED[first.char_start] == "*"
    assert CRAFTED[first.char_start:].startswith("* First Heading")
    # Directly-owned span ends at the next heading (the child).
    child = by_text["Child Heading"]
    assert first.char_end == child.char_start
    assert CRAFTED[child.char_start:].startswith("** Child Heading")

    # Last heading owns through EOF.
    second = by_text["Second Heading"]
    assert second.char_end == len(CRAFTED)


def test_crafted_links_all_forms():
    note = parse_note(CRAFTED, "notes/crafted.org")
    # Verify each link's span reproduces the exact bracketed substring.
    for link in note.links:
        assert CRAFTED[link.char_start:link.char_end].startswith("[[id:")
        assert CRAFTED[link.char_end - 2:link.char_end] == "]]"

    targets = [(lk.target_uuid, lk.search_string) for lk in note.links]
    assert targets == [
        ("AAAA", None),                # [[id:AAAA][a note]]
        ("BBBB", None),                # [[id:BBBB]]
        ("CCCC", "*Some Heading"),     # [[id:CCCC::*Some Heading][see here]]
        ("DDDD", "search only"),       # [[id:DDDD::search only]]
    ]


def test_title_fallback_from_filename():
    note = parse_note("no title keyword here\n", "notes/my_cool_note-v2.org")
    assert note.title == "my cool note v2"


def test_missing_file_id_is_empty_string():
    note = parse_note("#+title: No Drawer\n\nbody\n", "notes/x.org")
    assert note.id == ""
    assert note.aliases == []


def test_real_note_parses_without_error():
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "notes" / "entropy.org"
    text = path.read_text(encoding="utf-8")
    note = parse_note(text, "notes/entropy.org")

    assert note.id == "40628C21-A838-45DA-836C-2FA6E9F3B4E6"
    assert note.title == "Entropy"
    assert len(note.headings) > 0
    # Differential Entropy heading carries its own :ID:.
    diff = next(h for h in note.headings if h.text == "Differential Entropy")
    assert diff.id == "6398DC98-3FD0-45B5-B2CA-E0D8E81F5583"
    # Indices are 1-based and contiguous in document order.
    assert [h.index for h in note.headings] == list(range(1, len(note.headings) + 1))
    # Existing links are extracted; every span is a valid bracketed link.
    assert len(note.links) > 0
    for link in note.links:
        assert text[link.char_start:link.char_end].startswith("[[id:")
