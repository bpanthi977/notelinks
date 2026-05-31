"""Retrieval & candidate selection (design §8).

Two layers:

* :func:`select_candidates` — the **pure**, infra-free selection core. Given
  ranked hits per source chunk, it applies the floor, the per-source cap, and
  the round-robin-by-rank merge under a global cap. Fully unit-testable with
  synthetic data.
* :func:`retrieve_candidates` — the thin **query wrapper**. It drives the Store
  (fixed ``store.query_chunks`` contract), dedups targets seen by multiple
  sources, then delegates ordering to :func:`select_candidates`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from notelinks.models import Candidate, Chunk, OrgLink
from notelinks.pipeline.exclude import chunk_already_linked

if TYPE_CHECKING:
    from notelinks.config import Settings


class _Store(Protocol):
    """The FIXED Store contract this module codes to (does not implement)."""

    def query_chunks(
        self,
        query_embedding: list[float],
        k: int,
        exclude_note_uuid: str | None = None,
        exclude_target_uuids: list[str] | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Return up to ``k`` (target_chunk, cosine_similarity) pairs, best-first."""
        ...


def select_candidates(
    hits_by_source: list[tuple[Chunk, list[tuple[Chunk, float]]]],
    *,
    per_source_n: int,
    global_cap_m: int,
    sim_floor: float,
) -> list[Candidate]:
    """Pure selection core (design §8).

    Args:
        hits_by_source: one entry per source chunk: ``(source_chunk, hits)``
            where ``hits`` is a ranked list of ``(target_chunk, similarity)``
            sorted by similarity descending.
        per_source_n: max targets kept per source chunk (N).
        global_cap_m: global cap on emitted candidates (M).
        sim_floor: drop any hit with ``similarity < sim_floor``.

    Returns:
        Ordered ``list[Candidate]``: round-robin by rank across sources — all
        rank-1 hits (similarity desc across sources), then all rank-2s, etc.,
        appending until ``global_cap_m`` is reached.
    """
    # Steps 1 & 2: floor, then keep top-N per source (input already ranked desc).
    per_source_kept: list[list[Candidate]] = []
    for source_chunk, hits in hits_by_source:
        kept = [
            Candidate(source_chunk=source_chunk, target_chunk=target, score=sim)
            for target, sim in hits
            if sim >= sim_floor
        ][:per_source_n]
        if kept:
            per_source_kept.append(kept)

    # Step 3: round-robin by rank. For each rank position, gather that rank's
    # candidate from every source, order those by score desc, then emit.
    selected: list[Candidate] = []
    max_rank = max((len(kept) for kept in per_source_kept), default=0)
    for rank in range(max_rank):
        rank_slice = [kept[rank] for kept in per_source_kept if rank < len(kept)]
        rank_slice.sort(key=lambda c: c.score, reverse=True)
        for candidate in rank_slice:
            if len(selected) >= global_cap_m:
                return selected
            selected.append(candidate)
    return selected


def retrieve_candidates(
    store: _Store,
    source_chunks: list[Chunk],
    source_embeddings: list[list[float]],
    *,
    settings: Settings,
    current_note_uuid: str,
    existing_links: list[OrgLink],
) -> list[Candidate]:
    """Query the Store for each source chunk, dedup targets, then select.

    For each ``(source_chunk, embedding)`` pair, calls ``store.query_chunks``
    excluding only the current note's own chunks (``exclude_note_uuid``).
    Already-linked targets are dropped **per chunk** via
    :func:`~notelinks.pipeline.exclude.chunk_already_linked` (heading-level —
    note-level ``$nin`` would over-exclude headings the source hasn't linked).
    A target chunk hit by multiple source chunks keeps only its single
    best-scoring pairing (design §8 dedup). The surviving hits are regrouped into
    ``hits_by_source`` and ordered by :func:`select_candidates`.
    """
    # Best (source_chunk, similarity) seen per target chunk_id.
    best_for_target: dict[str, tuple[Chunk, Chunk, float]] = {}
    for source_chunk, embedding in zip(source_chunks, source_embeddings, strict=True):
        hits = store.query_chunks(
            embedding,
            k=settings.top_k,
            exclude_note_uuid=current_note_uuid,
        )
        for target_chunk, sim in hits:
            # Heading-level already-linked filter: skip a target whose heading
            # the source already links (or the preamble of a whole-note link).
            if chunk_already_linked(target_chunk, existing_links):
                continue
            tid = target_chunk.chunk_id
            existing = best_for_target.get(tid)
            if existing is None or sim > existing[2]:
                best_for_target[tid] = (source_chunk, target_chunk, sim)

    # Regroup the deduped hits back under their winning source chunk, preserving
    # the best-first ordering select_candidates expects.
    grouped: dict[str, list[tuple[Chunk, float]]] = {}
    source_by_id: dict[str, Chunk] = {}
    for source_chunk, target_chunk, sim in best_for_target.values():
        sid = source_chunk.chunk_id
        source_by_id[sid] = source_chunk
        grouped.setdefault(sid, []).append((target_chunk, sim))

    hits_by_source: list[tuple[Chunk, list[tuple[Chunk, float]]]] = []
    for sid, hits in grouped.items():
        hits.sort(key=lambda pair: pair[1], reverse=True)
        hits_by_source.append((source_by_id[sid], hits))

    return select_candidates(
        hits_by_source,
        per_source_n=settings.per_source_n,
        global_cap_m=settings.global_cap_m,
        sim_floor=settings.sim_floor,
    )
