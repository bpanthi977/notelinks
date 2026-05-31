"""The transport-agnostic core (design §2).

An :class:`Engine` owns the long-lived state — ONE shared :class:`OpenAI` client
(built once via :func:`notelinks.providers.llm.make_client`, reused for both
embeddings and the judge since both providers share the same OpenRouter client)
and ONE long-lived :class:`~notelinks.index.store.Store` — and exposes the two
verbs the adapters drive:

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
from datetime import UTC, datetime
from pathlib import Path

from notelinks.config import Settings
from notelinks.index import build
from notelinks.index.store import Store
from notelinks.models import Envelope, Source, Suggestion
from notelinks.org.chunk import chunk_note
from notelinks.org.parse import parse_note
from notelinks.pipeline.judge import judge_candidates
from notelinks.pipeline.retrieve import retrieve_candidates
from notelinks.providers import llm
from notelinks.providers.embeddings import embed_texts

_ENVELOPE_VERSION = 1


class Engine:
    """Long-lived core holding the shared provider client + Chroma store."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # ONE shared OpenAI client. Built lazily so an Engine can be constructed
        # (and ``refresh`` driven with a monkeypatched embedder in tests) without
        # an API key. The client is injected into every embedding + judge call.
        self._client = None
        # The Store is the long-lived Chroma handle — opened once, reused.
        self.store = Store(settings)

    @property
    def client(self):
        """The shared OpenAI client, built once on first real provider use.

        Both ``providers/embeddings`` and ``providers/llm`` build an identical
        OpenRouter-backed client, so one handle serves both. Lazily constructed
        so importing/constructing the engine never needs a key (tests that
        monkeypatch ``embed_texts`` / ``complete_structured`` never trigger it).
        """
        if self._client is None:
            self._client = llm.make_client(self.settings)
        return self._client

    def refresh(self, *, rebuild: bool = False) -> dict:
        """Incrementally refresh the corpus index (design §7).

        Delegates to :func:`notelinks.index.build.refresh` with the shared store
        and client. ``rebuild=True`` forces a full reindex. Returns the build
        stats dict (``indexed`` / ``reindexed`` / ``skipped`` / ``deleted`` /
        ``chunks``).
        """
        return build.refresh(
            self.store, self.settings, client=self.client, rebuild=rebuild
        )

    def suggest(self, buffer_text: str, file_path: str) -> Envelope:
        """Run the full pipeline for one note buffer → an :class:`Envelope`.

        Flow (design §8–§11):

        1. Auto-refresh the corpus first (design H) — incremental, so it is cheap
           when nothing changed; this also ensures the on-disk copy of the
           current note is in the index (it is excluded from retrieval by uuid).
        2. Parse the BUFFER (not the on-disk copy) so unsaved edits count. The
           buffer supplies the query chunks; ``content_hash`` covers it verbatim.
        3. Embed the buffer's chunks and retrieve candidates, excluding the
           current note's own chunks and any already-linked targets.
        4. Judge the candidates into raw suggestions.
        5. Rank / dedup / cap / enforce invariants (design §10).
        6. Build and return the wire :class:`Envelope`.
        """
        if self.settings.corpus_dir is None:
            raise ValueError(
                "settings.corpus_dir is None; set NOTELINKS_CORPUS_DIR or pass --corpus."
            )

        # 1. Auto-refresh the corpus (incremental; cheap when nothing changed).
        self.refresh()

        # 2. Parse the buffer. The path is normalised repo-relative to corpus_dir.
        rel_path = self._rel_path(file_path)
        note = parse_note(buffer_text, rel_path)

        # 3. Query chunks come from the buffer (unsaved edits count).
        source_chunks = chunk_note(note, self.settings)
        source_embeddings = embed_texts(
            [c.embed_text for c in source_chunks], self.settings, client=self.client
        )

        # 4. Already-linked targets are never re-suggested.
        exclude_target_uuids = [link.target_uuid for link in note.links]

        candidates = retrieve_candidates(
            self.store,
            source_chunks,
            source_embeddings,
            settings=self.settings,
            current_note_uuid=note.id,
            exclude_target_uuids=exclude_target_uuids,
        )

        # 5. Judge → raw suggestions.
        raw_suggestions = judge_candidates(
            candidates, note, self.store, self.settings, client=self.client
        )

        # 6. Rank / dedup / cap / invariants.
        suggestions = self._finalize(
            raw_suggestions,
            note_id=note.id,
            exclude_target_uuids=set(exclude_target_uuids),
        )

        # 7. Build the envelope.
        source = Source(
            file=rel_path,
            title=note.title,
            id=note.id,
            queried_at=datetime.now(UTC).isoformat(),
            content_hash="sha256:" + hashlib.sha256(buffer_text.encode("utf-8")).hexdigest(),
        )
        return Envelope(version=_ENVELOPE_VERSION, source=source, suggestions=suggestions)

    # -- helpers -----------------------------------------------------------

    def _rel_path(self, file_path: str) -> str:
        """``file_path`` made relative to ``corpus_dir`` (posix), if possible.

        If ``file_path`` is already relative or does not live under the corpus
        root, it is returned as a plain posix string — the parser only needs it
        for the filename-title fallback and ``source.file`` display.
        """
        assert self.settings.corpus_dir is not None  # guarded by caller
        path = Path(file_path)
        root = self.settings.corpus_dir
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            # Not under the corpus root (or already relative) — use as-is.
            return path.as_posix()

    def _finalize(
        self,
        suggestions: list[Suggestion],
        *,
        note_id: str,
        exclude_target_uuids: set[str],
    ) -> list[Suggestion]:
        """Drop invalid, dedup, stable-sort by confidence, cap, renumber (design §10).

        Invariants:

        * No self-links (``target.file_id == note_id``).
        * No suggestion to an already-linked target (``target.file_id`` in the
          exclusion set) — belt-and-suspenders; retrieval already excludes these,
          but the judge could still emit one via target_is_note edge cases.

        Dedup: at most one suggestion per ``(target.file_id, heading text|None)``,
        keeping the highest confidence.

        Sort: ``confidence`` descending with a STABLE sort. ``Suggestion`` carries
        no retrieval score, so the *incoming order* (already retrieval-rank ordered
        by the judge) is the tie-break — a deliberate simplification (decision doc).

        Finally cap to ``settings.top_n`` and reassign ``s01``, ``s02``, … ids in
        final order.
        """
        # Drop invalid (self-links + already-linked targets).
        kept: list[Suggestion] = [
            s
            for s in suggestions
            if s.target.file_id != note_id and s.target.file_id not in exclude_target_uuids
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
