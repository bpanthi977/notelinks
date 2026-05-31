# T11 — `src/notelinks/index/build.py`

Incremental corpus refresh (design §7), as **stateless functions** over an
injected long-lived `Store` (design §2). The `Engine` owns the store + the
shared embeddings `client`; nothing here holds state of its own.

## API

```python
def index_note(rel_path: str, store: Store, settings: Settings, *, client=None) -> int
def refresh(store: Store, settings: Settings, *, client=None, rebuild: bool = False) -> dict
```

- `index_note` (re)indexes one note and returns the number of chunks written.
- `refresh` walks the corpus and returns a stats dict
  `{"indexed", "reindexed", "skipped", "deleted", "chunks"}`.

## Refresh algorithm (mtime-gated, hash-confirmed)

Per `*.org` file (design §7 pseudocode):

```
mtime = stat(corpus_dir/rel_path).st_mtime
row   = store.get_manifest(rel_path)
if rebuild or row is None:               # forced, or new file
    index_note(rel_path)                 # -> indexed (new) or reindexed (rebuild)
elif mtime > row.last_indexed_mtime:     # touched since last check
    H = sha256(text)
    if H != row.content_hash: index_note(rel_path)        # real change
    else: upsert_manifest(rel_path, row.uuid, H, mtime)   # advance watermark
else:
    skip                                 # untouched — no file read at all
```

The `else` branch (`mtime <= watermark`) does **no filesystem read** beyond the
`stat` — the fast path stays stat-only, which is the whole point of keying the
manifest by path.

## Path / relpath convention

Paths are **relative to `settings.corpus_dir`**, as POSIX strings
(`Path.relative_to(root).as_posix()`), used identically as the manifest key,
`note_path`, and `source.file`. The filesystem is touched only via
`corpus_dir / rel_path` for `stat`/read. The corpus is walked with
`root.rglob("*.org")` (recursive; nested dirs like `sub/beta.org` are included),
sorted for deterministic ordering, and non-files are skipped.

`corpus_dir is None` raises `ValueError` (the CLI is responsible for resolving
it from `--corpus` / `NOTELINKS_CORPUS_DIR` before calling refresh).

## Hash format

`content_hash = "sha256:" + sha256(text.encode("utf-8")).hexdigest()`. The
`"sha256:"` prefix is part of the **stored** string and is what gets compared,
so it self-documents the algorithm and leaves room to migrate later. The
manifest stores it verbatim; comparison is a plain string equality against
`row["content_hash"]`.

## Watermark-advance rule

When a file's mtime moved past the watermark but its content hash is **unchanged**
(e.g. `touch`, a save with no edits, a checkout that rewrites mtimes), we
`upsert_manifest(rel_path, row.uuid, H, mtime)` to advance `last_indexed_mtime`
to the new mtime **without** re-embedding. This is REQUIRED so the file isn't
re-hashed on every subsequent run — the next run sees `mtime <= watermark` and
skips with a pure stat. Such a file counts as `skipped` (no re-embed happened).

## Rebuild semantics

`rebuild=True` forces `index_note` for every file on disk, ignoring mtime/hash
entirely. Pre-existing files count as `reindexed` (only brand-new ones, i.e.
`row is None`, count as `indexed`). Deletions are still swept afterward, so a
rebuild also reconciles files that vanished from disk.

## Deletion handling

After the on-disk walk, every `store.all_manifest_paths()` entry **not** seen on
disk this run is removed: `delete_note(row.uuid)` clears its chunks, then
`delete_manifest(rel_path)` drops the row; each increments `deleted`.

## `index_note` details

Reads the file, parses (`parse_note(text, rel_path)`), chunks
(`chunk_note(note, settings)`), embeds `[c.embed_text for c in chunks]` via the
injected `client`, then `store.delete_note(note.id)` (idempotent — clears any
stale chunks/ordinals from a prior version) **before** `store.upsert_chunks`,
and finally records the manifest row. Returns `len(chunks)`. A note that chunks
to zero chunks still gets a manifest row (so it won't be re-read every run) and
contributes 0 to `chunks`.

## Inject-store/client design

Both functions are stateless given the injected `store` and `client` (design §2:
no per-call singletons; Chroma + provider clients constructed once and reused).
`client` is threaded straight through to `embed_texts` so the `Engine`'s shared
`OpenAI` client is used; when omitted, `embed_texts` builds one lazily (handy for
one-offs, never for the hot path).

## Tests (`tests/test_build.py`)

Fully offline: a real `Store` on a temp dir + `embed_texts` monkeypatched on
`notelinks.index.build` with a deterministic fake (`EmbedSpy`) that records call
count. Sockets are hard-blocked to prove no network. Covers: (1) first refresh
indexes all 3 files (stats + chunks in store + manifest paths + `sha256:` hash);
(2) re-run re-embeds nothing (`spy.calls == 0`, all skipped); (3) edit+`os.utime`
re-embeds only that file; (4) `os.utime` with identical content advances the
watermark with no re-embed (and the following run also re-hashes nothing);
(5) deleting a file removes its chunks AND manifest row; (6) `rebuild=True`
reindexes everything; plus rebuild-still-sweeps-deletions.
