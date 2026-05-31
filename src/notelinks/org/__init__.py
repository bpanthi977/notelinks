"""Org-mode parsing and chunking for notelinks.

``parse.py`` turns a raw ``.org`` buffer into a :class:`notelinks.models.Note`
(file ID, title, aliases, headings with char spans, existing ``[[id:...]]``
links). ``chunk.py`` (T10) consumes that structure.
"""

from notelinks.org.parse import parse_note

__all__ = ["parse_note"]
