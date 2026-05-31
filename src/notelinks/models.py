"""Pydantic v2 models for notelinks.

This module holds two families of models:

* **Output models** — mirror ``docs/json-format.md`` EXACTLY (field names,
  nesting, types). ``Envelope(...).model_dump(mode="json")`` reproduces the
  authoritative engine -> emacs JSON contract verbatim.
* **Internal models** — used by the pipeline (parse, chunk, retrieve, judge).
  These are NOT part of the wire contract and may evolve freely.

Kept import-light on purpose: no chromadb / openai imports here.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

# ---------------------------------------------------------------------------
# A) OUTPUT models — must serialize to EXACTLY the json-format.md shape.
# ---------------------------------------------------------------------------


class ConnectionType(StrEnum):
    """Connection type enum (json-format.md). Note the hyphenated values."""

    ELABORATES = "elaborates"
    ANALOGOUS_MECHANISM = "analogous-mechanism"
    CONTRADICTS = "contradicts"
    INSTANCE_OF = "instance-of"
    GENERALIZES = "generalizes"
    MENTION = "mention"


class Source(BaseModel):
    """``envelope.source`` — identifies the current note (the query)."""

    file: str  # repo-relative path
    title: str
    id: str  # org-id of the note (always present)
    queried_at: str  # ISO-8601 UTC timestamp
    content_hash: str  # "sha256:..." of the buffer text at query time


class SourceChunk(BaseModel):
    """``suggestion.source_chunk`` — DISPLAY: what resonated in the current note."""

    text: str
    heading: str | None  # heading in the current note, or null
    char_start: int
    char_end: int


class TargetHeading(BaseModel):
    """``suggestion.target.heading`` — heading link components, or null = file-level."""

    text: str
    id: str | None  # org-id of the heading, or null
    level: int


class Target(BaseModel):
    """``suggestion.target`` — NAVIGATION + link-assembly components (no finished link)."""

    file: str  # repo-relative path
    title: str
    file_id: str  # org-id of the note (always present)
    heading: TargetHeading | None  # null = file-level target


class SourceAnchor(BaseModel):
    """``suggestion.source_anchor`` — EDIT: replace [char_start,char_end) with template."""

    char_start: int
    char_end: int  # == char_start for an insert
    expect: str  # text currently in the region ("" for insert)
    before: str  # ~40 chars before, to disambiguate
    after: str  # ~40 chars after
    template: str  # text to place; prose w/ {{link}} for insert
    link_description: str  # desc for the assembled link


class Suggestion(BaseModel):
    """One suggested link (json-format.md `Suggestion`)."""

    id: str  # stable within this run
    type: ConnectionType
    confidence: int = Field(ge=1, le=5)  # 1-5
    why: str
    source_chunk: SourceChunk
    target_excerpt: str
    target: Target
    source_anchor: SourceAnchor


class Envelope(BaseModel):
    """Top-level output object — one engine run = one current note."""

    version: int
    source: Source
    suggestions: list[Suggestion]


# ---------------------------------------------------------------------------
# B) INTERNAL models — pipeline-only, NOT part of the contract (design §4-§8).
# ---------------------------------------------------------------------------


class OrgLink(BaseModel):
    """An existing ``[[id:...]]`` link found in a note (design §4)."""

    target_uuid: str
    search_string: str | None  # the "::*Heading" / "::search" part, if any
    char_start: int
    char_end: int


class OrgHeading(BaseModel):
    """A heading parsed from a note (design §4)."""

    text: str
    level: int
    id: str | None  # heading's own org-id, if present
    char_start: int
    char_end: int
    index: int  # heading/segment ordinal in note; 0 = preamble
    ancestor_path: list[str] = Field(default_factory=list)  # ancestor heading texts


class Note(BaseModel):
    """A parsed ``.org`` note (design §4)."""

    id: str  # file-level uuid
    path: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    headings: list[OrgHeading] = Field(default_factory=list)
    links: list[OrgLink] = Field(default_factory=list)
    text: str  # raw buffer text


class Chunk(BaseModel):
    """An index unit: a chunk body plus all metadata the index needs (design §5-§7)."""

    note_uuid: str
    note_path: str  # repo-relative
    note_title: str
    heading_path: str  # breadcrumb string
    heading_text: str | None
    heading_id: str | None
    heading_level: int | None
    heading_index: int  # heading/segment ordinal; 0 = preamble
    chunk_in_heading: int  # index of this chunk within its heading/segment (0-based)
    ordinal: int  # global, for the "{uuid}:{ordinal}" chunk id
    char_start: int
    char_end: int
    text: str  # chunk body
    embed_text: str  # breadcrumb + body, the text that gets embedded

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chunk_id(self) -> str:
        """Chroma chunk id: ``"{note_uuid}:{ordinal}"`` (design §7)."""
        return f"{self.note_uuid}:{self.ordinal}"


class Candidate(BaseModel):
    """A retrieval candidate: (source_chunk, target_chunk, score) (design §8)."""

    source_chunk: Chunk
    target_chunk: Chunk
    score: float


# ---------------------------------------------------------------------------
# C) JUDGE I/O — MINIMAL skeleton only.
#
# NOTE: ``pipeline/judge.py`` (a later task) OWNS and EXTENDS these models.
# They are intentionally lean here; do not treat them as final.
# ---------------------------------------------------------------------------


class JudgeAnchor(BaseModel):
    """Where/how the judge wants the link to attach (judge-returned verbatim text).

    The engine turns this into the wire-level ``SourceAnchor`` by locating the
    text in the buffer and computing offsets (design §9).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["wrap", "insert"]
    expect: str  # verbatim span to wrap, or the sentence to insert after
    insert_text: str | None = None  # prose with {{link}} for insert mode


class RawJudgeSuggestion(BaseModel):
    """One judge-returned suggestion, pre-anchoring (design §9)."""

    model_config = ConfigDict(extra="forbid")

    target_chunk_id: str
    type: ConnectionType
    confidence: int = Field(ge=1, le=5)
    why: str
    anchor: JudgeAnchor
    target_is_note: bool  # True = link the whole note; False = the chunk's heading


class JudgeResponse(BaseModel):
    """Structured judge output for one call (design §9)."""

    model_config = ConfigDict(extra="forbid")

    suggestions: list[RawJudgeSuggestion]
