"""Tests for the T17 observability machinery (logging + optional tracing).

All offline: NO network, NO API key, and the ``trace`` extra is assumed ABSENT
(the suite must be green without it). We assert:

* ``setup_logging(verbose=True)`` installs a single stderr ``StreamHandler`` on
  the ``notelinks`` logger at ``DEBUG``; ``verbose=False`` => ``WARNING``; and
  re-invocation does not stack duplicate handlers.
* :func:`retrieval_span` is a no-op (yields, records nothing, imports no otel)
  when tracing was never set up.
* :func:`setup_tracing` raises a clear, actionable error when the extra is
  absent (simulated by forcing the phoenix import to fail).
"""

from __future__ import annotations

import builtins
import logging
import sys

import pytest

from notelinks import observability


@pytest.fixture(autouse=True)
def _clean_notelinks_logger():
    """Snapshot/restore the notelinks logger + reset the module tracer per test."""
    logger = logging.getLogger("notelinks")
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    saved_propagate = logger.propagate
    observability._reset_tracing_for_tests()
    yield
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)
    logger.propagate = saved_propagate
    observability._reset_tracing_for_tests()


def _notelinks_handlers():
    return [
        h
        for h in logging.getLogger("notelinks").handlers
        if getattr(h, "name", None) == "notelinks-stderr"
    ]


def test_setup_logging_verbose_installs_debug_stderr_handler():
    observability.setup_logging(verbose=True)

    logger = logging.getLogger("notelinks")
    assert logger.level == logging.DEBUG

    handlers = _notelinks_handlers()
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    # The handler targets the stderr that was live when it was created. pytest
    # may reassign sys.stderr for capture, so assert it is a stderr-like stream
    # rather than object-identity to the *current* sys.stderr.
    assert handler.stream.name == "<stderr>" or handler.stream is sys.stderr
    assert handler.level == logging.DEBUG
    # Timestamped formatter (asctime present in the format string).
    assert "asctime" in handler.formatter._fmt


def test_setup_logging_nonverbose_is_warning():
    observability.setup_logging(verbose=False)
    logger = logging.getLogger("notelinks")
    assert logger.level == logging.WARNING
    assert _notelinks_handlers()[0].level == logging.WARNING


def test_setup_logging_is_idempotent():
    observability.setup_logging(verbose=True)
    observability.setup_logging(verbose=False)
    # Only one notelinks handler even after two calls; level reflects the last.
    assert len(_notelinks_handlers()) == 1
    assert _notelinks_handlers()[0].level == logging.WARNING


def test_verbose_logging_emits_debug_records():
    # propagate=False means caplog's root handler won't see these; capture via a
    # stream handler attached to the notelinks logger instead.
    import io

    observability.setup_logging(verbose=True)
    logger = logging.getLogger("notelinks")
    buf = io.StringIO()
    probe = logging.StreamHandler(buf)
    probe.setLevel(logging.DEBUG)
    logger.addHandler(probe)
    try:
        logging.getLogger("notelinks.engine").debug("hello %s", "world")
    finally:
        logger.removeHandler(probe)
    assert "hello world" in buf.getvalue()


def test_retrieval_span_is_noop_when_tracing_not_setup():
    # Tracing was never set up (the fixture reset _TRACER to None). Entering the
    # span and recording candidates must not raise and must import no otel.
    assert observability._TRACER is None
    with observability.retrieval_span("a query") as span:
        span.record_candidates([])  # empty is fine; the point is no-op safety
    # No tracer was created as a side effect.
    assert observability._TRACER is None


def test_retrieval_span_record_candidates_noop_with_fake_candidate():
    # Even with a non-empty list, the no-op recorder must not touch attributes
    # (there is no span object). A trivial duck-typed candidate suffices.
    class _TC:
        chunk_id = "u:0"
        text = "body"

    class _Cand:
        target_chunk = _TC()
        score = 0.9

    with observability.retrieval_span("q") as span:
        span.record_candidates([_Cand()])  # must simply do nothing


def test_setup_tracing_without_extra_raises_clear_error(monkeypatch):
    """Simulate the trace extra being absent -> actionable RuntimeError."""
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name.startswith("phoenix") or name.startswith("openinference"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with pytest.raises(RuntimeError) as excinfo:
        observability.setup_tracing()

    msg = str(excinfo.value)
    assert "trace" in msg
    assert "uv sync --extra trace" in msg
    assert "--verbose" in msg  # tells the user logging works without the extra


def test_tracing_endpoint_default_and_env(monkeypatch):
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    assert observability.tracing_endpoint() == "http://localhost:6006"
    assert observability.tracing_endpoint("http://x:1234") == "http://x:1234"
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://env:9999")
    assert observability.tracing_endpoint() == "http://env:9999"


# ---------------------------------------------------------------------------
# Unified-trace helpers: root_span / context propagation / judge_span no-ops.
# These must be safe no-ops AND import zero otel when tracing was never set up.
# ---------------------------------------------------------------------------


def _block_otel_imports(monkeypatch):
    """Force any import of opentelemetry/phoenix/openinference to fail.

    Lets a test prove the no-op paths import NO otel: if a helper tried to import
    it on the inactive path, the import would raise and the test would fail.
    """
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if (
            name.startswith("opentelemetry")
            or name.startswith("phoenix")
            or name.startswith("openinference")
        ):
            raise ImportError(f"blocked import of {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


def test_root_span_is_noop_when_tracing_not_setup(monkeypatch):
    assert observability._TRACER is None
    _block_otel_imports(monkeypatch)
    # Yields None, records the attributes safely, imports no otel.
    with observability.root_span("notelinks.suggest", {"notelinks.note_id": "X"}) as span:
        assert span is None
    # No attributes / None-attributes variant also fine.
    with observability.root_span("notelinks.suggest") as span:
        assert span is None
    assert observability._TRACER is None


def test_current_context_is_none_when_tracing_off(monkeypatch):
    assert observability._TRACER is None
    _block_otel_imports(monkeypatch)
    assert observability.current_context() is None


def test_context_propagating_wrapper_is_identity_when_off(monkeypatch):
    assert observability._TRACER is None
    _block_otel_imports(monkeypatch)

    def fn(a, b):
        return a + b

    # ctx is None (tracing off) => returns fn UNCHANGED (identity), no otel import.
    wrapped = observability.context_propagating_wrapper(fn, None)
    assert wrapped is fn
    assert wrapped(2, 3) == 5


def test_judge_span_is_noop_when_tracing_off(monkeypatch):
    assert observability._TRACER is None
    _block_otel_imports(monkeypatch)
    with observability.judge_span("SRC-UUID:0") as span:
        assert span is None
