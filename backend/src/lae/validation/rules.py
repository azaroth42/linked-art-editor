"""Python rules for what SHACL handles poorly (spec §5; decision C1).

- ``LAE-RULE-001`` (violation): a ``TimeSpan``'s ``begin_of_the_begin`` is not later than its
  ``end_of_the_end``. Path: the ``TimeSpan``. Params: ``begin``, ``end``.
- ``LAE-RULE-002`` (warning): a ``Name`` or ``Identifier`` in a ``TimeSpan``'s ``identified_by``
  that is classified as EDTF (``EDTF_TYPE``) is valid EDTF. Path: its ``content``. Params:
  ``value``.
- ``LAE-RULE-003`` (violation): ``content`` is not empty or white space only. Path: the
  ``content``.
- ``LAE-RULE-004`` (violation): no reference to a record whose ``status`` is ``deleted``. Path:
  the reference. Params: ``uri``.
- ``LAE-RULE-005`` (violation): every ``TimeSpan`` bound is a valid ``xsd:dateTime``. Path: the
  bound. Params: ``property``, ``value``.

``LAE-RULE-001`` and ``-005`` replace the Spike S4 shapes ``LAE-SHACL-010`` and ``-009``: rdflib
cannot represent a year before 1, so pyshacl rejects or crashes on BCE bounds
(``docs/spikes/S4-shacl.md``, finding 6). Here bounds are compared as instants with signed years
(``lae.validation.datetimes``). A bound that is not a valid date-time is reported by ``-005`` and
left out of the comparison.

``LAE-RULE-002`` checks only strings that claim to be EDTF. A ``TimeSpan``'s other names are
display labels in free text ("ca. 1642", "1503-1506") and are never checked. It is a warning
because the string is a label next to the bounds, which are what is indexed.

The rules are pure functions of the JSON-LD and the set of deleted record URIs, which the caller
looks up (``lae.validation.engine``); nothing here touches the database.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from typing import Any, Final

from lae.validation.datetimes import parse_datetime
from lae.validation.diagnostics import Diagnostic, child, diagnostic
from lae.validation.edtf import is_edtf

RULE_TIMESPAN_ORDER: Final = "LAE-RULE-001"
RULE_EDTF: Final = "LAE-RULE-002"
RULE_EMPTY_CONTENT: Final = "LAE-RULE-003"
RULE_DELETED_REFERENCE: Final = "LAE-RULE-004"
RULE_TIMESPAN_DATETIME: Final = "LAE-RULE-005"

RULE_CODES: Final = (
    RULE_TIMESPAN_ORDER,
    RULE_EDTF,
    RULE_EMPTY_CONTENT,
    RULE_DELETED_REFERENCE,
    RULE_TIMESPAN_DATETIME,
)
"""Every code this module emits."""

EDTF_TYPE: Final = "http://id.loc.gov/datatypes/edtf"
"""``classified_as`` id that marks a ``TimeSpan`` name or identifier as an EDTF string.

It is the IRI of the Library of Congress EDTF datatype; AAT has no concept for EDTF
(``docs/questions.md``, Step 1.9).
"""

TIMESPAN_BOUNDS: Final = (
    "begin_of_the_begin",
    "end_of_the_begin",
    "begin_of_the_end",
    "end_of_the_end",
)
_CONTEXT_KEY: Final = "@context"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _is_classified_as(node: dict[str, Any], uri: str) -> bool:
    return any(
        isinstance(entry, dict) and entry.get("id") == uri
        for entry in _as_list(node.get("classified_as"))
    )


def _timespan(node: dict[str, Any], path: str) -> Iterator[Diagnostic]:
    instants = {}
    for bound in TIMESPAN_BOUNDS:
        if bound not in node:
            continue
        instants[bound] = parse_datetime(node[bound])
        if instants[bound] is None:
            params = {"property": bound, "value": node[bound]}
            yield diagnostic(RULE_TIMESPAN_DATETIME, child(path, bound), "violation", params)
    begin, end = instants.get("begin_of_the_begin"), instants.get("end_of_the_end")
    if begin is not None and end is not None and begin > end:
        params = {"begin": node["begin_of_the_begin"], "end": node["end_of_the_end"]}
        yield diagnostic(RULE_TIMESPAN_ORDER, path, "violation", params)
    names = node.get("identified_by")
    for index, name in enumerate(names if isinstance(names, list) else []):
        if not isinstance(name, dict) or not _is_classified_as(name, EDTF_TYPE):
            continue
        content = name.get("content")
        if isinstance(content, str) and content.strip() and not is_edtf(content):
            where = child(child(child(path, "identified_by"), index), "content")
            yield diagnostic(RULE_EDTF, where, "warning", {"value": content})


def _walk(
    value: Any, path: str, deleted: Collection[str], *, is_root: bool
) -> Iterator[Diagnostic]:
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, child(path, index), deleted, is_root=False)
        return
    if not isinstance(value, dict):
        return
    if not is_root and isinstance(value.get("id"), str) and value["id"] in deleted:
        yield diagnostic(RULE_DELETED_REFERENCE, path, "violation", {"uri": value["id"]})
    if value.get("type") == "TimeSpan":
        yield from _timespan(value, path)
    content = value.get("content")
    if isinstance(content, str) and not content.strip():
        yield diagnostic(RULE_EMPTY_CONTENT, child(path, "content"), "violation")
    for key, item in value.items():
        if key != _CONTEXT_KEY:
            yield from _walk(item, child(path, key), deleted, is_root=False)


def validate_rules(jsonld: dict[str, Any], deleted_refs: Collection[str] = ()) -> list[Diagnostic]:
    """Return the rule diagnostics of a record, in document order.

    ``deleted_refs`` holds the URIs of records whose ``status`` is ``deleted``; a reference to
    one of them anywhere below the root is ``LAE-RULE-004``.
    """
    return list(_walk(jsonld, "", deleted_refs, is_root=True))
