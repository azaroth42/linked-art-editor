"""Canonical N-Triples: the exact, reproducible form of a revision's triples (decisions A8, A9).

A triple is one **canonical line**: ``<s> <p> <o> .`` or ``<s> <p> "lexical"[^^<datatype>|@lang] .``
with single spaces, no trailing newline, no blank nodes, IRIs written verbatim, and literals
escaped exactly as ``escape_literal`` does. Two triples are equal exactly when their lines are
equal strings, which is what lets ``diff_triples`` work on sets of strings and lets the sync worker
paste lines into ``DELETE DATA`` / ``INSERT DATA`` unchanged.

A canonical line therefore has to mean the same triple to an N-Triples parser (the index build)
and to a SPARQL parser (updates). The two differ in one place: SPARQL replaces every codepoint
escape (backslash, ``u``, four hex digits; or backslash, ``U``, eight) in the query text *before*
it tokenizes (SPARQL 1.1 §19.2), and QLever does so without regard to a preceding backslash. A
literal whose text is backslash ``u0041``, written the usual way with the backslash doubled,
reaches QLever's tokenizer as backslash ``A`` and the update is refused; for backslash ``u0022``
and the like a different literal would be stored. So a ``u`` or ``U`` that follows a backslash
is itself written as the codepoint escape of ``u`` (0075) or ``U`` (0055), after the doubled
backslash: both parsers then read backslash, ``u``. Measured against QLever 0.6.0 by the
Step 1.3 review.

The canonical document is the sorted, deduplicated lines, each terminated by ``\\n``, in UTF-8.
Sorting is by Unicode code point, which is the same order as sorting the UTF-8 bytes. Revisions
store it zstd-compressed at level 9.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

import zstandard

ZSTD_LEVEL: Final = 9

XSD: Final = "http://www.w3.org/2001/XMLSchema#"
XSD_STRING: Final = XSD + "string"

# Characters that may not appear raw in a canonical literal, and how each is written. This is the
# RDF canonical N-Triples escaping: the named escapes where one exists, \u00XX for every other
# control character, everything else (including all non-ASCII text) verbatim. The one addition is
# a "u" or "U" that follows a backslash (module docstring).
_LITERAL_ESCAPES: Final = {
    **{chr(c): f"\\u{c:04X}" for c in (*range(0x20), 0x7F)},
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
    "\\u": "\\\\\\u0075",
    "\\U": "\\\\\\u0055",
}
_NEEDS_ESCAPE: Final = re.compile(r'\\[uU]|[\x00-\x1f\x7f"\\]')

_IRIREF: Final = r'<[^\x00-\x20<>"{}|^`\\]+>'
# Exactly what escape_literal produces: no raw control character, quote or backslash; the seven
# named escapes; \u00XX only for the control characters that have no named escape, and for a "u"
# or "U" straight after an escaped backslash, which may not be written raw there. Keeping the
# \u form this narrow matters for SPARQL, which decodes \uXXXX before it tokenizes: " or
# \u000A inside a string would end it.
_LITERAL_BODY: Final = (
    r'(?:[^\x00-\x1f\x7f"\\]|\\[tbnrf"]|\\\\(?:\\u00(?:75|55)|(?![uU]))'
    r"|\\u00(?:0[0-7BEF]|1[0-9A-F]|7F))*"
)
_LANGTAG: Final = r"@[a-z]{1,8}(?:-[a-z0-9]{1,8})*"
CANONICAL_LINE: Final = re.compile(
    rf'{_IRIREF} {_IRIREF} (?:{_IRIREF}|"{_LITERAL_BODY}"(?:\^\^{_IRIREF}|{_LANGTAG})?) \.'
)
"""Matches (with ``fullmatch``) one canonical N-Triples line and nothing else."""


def escape_literal(value: str) -> str:
    """Return the canonical N-Triples escaping of a literal's lexical form (without quotes)."""
    if not _NEEDS_ESCAPE.search(value):
        return value
    return _NEEDS_ESCAPE.sub(lambda match: _LITERAL_ESCAPES[match[0]], value)


def is_canonical_line(line: str) -> bool:
    """True if ``line`` is exactly one canonical N-Triples line."""
    return CANONICAL_LINE.fullmatch(line) is not None


def check_lines(lines: Iterable[str]) -> list[str]:
    """Return ``lines`` sorted and deduplicated; raise ``ValueError`` if one is not canonical."""
    unique = sorted(set(lines))
    for line in unique:
        if not is_canonical_line(line):
            raise ValueError(f"not a canonical N-Triples line: {line!r}")
    return unique


def canonical_ntriples(triples: Iterable[str]) -> bytes:
    """Return the canonical document: sorted, deduplicated lines, each ending in a newline, UTF-8.

    Raises ``ValueError`` for a line that is not canonical (see the module docstring) or that
    cannot be encoded as UTF-8.
    """
    return "".join(f"{line}\n" for line in check_lines(triples)).encode("utf-8")


def parse_ntriples(b: bytes) -> frozenset[str]:
    """Return the lines of a canonical document; raise ``ValueError`` if it is not one.

    This reads only what ``canonical_ntriples`` writes. It is not a general N-Triples parser:
    comments, blank lines, blank nodes and other spellings of the same triple are rejected, since
    a line that differed from its canonical form would never match in a diff.
    """
    if not b:
        return frozenset()
    text = b.decode("utf-8")
    if not text.endswith("\n"):
        raise ValueError("canonical N-Triples must end with a newline")
    lines = text[:-1].split("\n")
    for line in lines:
        if not is_canonical_line(line):
            raise ValueError(f"not a canonical N-Triples line: {line!r}")
    return frozenset(lines)


def compress(b: bytes) -> bytes:
    """Compress ``b`` as one zstd frame at level 9 (the frame records the content size)."""
    return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(b)


def decompress(b: bytes) -> bytes:
    """Decompress one zstd frame written by ``compress``."""
    return zstandard.ZstdDecompressor().decompress(b)
