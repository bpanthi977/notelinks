"""Judge: turn retrieval candidates into ranked-able `Suggestion`s (design §9).

Two layers, mirroring `retrieve.py`:

* :func:`resolve_anchor` — the **pure**, infra-free anchor-resolution core. Given
  a judge-returned :class:`JudgeAnchor`, the source :class:`Chunk`, and the full
  buffer, it locates the ``expect`` text **within the source chunk's span only**
  (exact, else tolerant of rendered org links / wrapped whitespace — see
  :func:`_find_span`) and produces the wire-level :class:`SourceAnchor` (offsets,
  ~40-char ``before``/``after``, ``template``, ``link_description``). Fully
  unit-testable with synthetic data.
* :func:`judge_candidates` — the orchestration. It groups candidates by TARGET
  FILE (one judge call per target note), builds a cached-prefix message layout
  (system + full current note) plus a per-call suffix of that target note's
  candidate passages, calls
  :func:`notelinks.providers.llm.complete_structured`, and assembles
  :class:`Suggestion`s from the validated :class:`JudgeResponse`. The judge
  decides, for each candidate passage, whether and WHERE in the current note the
  link attaches — so the anchor is resolved against the whole buffer.

This module does NOT rank / dedup / cap / enforce invariants — that is the
engine's job (T13, design §10). It returns the raw `list[Suggestion]`.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from notelinks import observability
from notelinks.models import (
    JudgeAnchor,
    JudgeResponse,
    RawJudgeSuggestion,
    SourceAnchor,
    Suggestion,
    Target,
    TargetHeading,
)
from notelinks.providers import llm

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from openai import OpenAI

    from notelinks.config import Settings
    from notelinks.index.store import Store
    from notelinks.models import Candidate, Chunk, Note

# How many chars of buffer context to capture on each side of the region.
_CONTEXT_CHARS = 40
# Cap on the target-chunk excerpt copied into the output (chars).
_TARGET_EXCERPT_MAX = 600

# Property/other drawer lines (``:PROPERTIES:`` … ``:END:``). Stripped from the
# current-note view shown to the judge — never a valid link anchor (design §4).
_DRAWER_OPEN_RE = re.compile(r"^[ \t]*:([A-Za-z0-9_-]+):[ \t]*$")
_DRAWER_END_RE = re.compile(r"^[ \t]*:END:[ \t]*$", re.IGNORECASE)

# Inline anchor marker in the judge's ``insert_text``: the words wrapped in
# ``{{...}}`` become the link (its description); the engine rewrites that span to
# the wire ``{{link}}`` token. Non-greedy so only the marked phrase is captured.
_BRACE_RE = re.compile(r"\{\{(.+?)\}\}")


def _strip_drawers(text: str) -> str:
    """Drop ``:DRAWER:`` … ``:END:`` blocks from the note view shown to the judge.

    Display-only: the judge anchors on body prose, so removing drawers keeps it
    from ever proposing a property-drawer line. ``resolve_anchor`` still searches
    the ORIGINAL ``note.text``, so output offsets are unaffected.
    """
    out: list[str] = []
    in_drawer = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if in_drawer:
            if _DRAWER_END_RE.match(stripped):
                in_drawer = False
            continue
        if _DRAWER_OPEN_RE.match(stripped) and not _DRAWER_END_RE.match(stripped):
            in_drawer = True
            continue
        out.append(line)
    return "".join(out)


# Org links: ``[[target]]`` or ``[[target][description]]``. Flattened to their
# visible text when matching, since the judge renders them (it sees the raw note
# but reproduces ``[[id:..][entropy]]`` as ``entropy``).
_ORG_LINK_RE = re.compile(r"\[\[([^\]]+)\](?:\[([^\]]+)\])?\]")


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize TEXT for tolerant matching and map each normalized char back.

    Two transforms make the judge's *rendered* ``expect`` matchable against the
    raw org buffer: org links are flattened to their visible text (the
    description, or the target when there is none), and runs of whitespace
    (including the newlines of a wrapped paragraph) collapse to a single space.

    ``idx_map[j]`` is the index in TEXT that produced ``norm[j]``; ``idx_map`` has
    ``len(norm) + 1`` entries, the last being ``len(text)``, so a match's end
    offset maps back too.
    """
    norm: list[str] = []
    idx_map: list[int] = []
    i, n = 0, len(text)
    prev_ws = False
    while i < n:
        m = _ORG_LINK_RE.match(text, i)
        if m:
            visible = m.group(2) if m.group(2) is not None else m.group(1)
            for ch in visible:
                norm.append(ch)
                idx_map.append(i)
            prev_ws = False
            i = m.end()
            continue
        ch = text[i]
        if ch.isspace():
            if not prev_ws:
                norm.append(" ")
                idx_map.append(i)
                prev_ws = True
            i += 1
        else:
            norm.append(ch)
            idx_map.append(i)
            prev_ws = False
            i += 1
    idx_map.append(n)
    return "".join(norm), idx_map


# Fuzzy-match budget: a candidate region may differ from ``expect`` by at most
# this fraction of ``expect``'s length (edits). Deliberately tight — the design
# is precision-first (dropping a suggestion beats anchoring it in the wrong
# place), and the judge is told to copy verbatim, so only small residual diffs
# (a "corrected" typo, a dropped word) should ever need fuzzing.
_FUZZY_MAX_ERROR_RATIO = 0.2


def _fuzzy_find(haystack: str, needle: str, max_errors: int) -> tuple[int, int] | None:
    """Best approximate-substring match of NEEDLE in HAYSTACK by edit distance.

    Sellers' algorithm: a Levenshtein DP in which starting the match at any
    position in HAYSTACK is free (row 0 is all-zero), so the minimum of the final
    row is the cost of the best-matching substring ending at each position. We
    carry each cell's start column alongside the cost to recover ``(start, end)``.

    Returns the lowest-cost region, or ``None`` when even the best match needs
    more than MAX_ERRORS edits. O(len(needle) * len(haystack)).
    """
    m, n = len(needle), len(haystack)
    if m == 0 or max_errors < 0:
        return None
    prev_cost = [0] * (n + 1)
    prev_start = list(range(n + 1))  # an empty match at column j starts at j
    for i in range(1, m + 1):
        cur_cost = [i] + [0] * n
        cur_start = [0] * (n + 1)
        pc = needle[i - 1]
        for j in range(1, n + 1):
            diag = prev_cost[j - 1] + (0 if pc == haystack[j - 1] else 1)
            up = prev_cost[j] + 1  # skip a needle char
            left = cur_cost[j - 1] + 1  # skip a haystack char
            best = min(diag, up, left)
            cur_cost[j] = best
            if best == diag:
                cur_start[j] = prev_start[j - 1]
            elif best == up:
                cur_start[j] = prev_start[j]
            else:
                cur_start[j] = cur_start[j - 1]
        prev_cost, prev_start = cur_cost, cur_start
    best_j = min(range(n + 1), key=lambda j: prev_cost[j])
    if prev_cost[best_j] > max_errors:
        return None
    return prev_start[best_j], best_j


def _find_span(haystack: str, needle: str) -> tuple[int, int] | None:
    """Locate NEEDLE in HAYSTACK, returning ``(start, end)`` offsets in HAYSTACK.

    Three escalating passes, all mapped back to raw HAYSTACK offsets:

    1. **exact** substring match (fast path);
    2. **normalized** match — org links flattened, whitespace collapsed
       (:func:`_normalize_with_map`) — so the judge's rendered ``expect`` anchors
       across a link or a wrapped newline;
    3. **fuzzy** match (:func:`_fuzzy_find`) on the normalized strings, accepted
       only within :data:`_FUZZY_MAX_ERROR_RATIO`, so small residual wording
       differences (a "corrected" typo, a dropped word) still anchor while a
       genuinely absent ``expect`` is still rejected.

    Returns ``None`` when NEEDLE is found by none of them.
    """
    exact = haystack.find(needle)
    if exact != -1:
        return exact, exact + len(needle)
    norm_hay, idx_map = _normalize_with_map(haystack)
    norm_needle, _ = _normalize_with_map(needle)
    if not norm_needle:
        return None
    pos = norm_hay.find(norm_needle)
    if pos != -1:
        return idx_map[pos], idx_map[pos + len(norm_needle)]
    fuzzy = _fuzzy_find(norm_hay, norm_needle, round(_FUZZY_MAX_ERROR_RATIO * len(norm_needle)))
    if fuzzy is None:
        return None
    return idx_map[fuzzy[0]], idx_map[fuzzy[1]]


# ---------------------------------------------------------------------------
# Piece 1: pure anchor resolution (the testable core).
# ---------------------------------------------------------------------------


def resolve_anchor(
    anchor: JudgeAnchor, source_chunk: Chunk | None, buffer: str
) -> SourceAnchor | None:
    """Locate ``anchor.expect`` in the buffer → a `SourceAnchor`.

    Search scope depends on ``source_chunk``:

    * ``source_chunk is None`` (the target-file grouping path) → search the
      **whole buffer**. With one call per target note, the judge anchors anywhere
      in the current note, so there is no single source chunk to scope to. The
      residual ambiguity is the same ``expect`` string appearing more than once
      in the note, in which case we pick the **first** occurrence.
    * ``source_chunk`` given → **chunk-scoped**: ``expect`` is matched only within
      ``buffer[source_chunk.char_start : source_chunk.char_end]`` (the original
      behaviour, retained for the pure unit tests / a possible chunk-scoped
      fallback). First occurrence within the span wins.

    Matching escalates via :func:`_find_span`: exact → normalized (links
    flattened, whitespace collapsed) → fuzzy (bounded edit distance). The judge
    sees the raw note but renders org links (``[[id:..][entropy]]`` → ``entropy``),
    collapses wrapped newlines, and may lightly reword (a "corrected" typo), so a
    literal search misses; the escalating passes recover the span and map it back
    to raw offsets, while a genuinely absent ``expect`` is still rejected.

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
    if source_chunk is None:
        span_start = 0
        span_end = len(buffer)
    else:
        span_start = source_chunk.char_start
        span_end = source_chunk.char_end
    chunk_span = buffer[span_start:span_end]

    # First occurrence within the search span (deferred-ambiguity: first wins).
    # Tolerant of org-link markup and wrapped whitespace the judge renders away.
    found = _find_span(chunk_span, anchor.expect)
    if found is None:
        return None
    match_start = span_start + found[0]
    match_end = span_start + found[1]

    if anchor.mode == "insert":
        # Empty region at the end of the matched span.
        region_start = match_end
        region_end = match_end
        expect = ""
        # The judge marks the link anchor INLINE with ``{{words}}``: that phrase
        # is the link description; rewrite it to the wire ``{{link}}`` token so
        # the surrounding prose flows naturally. Back-compat: an insert_text that
        # already uses a literal ``{{link}}`` (or no marker) falls back to the
        # expect sentence as the description.
        raw_insert = anchor.insert_text if anchor.insert_text is not None else "{{link}}"
        brace = _BRACE_RE.search(raw_insert)
        if brace is not None and brace.group(1) != "link":
            link_description = brace.group(1)
            template = raw_insert[: brace.start()] + "{{link}}" + raw_insert[brace.end() :]
        else:
            link_description = anchor.expect
            template = raw_insert
    else:  # "wrap"
        region_start = match_start
        region_end = match_end
        # The verbatim region text (which may differ from the judge's rendered
        # ``expect`` — e.g. embedded markup), so the client matches it as-is.
        expect = buffer[match_start:match_end]
        template = "{{link}}"
        link_description = expect

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

You are given the FULL current note the user is writing, then a set of candidate \
passages from ONE OTHER note in their corpus. For EACH candidate passage, decide \
whether it is a GENUINE, worth-linking resonance with some passage in the current \
note and, if so, WHERE in the current note the link should attach; REJECT the \
rest. Precision matters far more than recall: it is correct and expected to \
return an EMPTY list when nothing genuinely resonates. Do not link mere topical \
overlap or shared keywords. Do not suggest linking if the link doesn't add substantially\
to the usefullness of the note.

For each candidate you ACCEPT, return one suggestion with:
* candidate_id — the number of the accepted candidate (as labelled, e.g. 1).
* type — classify the connection, choosing from EXACTLY this enum:
    - "elaborates"            : target develops / adds detail to the current note's idea.
    - "analogous-mechanism"   : different domains, same underlying mechanism.
    - "contradicts"           : target is in tension with / opposes the current note's claim.
    - "instance-of"           : the current note is a concrete instance of the target idea.
    - "generalizes"           : target is a general idea the current note instantiates.
    - "mention"               : the current note explicitly names the concept the target is about.
* confidence — integer 1-3 rating the connection's strength:
    - 3 : very strong, NOVEL connection (a non-obvious resonance worth surfacing).
    - 2 : strong connection.
    - 1 : good connection.
  Anything weaker than "good" is NOT worth linking — REJECT it (omit it entirely)
  rather than emitting a low-confidence suggestion. Precision over recall.
* why — one tight sentence naming the shared idea (what resonates), not a summary.
* target_is_note — true to link the WHOLE target note (connection is note-wide),
    false to link the target passage's owning HEADING (the default; prefer this).
* anchor — WHERE in the CURRENT NOTE the link attaches. Copy text VERBATIM from
    the CURRENT NOTE (never paraphrase, never use target text). Two modes:
    - mode="insert" — Add ONE short, complete, natural sentence that points the
      reader to the target. expect=<the exact verbatim sentence in the current
      note to insert AFTER>; insert_text=<your authored sentence>. Inside that
      sentence, wrap the EXACT words that should become the link in double curly
      braces {{...}} — exactly once. The braced words must read naturally where
      they sit (a content word or short phrase, ~1-4 words);
      Prefer insert for elaborates / analogous-mechanism / contradicts /
      instance-of / generalizes.
    - mode="wrap" — USUALLY for "mention", or when a SHORT noun phrase already names
      the target concept. expect=<the exact phrase to wrap>, and it MUST be a
      short noun phrase of about 1-5 words (a term, not a clause). NEVER wrap a
      whole sentence, clause, or passage — if the natural anchor is longer than a
      few words, use mode="insert" instead.
    The expect text MUST appear verbatim in the current note, and MUST be drawn
    from the note's BODY PROSE — never anchor on the note title (`#+title:`) or a
    heading line (a line beginning with `*`). Headings and the title are shown
    only as structure/context, not as link targets. Do not emit before/after
    offsets — the engine computes those.

Return ONLY the structured object. An empty suggestions list is a valid, often
correct, answer.
"""


def _format_target_block(
    candidate: Candidate, neighbour: Chunk | None, label: int
) -> str:
    """Render one candidate target passage (+ optional adjacent neighbour)."""
    tc = candidate.target_chunk
    breadcrumb = tc.heading_path or tc.note_title
    lines = [
        f"--- CANDIDATE {label} ---",
        f"candidate_id: {label}",
        f"target note title: {tc.note_title}",
        f"breadcrumb: {breadcrumb}",
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
    note: Note, target_title: str, candidates: list[Candidate], store: Store
) -> list[dict]:
    """Cached-prefix (system + full current note) + per-target-file suffix.

    Every candidate here is from ONE target note. The full-current-note prefix is
    constant across all per-target-file calls for this note, so it is wrapped via
    :func:`llm.cached_text` (Anthropic ephemeral cache breakpoint) to keep cost
    bounded. The suffix (this target note's candidate passages) is the
    cache-miss tail.
    """
    suffix_parts = [
        f"TARGET NOTE: {target_title}",
        "Below are candidate passages from this one other note. For each, decide "
        "whether it genuinely resonates with some passage in the CURRENT NOTE "
        "above and, if so, WHERE in the current note the link should attach.",
        "",
        "CANDIDATE TARGET PASSAGES:",
    ]
    for idx, candidate in enumerate(candidates, start=1):
        neighbour = _fetch_neighbour(store, candidate.target_chunk)
        suffix_parts.append(_format_target_block(candidate, neighbour, idx))
    suffix = "\n".join(suffix_parts)

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                llm.cached_text("FULL CURRENT NOTE:\n\n" + _strip_drawers(note.text)),
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
    max_workers: int = 8,
) -> list[Suggestion]:
    """Judge candidates into `Suggestion`s, one LLM call per TARGET FILE.

    Candidates are grouped by target note (``target_chunk.note_uuid``): one judge
    call per target note presents all of that note's matched passages together,
    so the judge can choose note-vs-heading granularity and avoid over-linking the
    same target note from several spots. For each group a cached-prefix message
    (system + full current note) plus a per-call suffix of that target note's
    candidate passages is sent to :func:`llm.complete_structured`, validated as a
    :class:`JudgeResponse`. The judge MAY reject everything (empty list).

    **Anchoring.** Because grouping is by target, the judge decides WHERE in the
    current note each accepted link attaches; the anchor is resolved against the
    whole buffer (``resolve_anchor(..., None, note.text)``). The output carries
    only the resolved ``source_anchor`` (offsets + verbatim text) — there is no
    separate ``source_chunk`` display field.

    **Parallelism.** The per-target-file LLM calls are I/O-bound and the OpenAI
    SDK client is sync and thread-safe, so they run concurrently on a
    :class:`~concurrent.futures.ThreadPoolExecutor` (``max_workers``). The work
    splits cleanly: only the message build + LLM call run in threads; **all**
    assembly (anchor resolution, `Suggestion` construction, id assignment) runs
    sequentially afterwards in the original group order. This keeps the output
    deterministic: the returned list and the ``s01``/``s02``/… ids depend only on
    first-seen target-file order, never on which LLM call finishes first.

    **Failure isolation.** An exception in one group's LLM call is logged and that
    group is skipped (it contributes no suggestions); the other groups still
    produce results — one slow/failing call cannot abort the whole run.

    Each accepted :class:`RawJudgeSuggestion` is anchored via
    :func:`resolve_anchor`. **Fallback choice:** if ``expect`` is not found
    verbatim in the current note, we DROP that suggestion. Rationale: a
    non-matching ``expect`` means the judge paraphrased or invented text, so the
    rationale/anchor pairing is untrustworthy — dropping favours precision,
    consistent with the judge's reject-by-default stance.

    Returns the assembled `list[Suggestion]` with stable ``s01``, ``s02``, … ids.
    Ranking / dedup / top_n / invariants are the engine's job (T13), NOT here.
    """
    # Group candidates by TARGET FILE (note uuid), preserving first-seen order.
    groups: dict[str, list[Candidate]] = {}
    title_by_uuid: dict[str, str] = {}
    for candidate in candidates:
        tid = candidate.target_chunk.note_uuid
        title_by_uuid.setdefault(tid, candidate.target_chunk.note_title)
        groups.setdefault(tid, []).append(candidate)

    # Stable, original group order — assembly and id assignment key off this.
    ordered_tids = list(groups)

    logger.info(
        "judge: %d candidates grouped into %d target-file calls",
        len(candidates),
        len(ordered_tids),
    )

    def _call(tid: str) -> JudgeResponse:
        # A per-group span (no-op unless tracing): created INSIDE the worker
        # thread under the propagated context, so the group's OpenAI auto-span
        # nests under the suggest root trace rather than splitting off.
        with observability.judge_span(tid):
            messages = _build_messages(note, title_by_uuid[tid], groups[tid], store)
            started = time.perf_counter()
            response = llm.complete_structured(
                messages, settings, JudgeResponse, client=client
            )
            assert isinstance(response, JudgeResponse)  # narrow for type-checkers
            logger.debug(
                "judge: target %s (%d passages) -> %d suggestions in %.2fs",
                tid,
                len(groups[tid]),
                len(response.suggestions),
                time.perf_counter() - started,
            )
            return response

    # OTel context is contextvars-based and does NOT propagate into worker
    # threads, so capture the current context here (no-op/None when tracing off)
    # and replay it inside each worker — so worker spans nest under the root.
    parent_ctx = observability.current_context()

    # Parallel I/O: one LLM call per source chunk. Results are collected keyed by
    # sid so the order in which calls *finish* never affects the output.
    responses: dict[str, JudgeResponse] = {}
    if ordered_tids:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_tid = {
                executor.submit(
                    observability.context_propagating_wrapper(_call, parent_ctx), tid
                ): tid
                for tid in ordered_tids
            }
            for future, tid in future_to_tid.items():
                try:
                    responses[tid] = future.result()
                except Exception:
                    # Failure isolation: log and skip this group; others survive.
                    logger.exception(
                        "judge LLM call failed for target file %s; skipping group",
                        tid,
                    )

    # Sequential, pure assembly in the original group order (deterministic ids).
    suggestions: list[Suggestion] = []
    counter = 0
    for tid in ordered_tids:
        response = responses.get(tid)
        if response is None:
            continue  # the group's LLM call failed and was skipped above.
        group = groups[tid]
        # Map the judge's candidate number (1..N, as labelled) back to a Candidate.
        target_by_label = {i: c for i, c in enumerate(group, start=1)}

        for raw in response.suggestions:
            candidate = target_by_label.get(raw.candidate_id)
            if candidate is None:
                # Judge referenced an id we did not offer — ignore defensively.
                continue
            # Target-file grouping: the judge chose where in the current note the
            # link attaches, so anchor against the whole buffer (source_chunk=None).
            anchor = resolve_anchor(raw.anchor, None, note.text)
            if anchor is None:
                # Fallback choice: drop (see docstring). Favours precision.
                continue

            target_chunk = candidate.target_chunk
            target = _build_target(raw, target_chunk)

            counter += 1
            excerpt = target_chunk.text[:_TARGET_EXCERPT_MAX]
            suggestions.append(
                Suggestion(
                    id=f"s{counter:02d}",
                    type=raw.type,
                    confidence=raw.confidence,
                    why=raw.why,
                    target_excerpt=excerpt,
                    target=target,
                    source_anchor=anchor,
                )
            )

    return suggestions
