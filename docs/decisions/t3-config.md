# T3 — `config.py` decisions

The settings module (`src/notelinks/config.py`) for notelinks. Implements the
`pydantic-settings` defaults from design §12 and the injectable-config rule from
design §2.

## Settings: fields, defaults, rationale

| Field | Env var | Default | Rationale |
|---|---|---|---|
| **Provider** | | | |
| `openrouter_api_key` | `OPENROUTER_API_KEY` | `""` | One key for both embeddings and chat (OpenAI SDK pointed at OpenRouter). Empty default so the module imports without secrets in tests/CI; providers validate non-empty at request time, not import time. |
| `openrouter_base_url` | `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter OpenAI-compatible endpoint (design §3). |
| **Models** | | | |
| `embedding_model` | `EMBEDDING_MODEL` | `openai/text-embedding-3-small` | 1536-dim; recall handled by retrieval, precision by the judge, so the smaller/cheaper model is the right trade (design §3). |
| `embedding_dim` | `EMBEDDING_DIM` | `1536` | Matches `text-embedding-3-small`. |
| `judge_model` | `JUDGE_MODEL` | `anthropic/claude-sonnet-4.5` | Latest Claude Sonnet OpenRouter slug; trivially swappable to a newer slug as one string. |
| **Paths** | | | |
| `corpus_dir` | `NOTELINKS_CORPUS_DIR` | `None` | Notes root. `None` = unset; CLI resolves from env or `--corpus` at call time (design §11). |
| `index_dir` | `NOTELINKS_INDEX_DIR` | `.notelinks/index` | Chroma persistent store path; repo-relative, hidden dir keeps the corpus clean. |
| **Chunking** (token-based, tiktoken) | | | |
| `chunk_target_tokens` | `CHUNK_TARGET_TOKENS` | `256` | Target chunk size (design §5). |
| `chunk_max_tokens` | `CHUNK_MAX_TOKENS` | `400` | Hard cap before a split is forced. |
| `chunk_overlap_tokens` | `CHUNK_OVERLAP_TOKENS` | `32` | Prose-only overlap to preserve idea continuity across a mid-paragraph cut. |
| `chunk_min_tokens` | `CHUNK_MIN_TOKENS` | `64` | Sub-min tail chunks merge into the previous chunk of the same heading. |
| `tokenizer_encoding` | `TOKENIZER_ENCODING` | `cl100k_base` | tiktoken encoding (design §3). |
| **Retrieval** | | | |
| `top_k` | `TOP_K` | `8` | Nearest neighbours per source chunk (design §8). |
| `per_source_n` | `PER_SOURCE_N` | `3` | Targets kept per source chunk (N). |
| `global_cap_m` | `GLOBAL_CAP_M` | `40` | Global candidate cap (M) after round-robin selection. |
| `sim_floor` | `SIM_FLOOR` | `0.2` | Cosine-similarity floor; pairs below are dropped before judging. Conservative placeholder — **calibrate against the real corpus later** (design §8 leaves it tunable). |
| **Output** | | | |
| `top_n` | `TOP_N` | `12` | Final ranked-suggestions cap (design §10). |

Every field is overridable by its env var (field name upper-cased) — no custom
`env=` aliases needed since `pydantic-settings` derives them from field names.

## Env-loading behavior

- `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`.
- Precedence (pydantic-settings v2): explicit constructor args > environment
  variables > `.env` file > field defaults.
- `extra="ignore"` so unrelated environment variables (e.g. `AWS_*`) never raise
  a validation error — important because this module is imported in any
  environment.
- `Path` fields are parsed from strings automatically by pydantic v2.

## Injectable vs. global

- **The `Settings` class is the contract.** The core `Engine(config)` receives a
  `Settings` instance and reads all knobs from it (design §2: "no per-call global
  singletons; ... config is injected"). Internal modules (pipeline steps,
  providers, index) take an explicit `Settings`, never reach for a module global.
- **`get_settings()`** is a thin, `functools.lru_cache`d convenience for
  *adapters* (CLI now, REST/daemon later) to build process defaults from the
  environment once. It is intentionally not exported as a mutable singleton and
  not used by internals — so tests can construct ad-hoc `Settings(...)` and the
  future daemon can hold its own instance without hidden global state.

## Notes / open choices

- `sim_floor=0.2` is the one value not pinned by design §12; chosen conservative
  to avoid dropping real candidates before calibration.
- pydantic v2 / pydantic-settings v2 idioms throughout (`SettingsConfigDict`,
  `Path | None`).
