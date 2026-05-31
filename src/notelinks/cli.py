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
    """Read the note buffer from stdin and print suggestion JSON to stdout."""
    _enable_observability(verbose, trace)
    buffer_text = sys.stdin.read()
    settings = _build_settings(corpus)
    envelope = Engine(settings).suggest(buffer_text)
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
    stats = Engine(settings).refresh(rebuild=rebuild)
    typer.echo(json.dumps(stats, indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
