"""JSON Schema, SHACL, and rule-based validation (spec §5; decisions C1-C3; ADR-0008).

``validate(jsonld, record_uri, session)`` is the entry point: JSON Schema first, then Python
rules and SHACL on documents that pass it, returning structured ``Diagnostics``
(``lae.validation.engine``). The codes it can emit:

- JSON Schema (``lae.validation.schema``): ``LAE-SCHEMA-<keyword>``, ``-ref``, ``-recordType``,
  ``-false``.
- RDF expansion (``lae.validation.expansion``): ``LAE-RDF-001`` to ``-005``, and the warning
  ``LAE-RDF-006`` for an ``id`` that is percent-encoded in RDF (ADR-0028).
- Python rules (``lae.validation.rules``): ``LAE-RULE-001`` to ``-005``.
- SHACL (``lae.validation.shacl``): ``LAE-SHACL-NNN``, the ``lae:code`` of each shape in
  ``profiles/shapes``, and ``LAE-SHACL-000``.

``all_codes()`` lists them; each has an English message in
``frontend/public/locales/en/errors.json``.
"""

from lae.validation.diagnostics import Diagnostic, Diagnostics, Severity
from lae.validation.engine import (
    ReferenceFacts,
    load_reference_facts,
    validate,
    validate_document,
    validate_many,
    warm_up,
)
from lae.validation.expansion import RDF_CODES
from lae.validation.rules import RULE_CODES, validate_rules
from lae.validation.schema import schema_codes, validate_schema
from lae.validation.shacl import all_shape_codes, validate_shacl

__all__ = [
    "Diagnostic",
    "Diagnostics",
    "ReferenceFacts",
    "Severity",
    "all_codes",
    "load_reference_facts",
    "validate",
    "validate_document",
    "validate_many",
    "validate_rules",
    "validate_schema",
    "validate_shacl",
    "warm_up",
]


def all_codes() -> frozenset[str]:
    """Return every diagnostic code validation can emit (the shapes are loaded to list theirs)."""
    return schema_codes() | frozenset(RDF_CODES) | frozenset(RULE_CODES) | all_shape_codes()
