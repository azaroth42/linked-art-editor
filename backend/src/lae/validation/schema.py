"""JSON Schema validation, the first-order gate (spec §5; decision C1, ADR-0008).

``validate_schema`` checks a record against the vendored Linked Art schema for it
(``lae.validation.schema_map``) and returns one violation per error:

- ``code`` is ``LAE-SCHEMA-<validator>``, the failing JSON Schema keyword (``required``,
  ``anyOf``, ``additionalProperties``, ``format``, ...). Three codes are not keywords:
  ``LAE-SCHEMA-false`` (a ``false`` schema), ``LAE-SCHEMA-ref`` (the schema itself has a
  ``$ref`` that cannot be resolved; the upstream ``LegalRight`` defect in ``provenance.json``)
  and ``LAE-SCHEMA-recordType`` (the record is not an object whose ``type`` is a Linked Art
  record class, so no schema applies).
- ``path`` is the JSON Pointer of the failing instance.
- ``params`` always has ``validator_value`` and ``instance_summary``, both kept small (a
  sub-schema is reduced to its ``$ref`` or its keywords). ``required`` adds ``missing`` (one
  diagnostic per object, naming every missing property), ``additionalProperties`` adds
  ``extra``, ``format`` adds ``format``, and ``anyOf`` / ``oneOf`` add ``cause`` when it can be
  told: the code and path of the first error in the one alternative whose ``type`` the instance
  has.

One class of error is not reported: a ``const`` / ``enum`` violation on the ``type`` of a root
``equivalent`` entry whose ``type`` is the record's own. ``event.json`` and ``abstract.json``
allow only ``Activity`` and ``LinguisticObject`` references there, although they are also the
schemas of ``Event``, ``Period`` and ``PropositionalObject`` records, whose equivalents are of
those classes. Every re-minted record carries its source URI in ``equivalent`` with its own
class (ADR-0030), so without this no such record could be published.

The ``id`` of every embedded node is removed first, because the public schema does not allow
``id`` on most of them: an internal node IRI (``strip_internal_iris``), and an id the node
brought with it from elsewhere (``lae.rdf.strip_embedded_ids``, ADR-0028; such a node is part of
the record, ADR-0021). A reference and the root keep their ``id``. Only the schema check ignores
these ids: storage, RDF, the rules and SHACL see them.

A ``@context`` below the root that repeats the record's pinned context is removed as well
(``lae.rdf.strip_nested_contexts``): expansion ignores it (ADR-0028), and the schema allows
``@context`` on few nodes. Any other nested ``@context`` is left for the schema, and for
expansion, to refuse.

Formats are checked for ``date-time`` and ``uri`` only, with this project's own checkers:
``date-time`` accepts signed (BCE) years, which RFC 3339 does not, and ``uri`` is exactly what RDF
expansion can write (``lae.rdf.repair_iri``): an absolute IRI, also one with characters that
expansion percent-encodes, which the pipeline reports as the warning ``LAE-RDF-006``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from functools import cache, lru_cache
from typing import Any, Final

from jsonschema import Draft7Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from lae.projection.project import strip_internal_iris
from lae.rdf import repair_iri, strip_embedded_ids, strip_nested_contexts
from lae.validation.datetimes import is_datetime
from lae.validation.diagnostics import Diagnostic, diagnostic, pointer
from lae.validation.schema_map import (
    SCHEMA_BASE_URI,
    SCHEMA_BY_TYPE,
    load_registry,
    schema_name_for,
)

SCHEMA_CODE_PREFIX: Final = "LAE-SCHEMA-"
SCHEMA_REF: Final = SCHEMA_CODE_PREFIX + "ref"
"""The schema has a ``$ref`` that does not resolve; param ``ref``."""
SCHEMA_RECORD_TYPE: Final = SCHEMA_CODE_PREFIX + "recordType"
"""The document is not a record of a known Linked Art class; param ``type``."""

SCHEMA_FALSE: Final = SCHEMA_CODE_PREFIX + "false"
"""The schema is ``false`` at this place: nothing is allowed there."""

EXTRA_SCHEMA_CODES: Final = (SCHEMA_REF, SCHEMA_RECORD_TYPE, SCHEMA_FALSE)
"""Schema codes that are not JSON Schema keywords."""

REPORTED_KEYWORDS: Final = (
    "additionalItems",
    "additionalProperties",
    "anyOf",
    "const",
    "contains",
    "dependencies",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "not",
    "oneOf",
    "pattern",
    "required",
    "type",
    "uniqueItems",
)
"""The Draft 7 keywords an error can name. The others (``properties``, ``items``, ``allOf``,
``$ref``, ``if``, ``propertyNames``, ``patternProperties``) only pass on the errors of their
sub-schemas."""

_MAX_TEXT: Final = 120
_MAX_ITEMS: Final = 20

FORMAT_CHECKER: Final = FormatChecker(formats=())
"""Checks ``date-time`` and ``uri`` only (module docstring); every other format is annotation."""


@FORMAT_CHECKER.checks("date-time")
def _check_datetime(value: object) -> bool:
    return not isinstance(value, str) or is_datetime(value)


@FORMAT_CHECKER.checks("uri")
def _check_uri(value: object) -> bool:
    return not isinstance(value, str) or repair_iri(value) is not None


def schema_codes() -> frozenset[str]:
    """Return every ``LAE-SCHEMA-*`` code: one per reported keyword, plus the three extras."""
    keywords = frozenset(SCHEMA_CODE_PREFIX + keyword for keyword in REPORTED_KEYWORDS)
    return keywords | frozenset(EXTRA_SCHEMA_CODES)


@lru_cache(maxsize=1)
def _registry() -> Registry[Any]:
    return load_registry()


@cache
def schema_validator(schema_name: str) -> Draft7Validator:
    """Return the validator for one entry-point schema file (``object.json``), built once."""
    resource = _registry()[SCHEMA_BASE_URI + schema_name]
    return Draft7Validator(resource.contents, registry=_registry(), format_checker=FORMAT_CHECKER)


def schema_for_type(record_type: str) -> Draft7Validator:
    """Return the validator for a record class by its ``type`` (``KeyError`` if there is none).

    ``Activity`` gives ``event.json``; a provenance activity needs ``provenance.json``, which only
    ``validator_for_record`` can tell, so use that when the record is at hand.
    """
    return schema_validator(SCHEMA_BY_TYPE[record_type])


def validator_for_record(record: Mapping[str, Any]) -> Draft7Validator:
    """Return the validator for ``record`` (``ValueError`` if its ``type`` is no record class)."""
    return schema_validator(schema_name_for(record))


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _subschema_summary(schema: Any) -> Any:
    """Reduce a sub-schema to its ``$ref``, or to the sorted names of its keywords."""
    if isinstance(schema, Mapping):
        ref = schema.get("$ref")
        return ref if isinstance(ref, str) else sorted(schema)[:_MAX_ITEMS]
    return schema


def _value_summary(value: Any) -> Any:
    """Keep ``validator_value`` JSON-sized: scalars as they are, sub-schemas by reference."""
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, Mapping):
        return _subschema_summary(value)
    if isinstance(value, list):
        return [
            _clip(item) if isinstance(item, str) else _subschema_summary(item)
            for item in value[:_MAX_ITEMS]
        ]
    return value


def _instance_summary(instance: Any) -> Any:
    """Describe the failing instance without copying it: scalars, or the shape of a container."""
    if isinstance(instance, str):
        return _clip(instance)
    if isinstance(instance, Mapping):
        summary: dict[str, Any] = {"keys": sorted(instance)[:_MAX_ITEMS]}
        for key in ("id", "type"):
            if isinstance(instance.get(key), str):
                summary[key] = _clip(instance[key])
        return summary
    if isinstance(instance, list):
        return {"length": len(instance)}
    return instance


def _extra_properties(error: ValidationError) -> list[str]:
    schema, instance = error.schema, error.instance
    if not isinstance(schema, Mapping) or not isinstance(instance, Mapping):
        return []
    properties = schema.get("properties", {})
    patterns = list(schema.get("patternProperties", {}))
    return sorted(
        key
        for key in instance
        if key not in properties and not any(re.search(pattern, key) for pattern in patterns)
    )


def _is_type_mismatch(error: ValidationError) -> bool:
    """True for a ``const`` / ``enum`` error on the ``type`` of the instance being matched."""
    return error.validator in ("const", "enum") and list(error.relative_path) == ["type"]


def _branch_error(error: ValidationError) -> ValidationError | None:
    """Return the first error of the one ``anyOf`` / ``oneOf`` branch the instance was meant for.

    The Linked Art schemas tell their alternatives apart by a ``const`` on ``type``. The branch
    whose ``type`` the instance has is the one its author meant, so that branch's errors say what
    to fix. With no such branch, or several, there is no single cause.
    """
    branches: dict[Any, list[ValidationError]] = {}
    for sub in error.context or []:
        branch = sub.relative_schema_path[0] if sub.relative_schema_path else None
        branches.setdefault(branch, []).append(sub)
    meant = [errors for errors in branches.values() if not any(map(_is_type_mismatch, errors))]
    if len(meant) != 1:
        return None
    first = meant[0][0]
    if first.validator in ("anyOf", "oneOf"):
        return _branch_error(first) or first
    return first


def _cause(error: ValidationError) -> dict[str, str] | None:
    """Name the error underneath an ``anyOf`` / ``oneOf`` that most likely has to be fixed."""
    cause = _branch_error(error)
    if cause is None:
        return None
    return {"code": _code(cause), "path": pointer(cause.absolute_path)}


def _code(error: ValidationError) -> str:
    return SCHEMA_FALSE if error.validator is None else f"{SCHEMA_CODE_PREFIX}{error.validator}"


def _params(error: ValidationError) -> dict[str, Any]:
    params: dict[str, Any] = {
        "validator_value": _value_summary(error.validator_value),
        "instance_summary": _instance_summary(error.instance),
    }
    if error.validator == "required" and isinstance(error.instance, Mapping):
        required = error.validator_value if isinstance(error.validator_value, list) else []
        params["missing"] = [name for name in required if name not in error.instance]
    elif error.validator == "additionalProperties":
        params["extra"] = _extra_properties(error)
    elif error.validator == "format":
        params["format"] = error.validator_value
    elif error.validator in ("anyOf", "oneOf"):
        cause = _cause(error)
        if cause is not None:
            params["cause"] = cause
    return params


def _diagnostics(errors: Iterator[ValidationError]) -> Iterator[Diagnostic]:
    seen: set[tuple[str, str]] = set()
    for error in errors:
        code = _code(error)
        path = pointer(error.absolute_path)
        if error.validator == "required":
            # jsonschema reports each missing property separately; ``missing`` lists them all.
            if (code, path) in seen:
                continue
            seen.add((code, path))
        yield diagnostic(code, path, "violation", _params(error), fallback=error.message)


def _is_own_class_equivalent(error: ValidationError, record_type: Any) -> bool:
    """True for a ``type`` error on a root ``equivalent`` entry of the record's own class."""
    path = list(error.absolute_path)
    return (
        error.validator in ("const", "enum")
        and len(path) == 3
        and path[0] == "equivalent"
        and path[2] == "type"
        and isinstance(record_type, str)
        and error.instance == record_type
    )


def validate_schema(
    jsonld: Any, record_uri: str | None = None, *, context_version: int | None = None
) -> list[Diagnostic]:
    """Return the JSON Schema violations of a record, in document order; ``[]`` if it is valid.

    ``record_uri`` (default: the root ``id``) says which internal node IRIs to strip before
    validating; the ids of embedded nodes that have one of their own are ignored too, and so is
    a nested ``@context`` that repeats the pinned context of ``context_version`` (default: the
    root's own ``@context``) (module docstring). The input is not modified, and every path is a
    pointer into it.
    """
    record_type = jsonld.get("type") if isinstance(jsonld, Mapping) else None
    try:
        if not isinstance(jsonld, dict):
            raise ValueError("a record must be a JSON object")
        validator = validator_for_record(jsonld)
    except ValueError:
        params = {"type": record_type if isinstance(record_type, str) else None}
        return [diagnostic(SCHEMA_RECORD_TYPE, "/type" if record_type else "", "violation", params)]
    uri = record_uri if record_uri is not None else jsonld.get("id")
    owner = uri if isinstance(uri, str) else ""
    document = strip_nested_contexts(jsonld, context_version=context_version)
    document = strip_embedded_ids(document, owner)
    if owner:
        document = strip_internal_iris(document, owner)
    found: list[Diagnostic] = []
    try:
        errors = (
            error
            for error in validator.iter_errors(document)
            if not _is_own_class_equivalent(error, record_type)
        )
        found.extend(_diagnostics(errors))
    except Unresolvable as error:
        found.append(diagnostic(SCHEMA_REF, "", "violation", {"ref": error.ref}))
    return found
