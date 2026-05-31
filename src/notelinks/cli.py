"""Typer adapter — the v1 transport (design §2, §11).

A THIN adapter: it builds a :class:`~notelinks.config.Settings`, constructs one
:class:`~notelinks.engine.Engine`, drives ``refresh()`` / ``suggest()``, and
prints JSON. All real work (and all long-lived state) lives in the engine.

Commands (design §11):

* ``suggest <file> [--corpus DIR]`` — buffer on **stdin**: incremental refresh of
  the whole corpus → query the current buffer → emit the ``Envelope`` JSON.
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

from notelinks.config import Settings
from notelinks.engine import Engine

app = typer.Typer(help="Suggest idea-resonance links for an org-roam note.")

# Shared option types (Annotated style — keeps the typer.Option call out of the
# default position, so ruff's B008 false-positive never fires).
_FileArg = Annotated[
    str, typer.Argument(help="Path of the current note (repo-relative ok).")
]
_CorpusOpt = Annotated[
    Path | None,
    typer.Option("--corpus", help="Corpus root (overrides NOTELINKS_CORPUS_DIR)."),
]
_RebuildOpt = Annotated[
    bool, typer.Option("--rebuild", help="Force a full reindex.")
]


def _build_settings(corpus: Path | None) -> Settings:
    """Load ``Settings`` from env / ``.env``, overriding ``corpus_dir`` if given."""
    settings = Settings()
    if corpus is not None:
        settings = settings.model_copy(update={"corpus_dir": corpus})
    return settings


@app.command()
def suggest(file: _FileArg, corpus: _CorpusOpt = None) -> None:
    """Read the note buffer from stdin and print suggestion JSON to stdout."""
    buffer_text = sys.stdin.read()
    settings = _build_settings(corpus)
    envelope = Engine(settings).suggest(buffer_text, file)
    typer.echo(envelope.model_dump_json(indent=2))


@app.command()
def index(rebuild: _RebuildOpt = False, corpus: _CorpusOpt = None) -> None:
    """Refresh (or rebuild) the corpus index; print the build stats."""
    settings = _build_settings(corpus)
    stats = Engine(settings).refresh(rebuild=rebuild)
    typer.echo(json.dumps(stats, indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
