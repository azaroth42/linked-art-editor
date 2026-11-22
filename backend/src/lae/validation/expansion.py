"""RDF expansion failures as diagnostics.

``lae.rdf.expand_to_ntriples`` refuses a record whose triples would be unsafe to store or sync
and raises a subclass of ``ExpansionError`` (``docs/questions.md``, Step 1.3, item 3). A publish
must report that as a validation failure, not as a server error (Step 1.10), so each class has a
code of its own:

| Code | Raised as | Meaning |
| :-- | :-- | :-- |
| ``LAE-RDF-001`` | ``BlankNodeError`` | a node has no IRI |
| ``LAE-RDF-002`` | ``OwnershipError`` | a subject is not the record's own |
| ``LAE-RDF-003`` | ``InvalidIRIError`` | an IRI is relative, malformed or an undefined term |
| ``LAE-RDF-004`` | ``InvalidLiteralError`` | a literal is not valid for its datatype |
| ``LAE-RDF-005`` | ``ExpansionError`` | anything else JSON-LD processing rejects |

All are violations, and ``params.detail`` carries the exception text for logs and developers.
The exception does not say where in the document the term is, so the path is the record root
(``""``) unless the cause is one this module can find again in the JSON-LD: a root ``id`` that
is not the record URI (``/id``), and a literal that is not valid Unicode or not a finite number
(its own pointer). Malformed IRIs and date-times never get this far in ``validate``: the JSON
Schema ``format`` checks report them first, with their paths.
"""

from __future__ import annotations

import math
from typing import Any, Final

from lae.rdf import (
    BlankNodeError,
    ExpansionError,
    InvalidIRIError,
    InvalidLiteralError,
    OwnershipError,
    expand_to_ntriples,
)
from lae.validation.diagnostics import Diagnostic, child, diagnostic

RDF_BLANK_NODE: Final = "LAE-RDF-001"
RDF_OWNERSHIP: Final = "LAE-RDF-002"
RDF_INVALID_IRI: Final = "LAE-RDF-003"
RDF_INVALID_LITERAL: Final = "LAE-RDF-004"
RDF_EXPANSION: Final = "LAE-RDF-005"

_CODE_BY_ERROR: Final[tuple[tuple[type[ExpansionError], str], ...]] = (
    (BlankNodeError, RDF_BLANK_NODE),
    (OwnershipError, RDF_OWNERSHIP),
    (InvalidIRIError, RDF_INVALID_IRI),
    (InvalidLiteralError, RDF_INVALID_LITERAL),
    (ExpansionError, RDF_EXPANSION),
)

RDF_CODES: Final = tuple(code for _, code in _CODE_BY_ERROR)
"""Every code this module emits."""


def _unwritable_literal(value: Any, path: str) -> str | None:
    """Return the pointer of the first literal RDF cannot hold (bad Unicode, non-finite number)."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return path
    elif isinstance(value, float):
        if not math.isfinite(value):
            return path
    elif isinstance(value, list):
        for position, item in enumerate(value):
            found = _unwritable_literal(item, child(path, position))
            if found is not None:
                return found
    elif isinstance(value, dict):
        for key, item in value.items():
            found = _unwritable_literal(item, child(path, key))
            if found is not None:
                return found
    return None


def _place(error: ExpansionError, jsonld: Any, record_uri: str | None) -> str:
    """Return the JSON Pointer of what caused ``error`` when it can be found again, else ``""``."""
    if not isinstance(jsonld, dict):
        return ""
    if isinstance(error, InvalidLiteralError):
        return _unwritable_literal(jsonld, "") or ""
    if isinstance(error, OwnershipError):
        root_id = jsonld.get("id")
        if isinstance(root_id, str) and record_uri is not None and root_id != record_uri:
            return child("", "id")
    return ""


def expansion_diagnostic(
    error: ExpansionError, jsonld: Any = None, record_uri: str | None = None
) -> Diagnostic:
    """Return the violation for an expansion failure, by its most specific class.

    With the document that failed, the path names the cause where that is possible (module
    docstring); without it the path is the record root.
    """
    code = next(code for cls, code in _CODE_BY_ERROR if isinstance(error, cls))
    path = _place(error, jsonld, record_uri)
    return diagnostic(code, path, "violation", {"detail": str(error)})


def expand_or_diagnose(
    jsonld: dict[str, Any], record_uri: str, context_version: int
) -> tuple[frozenset[str] | None, list[Diagnostic]]:
    """Return ``(triples, [])``, or ``(None, [violation])`` when expansion refuses the record."""
    try:
        return expand_to_ntriples(jsonld, context_version, record_uri=record_uri), []
    except ExpansionError as error:
        return None, [expansion_diagnostic(error, jsonld, record_uri)]
