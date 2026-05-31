# T1 — Dependencies + `.env.example`

## Runtime dependencies added (`uv add`)

- **`tiktoken`** — token-accurate chunk sizing. Design §5 sizes chunks by tokens
  (target ~256, max ~400, overlap ~32) using the `cl100k_base` encoding; §3/§12
  name tiktoken as the tokeniser.
- **`typer`** — the v1 CLI adapter (`cli.py`), per design §2 and §3.

Resolved versions (from `uv.lock`): `tiktoken==0.13.0`, `typer==0.26.4`.

## Env vars added to `.env.example`

- `OPENROUTER_API_KEY=` — single key for both embeddings and the judge, used by
  the OpenAI SDK pointed at OpenRouter (design §3).
- `OPENROUTER_BASE_URL=https://openrouter.ai/api/v1` — OpenRouter base URL; also
  the `config.py` default (§12).
- `# NOTELINKS_CORPUS_DIR=` (commented) — corpus root, the org-roam notes dir;
  overridable via `--corpus` (§11/§12).
- `# NOTELINKS_INDEX_DIR=` (commented) — persistent Chroma index location;
  defaults to `<corpus_dir>/dbs/notelinks_index` when unset (§7/§12).

The pre-existing keys in `.env.example` (ANTHROPIC/OPENAI/ELEVEN_LABS/GOOGLE/AWS)
were kept untouched.

## Choices made

- Corpus/index dir vars are left **commented** since they are optional config
  knobs with `config.py` defaults / CLI overrides, not required secrets.
- `OPENROUTER_BASE_URL` is given its real default value (not a placeholder) to
  mirror the documented `config.py` default.
- No version pins beyond the floors `uv add` chose; `uv.lock` records the exact
  resolution.
