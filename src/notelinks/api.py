"""FastAPI adapter — the daemon transport (design §2, T19).

A THIN adapter over the same long-lived :class:`~notelinks.engine.Engine` the CLI
drives, exposed over HTTP so a client (the org-roam frontend) gets suggestions
from a warm process instead of paying cold-start + a full index walk on every
keystroke-driven query.

Design notes
------------

* **Optional ``[server]`` extra.** FastAPI / uvicorn / watchfiles are NOT core
  dependencies; this module is imported only from ``cli.serve`` (lazily) and the
  API tests (guarded by ``pytest.importorskip``), so the core install and the
  default test run never import them — same pattern as the ``trace`` extra.
* **One Engine, held in app state.** The lifespan constructs a single
  :class:`Engine`, does an **initial refresh**, then launches a
  :func:`watchfiles.awatch` task that runs a **debounced incremental refresh**
  whenever ``.org`` files under the corpus change. The task is cancelled on
  shutdown.
* **Single lock.** One :class:`threading.Lock` serializes ALL engine access —
  the watcher's refresh (write), ``POST /refresh`` (write), and ``POST /suggest``
  (read). Suggest is now a pure query (it does not refresh itself), so a query
  reads a consistent index and never races a concurrent write. The endpoints are
  plain ``def`` (sync), so FastAPI runs them in its threadpool and the blocking
  lock acquire never stalls the event loop; the watcher runs ``refresh`` via
  :func:`anyio.to_thread.run_sync` so it, too, holds the lock off the loop.
* **Localhost, no auth.** Bound to ``127.0.0.1`` by ``cli.serve``; this is a
  single-user local tool, so there is no authentication layer.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio
from fastapi import FastAPI
from pydantic import BaseModel
from watchfiles import awatch

from notelinks.engine import Engine
from notelinks.models import Envelope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from notelinks.config import Settings

logger = logging.getLogger(__name__)

# Debounce window: collect filesystem events for this long (ms) and refresh once.
# watchfiles already batches; this just caps how often we walk the corpus when a
# burst of saves arrives (e.g. a multi-file checkout).
_DEBOUNCE_MS = 400


class SuggestRequest(BaseModel):
    """``POST /suggest`` body: the current note buffer (org text)."""

    buffer: str


class RefreshRequest(BaseModel):
    """``POST /refresh`` body: ``rebuild`` forces a full reindex."""

    rebuild: bool = False


class _State:
    """Per-app state: the long-lived engine + the single serializing lock.

    ``lock`` guards EVERY engine call (refresh from the watcher or the endpoint,
    and suggest). ``last_refresh`` records the most recent successful refresh for
    ``GET /status``.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.lock = threading.Lock()
        self.last_refresh: str | None = None

    def refresh(self, *, rebuild: bool = False) -> dict:
        """Refresh under the lock and stamp ``last_refresh`` (UTC ISO-8601)."""
        with self.lock:
            stats = self.engine.refresh(rebuild=rebuild)
            self.last_refresh = datetime.now(UTC).isoformat()
            return stats

    def suggest(self, buffer_text: str) -> Envelope:
        """Pure query under the lock (read; serialized against refresh writes)."""
        with self.lock:
            return self.engine.suggest(buffer_text)

    def status(self) -> dict:
        """Index summary: note count, chunk count, last-refresh timestamp."""
        with self.lock:
            store = self.engine.store
            notes = len(store.all_manifest_paths())
            # The chunk collection has no public counter on Store; the adapter
            # reads it directly (read-only) for the status summary.
            chunks = store._chunks.count()
        return {"notes": notes, "chunks": chunks, "last_refresh": self.last_refresh}


async def _watch_corpus(state: _State, corpus_dir: str) -> None:
    """Background task: debounced incremental refresh on ``.org`` changes.

    ``awatch`` yields batches of change events (it coalesces a burst into one
    batch). For each batch we run ``state.refresh`` off the event loop via
    :func:`anyio.to_thread.run_sync` so acquiring the (blocking) lock and walking
    the corpus never stalls the loop and never races a concurrent query. Errors
    are logged and swallowed so a transient failure (e.g. a half-written file)
    does not kill the watcher.
    """
    logger.info("watcher: watching %s for .org changes", corpus_dir)
    async for changes in awatch(corpus_dir, debounce=_DEBOUNCE_MS):
        if not any(path.endswith(".org") for _, path in changes):
            continue
        logger.info("watcher: %d change(s) detected; refreshing", len(changes))
        try:
            stats = await anyio.to_thread.run_sync(state.refresh)
            logger.info("watcher: refresh done (%s)", stats)
        except Exception:  # noqa: BLE001 — keep the watcher alive across failures
            logger.exception("watcher: refresh failed; continuing")


def build_app(settings: Settings) -> FastAPI:
    """Build the FastAPI app over one long-lived :class:`Engine`.

    The lifespan constructs the engine, does the initial refresh, and starts the
    corpus watcher; on shutdown it cancels the watcher. All endpoints share the
    single lock in :class:`_State`.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = Engine(settings)
        state = _State(engine)
        app.state.notelinks = state

        # Initial refresh: build the index once at startup (off the event loop).
        await anyio.to_thread.run_sync(state.refresh)

        watcher_task = None
        if settings.corpus_dir is not None:
            import asyncio

            watcher_task = asyncio.create_task(
                _watch_corpus(state, str(settings.corpus_dir))
            )
        try:
            yield
        finally:
            if watcher_task is not None:
                watcher_task.cancel()
                try:
                    await watcher_task
                except BaseException:  # noqa: BLE001 — swallow CancelledError on shutdown
                    pass

    app = FastAPI(title="notelinks", lifespan=lifespan)

    def _state() -> _State:
        return app.state.notelinks

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    def status() -> dict[str, Any]:
        return _state().status()

    @app.post("/refresh")
    def refresh(req: RefreshRequest) -> dict:
        return _state().refresh(rebuild=req.rebuild)

    @app.post("/suggest")
    def suggest(req: SuggestRequest) -> Envelope:
        return _state().suggest(req.buffer)

    return app
