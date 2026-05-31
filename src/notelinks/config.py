"""Settings for notelinks.

The `Settings` class is the **configuration contract**: an `Engine` receives a
`Settings` instance and reads everything it needs from it (see design §2). There
is no mutable global singleton — `get_settings()` is only a convenience for
adapters (CLI / future REST) that want process defaults loaded from the
environment. Anything internal should take an injected `Settings`.

Values default to the design's §12 defaults and are overridable via environment
variables (and an optional `.env` file). Unrelated env vars (e.g. ``AWS_*``) are
ignored so importing this module never fails in a foreign environment.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, injected into the `Engine`.

    Fields are grouped by category: provider, models, paths, chunking,
    retrieval, output.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",  # don't choke on unrelated env vars (AWS_*, etc.)
    )

    # --- Provider (OpenRouter via the OpenAI SDK) ------------------------------
    # Empty default keeps the module importable in tests / CI without secrets.
    # Providers (providers/embeddings.py, providers/llm.py) must validate this is
    # non-empty at *runtime*, right before making a request — not at import time.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # --- Models ----------------------------------------------------------------
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dim: int = 1536
    # Latest Claude Sonnet OpenRouter slug; trivially swappable to a newer slug.
    judge_model: str = "anthropic/claude-sonnet-4.5"

    # --- Paths -----------------------------------------------------------------
    # corpus_dir: notes root; CLI may override with --corpus. None => required at
    # call time (the CLI resolves it from NOTELINKS_CORPUS_DIR / --corpus).
    corpus_dir: Path | None = None
    # index_dir: Chroma persistent store location (repo-relative by default).
    index_dir: Path = Path(".notelinks/index")

    # --- Chunking (token-based, tiktoken) --------------------------------------
    chunk_target_tokens: int = 256
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 32
    chunk_min_tokens: int = 64
    tokenizer_encoding: str = "cl100k_base"

    # --- Retrieval & candidate selection ---------------------------------------
    top_k: int = 8  # nearest neighbours per source chunk
    per_source_n: int = 3  # targets kept per source chunk (N)
    global_cap_m: int = 40  # global candidate cap (M)
    # Cosine-similarity floor: drop candidate pairs below this before judging.
    # Conservative placeholder; calibrate against the real corpus later.
    sim_floor: float = 0.2

    # --- Output ----------------------------------------------------------------
    top_n: int = 12  # final ranked suggestions cap


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide `Settings` loaded from the environment / `.env`.

    Convenience for adapters constructing an `Engine` from ambient config.
    Cached so repeated adapter calls share one instance. This is *not* an
    injectable contract — internals should accept an explicit `Settings`.
    """
    return Settings()
