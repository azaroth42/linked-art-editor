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

All are violations at the record root (the exception does not say where in the document the
term is); ``params.detail`` carries the exception text for logs and developers.
"""

from __future__ import annotations

from typing import Any, Final

from lae.rdf import (
    BlankNodeError,
    ExpansionError,
    InvalidIRIError,
    InvalidLiteralError,
    OwnershipError,
    expand_to_ntriples,
)
from lae.validation.diagnostics import Diagnostic, diagnostic

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


def expansion_diagnostic(error: ExpansionError) -> Diagnostic:
    """Return the violation for an expansion failure, by its most specific class."""
    code = next(code for cls, code in _CODE_BY_ERROR if isinstance(error, cls))
    return diagnostic(code, "", "violation", {"detail": str(error)})


def expand_or_diagnose(
    jsonld: dict[str, Any], record_uri: str, context_version: int
) -> tuple[frozenset[str] | None, list[Diagnostic]]:
    """Return ``(triples, [])``, or ``(None, [violation])`` when expansion refuses the record."""
    try:
        return expand_to_ntriples(jsonld, context_version, record_uri=record_uri), []
    except ExpansionError as error:
        return None, [expansion_diagnostic(error)]
