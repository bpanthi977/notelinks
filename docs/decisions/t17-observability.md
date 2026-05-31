# T17 — Observability

Two CLI flags add visibility without ever touching the stdout JSON contract
(design §11). Default behaviour (no flags) is byte-for-byte unchanged.

## The two flags

Both `suggest` and `index` accept:

- **`--verbose` / `-v`** — timestamped logging to **stderr**. Always available;
  pure stdlib `logging`, no extra dependency.
- **`--trace`** — export OpenTelemetry traces to a **local** Arize Phoenix
  collector. Requires the optional `trace` extra + a separately-run Phoenix.

`cli._enable_observability(verbose, trace)` runs first thing in each command:
`setup_logging(verbose)` always, then `setup_tracing()` only when `--trace`.

## `--verbose`: stderr logging

`observability.setup_logging(verbose)`:

- Attaches **one** `StreamHandler(sys.stderr)` to the `notelinks` logger with a
  timestamped `Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")`.
- Level `DEBUG` when verbose, else `WARNING`.
- **stderr only** — stdout stays the pure-JSON engine↔frontend contract.
- `propagate=False` so records never reach a foreign root handler / basicConfig.
- **Idempotent**: the handler is named `notelinks-stderr` and reused on
  re-invocation (no duplicate-handler stacking across commands in one process).

Log lines added (modest, not a full trace):

- `engine.refresh` — INFO: the `indexed/reindexed/skipped/deleted/chunks` stats.
- `engine.suggest` — INFO at each stage: source-chunk count, candidates
  retrieved, raw judge suggestions, final count after dedup/cap.
- `pipeline/judge` — INFO: candidate count + number of source-chunk calls;
  DEBUG per group: target count, suggestions returned, and call timing
  (`time.perf_counter`).
- Existing WARNING/EXCEPTION lines (transient LLM retries, judge-group failure)
  are unchanged and now surface through the same handler.

## `--trace`: local Phoenix tracing

### Optional extra + lazy import (core stays untouched)

The Phoenix / OpenTelemetry packages are an **optional extra**, NOT core deps
(`pyproject.toml [project.optional-dependencies] trace`):

- `arize-phoenix-otel>=0.16.1` — `phoenix.otel.register` (provider + OTLP exporter wiring)
- `openinference-instrumentation-openai>=0.1.49` — `OpenAIInstrumentor`
- `openinference-semantic-conventions>=0.1.21` — `SpanAttributes` / `DocumentAttributes`
- `arize-phoenix>=16.3.0` — so `phoenix serve` runs locally
- `opentelemetry-exporter-otlp>=1.30.0` — OTLP/HTTP span exporter

`observability.setup_tracing()` **lazy-imports** phoenix/openinference *inside*
the function. The module itself imports nothing from them, so importing
`notelinks.observability` (and running the whole engine + test suite) works with
the extra absent. If the import fails we raise a clear, actionable `RuntimeError`
telling the user to `uv sync --extra trace` and that `--verbose` works without
the extra.

### Why export to a local Phoenix, not an in-process launch

`setup_tracing` registers a tracer provider that **exports over OTLP/HTTP to a
separately-run Phoenix collector** (default `http://localhost:6006`, overridable
via the `--trace` path / `PHOENIX_COLLECTOR_ENDPOINT` env var). We deliberately
do **not** launch an in-process Phoenix server: the CLI is a short-lived process,
so an embedded server would be torn down the instant it exits — before the UI
could read the spans. Exporting to a persistent local Phoenix keeps the trace
viewable after the command returns.

`register(..., batch=True, verbose=False)`:
- `batch=True` — a BatchSpanProcessor flushes asynchronously so per-call export
  never blocks the pipeline, and connect-retry noise (no Phoenix running) stays
  off the critical path.
- `verbose=False` — Phoenix's default `register()` prints a config banner to
  **stdout**, which would corrupt the JSON contract. We silence it and print our
  own one-line hint to **stderr** from the CLI instead.

`OpenAIInstrumentor().instrument()` patches the OpenAI **SDK**, so every judge
and embedding call — all routed through the one OpenRouter-backed client — is
captured automatically, with no per-call code and working through the OpenRouter
`base_url`.

### The retrieval span (no-op-when-off)

The vector-search step is not an OpenAI SDK call, so it would otherwise be
invisible. `engine.suggest` wraps `retrieve_candidates` in
`observability.retrieval_span(query)`, a context manager that:

- is a **safe no-op** when tracing was never set up (`_TRACER is None`): it
  yields an inert recorder and **imports no otel** — the engine therefore has
  zero hard dependency on otel and core runs fine without the extra.
- when tracing is live, opens an OpenInference **RETRIEVER** span and records,
  via the lazily-imported semantic conventions:
  - `openinference.span.kind = "RETRIEVER"`
  - `input.value` = the query (the note title)
  - per candidate, `retrieval.documents.{i}.document.{id,content,score}` — the
    target chunk id, its text, and the **cosine score** — so the retrieved
    candidates render as documents in the same trace as the judge LLM calls.

`pipeline/retrieve.py` stays **pure** (no otel import); the span lives entirely
in `engine.py` via the helper.

### Unified trace (root span + thread context propagation)

**The problem.** `OpenAIInstrumentor` auto-instruments every embedding + judge
OpenAI-SDK call, and `engine.suggest` opens a manual `retrieve_candidates`
RETRIEVER span. But there was **no root span**, so OTel — which derives a span's
trace from the *current span* — made each auto-instrumented call (and the
retrieval span) its **own top-level trace**. One `suggest` run therefore
scattered into many disconnected traces in Phoenix. Worse, the judge runs its
per-source-chunk LLM calls in a `concurrent.futures.ThreadPoolExecutor` (the T12
parallelization), and the OTel "current span" lives in a `contextvars` context,
which **does not propagate into worker threads** — so even with a root span the
parallel judge spans (and the OpenAI auto-spans they create) would split off into
their own traces.

**The root span.** `observability.root_span(name, attributes=None)` is a context
manager — same shape/guard as `retrieval_span` — that starts a span as the
**current** span when tracing is active and is a **safe no-op (imports zero
otel)** when `_TRACER is None`. `engine.suggest` wraps the whole pipeline body in
`with observability.root_span("notelinks.suggest", {note id/title}):`. So the
query-embedding OpenAI auto-span (same thread) and the existing `retrieval_span`
now nest under it and share one `trace_id` — one connected tree per `suggest`
run. (The auto-refresh in step 1 is left OUTSIDE the root: it is its own concern
and its embedding traffic should not nest under this `suggest`.)

**Crossing the thread boundary.** Two helpers carry the OTel context into the
judge's worker threads, both no-ops importing zero otel when tracing is off:

- `current_context()` → captures the current `opentelemetry.context` on the
  submitting thread (or `None` when inactive).
- `context_propagating_wrapper(fn, ctx)` → when `ctx`/`_TRACER` is `None`, the
  **identity wrapper** (returns `fn` unchanged, imports no otel); otherwise wraps
  `fn` so that *inside the worker thread* it `context.attach(ctx)` … runs `fn` …
  `context.detach(token)`, so any span `fn` creates nests under the captured
  context's span (the root).

`judge_candidates` captures the context once before submitting and wraps each
`_call` task with it. Behaviour is **unchanged**: the wrapper only attaches/detaches
context around the existing call; the same `_call` runs, results are still keyed
by `sid` and assembled sequentially in first-seen group order, so the stable
`s01`/`s02`/… ids, failure isolation, and returned list are identical.

**The per-judge-group span.** Inside `_call` (i.e. in the worker thread, under
the propagated context) we open `observability.judge_span(source_chunk_id)`
(a no-op when off) around the message build + LLM call. It both enriches the
trace (a named node per source chunk around its OpenAI auto-span) **and** makes
propagation observable/testable — its `trace_id` must equal the root's, which it
only can if the context crossed the thread boundary.

**Testing it offline.** The auto OpenAI spans need real network calls, so they
are out of scope for the offline suite; the manual judge-group spans created in
the workers are the **proxy** that proves thread propagation. The trace-tree test
(`test_integration.py::test_suggest_emits_one_unified_trace`, skipped via
`pytest.importorskip` when the extra is absent) stands up a **LOCAL**
`TracerProvider` with an `InMemorySpanExporter` + `SimpleSpanProcessor` (NOT the
Phoenix OTLP exporter — there is no running collector), points
`observability._TRACER` at it, and runs `Engine.suggest` with `embed_texts` /
`complete_structured` monkeypatched (no network, no key). It asserts the root
`notelinks.suggest` span, the `retrieve_candidates` span, and EVERY
`judge_source_chunk` span (created in worker threads) all share the **same
`trace_id`** — proving the root span + cross-thread context propagation work. The
no-op paths get their own unit tests in `test_observability.py` that force any
`opentelemetry`/`phoenix`/`openinference` import to fail, proving the inactive
helpers import zero otel.

## stdout-purity guarantee

`suggest` emits only the `Envelope` JSON; `index` emits only the stats JSON.
All logging, the `--trace` hint, and Phoenix's own output go to **stderr** (the
banner is suppressed with `verbose=False`). Verified: `setup_tracing()` writes
0 bytes to stdout.

## Usage

Develop / enable the extra in this checkout:

```sh
uv sync --extra trace          # installs Phoenix + OTel (or: pip install 'notelinks[trace]')
```

Run a local Phoenix (UI + OTLP collector on :6006), in a separate terminal:

```sh
uv run phoenix serve           # open http://localhost:6006
```

Then trace a suggest run (stdout stays pure JSON; the hint goes to stderr):

```sh
notelinks suggest --corpus ~/notes --trace < current-note.org
# override the collector: PHOENIX_COLLECTOR_ENDPOINT=http://host:6006 notelinks suggest --trace ...
```

Verbose stderr logging needs no extra and composes with `--trace`:

```sh
notelinks suggest --corpus ~/notes -v < current-note.org
notelinks index --corpus ~/notes -v --trace
```
