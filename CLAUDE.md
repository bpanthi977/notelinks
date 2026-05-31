# CLAUDE.md

`notelinks` suggests idea-resonance links between an org-mode note and the rest
of a notes corpus. Before changing anything, read the relevant docs — they hold
the settled design decisions this code must stay consistent with:

- [`docs/objective.md`](docs/objective.md) — *what* we're building and its scope.
- [`docs/design.md`](docs/design.md) — *how* it works; the authoritative design.
- [`docs/json-format.md`](docs/json-format.md) — the engine ↔ frontend output contract.
- [`docs/decisions/`](docs/decisions/) — per-task decision log (rationale behind each change).

When touching a subsystem, read its decision doc first and keep changes aligned
with the design; if a change departs from it, update the docs in the same edit.

Run `uv run pytest` (offline, no keys needed) and `uv run ruff check` before finishing.
