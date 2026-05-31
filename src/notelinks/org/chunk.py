"""Heading-segment + recursive-split chunking (design §5, §6).

``chunk_note(note, settings) -> list[Chunk]`` turns a parsed :class:`Note` into
a list of size-bounded :class:`Chunk`s ready for embedding and indexing.

Pipeline per note:

1. **Segment by heading** (design §5.2). The pre-first-heading *preamble* is
   ``heading_index=0``; each :class:`OrgHeading` owns the body from just past
   its heading line to ``heading.char_end`` (the next heading of any level).
2. **Strip** drawers (``:PROPERTIES:``/``:LOGBOOK:``/…``:END:``),
   ``#+keyword:`` lines, and ``# comments`` from each segment body, KEEPING
   code blocks, tables, lists, and paragraphs (design §5.1). Stripping builds
   an **offset map** from cleaned-text positions back to original-buffer
   offsets, so emitted ``char_start``/``char_end`` are offsets into
   ``note.text`` (design §11 anchor convention).
3. **Recursively split** each cleaned body into size-bounded chunks using a
   separator hierarchy paragraph > line > sentence > word, with prose-only
   overlap (design §5.3).

All char offsets are 0-based codepoint indices into ``note.text``.
"""

import re

import tiktoken

from notelinks.config import Settings
from notelinks.models import Chunk, Note, OrgHeading

# A ``#+keyword:`` / ``#+KEYWORD:`` line (org "in-buffer setting"). Matches the
# whole line incl. ``#+begin_..``/``#+end_..`` — but those are handled by the
# code-block guard below, so plain keyword lines (#+title:, #+ATTR_HTML:, …)
# are stripped while block delimiters are kept.
_KEYWORD_RE = re.compile(r"^[ \t]*#\+\S")

# A ``# comment`` line (a ``#`` followed by space/EOL, not ``#+``).
_COMMENT_RE = re.compile(r"^[ \t]*#(?:[ \t].*)?$")

# A drawer opener ``:NAME:`` on its own line (e.g. ``:PROPERTIES:``,
# ``:LOGBOOK:``) and the ``:END:`` terminator.
_DRAWER_OPEN_RE = re.compile(r"^[ \t]*:([A-Za-z0-9_-]+):[ \t]*$")
_DRAWER_END_RE = re.compile(r"^[ \t]*:END:[ \t]*$", re.IGNORECASE)

# Org block delimiters ``#+begin_src`` … ``#+end_src`` (any block type). Kept
# verbatim; we must NOT strip ``#+keyword`` lines inside a block.
_BLOCK_BEGIN_RE = re.compile(r"^[ \t]*#\+begin_", re.IGNORECASE)
_BLOCK_END_RE = re.compile(r"^[ \t]*#\+end_", re.IGNORECASE)

# A structured-item line: list item (``-``/``+``/``*``/``N.``/``N)``) or table
# row (``|``). Splits AT such boundaries carry NO overlap (design §5.3).
_STRUCTURED_RE = re.compile(r"^[ \t]*(?:[-+*]\s|\d+[.)]\s|\|)")

# Sentence boundary: end punctuation followed by whitespace. Used as the
# third-tier separator below paragraph and line.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _clean_segment(body: str, base: int) -> tuple[str, list[int]]:
    """Strip drawers / keywords / comments from a segment body.

    ``body`` is the raw segment text; ``base`` is the offset of ``body[0]`` in
    ``note.text``. Returns ``(cleaned, offmap)`` where ``cleaned`` is the body
    with stripped lines removed and ``offmap`` maps each cleaned-text codepoint
    index ``j`` to its original ``note.text`` offset (``offmap[j]``). ``offmap``
    has ``len(cleaned) + 1`` entries; the final entry is the original offset
    just past the last kept character (so a half-open cleaned span
    ``[a, b)`` maps to original ``[offmap[a], offmap[b])``).

    Code blocks (``#+begin_..``…``#+end_..``) are kept verbatim, including any
    ``#+keyword``-looking lines inside them.
    """
    out: list[str] = []
    offmap: list[int] = []
    pos = base  # running original offset at the start of the current line
    in_drawer = False
    in_block = False

    # splitlines(keepends=True) preserves the trailing newline on each line so
    # offsets advance correctly; the final line may have no newline.
    for line in body.splitlines(keepends=True):
        length = len(line)
        # Content of the line without its trailing newline, for matching.
        stripped_nl = line.rstrip("\n")

        if in_block:
            keep = True
            if _BLOCK_END_RE.match(stripped_nl):
                in_block = False
        elif in_drawer:
            keep = False
            if _DRAWER_END_RE.match(stripped_nl):
                in_drawer = False
        elif _BLOCK_BEGIN_RE.match(stripped_nl):
            in_block = True
            keep = True
        elif _DRAWER_OPEN_RE.match(stripped_nl) and not _DRAWER_END_RE.match(stripped_nl):
            # A bare ``:NAME:`` opener that isn't ``:END:``. Begin drawer.
            in_drawer = True
            keep = False
        elif _KEYWORD_RE.match(stripped_nl):
            keep = False
        elif _COMMENT_RE.match(stripped_nl):
            keep = False
        else:
            keep = True

        if keep:
            for k in range(length):
                offmap.append(pos + k)
            out.append(line)
        pos += length

    offmap.append(pos)
    return "".join(out), offmap


def _ntokens(enc: "tiktoken.Encoding", text: str) -> int:
    return len(enc.encode(text))


def _split_on(text: str, sep: str) -> list[tuple[int, int]] | None:
    """Split ``text`` into ``[start, end)`` spans (relative to ``text``).

    ``sep`` is one of ``"\\n\\n"`` (paragraph), ``"\\n"`` (line),
    ``"sentence"``, ``" "`` (word). Returns the list of spans covering the
    whole string (separators absorbed into the preceding span), or ``None`` if
    the separator does not actually divide the text into 2+ pieces.
    """
    spans: list[tuple[int, int]] = []
    if sep == "sentence":
        last = 0
        for m in _SENTENCE_RE.finditer(text):
            spans.append((last, m.end()))
            last = m.end()
        if last < len(text):
            spans.append((last, len(text)))
    else:
        # Find separator occurrences; absorb the separator into the left span.
        last = 0
        idx = text.find(sep, last)
        while idx != -1:
            end = idx + len(sep)
            spans.append((last, end))
            last = end
            idx = text.find(sep, last)
        if last < len(text):
            spans.append((last, len(text)))
    # Drop empty leading/trailing artifacts but keep span coverage contiguous.
    spans = [(a, b) for (a, b) in spans if b > a]
    if len(spans) < 2:
        return None
    return spans


# Separator hierarchy, finest-last. ``overlap_ok`` marks tiers where a split is
# "mid-prose" and may carry overlap (sentence / word); paragraph and line
# boundaries decide overlap dynamically (a list-item / table-row start => no
# overlap).
_SEPARATORS = ["\n\n", "\n", "sentence", " "]


def _recursive_pieces(
    text: str,
    start: int,
    end: int,
    enc: "tiktoken.Encoding",
    max_tokens: int,
    sep_level: int,
) -> list[tuple[int, int, str]]:
    """Recursively split ``text[start:end)`` until each piece <= ``max_tokens``.

    Returns a list of ``(piece_start, piece_end, boundary_kind)`` where
    ``boundary_kind`` describes the separator the piece's LEFT edge falls on
    relative to its predecessor: ``"para"``, ``"line"``, ``"sentence"``,
    ``"word"``, or ``"hard"`` (forced token cut with no natural separator).
    The first piece's kind is ``"start"``.
    """
    sub = text[start:end]
    if _ntokens(enc, sub) <= max_tokens or sep_level >= len(_SEPARATORS):
        # At/under budget, or out of separators: emit as a single piece.
        # If still over budget at the finest level, fall back to a hard cut.
        if _ntokens(enc, sub) <= max_tokens:
            return [(start, end, "start")]
        return _hard_cut(text, start, end, enc, max_tokens)

    sep = _SEPARATORS[sep_level]
    spans = _split_on(sub, sep)
    if spans is None:
        return _recursive_pieces(text, start, end, enc, max_tokens, sep_level + 1)

    kind = {"\n\n": "para", "\n": "line", "sentence": "sentence", " ": "word"}[sep]
    pieces: list[tuple[int, int, str]] = []
    for i, (a, b) in enumerate(spans):
        abs_a, abs_b = start + a, start + b
        boundary = "start" if i == 0 else kind
        if _ntokens(enc, text[abs_a:abs_b]) <= max_tokens:
            pieces.append((abs_a, abs_b, boundary))
        else:
            deeper = _recursive_pieces(text, abs_a, abs_b, enc, max_tokens, sep_level + 1)
            # Preserve this piece's own boundary kind for its first sub-piece.
            if deeper:
                first = deeper[0]
                deeper[0] = (first[0], first[1], boundary)
            pieces.extend(deeper)
    return pieces


def _hard_cut(
    text: str,
    start: int,
    end: int,
    enc: "tiktoken.Encoding",
    max_tokens: int,
) -> list[tuple[int, int, str]]:
    """Last-resort token-budget cut for an unsplittable run (no separators)."""
    tokens = enc.encode(text[start:end])
    pieces: list[tuple[int, int, str]] = []
    cur = start
    i = 0
    first = True
    while i < len(tokens):
        chunk_toks = tokens[i : i + max_tokens]
        piece_text = enc.decode(chunk_toks)
        nxt = cur + len(piece_text)
        pieces.append((cur, min(nxt, end), "start" if first else "hard"))
        cur = nxt
        first = False
        i += max_tokens
    if not pieces:
        pieces.append((start, end, "start"))
    return pieces


def _pack_pieces(
    pieces: list[tuple[int, int, str]],
    cleaned: str,
    enc: "tiktoken.Encoding",
    settings: Settings,
) -> list[tuple[int, int, str]]:
    """Greedily pack atomic pieces into target-sized chunks.

    Accumulate consecutive pieces while the running token count stays at/under
    ``chunk_target_tokens`` (and never exceeds ``chunk_max_tokens``). Returns a
    list of ``(chunk_start, chunk_end, lead_boundary)`` cleaned-text spans,
    where ``lead_boundary`` is the boundary kind at this chunk's LEFT edge (the
    kind of its first piece relative to the previous chunk) — used to decide
    overlap.
    """
    target = settings.chunk_target_tokens
    hard = settings.chunk_max_tokens
    chunks: list[tuple[int, int, str]] = []
    cur_start: int | None = None
    cur_end = 0
    lead = "start"

    for p_start, p_end, kind in pieces:
        if cur_start is None:
            cur_start, cur_end, lead = p_start, p_end, kind
            continue
        cand_tokens = _ntokens(enc, cleaned[cur_start:p_end])
        if cand_tokens <= target or (
            cand_tokens <= hard and _ntokens(enc, cleaned[cur_start:cur_end]) < target
        ):
            cur_end = p_end
        else:
            chunks.append((cur_start, cur_end, lead))
            cur_start, cur_end, lead = p_start, p_end, kind

    if cur_start is not None:
        chunks.append((cur_start, cur_end, lead))
    return chunks


def _merge_small_tails(
    chunks: list[tuple[int, int, str]],
    cleaned: str,
    enc: "tiktoken.Encoding",
    min_tokens: int,
) -> list[tuple[int, int, str]]:
    """Merge any sub-min chunk into the previous chunk of the same segment.

    A chunk under ``min_tokens`` is folded into its predecessor (extending that
    chunk's end). A leading sub-min chunk with no predecessor is folded into the
    NEXT chunk instead. The merged chunk keeps the predecessor's lead boundary
    (so a tail merged back into prose stays prose).
    """
    if not chunks:
        return chunks
    out: list[tuple[int, int, str]] = []
    for span in chunks:
        s, e, kind = span
        toks = _ntokens(enc, cleaned[s:e])
        if toks < min_tokens and out:
            ps, _, pk = out[-1]
            out[-1] = (ps, e, pk)
        else:
            out.append(span)
    # Handle a sole/leading sub-min chunk by merging forward.
    if len(out) >= 2 and _ntokens(enc, cleaned[out[0][0] : out[0][1]]) < min_tokens:
        s0, _, k0 = out[0]
        _, e1, _ = out[1]
        out[1] = (s0, e1, k0)
        out.pop(0)
    return out


def _make_breadcrumb(note: Note, heading: OrgHeading | None) -> str:
    """Breadcrumb path string (design §6).

    Preamble (``heading is None``): the note title alone. A heading:
    ``"<title> > <ancestor…> > <heading.text>"``.
    """
    parts = [note.title]
    if heading is not None:
        parts.extend(heading.ancestor_path)
        parts.append(heading.text)
    return " > ".join(p for p in parts if p != "")


def _segment_body_bounds(note: Note, heading: OrgHeading) -> tuple[int, int]:
    """Original-buffer ``[start, end)`` for a heading's body (sans heading line).

    Start = just past the heading line (and its newline); end = ``char_end``.
    """
    text = note.text
    # The heading line runs from char_start to its line-terminating newline.
    nl = text.find("\n", heading.char_start)
    if nl == -1 or nl >= heading.char_end:
        body_start = heading.char_end
    else:
        body_start = nl + 1
    return body_start, heading.char_end


def chunk_note(note: Note, settings: Settings) -> list[Chunk]:
    """Segment ``note`` by heading and recursively split each body into chunks.

    Args:
        note: a parsed :class:`Note` (from ``parse_note``).
        settings: chunk knobs (target / max / overlap / min tokens, tokenizer).

    Returns:
        Global-ordinal-ordered ``list[Chunk]``. ``char_start``/``char_end`` are
        offsets into ``note.text``; ``text`` is the cleaned body; ``embed_text``
        is ``"<breadcrumb>\\n\\n<body>"`` (design §6). Empty segments yield no
        chunk.
    """
    enc = tiktoken.get_encoding(settings.tokenizer_encoding)

    # Build the ordered list of (heading_or_None, body_start, body_end) segments.
    segments: list[tuple[OrgHeading | None, int, int]] = []
    if note.headings:
        first_start = note.headings[0].char_start
        segments.append((None, 0, first_start))  # preamble
        for h in note.headings:
            bs, be = _segment_body_bounds(note, h)
            segments.append((h, bs, be))
    else:
        segments.append((None, 0, len(note.text)))

    chunks: list[Chunk] = []
    ordinal = 0

    for heading, seg_start, seg_end in segments:
        raw_body = note.text[seg_start:seg_end]
        cleaned, offmap = _clean_segment(raw_body, seg_start)
        if cleaned.strip() == "":
            continue

        pieces = _recursive_pieces(
            cleaned, 0, len(cleaned), enc, settings.chunk_max_tokens, 0
        )
        packed = _pack_pieces(pieces, cleaned, enc, settings)
        packed = _merge_small_tails(packed, cleaned, enc, settings.chunk_min_tokens)
        if not packed:
            continue

        breadcrumb = _make_breadcrumb(note, heading)
        heading_index = 0 if heading is None else heading.index

        prev_end_clean: int | None = None
        for chunk_in_heading, (c_start, c_end, lead) in enumerate(packed):
            # Apply prose-only overlap: prepend overlap tokens from the prior
            # chunk when this chunk's left edge is a mid-prose cut (sentence or
            # word) — NOT at a list-item / table-row / paragraph / line / hard
            # boundary, and not the segment's first chunk.
            body_start_clean = c_start
            if (
                settings.chunk_overlap_tokens > 0
                and prev_end_clean is not None
                and lead in ("sentence", "word")
                and not _starts_structured(cleaned, c_start)
            ):
                body_start_clean = _overlap_start(
                    cleaned, prev_end_clean, c_start, enc, settings.chunk_overlap_tokens
                )

            body = cleaned[body_start_clean:c_end]
            char_start = offmap[body_start_clean]
            char_end = offmap[c_end]

            chunks.append(
                Chunk(
                    note_uuid=note.id,
                    note_path=note.path,
                    note_title=note.title,
                    heading_path=breadcrumb,
                    heading_text=None if heading is None else heading.text,
                    heading_id=None if heading is None else heading.id,
                    heading_level=None if heading is None else heading.level,
                    heading_index=heading_index,
                    chunk_in_heading=chunk_in_heading,
                    ordinal=ordinal,
                    char_start=char_start,
                    char_end=char_end,
                    text=body.strip("\n"),
                    embed_text=f"{breadcrumb}\n\n{body.strip()}",
                )
            )
            ordinal += 1
            prev_end_clean = c_end

    return chunks


def _starts_structured(cleaned: str, pos: int) -> bool:
    """Whether the line at cleaned-text ``pos`` begins a list item / table row."""
    line_start = cleaned.rfind("\n", 0, pos) + 1
    line = cleaned[line_start:]
    return bool(_STRUCTURED_RE.match(line))


def _overlap_start(
    cleaned: str,
    prev_end: int,
    cur_start: int,
    enc: "tiktoken.Encoding",
    overlap_tokens: int,
) -> int:
    """Cleaned-text index ``overlap_tokens`` back from ``cur_start`` (capped at prev chunk).

    Walks back from ``cur_start`` into the previous chunk's region
    ``[..., prev_end)`` so the new chunk re-includes the last ``overlap_tokens``
    of context. Never crosses before the previous chunk's own start region.
    """
    window = cleaned[:cur_start]
    tokens = enc.encode(window)
    if len(tokens) <= overlap_tokens:
        return 0
    tail = enc.decode(tokens[-overlap_tokens:])
    start = cur_start - len(tail)
    # Don't reach back past where the previous chunk began contributing.
    return max(start, 0)
