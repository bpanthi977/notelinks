# Objective

A tool that suggests connections between the **current note** (one being
written or just finished) and the user's **other existing notes**, so the user
doesn't miss insightful links they would otherwise have to find by hand.

Notes are plain-text `.org` files. The user already cross-links notes manually;
this tool surfaces candidate links for the user to review and accept.

## v1 scope: idea resonance only

A passage in the current note echoes a passage in another note — an analogy, a
shared mechanism, a contradiction, an instance of a general claim, a
generalization of a specific one. The connection is conceptual and need not
share vocabulary. The unit of connection is a **passage/claim**, not the whole
note.

**Pipeline (two stages):**
1. **Retrieve** — embed structural chunks of every note into a vector store;
   for each chunk of the current note, retrieve nearest-neighbour chunks from
   other notes as candidates.
2. **Judge + explain** — an LLM decides whether each candidate is a real,
   worth-linking connection, classifies its type, writes a short rationale, and
   produces the anchor in the current note.

Chunking is **structural** (org headings / paragraphs / list items), not
LLM-extracted claims.

## Connection types
Fixed set emitted by the judge, plus a free-text "why":
`elaborates`, `analogous-mechanism`, `contradicts`, `instance-of`,
`generalizes`, `mention`.

## Targets and anchors
- **Target** of a link is either a **note file** or a **specific
  heading/subheading** inside a note. For resonance, target the heading/subtree
  the matched chunk belongs to; fall back to the file if the chunk is not under
  a heading.
- **Source anchor** — where in the **current note** the link attaches — is one
  of:
  - an existing **span** in the passage to wrap as a link, or
  - an **insertion point** (position) plus new text to insert that carries the
	link.
  The judge chooses which.

## Input / corpus model
- Operation is **asymmetric**: the current note's chunks are the *queries*; all
  other notes are the *index*.
- Operation is **incremental**: maintain the index as the corpus grows; for a
  new/edited note, embed only its chunks and query.
- One note = one `.org` file. A note has a title and optional aliases.

## Output (structured)
A ranked list of suggestions. Each suggestion:
- `type`: one of the connection types above
- `source_anchor`: where in the current note the link attaches (span, or
  insertion point + new text)
- `target`: the note file, optionally a heading within it
- `why`: short rationale
- `confidence`: ranking score (1-3; 3=very strong novel, 2=strong, 1=good)

Cardinality: multiple passages may point at different targets; the overall list
is capped to a reviewable, ranked top-N.

## Correctness constraints
- **Exclude metion links that already exist** in the current note —
  never suggest a link to a file if the user already has.
- Never suggest linking a note to itself.

## Scale
- ~500 notes now, scaling to ~2000.
