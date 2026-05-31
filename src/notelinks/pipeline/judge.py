"""Judge: turn retrieval candidates into ranked-able `Suggestion`s (design §9).

Two layers, mirroring `retrieve.py`:

* :func:`resolve_anchor` — the **pure**, infra-free anchor-resolution core. Given
  a judge-returned :class:`JudgeAnchor`, the source :class:`Chunk`, and the full
  buffer, it locates the verbatim ``expect`` text **within the source chunk's
  span only** and produces the wire-level :class:`SourceAnchor` (offsets,
  ~40-char ``before``/``after``, ``template``, ``link_description``). Fully
  unit-testable with synthetic data.
* :func:`judge_candidates` — the orchestration. It groups candidates by source
  chunk (one judge call per source chunk), builds a cached-prefix message layout
  (system + full current note) plus a per-call target suffix, calls
  :func:`notelinks.providers.llm.complete_structured`, and assembles
  :class:`Suggestion`s from the validated :class:`JudgeResponse`.

This module does NOT rank / dedup / cap / enforce invariants — that is the
engine's job (T13, design §10). It returns the raw `list[Suggestion]`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from notelinks.models import (
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

if TYPE_CHECKING:
    from openai import OpenAI

    from notelinks.config import Settings
    from notelinks.index.store import Store
    from notelinks.models import Candidate, Chunk, Note

# How many chars of buffer context to capture on each side of the region.
_CONTEXT_CHARS = 40
# Cap on the target-chunk excerpt copied into the output (chars).
_TARGET_EXCERPT_MAX = 600


# ---------------------------------------------------------------------------
# Piece 1: pure anchor resolution (the testable core).
# ---------------------------------------------------------------------------


def resolve_anchor(
    anchor: JudgeAnchor, source_chunk: Chunk, buffer: str
) -> SourceAnchor | None:
    """Locate ``anchor.expect`` in the source chunk's span → a `SourceAnchor`.

    The search is **chunk-scoped**: ``expect`` is matched only within
    ``buffer[source_chunk.char_start : source_chunk.char_end]`` (design §9 /
    `docs/decisions/t4-models.md` deferred-ambiguity note). This removes all
    cross-buffer collisions; the only residual ambiguity is the same string
    appearing twice inside one chunk, in which case we pick the **first**
    occurrence (documented; the deferred-ambiguity case).

    Returns ``None`` if ``expect`` is not found in the chunk span — the caller
    then drops the suggestion or falls back to a whole-chunk wrap.

    Modes (no mode flag survives to the wire — the shape falls out of the
    region, per `json-format.md`):

    * ``mode="wrap"`` — non-empty region = the ``expect`` span. ``expect`` = the
      span text, ``template="{{link}}"``, ``link_description`` = the span text.
    * ``mode="insert"`` — empty region (``char_start == char_end``) at the END
      of the matched ``expect`` span. ``expect=""``, ``template`` =
      ``anchor.insert_text`` (engine-authored prose carrying ``{{link}}``),
      ``link_description`` = a sensible default (the span text; the caller may
      override, e.g. with the target title).

    ``before``/``after`` are ~40 chars of buffer flanking the region, computed
    here from the buffer (never produced by the LLM).
    """
    span_start = source_chunk.char_start
    span_end = source_chunk.char_end
    chunk_span = buffer[span_start:span_end]

    # First occurrence within the chunk span (deferred-ambiguity: first wins).
    local = chunk_span.find(anchor.expect)
    if local == -1:
        return None
    match_start = span_start + local
    match_end = match_start + len(anchor.expect)

    if anchor.mode == "insert":
        # Empty region at the end of the matched span.
        region_start = match_end
        region_end = match_end
        expect = ""
        template = anchor.insert_text if anchor.insert_text is not None else "{{link}}"
        link_description = anchor.expect
    else:  # "wrap"
        region_start = match_start
        region_end = match_end
        expect = anchor.expect
        template = "{{link}}"
        link_description = anchor.expect

    before = buffer[max(0, region_start - _CONTEXT_CHARS) : region_start]
    after = buffer[region_end : region_end + _CONTEXT_CHARS]

    return SourceAnchor(
        char_start=region_start,
        char_end=region_end,
        expect=expect,
        before=before,
        after=after,
        template=template,
        link_description=link_description,
    )


# ---------------------------------------------------------------------------
# Piece 2: orchestration.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You judge IDEA-RESONANCE links between a CURRENT note the user is writing and \
candidate passages from other notes in their corpus. A resonance link is a \
conceptual echo between two passages: an analogy, a shared mechanism, a \
contradiction, an instance of a general idea, a generalization of a specific \
one, or an elaboration — EVEN WHEN THE TWO PASSAGES SHARE NO VOCABULARY. The \
unit of connection is a passage, not a whole note.

You are given the FULL current note for context, then ONE source chunk from it \
(marked with its character range and heading), then a set of candidate target \
passages from other notes. Decide which candidates are GENUINE, worth-linking \
connections to the marked source chunk, and REJECT the rest. Precision matters \
far more than recall: it is correct and expected to return an EMPTY list when \
nothing genuinely resonates. Do not link mere topical overlap or shared keywords.

For each candidate you ACCEPT, return one suggestion with:
* target_chunk_id — the exact id of the accepted candidate (as labelled).
* type — classify the connection, choosing from EXACTLY this enum:
    - "elaborates"            : target develops / adds detail to the source idea.
    - "analogous-mechanism"   : different domains, same underlying mechanism.
    - "contradicts"           : target tension with / opposes the source claim.
    - "instance-of"           : source is a concrete instance of the target's general idea.
    - "generalizes"           : target is a general idea the source instantiates.
    - "mention"               : source explicitly names the concept the target note is about.
* confidence — integer 1-5 (5 = certain, strong resonance worth surfacing).
* why — one tight sentence naming the shared idea (what resonates), not a summary.
* target_is_note — true to link the WHOLE target note (connection is note-wide),
    false to link the target chunk's owning HEADING (the default; prefer this).
* anchor — WHERE in the SOURCE chunk the link attaches. Copy text VERBATIM from
    the SOURCE chunk text (never paraphrase, never use target text):
    - To turn an existing phrase into a link: mode="wrap", expect=<the exact
      phrase from the source chunk to wrap>. Keep it as short as conveys the idea.
    - To add a new sentence carrying the link: mode="insert", expect=<the exact
      verbatim sentence in the source chunk to insert AFTER>, insert_text=<your
      authored prose containing the literal token {{link}} exactly once>.
    The expect text MUST appear verbatim in the source chunk. Do not emit
    before/after offsets — the engine computes those.

Return ONLY the structured object. An empty suggestions list is a valid, often
correct, answer.
"""


def _format_target_block(
    candidate: Candidate, neighbour: Chunk | None, label: str
) -> str:
    """Render one candidate target passage (+ optional adjacent neighbour)."""
    tc = candidate.target_chunk
    breadcrumb = tc.heading_path or tc.note_title
    lines = [
        f"--- CANDIDATE {label} ---",
        f"target_chunk_id: {tc.chunk_id}",
        f"target note title: {tc.note_title}",
        f"breadcrumb: {breadcrumb}",
        f"retrieval score: {candidate.score:.3f}",
        "target passage:",
        tc.text,
    ]
    if neighbour is not None:
        lines += [
            "adjacent context (same heading, for reference only):",
            neighbour.text,
        ]
    return "\n".join(lines)


def _fetch_neighbour(store: Store, target: Chunk) -> Chunk | None:
    """At most one adjacent same-heading chunk (prev preferred, else next).

    Uses the fixed ``store.get_chunk(note_uuid, heading_index, chunk_in_heading)``
    contract; returns ``None`` when the neighbour crosses the heading boundary or
    does not exist (design §7/§9: never cross a heading).
    """
    if target.chunk_in_heading > 0:
        prev = store.get_chunk(
            target.note_uuid, target.heading_index, target.chunk_in_heading - 1
        )
        if prev is not None:
            return prev
    return store.get_chunk(
        target.note_uuid, target.heading_index, target.chunk_in_heading + 1
    )


def _build_messages(
    note: Note, source_chunk: Chunk, candidates: list[Candidate], store: Store
) -> list[dict]:
    """Cached-prefix (system + full note) + per-call source/target suffix.

    The prefix repeats across every per-source-chunk call for this note, so the
    full-note part is wrapped via :func:`llm.cached_text` (Anthropic ephemeral
    cache breakpoint) to keep cost bounded. The suffix (the marked source chunk
    + its candidate targets) is the cache-miss tail.
    """
    source_heading = source_chunk.heading_text or "(note preamble)"
    suffix_parts = [
        "SOURCE CHUNK (from the current note above) — judge connections TO this:",
        f"heading: {source_heading}",
        f"char range: [{source_chunk.char_start}, {source_chunk.char_end})",
        "source chunk text:",
        source_chunk.text,
        "",
        "CANDIDATE TARGET PASSAGES:",
    ]
    for idx, candidate in enumerate(candidates, start=1):
        neighbour = _fetch_neighbour(store, candidate.target_chunk)
        suffix_parts.append(_format_target_block(candidate, neighbour, f"#{idx}"))
    suffix = "\n".join(suffix_parts)

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                llm.cached_text(
                    "FULL CURRENT NOTE (context; cached across calls):\n\n"
                    + note.text
                ),
                {"type": "text", "text": suffix},
            ],
        },
    ]


def _build_target(raw: RawJudgeSuggestion, target_chunk: Chunk) -> Target:
    """Assemble the `Target` link components from the matched target chunk.

    ``heading`` is the chunk's owning heading UNLESS ``raw.target_is_note`` or
    the chunk has no heading (preamble) → ``None`` = file-level target.
    """
    heading: TargetHeading | None = None
    if not raw.target_is_note and target_chunk.heading_text is not None:
        heading = TargetHeading(
            text=target_chunk.heading_text,
            id=target_chunk.heading_id,
            level=target_chunk.heading_level if target_chunk.heading_level is not None else 1,
        )
    return Target(
        file=target_chunk.note_path,
        title=target_chunk.note_title,
        file_id=target_chunk.note_uuid,
        heading=heading,
    )


def judge_candidates(
    candidates: list[Candidate],
    note: Note,
    store: Store,
    settings: Settings,
    *,
    client: OpenAI | None = None,
) -> list[Suggestion]:
    """Judge candidates into `Suggestion`s, one LLM call per source chunk.

    Candidates are grouped by source chunk (design §9: present a chunk's
    competing targets together so the judge can choose among them and avoid
    over-linking one passage). For each group a cached-prefix message (system +
    full current note) plus a per-call target suffix is sent to
    :func:`llm.complete_structured`, validated as a :class:`JudgeResponse`. The
    judge MAY reject everything (empty list).

    Each accepted :class:`RawJudgeSuggestion` is anchored via
    :func:`resolve_anchor`. **Fallback choice:** if ``expect`` is not found
    verbatim in the source chunk span, we DROP that suggestion (rather than a
    whole-chunk wrap). Rationale: a non-matching ``expect`` means the judge
    paraphrased or pointed outside the chunk, so the rationale/anchor pairing is
    untrustworthy — dropping favours precision, consistent with the judge's
    reject-by-default stance. (The whole-chunk-wrap fallback remains available;
    see the decision doc.)

    Returns the assembled `list[Suggestion]` with stable ``s01``, ``s02``, … ids.
    Ranking / dedup / top_n / invariants are the engine's job (T13), NOT here.
    """
    # Group candidates by source chunk, preserving first-seen order.
    groups: dict[str, list[Candidate]] = {}
    source_by_id: dict[str, Chunk] = {}
    for candidate in candidates:
        sid = candidate.source_chunk.chunk_id
        source_by_id.setdefault(sid, candidate.source_chunk)
        groups.setdefault(sid, []).append(candidate)

    suggestions: list[Suggestion] = []
    counter = 0
    for sid, group in groups.items():
        source_chunk = source_by_id[sid]
        # Map for resolving the judge's target_chunk_id back to a Candidate.
        target_by_id = {c.target_chunk.chunk_id: c for c in group}

        messages = _build_messages(note, source_chunk, group, store)
        response = llm.complete_structured(
            messages, settings, JudgeResponse, client=client
        )
        assert isinstance(response, JudgeResponse)  # narrow for type-checkers

        for raw in response.suggestions:
            candidate = target_by_id.get(raw.target_chunk_id)
            if candidate is None:
                # Judge referenced an id we did not offer — ignore defensively.
                continue
            anchor = resolve_anchor(raw.anchor, source_chunk, note.text)
            if anchor is None:
                # Fallback choice: drop (see docstring). Favours precision.
                continue

            target_chunk = candidate.target_chunk
            target = _build_target(raw, target_chunk)
            # For insert mode, default the link description to the target title
            # rather than the source-side expect sentence.
            if raw.anchor.mode == "insert":
                anchor.link_description = target_chunk.note_title

            counter += 1
            excerpt = target_chunk.text[:_TARGET_EXCERPT_MAX]
            suggestions.append(
                Suggestion(
                    id=f"s{counter:02d}",
                    type=raw.type,
                    confidence=raw.confidence,
                    why=raw.why,
                    source_chunk=SourceChunk(
                        text=source_chunk.text,
                        heading=source_chunk.heading_text,
                        char_start=source_chunk.char_start,
                        char_end=source_chunk.char_end,
                    ),
                    target_excerpt=excerpt,
                    target=target,
                    source_anchor=anchor,
                )
            )

    return suggestions
