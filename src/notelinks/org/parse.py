"""Custom org-roam v2 parser (design §4).

``parse_note(text, path) -> Note`` extracts, from a raw ``.org`` buffer:

* the file-level ``:ID:`` and ``:ROAM_ALIASES:`` from the top property drawer,
* the ``#+title:`` (case-insensitive; filename fallback),
* every heading (``^\\*+ ...``) with its level, cleaned text, own ``:ID:``
  (if a property drawer immediately follows), directly-owned char span,
  ancestor path, and 1-based document-order index, and
* every existing ``[[id:UUID...]]`` link with its target UUID, optional
  ``::search`` string, and char span.

Char offsets are 0-based codepoint indices into ``text`` (the same convention
the engine uses for anchors, design §11).

Index convention (shared with the chunker, T10): headings are numbered
``1, 2, 3, …`` in document order. Index ``0`` is reserved for the
pre-first-heading preamble segment, which is *not* an ``OrgHeading``.
"""

import os
import re

from notelinks.models import Note, OrgHeading, OrgLink

# A heading line: one-or-more leading stars, whitespace, then the title.
_HEADING_RE = re.compile(r"^(\*+)[ \t]+(.*)$", re.MULTILINE)

# A leading TODO/DONE keyword to strip from heading text. org-roam's default
# keyword set; we only strip the two standard ones.
_TODO_RE = re.compile(r"^(?:TODO|DONE)[ \t]+")

# Trailing org tags: ``:tag:tag2:`` at end of a heading title (allowing
# whitespace before them). Tags share colons (``:tag1:tag2:``); tag chars are
# word chars, ``@``, ``#``, ``%``.
_TAGS_RE = re.compile(r"[ \t]+(?::[\w@#%]+)+:[ \t]*$")

# A property drawer line ``:KEY: value`` (value may be empty).
_PROP_RE = re.compile(r"^[ \t]*:([A-Za-z0-9_-]+):[ \t]*(.*?)[ \t]*$")

# Existing id-link: ``[[id:UUID]]`` / ``[[id:UUID::search]]`` with optional
# ``[description]``. The UUID/search part stops at ``]`` or ``::``.
_LINK_RE = re.compile(
    r"\[\[id:([^\]:]+)(?:::([^\]]*))?\](?:\[[^\]]*\])?\]"
)

# Quoted-or-bareword token, used to split ``:ROAM_ALIASES:`` values.
_ALIAS_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')

# ``#+title:`` keyword line (case-insensitive).
_TITLE_RE = re.compile(r"^[ \t]*#\+title:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _parse_top_drawer(text: str) -> tuple[str, list[str]]:
    """Return ``(file_id, aliases)`` from the leading ``:PROPERTIES:`` drawer.

    org-roam places the file-level drawer at the very top of the buffer (before
    ``#+title:``). We only treat a drawer as the file drawer if ``:PROPERTIES:``
    is the first non-blank line. Missing ``:ID:`` -> ``""``; missing aliases ->
    ``[]``.
    """
    lines = text.splitlines()
    # Find first non-blank line.
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip().upper() != ":PROPERTIES:":
        return "", []

    file_id = ""
    aliases: list[str] = []
    i += 1
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.upper() == ":END:":
            break
        m = _PROP_RE.match(lines[i])
        if m:
            key, value = m.group(1).upper(), m.group(2)
            if key == "ID":
                file_id = value.strip()
            elif key == "ROAM_ALIASES":
                aliases = _parse_aliases(value)
        i += 1
    return file_id, aliases


def _parse_aliases(value: str) -> list[str]:
    """Split a ``:ROAM_ALIASES:`` value into a list.

    Values are space-separated; a quoted token may itself contain spaces, e.g.
    ``"Large Language Model" RLHF`` -> ``["Large Language Model", "RLHF"]``.
    """
    out: list[str] = []
    for quoted, bare in _ALIAS_TOKEN_RE.findall(value):
        token = quoted if bare == "" else bare
        if token != "":
            out.append(token)
    return out


def _title_from_filename(path: str) -> str:
    """Derive a title from the file's basename: drop ``.org``, ``_``/``-`` -> space."""
    base = os.path.basename(path)
    stem = base[:-4] if base.lower().endswith(".org") else base
    return stem.replace("_", " ").replace("-", " ").strip()


def _clean_heading_text(raw: str) -> str:
    """Strip a leading TODO/DONE keyword and trailing ``:tag:`` cookies."""
    text = _TODO_RE.sub("", raw, count=1)
    text = _TAGS_RE.sub("", text)
    return text.strip()


def _heading_own_id(text: str, body_start: int) -> str | None:
    """Return the heading's own ``:ID:`` if a property drawer immediately follows.

    ``body_start`` is the offset just past the heading line's newline. A drawer
    "immediately follows" when ``:PROPERTIES:`` is the first non-blank line of
    the heading body.
    """
    rest = text[body_start:]
    lines = rest.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip().upper() != ":PROPERTIES:":
        return None
    i += 1
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.upper() == ":END:":
            return None
        m = _PROP_RE.match(lines[i])
        if m and m.group(1).upper() == "ID":
            return m.group(2).strip()
        i += 1
    return None


def _parse_headings(text: str) -> list[OrgHeading]:
    """Parse all headings with char spans, own IDs, ancestor paths, and indices."""
    matches = list(_HEADING_RE.finditer(text))
    headings: list[OrgHeading] = []
    # Stack of (level, cleaned_text) for ancestor-path tracking.
    stack: list[tuple[int, str]] = []

    for idx, m in enumerate(matches):
        level = len(m.group(1))
        cleaned = _clean_heading_text(m.group(2))
        char_start = m.start()
        # Directly-owned segment ends at the next heading of ANY level, else EOF.
        char_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)

        # body begins after this heading line.
        body_start = m.end()
        if body_start < len(text) and text[body_start] == "\n":
            body_start += 1
        own_id = _heading_own_id(text, body_start)

        # Maintain ancestor stack: pop entries at >= current level.
        while stack and stack[-1][0] >= level:
            stack.pop()
        ancestor_path = [t for _, t in stack]
        stack.append((level, cleaned))

        headings.append(
            OrgHeading(
                text=cleaned,
                level=level,
                id=own_id,
                char_start=char_start,
                char_end=char_end,
                index=idx + 1,  # 1-based; 0 reserved for preamble
                ancestor_path=ancestor_path,
            )
        )
    return headings


def _parse_links(text: str) -> list[OrgLink]:
    """Parse all ``[[id:UUID...]]`` links with target UUID, search string, span."""
    links: list[OrgLink] = []
    for m in _LINK_RE.finditer(text):
        search = m.group(2)
        links.append(
            OrgLink(
                target_uuid=m.group(1).strip(),
                search_string=search if search not in (None, "") else None,
                char_start=m.start(),
                char_end=m.end(),
            )
        )
    return links


def parse_note(text: str, path: str = "") -> Note:
    """Parse a raw ``.org`` buffer into a :class:`Note`.

    Args:
        text: the full note buffer.
        path: the repo-relative path of the note. Optional — the current note is
            parsed from its buffer without a path (it is identified by its
            ``:ID:``); corpus indexing still passes the on-disk relative path.
            Used only for ``Note.path`` and the filename→title fallback.

    Returns:
        A ``Note`` with file ``id`` (``""`` if no file-level ``:ID:``), ``title``
        (from ``#+title:``, else derived from the filename when a path is given,
        else ``""``), ``aliases``, ``headings``, ``links``, and the raw ``text``.
    """
    file_id, aliases = _parse_top_drawer(text)

    title_match = _TITLE_RE.search(text)
    if title_match and title_match.group(1):
        title = title_match.group(1)
    else:
        title = _title_from_filename(path)

    headings = _parse_headings(text)
    links = _parse_links(text)

    return Note(
        id=file_id,
        path=path,
        title=title,
        aliases=aliases,
        headings=headings,
        links=links,
        text=text,
    )
