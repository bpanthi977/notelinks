"""Observability — the two CLI flags' machinery (T17, design §11).

Two **independent**, fully optional facilities, both of which send everything to
**stderr** so the ``suggest`` / ``index`` stdout stays pure JSON (the elisp
contract, design §11):

1. ``--verbose`` → :func:`setup_logging`: attach a timestamped
   :class:`logging.StreamHandler` (on ``sys.stderr``) to the ``notelinks``
   logger at ``DEBUG`` when verbose, else ``WARNING``. Pure stdlib; always
   available.

2. ``--trace`` → :func:`setup_tracing`: an OpenTelemetry tracer provider that
   exports to a **separately-run, LOCAL** Arize Phoenix collector, plus
   ``OpenAIInstrumentor().instrument()`` so every judge + embedding OpenAI-SDK
   call is captured automatically (it patches the SDK, so it works through the
   OpenRouter ``base_url``). Phoenix / OpenTelemetry are an **optional extra**
   (``pip install 'notelinks[trace]'`` / ``uv sync --extra trace``); they are
   **lazy-imported inside the functions**, so core never imports them and the
   whole package + test suite runs unchanged with the extra absent.

   :func:`retrieval_span` gives ``engine.suggest`` a context manager that records
   the vector-search step as an OpenInference ``RETRIEVER`` span in the SAME
   trace as the judge calls — and is a **safe no-op** when tracing was never set
   up (so the engine has zero hard dependency on otel and core runs fine without
   the extra).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from notelinks.models import Candidate

# The package-root logger every module logs under (``logging.getLogger(__name__)``
# in submodules => "notelinks.<...>" => children of this).
_ROOT_LOGGER_NAME = "notelinks"

# Default LOCAL Phoenix collector endpoint. Overridable via the env var Phoenix
# itself honours, so a user who already exports PHOENIX_COLLECTOR_ENDPOINT for
# their `phoenix serve` gets it for free.
_DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"
_ENDPOINT_ENV = "PHOENIX_COLLECTOR_ENDPOINT"

# Module-level handle: the active tracer once tracing is set up, else None. The
# no-op span path checks this and never imports otel when it is None.
_TRACER: Any = None


def setup_logging(verbose: bool) -> None:
    """Attach a timestamped stderr handler to the ``notelinks`` logger.

    Idempotent: re-invoking (e.g. across CLI commands in one process) does not
    stack duplicate handlers — the existing notelinks handler is reused and its
    level adjusted. Level is ``DEBUG`` when ``verbose`` else ``WARNING``.

    **stderr only** — the handler writes to ``sys.stderr`` so stdout stays the
    pure-JSON engine↔frontend contract. ``propagate`` is left False so these
    records never reach the root logger / a foreign basicConfig handler.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    handler = _notelinks_handler(logger)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.set_name("notelinks-stderr")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    handler.setLevel(level)


def _notelinks_handler(logger: logging.Logger) -> logging.StreamHandler | None:
    """Our previously-installed stderr handler, if any (keeps setup idempotent)."""
    for h in logger.handlers:
        if getattr(h, "name", None) == "notelinks-stderr":
            return h  # type: ignore[return-value]
    return None


def setup_tracing(endpoint: str | None = None) -> None:
    """Register an OTel provider exporting to a LOCAL Phoenix + instrument OpenAI.

    Lazy-imports phoenix / opentelemetry / openinference **inside** this function
    so importing :mod:`notelinks.observability` (and running the engine) never
    requires the ``trace`` extra. When the extra is absent we raise a clear,
    actionable :class:`RuntimeError` (how to install it, and that ``--verbose``
    works without it).

    Exports to a **separately-run** Phoenix collector (default
    ``http://localhost:6006``, overridable via ``endpoint`` or the
    ``PHOENIX_COLLECTOR_ENDPOINT`` env var). We deliberately do NOT launch an
    in-process Phoenix server: that would be torn down the instant the
    short-lived CLI process exits, before the UI could read the spans.

    After registering the provider we call ``OpenAIInstrumentor().instrument()``,
    which patches the OpenAI SDK — so every judge + embedding call (all routed
    through the one OpenRouter-backed client) is captured with no per-call code.
    Sets the module-level ``_TRACER`` so :func:`retrieval_span` becomes live.
    """
    global _TRACER

    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor
        from phoenix.otel import register
    except ImportError as exc:  # the trace extra is not installed.
        raise RuntimeError(
            "Tracing requires the optional 'trace' extra, which is not installed. "
            "Install it with:  uv sync --extra trace   "
            "(or: pip install 'notelinks[trace]'). "
            "Note: --verbose stderr logging works WITHOUT the trace extra."
        ) from exc

    collector = endpoint or os.environ.get(_ENDPOINT_ENV) or _DEFAULT_PHOENIX_ENDPOINT
    # phoenix.otel.register wires a TracerProvider with an OTLP span exporter
    # pointed at the Phoenix collector and sets it as the global provider.
    #   verbose=False : Phoenix's default register() prints a config banner to
    #     STDOUT — that would corrupt the pure-JSON contract. We silence it and
    #     emit our own one-line hint to STDERR from the CLI instead.
    #   batch=True : a BatchSpanProcessor flushes asynchronously, so per-call
    #     export does not block the pipeline (and noisy connect-retry logs, when
    #     no Phoenix is running, stay off the critical path — and on stderr).
    provider = register(
        endpoint=f"{collector.rstrip('/')}/v1/traces",
        project_name="notelinks",
        batch=True,
        set_global_tracer_provider=True,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    _TRACER = provider.get_tracer("notelinks")


def tracing_endpoint(endpoint: str | None = None) -> str:
    """The collector endpoint that :func:`setup_tracing` would use (for hints)."""
    return endpoint or os.environ.get(_ENDPOINT_ENV) or _DEFAULT_PHOENIX_ENDPOINT


@contextlib.contextmanager
def retrieval_span(query: str) -> Iterator[_RetrievalSpan]:
    """A RETRIEVER span around the vector-search step — no-op if tracing is off.

    When :func:`setup_tracing` has not run (``_TRACER is None``, e.g. the extra
    is absent or ``--trace`` was not passed) this yields an inert recorder and
    **imports nothing** from otel — the engine has zero hard otel dependency.

    When tracing is live it opens an OpenInference ``RETRIEVER`` span, records the
    query, and (via :meth:`_RetrievalSpan.record_candidates`) attaches the
    retrieved candidates with their cosine scores as ``retrieval.documents`` —
    so the vector-search step appears in the same trace as the judge LLM calls.
    """
    if _TRACER is None:
        yield _RetrievalSpan(None)
        return

    # Lazy: only import the semantic-convention names when actually tracing.
    from openinference.semconv.trace import (
        OpenInferenceSpanKindValues,
        SpanAttributes,
    )

    with _TRACER.start_as_current_span("retrieve_candidates") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.RETRIEVER.value,
        )
        span.set_attribute(SpanAttributes.INPUT_VALUE, query)
        yield _RetrievalSpan(span)


class _RetrievalSpan:
    """Thin recorder wrapping the optional OTel span (or nothing, when no-op)."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def record_candidates(self, candidates: list[Candidate]) -> None:
        """Attach retrieved candidates as RETRIEVER documents (+ cosine scores).

        No-op when the span is inert. Uses the OpenInference document conventions
        (``retrieval.documents.{i}.document.{content,score,id}``) so Phoenix
        renders each candidate target chunk with its similarity.
        """
        if self._span is None:
            return
        from openinference.semconv.trace import (
            DocumentAttributes,
            SpanAttributes,
        )

        prefix = SpanAttributes.RETRIEVAL_DOCUMENTS
        for i, cand in enumerate(candidates):
            tc = cand.target_chunk
            self._span.set_attribute(
                f"{prefix}.{i}.{DocumentAttributes.DOCUMENT_ID}", tc.chunk_id
            )
            self._span.set_attribute(
                f"{prefix}.{i}.{DocumentAttributes.DOCUMENT_CONTENT}", tc.text
            )
            self._span.set_attribute(
                f"{prefix}.{i}.{DocumentAttributes.DOCUMENT_SCORE}", cand.score
            )


def _reset_tracing_for_tests() -> None:
    """Test hook: clear the module tracer so the no-op path is exercised."""
    global _TRACER
    _TRACER = None
