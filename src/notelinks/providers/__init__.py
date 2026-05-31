"""Provider adapters — the ONLY provider-aware code (design §2).

* ``embeddings.py`` — SWAP POINT 1 (embedding provider).
* ``llm.py`` — SWAP POINT 2 (chat / judge provider + prompt caching).

Everything else in the package is provider-agnostic: it speaks pydantic models
and plain Python, never the OpenAI SDK directly.
"""
