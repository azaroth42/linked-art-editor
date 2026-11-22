"""The projection rules that need no Y document: constants, classification, encodings, keys.

The specification is rules R1-R19 of ``docs/spikes/S3-projection.md`` (ADR-0018; register B2,
B3); comments cite them as ``R<n>``. Everything here is pure, and the TypeScript mirror
(``frontend/src/crdt/``, Step 1.17) must compute the same results from the same inputs.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection
from typing import Any, Final

ROOT_MAP: Final = "record"
"""Name of the one root ``Map`` of a record's ``Doc`` (R1)."""

ORDER_KEY: Final = "_order"
"""Key of the fractional index carried by every array entry (R6, R12)."""

VALUE_KEY: Final = "_value"
"""Key that holds the item of an entry whose item is opaque (R6)."""

JSON_KEY: Final = "_json"
"""The single key of the plain wrapper that stores exact JSON text (R9)."""

RESERVED_KEYS: Final = frozenset({ORDER_KEY, VALUE_KEY})
"""Keys that a node, a reference or the root may not have in the input (R5)."""

TEXT_CONTENT_TYPES: Final = frozenset({"Name", "Identifier", "LinguisticObject"})
"""Types whose string ``content`` is a shared ``Text`` (R8.2)."""

REFERENCE_KEYS: Final = frozenset({"id", "type", "_label"})
"""The only keys a reference may have (R3)."""

OPAQUE_KEYS: Final = frozenset({"@context"})
"""Keys whose value is opaque at any depth, whatever its form (R3)."""

ENTRY_KEY_PREFIX: Final = "["
"""A map key that starts with this is the key text of an entry key or an escaped property key."""

MAX_SAFE_INT: Final = 2**53
"""Integers up to this magnitude are exact as IEEE doubles and stored as Y numbers (R9)."""

INT_MIN: Final = -(2**63)
INT_MAX: Final = 2**64 - 1

SINGLE_VALUED_CLASSES: Final[dict[str, str]] = {
    "timespan": "TimeSpan",
    "produced_by": "Production",
    "created_by": "Creation",
    "destroyed_by": "Destruction",
    "born": "Birth",
    "died": "Death",
    "formed_by": "Formation",
    "dissolved_by": "Dissolution",
    "duration": "Dimension",
    "paid_amount": "MonetaryAmount",
}
"""Single-valued properties whose value is an embedded node, with the class of that node.

Used only when a write path has to create a missing intermediate node (``profiles/README.md``,
"Writing"): a property listed here is created as a single value of the given class; any other
property is created as an array entry and needs a class filter in the path.
"""

PROJ_RESERVED_KEY: Final = "LAE-PROJ-001"
PROJ_BAD_NUMBER: Final = "LAE-PROJ-002"
PROJ_BAD_UNICODE: Final = "LAE-PROJ-003"
PROJ_NO_RECORD_URI: Final = "LAE-PROJ-004"
PROJ_BAD_STATE: Final = "LAE-PROJ-005"
PROJ_SINGLE_VALUE: Final = "LAE-PROJ-006"
PROJ_BAD_PATH: Final = "LAE-PROJ-007"
PROJ_NO_ENTRY: Final = "LAE-PROJ-008"


class ProjectionError(ValueError):
    """A document cannot be hydrated or projected, or an operation cannot be applied.

    ``code`` is the diagnostics code (R10, plus the two operation codes below); ``detail`` is
    for logs only and is never shown to users.

    =============== ==========================================================================
    ``LAE-PROJ-001`` reserved key ``_order`` or ``_value`` on the root, a node or a reference
    ``LAE-PROJ-002`` a number that is NaN or infinite, or an integer outside -2^63 .. 2^64-1
    ``LAE-PROJ-003`` a string or key that is not valid Unicode (a lone surrogate)
    ``LAE-PROJ-004`` no record URI
    ``LAE-PROJ-005`` a value or map key in the document that the rules cannot have produced,
                     or an input that is not JSON
    ``LAE-PROJ-006`` an insert into a property that holds a single value (R14)
    ``LAE-PROJ-007`` a path that cannot be written: it addresses more than one node, passes
                     through a value that is not a node, needs an intermediate node whose class
                     is unknown, or the value does not fit the target
    ``LAE-PROJ-008`` an entry key or text position that does not exist
    =============== ==========================================================================
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------------------------
# Classification (R3)
# --------------------------------------------------------------------------------------------


def is_reference(obj: Any) -> bool:
    """True for a reference: a string ``id`` and no keys beyond ``id``, ``type``, ``_label``."""
    return isinstance(obj, dict) and isinstance(obj.get("id"), str) and obj.keys() <= REFERENCE_KEYS


def is_node(obj: Any) -> bool:
    """True for an object that has ``type`` and whose ``id`` is absent or a string.

    A reference that has ``type`` satisfies this too; test ``is_reference`` first when the
    difference matters (only a node without ``id`` gets an internal IRI, R4).
    """
    return (
        isinstance(obj, dict) and "type" in obj and ("id" not in obj or isinstance(obj["id"], str))
    )


def is_structured(obj: Any) -> bool:
    """True if ``obj`` is stored as a shared ``Map`` (a node or a reference); else it is opaque."""
    return is_reference(obj) or is_node(obj)


# --------------------------------------------------------------------------------------------
# Plain values (R9, R10)
# --------------------------------------------------------------------------------------------


def check_text(value: str, where: str) -> str:
    """Return ``value``; raise ``LAE-PROJ-003`` if it holds a lone surrogate (R10)."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectionError(PROJ_BAD_UNICODE, f"{where} is not valid Unicode: {exc}") from exc
    return value


def check_json(value: Any) -> None:
    """Raise unless ``value`` is JSON that can be stored: R10 numbers, strings and keys."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        check_text(value, "a string")
    elif isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise ProjectionError(PROJ_BAD_NUMBER, f"integer {value} is outside the 64-bit range")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectionError(PROJ_BAD_NUMBER, f"number {value!r} is not finite")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProjectionError(PROJ_BAD_STATE, f"non-string key {key!r}")
            check_text(key, "a key")
            check_json(item)
    elif isinstance(value, list):
        for item in value:
            check_json(item)
    else:
        raise ProjectionError(PROJ_BAD_STATE, f"not a JSON value: {type(value).__name__}")


def json_text(value: Any) -> str:
    """Exact JSON text of ``value``: sorted keys, compact, non-ASCII verbatim, no NaN (R9)."""
    check_json(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def encode_plain(value: Any) -> Any:
    """Encode a JSON value that is not a node, reference or property-level array (R9).

    The result is what is stored in a ``Map``: the value itself, a double, or a ``_json``
    wrapper.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return check_text(value, "a string")
    if isinstance(value, int):
        check_json(value)
        # Safe integers travel as a Y number (always a double on the wire and in pycrdt).
        return float(value) if abs(value) <= MAX_SAFE_INT else {JSON_KEY: json_text(value)}
    if isinstance(value, float):
        check_json(value)
        # A float with an integral value (33.0, -0.0, 1e21) would come back as an integer.
        return {JSON_KEY: json_text(value)} if value.is_integer() else value
    if isinstance(value, dict | list):
        return {JSON_KEY: json_text(value)}
    raise ProjectionError(PROJ_BAD_STATE, f"not a JSON value: {type(value).__name__}")


def decode_plain(value: Any) -> Any:
    """Decode a plain value read from a ``Map`` back to JSON (inverse of ``encode_plain``)."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectionError(PROJ_BAD_STATE, f"non-finite number {value!r} in the document")
        return int(value) if value.is_integer() and abs(value) <= MAX_SAFE_INT else value
    if isinstance(value, dict) and value.keys() == {JSON_KEY} and isinstance(value[JSON_KEY], str):
        try:
            return json.loads(value[JSON_KEY])
        except ValueError as exc:
            raise ProjectionError(PROJ_BAD_STATE, f"malformed _json text: {exc}") from exc
    raise ProjectionError(PROJ_BAD_STATE, f"unexpected plain value in the document: {value!r}")


EMPTY_ARRAY: Final[dict[str, str]] = {JSON_KEY: "[]"}
"""``plain([])``: the property-key marker of an array that was empty in the source (R8.5)."""


# --------------------------------------------------------------------------------------------
# Keys (R6, R7)
# --------------------------------------------------------------------------------------------


def key_text(parts: list[str]) -> str:
    """JSON text of a list of strings; identical to JavaScript ``JSON.stringify(parts)``."""
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def property_key(prop: str) -> str:
    """Map key of a property that has no entries: the JSON key, escaped if it starts with ``[``."""
    return key_text([prop]) if prop.startswith(ENTRY_KEY_PREFIX) else prop


def entry_key(prop: str, item_key: str) -> str:
    """Map key of one array item of property ``prop``: the key text of ``[prop, item_key]``."""
    return key_text([prop, item_key])


def parse_key(key: str) -> tuple[str, str | None]:
    """``(property, item key)`` of a map key; the item key is ``None`` for a property key."""
    if not key.startswith(ENTRY_KEY_PREFIX):
        return key, None
    try:
        parts = json.loads(key)
    except ValueError:
        parts = None
    if (
        isinstance(parts, list)
        and len(parts) in (1, 2)
        and all(isinstance(part, str) for part in parts)
    ):
        return (parts[0], None) if len(parts) == 1 else (parts[0], parts[1])
    raise ProjectionError(PROJ_BAD_STATE, f"malformed map key {key!r}")


def value_key(value: Any) -> str:
    """Base item key of an opaque array item (R7)."""
    if isinstance(value, str):
        return "=s:" + value
    if value is None:
        return "=z"
    if isinstance(value, bool):
        return "=b:true" if value else "=b:false"
    if isinstance(value, int | float):
        return "=n:" + json_text(value)
    return "=j:" + json_text(value)


def unique_key(base: str, used: Collection[str]) -> str:
    """``base`` if unused, else the first unused of ``base #2``, ``base #3``, ... (R7)."""
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base} #{n}"
        n += 1
    return candidate


# --------------------------------------------------------------------------------------------
# _order arithmetic (R12)
# --------------------------------------------------------------------------------------------


def order_between(before: float | None, after: float | None) -> float | None:
    """The ``_order`` for a slot between two neighbours, or ``None`` if a rebalance is needed."""
    if before is None and after is None:
        return 0.0
    if before is None:
        assert after is not None
        candidate = after - 1.0
        ok = candidate < after
    elif after is None:
        candidate = before + 1.0
        ok = candidate > before
    else:
        candidate = (before + after) / 2.0
        ok = before < candidate < after
    return candidate if ok and math.isfinite(candidate) else None


# --------------------------------------------------------------------------------------------
# Text positions (R19)
# --------------------------------------------------------------------------------------------


def utf8_offset(text: str, index: int) -> int:
    """The ``pycrdt`` (UTF-8 byte) offset of code-point position ``index`` in ``text`` (R19)."""
    return len(text[:index].encode("utf-8"))


def text_change(old: str, new: str) -> tuple[int, int, str]:
    """The single splice that turns ``old`` into ``new``: ``(index, delete count, insert)``.

    Counted in code points: ``index`` is the length of the longest common prefix, and the
    longest common suffix of what remains on both sides is kept. ``(len(old), 0, "")`` style
    results with nothing to delete and nothing to insert mean the strings are equal.
    """
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    return prefix, len(old) - prefix - suffix, new[prefix : len(new) - suffix]
