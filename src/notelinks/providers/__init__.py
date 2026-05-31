"""Provider adapters — the ONLY provider-aware code in notelinks (design §2).

Everything outside this package is provider-agnostic: it speaks in plain
``list[str] -> list[vector]`` (``embeddings.py``, SWAP POINT 1) and structured
judge calls (``llm.py``, SWAP POINT 2). Swapping OpenRouter for a different
backend touches only these modules.
"""
