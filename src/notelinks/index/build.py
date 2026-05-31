"""Incremental corpus refresh — mtime-gated, hash-confirmed (design §7).

Stateless functions over an injected, long-lived :class:`Store` (and optional
shared embeddings ``client``). The :class:`Engine` (design §2) owns the store
and client and calls :func:`refresh` per request; nothing here holds state of
its own.

Paths are **relative to ``settings.corpus_dir``** everywhere — the manifest key,
``note_path``, and ``source.file`` all use the same repo-relative string. The
filesystem is touched only via ``corpus_dir / rel_path`` for ``stat`` / read.

The refresh algorithm, per ``.org`` file::

    mtime = stat(corpus_dir/rel_path).st_mtime
    row   = store.get_manifest(rel_path)
    if rebuild or row is None:               # forced, or new file
        index_note(rel_path)
    elif mtime > row.last_indexed_mtime:     # touched since last check
        H = sha256(text)
        if H != row.content_hash: index_note(rel_path)   # real change -> reindex
        else: upsert_manifest(rel_path, row.uuid, H, mtime)  # advance watermark
    else:
        skip                                 # untouched — no file read at all

Advancing the watermark when the hash is unchanged but mtime moved means a
touched-but-unchanged file isn't re-hashed on every future run. Deletions:
manifest paths no longer present on disk get their chunks + manifest row removed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from notelinks.config import Settings
from notelinks.index.store import Store
from notelinks.org.chunk import chunk_note
from notelinks.org.parse import parse_note
from notelinks.providers.embeddings import embed_texts

# content_hash format: "sha256:<hex>". The prefix self-documents the algorithm
# and leaves room to migrate hashes later without ambiguity. The manifest stores
# (and compares) this exact string, so the prefix is part of the stored value.
_HASH_PREFIX = "sha256:"


def _content_hash(text: str) -> str:
    """``"sha256:<hexdigest>"`` of the file text (utf-8)."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_HASH_PREFIX}{digest}"


def _corpus_root(settings: Settings) -> Path:
    """The corpus root, or raise if it was never configured."""
    if settings.corpus_dir is None:
        raise ValueError(
            "settings.corpus_dir is None; set NOTELINKS_CORPUS_DIR or pass --corpus."
        )
    return settings.corpus_dir


def index_note(
    rel_path: str,
    store: Store,
    settings: Settings,
    *,
    client=None,
) -> int:
    """(Re)index one note: read, parse, chunk, embed, store, record manifest.

    Reads ``corpus_dir/rel_path``, parses + chunks it, embeds every chunk's
    ``embed_text``, replaces the note's chunks in the store, and records the
    manifest row (uuid, content hash, mtime). Returns the number of chunks
    written. Stateless given the injected ``store`` / ``client``.
    """
    root = _corpus_root(settings)
    abs_path = root / rel_path
    text = abs_path.read_text(encoding="utf-8")
    mtime = abs_path.stat().st_mtime
    content_hash = _content_hash(text)

    note = parse_note(text, rel_path)
    chunks = chunk_note(note, settings)
    embeddings = embed_texts([c.embed_text for c in chunks], settings, client=client)

    # delete_note is idempotent: clears any stale chunks from a prior version
    # (incl. ordinals that no longer exist) before re-adding the current set.
    store.delete_note(note.id)
    store.upsert_chunks(chunks, embeddings)
    store.upsert_manifest(rel_path, note.id, content_hash, mtime)
    return len(chunks)


def refresh(
    store: Store,
    settings: Settings,
    *,
    client=None,
    rebuild: bool = False,
) -> dict:
    """Incrementally refresh the whole corpus against the store (design §7).

    Walks ``corpus_dir`` for ``*.org`` files and, per file, applies the
    mtime-gated / hash-confirmed rule (see module docstring). ``rebuild=True``
    forces a reindex of every file (mtime/hash ignored) and still handles
    deletions. Returns a stats dict::

        {"indexed": n, "reindexed": n, "skipped": n, "deleted": n, "chunks": n}

    where ``indexed`` counts brand-new files, ``reindexed`` counts changed (or
    force-rebuilt) files, ``skipped`` counts untouched / watermark-only files,
    ``deleted`` counts removed files, and ``chunks`` is the total chunks written
    this run. Stateless given the injected ``store`` / ``client``.
    """
    root = _corpus_root(settings)

    stats = {"indexed": 0, "reindexed": 0, "skipped": 0, "deleted": 0, "chunks": 0}

    seen: set[str] = set()
    for abs_path in sorted(root.rglob("*.org")):
        if not abs_path.is_file():
            continue
        rel_path = abs_path.relative_to(root).as_posix()
        seen.add(rel_path)

        mtime = abs_path.stat().st_mtime
        row = store.get_manifest(rel_path)

        if rebuild or row is None:
            stats["chunks"] += index_note(rel_path, store, settings, client=client)
            if row is None:
                stats["indexed"] += 1
            else:
                stats["reindexed"] += 1
        elif mtime > row["last_indexed_mtime"]:
            # Touched since last check — confirm with a content hash before paying
            # to re-embed.
            text = abs_path.read_text(encoding="utf-8")
            content_hash = _content_hash(text)
            if content_hash != row["content_hash"]:
                stats["chunks"] += index_note(rel_path, store, settings, client=client)
                stats["reindexed"] += 1
            else:
                # Same content, newer mtime: advance the watermark so we don't
                # re-hash this file on every future run.
                store.upsert_manifest(rel_path, row["uuid"], content_hash, mtime)
                stats["skipped"] += 1
        else:
            # Untouched (mtime <= watermark): no file read, no work.
            stats["skipped"] += 1

    # Deletions: manifest paths no longer present on disk.
    for rel_path in store.all_manifest_paths():
        if rel_path in seen:
            continue
        row = store.get_manifest(rel_path)
        if row is not None:
            store.delete_note(row["uuid"])
        store.delete_manifest(rel_path)
        stats["deleted"] += 1

    return stats
