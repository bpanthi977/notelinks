"""The transport-agnostic core (design §2).

An :class:`Engine` owns the long-lived state — TWO per-role :class:`OpenAI`
clients (an ``embedding_client``, ALWAYS OpenRouter via
:func:`notelinks.providers.embeddings.make_client`, and a ``judge_client``,
routed by ``settings.judge_provider`` via
:func:`notelinks.providers.llm.make_judge_client` to OpenRouter or a local Ollama
endpoint — T18) and ONE long-lived :class:`~notelinks.index.store.Store` — and
exposes the two verbs the adapters drive:

* :meth:`refresh` — incremental corpus refresh (cheap when nothing changed).
* :meth:`suggest` — the full pipeline for one note buffer, returning the wire
  :class:`~notelinks.models.Envelope`.

Adapters (``cli.py`` now, ``api.py`` later) construct ONE ``Engine`` and reuse
it; the engine never builds-and-tears-down clients or the store per call
(design §2: clients constructed once and injected, store is a long-lived handle,
``pipeline/`` steps are stateless functions).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from notelinks import observability
from notelinks.config import Settings
from notelinks.index import build
from notelinks.index.store import Store
from notelinks.models import Envelope, OrgLink, Source, Suggestion
from notelinks.org.chunk import chunk_note
from notelinks.org.parse import parse_note
from notelinks.pipeline.exclude import suggestion_duplicates_link
from notelinks.pipeline.judge import judge_candidates
from notelinks.pipeline.retrieve import retrieve_candidates
from notelinks.providers import embeddings, llm
from notelinks.providers.embeddings import embed_texts

logger = logging.getLogger(__name__)

_ENVELOPE_VERSION = 1


class Engine:
    """Long-lived core holding the shared provider client + Chroma store."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # TWO per-role OpenAI clients, built lazily so an Engine can be
        # constructed (and ``refresh`` driven with a monkeypatched embedder in
        # tests) without any key. ``embedding_client`` is ALWAYS OpenRouter;
        # ``judge_client`` is routed by ``settings.judge_provider`` (OpenRouter or
        # local Ollama — T18). Built once and injected into every call.
        self._embedding_client = None
        self._judge_client = None
        # The Store is the long-lived Chroma handle — opened once, reused.
        self.store = Store(settings)

    @property
    def embedding_client(self):
        """The OpenRouter embedding client, built once on first real use.

        Embeddings ALWAYS go through OpenRouter (so the Chroma index is provider-
        stable), so this requires OPENROUTER_API_KEY even when the judge is local.
        Lazily constructed so importing/constructing the engine never needs a key
        (tests that monkeypatch ``embed_texts`` never trigger it).
        """
        if self._embedding_client is None:
            self._embedding_client = embeddings.make_client(self.settings)
        return self._embedding_client

    @property
    def judge_client(self):
        """The judge client, built once on first real use, routed by provider.

        ``settings.judge_provider`` selects OpenRouter (default) or a local Ollama
        endpoint; with Ollama no OpenRouter key is required for the judge. Lazily
        constructed so tests that monkeypatch ``complete_structured`` never trigger
        it.
        """
        if self._judge_client is None:
            self._judge_client = llm.make_judge_client(self.settings)
        return self._judge_client

    def refresh(self, *, rebuild: bool = False) -> dict:
        """Incrementally refresh the corpus index (design §7).

        Delegates to :func:`notelinks.index.build.refresh` with the shared store
        and client. ``rebuild=True`` forces a full reindex. Returns the build
        stats dict (``indexed`` / ``reindexed`` / ``skipped`` / ``deleted`` /
        ``chunks``).
        """
        stats = build.refresh(
            self.store, self.settings, client=self.embedding_client, rebuild=rebuild
        )
        logger.info(
            "refresh: indexed=%d reindexed=%d skipped=%d deleted=%d chunks=%d (rebuild=%s)",
            stats["indexed"],
            stats["reindexed"],
            stats["skipped"],
            stats["deleted"],
            stats["chunks"],
            rebuild,
        )
        return stats

    def suggest(self, buffer_text: str) -> Envelope:
        """Run the full pipeline for one note buffer → an :class:`Envelope`.

        **Pure query (T19):** this no longer refreshes the corpus — it reads the
        already-built index as it stands. Callers are responsible for freshness:
        the CLI ``suggest`` calls :meth:`refresh` first (one-shot), and the REST
        adapter keeps the index fresh via an initial refresh + a file watcher, so
        a query never auto-refreshes (which would serialize writes into every
        read and make the server's lock model far harder to reason about).

        Flow (design §8–§11):

        1. Parse the BUFFER (not the on-disk copy) so unsaved edits count. The
           buffer supplies the query chunks; ``content_hash`` covers it verbatim.
        2. Embed the buffer's chunks and retrieve candidates, excluding the
           current note's own chunks and any already-linked targets.
        3. Judge the candidates into raw suggestions.
        4. Rank / dedup / cap / enforce invariants (design §10).
        5. Build and return the wire :class:`Envelope`.
        """
        if self.settings.corpus_dir is None:
            raise ValueError(
                "settings.corpus_dir is None; set NOTELINKS_CORPUS_DIR or pass --corpus."
            )

        # 1. Parse the buffer. No file path is taken as input — the note is
        #    identified by its own :ID: (uuid), which also drives self-exclusion.
        note = parse_note(buffer_text)

        # The whole pipeline body runs under a single (no-op-unless-traced) ROOT
        # span, so the query-embedding OpenAI auto-span (same thread), the
        # retrieval span, and ALL parallel judge spans share one trace_id —
        # forming one connected tree in Phoenix per suggest run instead of many
        # disconnected top-level traces.
        with observability.root_span(
            "notelinks.suggest",
            {"notelinks.note_id": note.id, "notelinks.note_title": note.title},
        ):
            # 2. Query chunks come from the buffer (unsaved edits count).
            source_chunks = chunk_note(note, self.settings)
            source_embeddings = embed_texts(
                [c.embed_text for c in source_chunks],
                self.settings,
                client=self.embedding_client,
            )
            logger.info(
                "suggest: note=%s (%r) chunked into %d source chunks",
                note.id,
                note.title,
                len(source_chunks),
            )

            # 3. Already-linked targets are never re-suggested. Exclusion is
            #    heading-level (design §8): a link to one heading of a note
            #    suppresses only that heading; a bare whole-note link suppresses
            #    only the note's preamble.
            existing_links = note.links

            # Retrieval wrapped in a (no-op-unless-traced) RETRIEVER span so the
            # vector-search step shows in the same Phoenix trace as the judge calls.
            with observability.retrieval_span(note.title) as span:
                candidates = retrieve_candidates(
                    self.store,
                    source_chunks,
                    source_embeddings,
                    settings=self.settings,
                    current_note_uuid=note.id,
                    existing_links=existing_links,
                )
                span.record_candidates(candidates)
            logger.info(
                "suggest: retrieved %d candidates (filtering headings already "
                "linked by %d existing links)",
                len(candidates),
                len(existing_links),
            )

            # 4. Judge → raw suggestions.
            raw_suggestions = judge_candidates(
                candidates, note, self.store, self.settings, client=self.judge_client
            )
            logger.info("suggest: judge returned %d raw suggestions", len(raw_suggestions))

            # 5. Rank / dedup / cap / invariants.
            suggestions = self._finalize(
                raw_suggestions,
                note_id=note.id,
                existing_links=existing_links,
            )
            logger.info(
                "suggest: %d final suggestions after dedup/cap (top_n=%d)",
                len(suggestions),
                self.settings.top_n,
            )

            # 6. Build the envelope.
            source = Source(
                title=note.title,
                id=note.id,
                queried_at=datetime.now(UTC).isoformat(),
                content_hash="sha256:"
                + hashlib.sha256(buffer_text.encode("utf-8")).hexdigest(),
            )
            return Envelope(
                version=_ENVELOPE_VERSION, source=source, suggestions=suggestions
            )

    # -- helpers -----------------------------------------------------------

    def _finalize(
        self,
        suggestions: list[Suggestion],
        *,
        note_id: str,
        existing_links: list[OrgLink],
    ) -> list[Suggestion]:
        """Drop invalid, dedup, stable-sort by confidence, cap, renumber (design §10).

        Invariants:

        * No self-links (``target.file_id == note_id``).
        * No suggestion that would assemble to a link the note already has
          (:func:`~notelinks.pipeline.exclude.suggestion_duplicates_link`) —
          heading-level, belt-and-suspenders behind the retrieval filter, which
          is authoritative; the judge could still promote a non-excluded chunk to
          a whole-note target that duplicates an existing bare link.

        Dedup: at most one suggestion per ``(target.file_id, heading text|None)``,
        keeping the highest confidence.

        Sort: ``confidence`` descending with a STABLE sort. ``Suggestion`` carries
        no retrieval score, so the *incoming order* (already retrieval-rank ordered
        by the judge) is the tie-break — a deliberate simplification (decision doc).

        Finally cap to ``settings.top_n`` and reassign ``s01``, ``s02``, … ids in
        final order.
        """
        # Drop invalid (self-links + suggestions duplicating an existing link).
        kept: list[Suggestion] = [
            s
            for s in suggestions
            if s.target.file_id != note_id
            and not suggestion_duplicates_link(s.target, existing_links)
        ]

        # Dedup by (target file_id, heading text or None), keeping highest confidence.
        # Iterate in incoming order; on a tie we keep the first-seen (earlier rank).
        best_by_key: dict[tuple[str, str | None], Suggestion] = {}
        for s in kept:
            heading_text = s.target.heading.text if s.target.heading is not None else None
            key = (s.target.file_id, heading_text)
            existing = best_by_key.get(key)
            if existing is None or s.confidence > existing.confidence:
                best_by_key[key] = s
        deduped = list(best_by_key.values())

        # Stable sort by confidence desc; incoming order breaks ties.
        deduped.sort(key=lambda s: s.confidence, reverse=True)

        # Cap, then renumber ids in final order.
        capped = deduped[: self.settings.top_n]
        return [s.model_copy(update={"id": f"s{i:02d}"}) for i, s in enumerate(capped, start=1)]
