"""Unit tests for ``notelinks.org.chunk`` (T10).

Exercises: preamble + nested headings + a property drawer + a ``#+keyword`` +
a ``# comment`` + a list + a long prose paragraph. Asserts stripping, offset
round-trip into ``note.text``, ``heading_index``/``chunk_in_heading``/``ordinal``
correctness, prose-vs-structured overlap behaviour, size bounds, and the
``embed_text`` breadcrumb prefix. Also chunks a real ``notes/*.org`` file.
"""

from pathlib import Path

import tiktoken

from notelinks.config import Settings
from notelinks.org.chunk import chunk_note
from notelinks.org.parse import parse_note

# A long prose paragraph (single \n\n-delimited block, many sentences) that
# will exceed the (shrunk) token budget and force a sentence/word split.
_PROSE = " ".join(
    f"Idea number {i} resonates across distinct notes through shared mechanism."
    for i in range(40)
)

# A self-contained list (structured items) long enough to force a split too.
_LIST = "\n".join(f"- list item {i} which is fully self contained on its own" for i in range(30))

CRAFTED = f""":PROPERTIES:
:ID:       FILE-UUID-0001
:END:
#+title: Crafted Note
#+date: <2026-05-31 Sun>

Preamble paragraph with enough words here to form one real preamble chunk body.
#+attr_html: :width 300px
# this is a comment line that must be stripped from the preamble

* First Heading
:PROPERTIES:
:ID:       HEAD-UUID-1
:END:
:LOGBOOK:
some logbook entry that must vanish
:END:
{_PROSE}

** Child Heading
{_LIST}

* Second Heading
Short tail body.
"""


def _settings() -> Settings:
    # Shrink the budget so the crafted prose / list actually split.
    return Settings(
        chunk_target_tokens=60,
        chunk_max_tokens=90,
        chunk_min_tokens=12,
        chunk_overlap_tokens=16,
    )


def _enc(s: Settings) -> tiktoken.Encoding:
    return tiktoken.get_encoding(s.tokenizer_encoding)


def test_segmentation_and_indices():
    note = parse_note(CRAFTED, "notes/crafted.org")
    chunks = chunk_note(note, _settings())

    # Preamble => heading_index 0; First Heading => 1; Child => 2; Second => 3.
    by_index: dict[int, list] = {}
    for c in chunks:
        by_index.setdefault(c.heading_index, []).append(c)

    assert 0 in by_index  # preamble emitted
    assert by_index[0][0].heading_text is None
    assert by_index[0][0].heading_level is None
    assert by_index[0][0].heading_id is None

    # First Heading (index 1) keeps its own :ID:.
    first = by_index[1]
    assert first[0].heading_text == "First Heading"
    assert first[0].heading_id == "HEAD-UUID-1"
    assert first[0].heading_level == 1

    # Child Heading breadcrumb includes the ancestor.
    child = by_index[2]
    assert child[0].heading_path == "Crafted Note > First Heading > Child Heading"

    # ordinal is a contiguous 0-based global sequence in document order.
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))

    # chunk_in_heading is 0-based and contiguous within each heading.
    for cs in by_index.values():
        assert [c.chunk_in_heading for c in cs] == list(range(len(cs)))


def test_stripping():
    note = parse_note(CRAFTED, "notes/crafted.org")
    chunks = chunk_note(note, _settings())
    for c in chunks:
        assert ":PROPERTIES:" not in c.text
        assert ":END:" not in c.text
        assert ":LOGBOOK:" not in c.text
        assert "logbook entry" not in c.text
        assert "#+attr_html" not in c.text.lower()
        assert "this is a comment" not in c.text
        assert ":ID:" not in c.text


def test_offset_roundtrip():
    note = parse_note(CRAFTED, "notes/crafted.org")
    chunks = chunk_note(note, _settings())
    for c in chunks:
        span = note.text[c.char_start : c.char_end]
        # Each non-empty body line must appear within the original-buffer span
        # (interior stripped lines may sit inside the span — harmless per spec).
        for line in (ln for ln in c.text.split("\n") if ln.strip()):
            assert line in span, (c.ordinal, repr(line), repr(span[:120]))


def test_prose_has_overlap_list_does_not():
    s = _settings()
    note = parse_note(CRAFTED, "notes/crafted.org")
    chunks = chunk_note(note, s)

    prose_chunks = [c for c in chunks if c.heading_index == 1]
    list_chunks = [c for c in chunks if c.heading_index == 2]

    assert len(prose_chunks) >= 2, "prose should have split into multiple chunks"
    assert len(list_chunks) >= 2, "list should have split into multiple chunks"

    # Prose: consecutive chunks share an overlapping tail/head (idea continuity).
    overlapped = False
    for a, b in zip(prose_chunks, prose_chunks[1:], strict=False):
        tail = " ".join(a.text.split()[-4:])
        if tail and tail in b.text:
            overlapped = True
    assert overlapped, "prose splits must carry overlap"

    # List: consecutive chunks must NOT repeat the previous chunk's last item.
    for a, b in zip(list_chunks, list_chunks[1:], strict=False):
        last_item = a.text.strip().splitlines()[-1].strip()
        assert not b.text.lstrip().startswith(last_item), (
            "structured list splits must not carry overlap"
        )


def test_size_bounds():
    s = _settings()
    enc = _enc(s)
    note = parse_note(CRAFTED, "notes/crafted.org")
    chunks = chunk_note(note, s)
    for c in chunks:
        n = len(enc.encode(c.text))
        # Hard max respected. (Overlap may push a prose chunk slightly above the
        # body budget, but never above max + overlap.)
        assert n <= s.chunk_max_tokens + s.chunk_overlap_tokens, (c.ordinal, n)


def test_min_merge_no_orphan_tail():
    s = _settings()
    enc = _enc(s)
    note = parse_note(CRAFTED, "notes/crafted.org")
    chunks = chunk_note(note, s)
    # Sub-min tails merge into the previous chunk of the SAME heading, so within
    # any multi-chunk heading no non-final chunk is below min; and a lone tail
    # never survives as its own sub-min chunk when a predecessor exists.
    by_index: dict[int, list] = {}
    for c in chunks:
        by_index.setdefault(c.heading_index, []).append(c)
    for cs in by_index.values():
        for c in cs[:-1]:  # every non-final chunk must clear the floor
            assert len(enc.encode(c.text)) >= s.chunk_min_tokens, (c.heading_index, c.ordinal)


def test_embed_text_breadcrumb_prefix():
    note = parse_note(CRAFTED, "notes/crafted.org")
    chunks = chunk_note(note, _settings())
    for c in chunks:
        assert c.embed_text.startswith(c.heading_path)
        assert c.embed_text.startswith(c.heading_path + "\n\n")


def test_empty_segment_yields_no_chunk():
    text = """#+title: Empty Sections

* Has Body
Real content lives here so this heading produces a chunk.

* Drawer Only
:PROPERTIES:
:custom: nothing-substantive
:END:

* Tail
Final body.
"""
    note = parse_note(text, "notes/empty.org")
    chunks = chunk_note(note, _settings())
    # "Drawer Only" (index 2) is entirely stripped => no chunk for it.
    indices = {c.heading_index for c in chunks}
    assert 2 not in indices
    assert 1 in indices and 3 in indices


def test_real_note_chunks_without_error():
    path = Path("notes/mamba.org")
    note = parse_note(path.read_text(), "notes/mamba.org")
    chunks = chunk_note(note, Settings())
    assert chunks, "real note should produce at least one chunk"
    for c in chunks:
        assert c.note_uuid == note.id
        assert c.note_path == "notes/mamba.org"
        assert note.text[c.char_start : c.char_end]  # non-empty span
        assert c.embed_text.startswith(c.heading_path)
