"""Typer adapter — the v1 transport (design §2, §11).

A THIN adapter: it builds a :class:`~notelinks.config.Settings`, constructs one
:class:`~notelinks.engine.Engine`, drives ``refresh()`` / ``suggest()``, and
prints JSON. All real work (and all long-lived state) lives in the engine.

Commands (design §11):

* ``suggest [--corpus DIR]`` — buffer on **stdin**: incremental refresh of the
  whole corpus → query the current buffer → emit the ``Envelope`` JSON. The note
  is identified by its own ``:ID:`` in the buffer; no file path is taken.
* ``index [--rebuild] [--corpus DIR]`` — first / forced full build; prints stats.

The corpus root comes from ``NOTELINKS_CORPUS_DIR`` (via ``Settings``),
overridable by ``--corpus``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from notelinks import observability
from notelinks.config import Settings
from notelinks.engine import Engine

app = typer.Typer(help="Suggest idea-resonance links for an org-roam note.")

# Width of the drawn bar (the "[####----]" part), in characters.
_BAR_WIDTH = 30


def _make_progress_bar():
    """An ``index``-progress callback that draws a bar on **stderr**, or ``None``.

    Returns a ``progress(done, total, rel_path)`` closure suitable for
    ``Engine.refresh(progress=...)`` — but only when stderr is an interactive TTY.
    When stderr is redirected/piped (the elisp subprocess, ``2>file``, CI) it
    returns ``None`` so no carriage-return spam pollutes captured logs; stdout
    stays the pure-JSON contract regardless. The bar is rewritten in place with
    ``\\r`` and cleared with a final newline on completion.
    """
    if not sys.stderr.isatty():
        return None

    def _progress(done: int, total: int, rel_path: str) -> None:
        total = max(total, 1)
        filled = int(_BAR_WIDTH * done / total)
        bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
        # Truncate the filename so the line never wraps and smears the bar.
        name = rel_path if len(rel_path) <= 30 else "…" + rel_path[-29:]
        end = "\n" if done >= total else ""
        print(
            f"\r[notelinks] indexing [{bar}] {done}/{total} {name:<30}",
            end=end,
            file=sys.stderr,
            flush=True,
        )

    return _progress

# Shared option types (Annotated style — keeps the typer.Option call out of the
# default position, so ruff's B008 false-positive never fires).
_CorpusOpt = Annotated[
    Path | None,
    typer.Option("--corpus", help="Corpus root (overrides NOTELINKS_CORPUS_DIR)."),
]
_RebuildOpt = Annotated[
    bool, typer.Option("--rebuild", help="Force a full reindex.")
]
_VerboseOpt = Annotated[
    bool,
    typer.Option(
        "--verbose", "-v", help="Timestamped DEBUG logging to stderr (stdout stays JSON)."
    ),
]
_TraceOpt = Annotated[
    bool,
    typer.Option(
        "--trace",
        help="Export OpenTelemetry traces to a local Phoenix collector "
        "(needs the 'trace' extra + a running `phoenix serve`).",
    ),
]
_HostOpt = Annotated[
    str, typer.Option("--host", help="Bind address (localhost-only by default).")
]
_PortOpt = Annotated[int, typer.Option("--port", help="TCP port to listen on.")]


def _enable_observability(verbose: bool, trace: bool) -> None:
    """Set up logging (always) then tracing (only with --trace) — both to stderr.

    Logging is configured first thing so even the trace-setup errors are logged.
    When ``--trace`` is on we print a one-line hint to **stderr** (never stdout)
    naming the collector endpoint + the command to view it locally.
    """
    observability.setup_logging(verbose)
    if trace:
        observability.setup_tracing()
        endpoint = observability.tracing_endpoint()
        typer.echo(
            f"[notelinks] tracing -> {endpoint} "
            f"(run a local Phoenix to view: `phoenix serve`, then open {endpoint})",
            err=True,
        )


def _build_settings(corpus: Path | None) -> Settings:
    """Load ``Settings`` from env / ``.env``, overriding ``corpus_dir`` if given.

    ``--corpus`` is passed as an init arg (highest precedence) and re-runs full
    construction, so a derived ``index_dir`` follows the overridden corpus —
    unlike ``model_copy``, which would skip the validator and leave it stale.
    """
    if corpus is not None:
        return Settings(corpus_dir=corpus)
    return Settings()


@app.command()
def suggest(
    corpus: _CorpusOpt = None,
    verbose: _VerboseOpt = False,
    trace: _TraceOpt = False,
) -> None:
    """Read the note buffer from stdin and print suggestion JSON to stdout.

    One-shot: ``Engine.suggest`` is a pure query (it no longer auto-refreshes —
    T19), so this adapter refreshes the corpus index FIRST (incremental, cheap
    when nothing changed) and then queries, preserving the original CLI
    behaviour. The long-lived server adapter (``api.py``) keeps the index fresh
    differently — an initial refresh plus a file watcher.
    """
    _enable_observability(verbose, trace)
    buffer_text = sys.stdin.read()
    settings = _build_settings(corpus)
    engine = Engine(settings)
    engine.refresh(progress=_make_progress_bar())
    envelope = engine.suggest(buffer_text)
    typer.echo(envelope.model_dump_json(indent=2))


@app.command()
def index(
    rebuild: _RebuildOpt = False,
    corpus: _CorpusOpt = None,
    verbose: _VerboseOpt = False,
    trace: _TraceOpt = False,
) -> None:
    """Refresh (or rebuild) the corpus index; print the build stats."""
    _enable_observability(verbose, trace)
    settings = _build_settings(corpus)
    stats = Engine(settings).refresh(rebuild=rebuild, progress=_make_progress_bar())
    typer.echo(json.dumps(stats, indent=2))


@app.command()
def serve(
    host: _HostOpt = "127.0.0.1",
    port: _PortOpt = 8765,
    corpus: _CorpusOpt = None,
    verbose: _VerboseOpt = False,
    trace: _TraceOpt = False,
) -> None:
    """Run the daemon: a FastAPI app over one long-lived ``Engine`` (T19).

    Constructs ``Settings`` (``--corpus`` overrides ``NOTELINKS_CORPUS_DIR``),
    sets up logging/tracing via the same observability helpers as the one-shot
    commands, then serves the FastAPI app under uvicorn bound to ``host``/``port``
    (``127.0.0.1`` by default — single-user local tool, no auth).

    The server lives behind the optional ``[server]`` extra (FastAPI + uvicorn +
    watchfiles); it is lazy-imported here so the core install and the default
    test run never need it. When the extra is absent we exit with an actionable
    message instead of a raw ``ImportError``.
    """
    _enable_observability(verbose, trace)
    try:
        import uvicorn

        from notelinks.api import build_app
    except ImportError as exc:
        typer.echo(
            "[notelinks] `serve` needs the optional 'server' extra "
            "(FastAPI + uvicorn + watchfiles), which is not installed. "
            "Install it with:  uv sync --extra server   "
            "(or: pip install 'notelinks[server]').",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    settings = _build_settings(corpus)
    app_ = build_app(settings)
    typer.echo(f"[notelinks] serving on http://{host}:{port} (Ctrl-C to stop)", err=True)
    uvicorn.run(app_, host=host, port=port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    app()
