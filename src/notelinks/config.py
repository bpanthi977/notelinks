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
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Index location relative to the corpus root when ``index_dir`` is not set
# explicitly: one Chroma store per corpus, living inside the notes folder.
_INDEX_SUBPATH = Path("dbs") / "notelinks_index"


class Settings(BaseSettings):
    """Runtime configuration, injected into the `Engine`.

    Fields are grouped by category: provider, models, paths, chunking,
    retrieval, output.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",  # don't choke on unrelated env vars (AWS_*, etc.)
        populate_by_name=True,  # allow init by field name even when a field has an alias
    )

    # --- Provider (OpenRouter via the OpenAI SDK) ------------------------------
    # Empty default keeps the module importable in tests / CI without secrets.
    # Providers (providers/embeddings.py, providers/llm.py) must validate this is
    # non-empty at *runtime*, right before making a request — not at import time.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # --- Judge provider routing (T18) ------------------------------------------
    # Which backend serves the JUDGE. "openrouter" (default) keeps byte-for-byte
    # behaviour: Anthropic via OpenRouter with prompt caching. "ollama" routes the
    # judge to a local Ollama OpenAI-compatible endpoint. EMBEDDINGS ALWAYS use
    # OpenRouter regardless of this setting (so the Chroma index is untouched and
    # OPENROUTER_API_KEY is still required for embeddings even with a local judge).
    judge_provider: Literal["openrouter", "ollama"] = Field(
        default="openrouter", validation_alias="NOTELINKS_JUDGE_PROVIDER"
    )
    # Local Ollama OpenAI-compatible base URL; used only when judge_provider="ollama".
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1", validation_alias="OLLAMA_BASE_URL"
    )

    # --- Models ----------------------------------------------------------------
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dim: int = 1536
    # The JUDGE model slug. With judge_provider="openrouter" this is an OpenRouter
    # slug (e.g. anthropic/claude-sonnet-4.5). With judge_provider="ollama" set it
    # to your local Ollama tag (e.g. gemma3n:e2b) via the JUDGE_MODEL env var.
    judge_model: str = "anthropic/claude-sonnet-4.5"

    # --- Paths -----------------------------------------------------------------
    # corpus_dir: notes root, read from NOTELINKS_CORPUS_DIR; CLI may override with
    # --corpus. None => required at call time (Engine.suggest guards on it).
    corpus_dir: Path | None = Field(
        default=None, validation_alias="NOTELINKS_CORPUS_DIR"
    )
    # index_dir: Chroma persistent store location, read from NOTELINKS_INDEX_DIR.
    # Left None here so the validator below can derive it from corpus_dir
    # (`<corpus_dir>/dbs/notelinks_index`, one index per corpus). An explicit
    # value (init arg or NOTELINKS_INDEX_DIR) always wins; it stays None only when
    # neither that nor corpus_dir is set — opening a Store in that state raises
    # (no silent fallback).
    index_dir: Path | None = Field(
        default=None, validation_alias="NOTELINKS_INDEX_DIR"
    )

    # --- Chunking (token-based, tiktoken) --------------------------------------
    chunk_target_tokens: int = 256
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 32
    chunk_min_tokens: int = 64
    tokenizer_encoding: str = "cl100k_base"

    # --- Retrieval & candidate selection ---------------------------------------
    top_k: int = 8  # nearest neighbours per source chunk
    per_source_n: int = 2  # targets kept per source chunk (N)
    global_cap_m: int = 20  # global candidate cap (M)
    # Cosine-similarity floor: drop candidate pairs below this before judging.
    # Conservative placeholder; calibrate against the real corpus later.
    sim_floor: float = 0.5

    # --- Output ----------------------------------------------------------------
    top_n: int = 12  # final ranked suggestions cap

    @model_validator(mode="after")
    def _derive_index_dir(self) -> "Settings":
        """Default ``index_dir`` to ``<corpus_dir>/dbs/notelinks_index``.

        Runs only when ``index_dir`` was left unset (no init arg, no
        ``NOTELINKS_INDEX_DIR``): an explicit value is preserved. With no
        ``corpus_dir`` to anchor under, it is left ``None`` — there is no silent
        fallback; constructing a :class:`~notelinks.index.store.Store` then
        raises.
        """
        if self.index_dir is None and self.corpus_dir is not None:
            self.index_dir = self.corpus_dir / _INDEX_SUBPATH
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide `Settings` loaded from the environment / `.env`.

    Convenience for adapters constructing an `Engine` from ambient config.
    Cached so repeated adapter calls share one instance. This is *not* an
    injectable contract — internals should accept an explicit `Settings`.
    """
    return Settings()
