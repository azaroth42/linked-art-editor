# Vendored from lux/data-pipeline/lux_pipeline/sources/lux/qlever/mapper2.py
# @ 443793ee3b7aadb9716c45777d5a606348ef30e7
# (`VALID_IRI`, `IRI_ESCAPE_TABLE`, `VALID_DATETIME`, `clean_iri`, `clean_datetime`; type hints
# added and the layout reformatted, behaviour unchanged). Tests: tests/rdf/test_sanitize.py, from
# the upstream tests/test_qlever_mapper_ntriples.py.
"""IRI and ``xsd:dateTime`` sanitation for everything written as N-Triples.

One bad term is enough to break a QLever index build: a ``"`` or ``\\`` in an IRI fails the
build, and a ``"`` in a literal makes the parser stop silently and drop every triple after that
line. ``lae.rdf.expand`` therefore checks every IRI and every ``xsd:dateTime`` literal with these
functions and rejects the record rather than writing a bad term.

``VALID_DATETIME`` accepts signed years, so BCE bounds such as ``-0500-01-01T00:00:00Z`` are
valid (decision for Step 0.9 in ``docs/questions.md``).
"""

from __future__ import annotations

import re
from typing import Final

# An http(s) IRI needs a host, so `https:/` is rejected; any other IRI needs at least a scheme.
# The fast path is one fullmatch, because nearly every IRI is fine.
_IRI_CHAR: Final = r'[^\x00-\x20<>"{}|^`\\]'
VALID_IRI: Final = re.compile(
    rf"(?i:https?)://[^/?#\x00-\x20<>\"{{}}|^`\\]{_IRI_CHAR}*"
    rf"|(?!(?i:https?):)[A-Za-z][A-Za-z0-9+.\-]*:{_IRI_CHAR}+"
)
# Line breaks inside a source IRI are wrapping, so delete them; the other characters N-Triples
# forbids get percent-encoded.
IRI_ESCAPE_TABLE: Final = str.maketrans(
    {
        **{chr(c): f"%{c:02X}" for c in range(0x21)},
        **{c: f"%{ord(c):02X}" for c in '<>"{}|^`\\'},
        "\n": None,
        "\r": None,
        "\t": None,
    }
)

VALID_DATETIME: Final = re.compile(
    r"-?(?:[1-9]\d{4,}|\d{4})-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?"
)


def clean_iri(value: object) -> str | None:
    """Return ``value`` as a writable IRI (percent-encoding where it can), or ``None``."""
    if type(value) is not str:
        return None
    if VALID_IRI.fullmatch(value):
        return value
    value = value.strip().translate(IRI_ESCAPE_TABLE)
    if VALID_IRI.fullmatch(value):
        return value
    return None


def clean_datetime(value: object) -> str | None:
    """Return ``value`` (trimmed) if it is a valid ``xsd:dateTime`` lexical form, else ``None``."""
    if type(value) is not str:
        return None
    value = value.strip()
    if VALID_DATETIME.fullmatch(value):
        return value
    return None
