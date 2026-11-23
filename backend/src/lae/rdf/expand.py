"""Expansion of a record's JSON-LD to canonical N-Triples under strict subject ownership.

This is what the sync worker diffs and what revisions store (spec §3.3; decisions A7, A9, A12,
A13), so three properties are enforced here rather than assumed:

**Ownership.** A record emits only triples whose subject is its own URI or lies in its node
namespace ``<record_uri>/node/``: an internal node IRI (``/node/<uuidv7>``, assigned by the
projection) or a derived node IRI (``/node/<uuidv5>``, below). No other record can emit a subject
in that namespace, so a diff or a ``DELETE WHERE`` for one record cannot touch another's triples.
An object whose ``id`` is anything else is *foreign*, and ``strip_stub_attributes`` rewrites it in
one of two ways before expansion (ADR-0021):

- A **reference** (to a local record, a sidecar entity or an unresolved external URI) becomes
  ``{"id": uri}`` and contributes the linking triple and nothing else: no ``rdf:type``, no label
  (also when the reference kept its ``_label``, ADR-0006), and nothing nested inside it. The
  target owns its own description. This includes a reference that repeats the target's
  classification inline (``{"id": aat:…, "type": "Type", "classified_as": [meta-type]}``).
- An **embedded node that arrived with an id of its own** (institutional data often mints IRIs
  for a ``Name``, ``Identifier``, ``Production``, ``TimeSpan``, ``Dimension`` or a statement) is
  part of this record, so its content is emitted, under the derived node IRI
  ``<record_uri>/node/<uuidv5(original id)>`` instead of the original one, plus one triple
  ``<derived> la:equivalent <original id>``. The JSON-LD keeps the original id; only the RDF
  uses the derived one. Every other object in the record with that id points at the same
  derived node.

``_is_embedded`` decides which of the two a foreign object is, from the object alone (never from
database state, so expansion stays a pure function): only ``id`` / ``type`` / ``_label`` is a
reference; a ``type`` in ``ENTITY_TYPES`` is a reference whatever else it carries; a ``type`` in
``EMBEDDED_TYPES`` is an embedded node; anything else is embedded exactly when it says more than
``classified_as``. After expansion every subject is checked again, and ``OwnershipError`` is
raised for one the record does not own, whatever JSON-LD construct produced it.

**No blank nodes.** ``DELETE DATA`` cannot name a blank node, so every embedded node must carry
an internal node IRI (assigned by the projection, Step 1.4). ``BlankNodeError`` otherwise.

**Nothing silently wrong or missing.** pyld drops a triple whose IRI is malformed, resolves a
relative ``id`` or an undefined ``type`` against a default base, and writes ``INF`` for an
overflowing float. Every IRI is therefore checked on the expanded document, before pyld can drop
it, and every literal when it is written; a bad one raises (``InvalidIRIError``,
``InvalidLiteralError``) instead of being skipped. Properties that the pinned context
does not define produce no triple; that is ordinary JSON-LD and is not an error.

**Two repairs, in RDF only (ADR-0028).** Real data needs them, and neither changes the stored
JSON-LD:

- An ``id`` that is an absolute IRI except for characters an IRI may not contain (a space, ``<``,
  ``>``, ``"``, ``{``, ``}``, ``|``, ``^``, a backslash, a backtick, a control character or any
  other whitespace) is written with exactly those characters percent-encoded: UTF-8, upper-case hex
  (``repair_iri``). ``.../Self Portrait.jpg`` becomes ``.../Self%20Portrait.jpg``. Nothing else
  is touched: an existing ``%`` is not encoded again, and an ``id`` that is still not an absolute
  IRI afterwards (relative, no scheme, a bare ``https:/``) raises ``InvalidIRIError`` as before.
  Only node identifiers are repaired; a class or property IRI comes from the vocabulary, not from
  data, and a bad one still raises. ``repaired_iris`` lists the repairs of a record, so that
  validation can warn about each (``LAE-RDF-006``). The record URI itself (the root ``id``) is
  never repaired, in either serialization. A repaired IRI is only ever the object of a triple:
  every subject is the record URI, an internal node IRI or a derived node IRI, none of which
  ``repair_iri`` changes, so two spellings that encode to one IRI (``a b`` and ``a%20b``) can
  never merge two subjects, in one record or across records.
- A node below the root may repeat the record's pinned context (``"@context": <pinned URL>``, or
  the one-element array of it). That says nothing new and is dropped. Any other nested
  ``@context`` raises ``ExpansionError``, because it could rename ``@id`` or any property.

Both happen in ``rdf_triples``, which ``expand_to_ntriples`` and ``to_turtle`` share.

Expansion is deterministic: the same JSON-LD and context version give the same set of lines in
every process. ``EXPANDER_VERSION`` changes whenever that output could change for some input,
which includes any change to ``ENTITY_TYPES`` or ``EMBEDDED_TYPES``, and an input that used to be
refused and now expands (ADR-0007 compares it, with the context version, before trusting a
recomputed diff; a record that an older expander refused has no triples in the index it built).

pyld is synchronous and slow (about 0.5 ms per corpus record, up to 7 ms); call this module through
``asyncio.to_thread`` or a process pool from async code (conventions §4).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from typing import Any, Final

from pyld import jsonld as pyld_jsonld
from pyld.context_resolver import ContextResolver

from lae.ids import derived_node_iri, is_internal_node_iri
from lae.rdf.canonical import XSD, XSD_STRING, escape_literal
from lae.rdf.context import CONTEXT_URLS, document_loader
from lae.rdf.sanitize import clean_datetime, clean_iri

EXPANDER_VERSION: Final = 2
"""Bump when the triples produced for some input change (see the module docstring).

- 1: Phase 1 Step 1.3, with ADR-0021.
- 2: ADR-0028. Every input that version 1 expanded gives the same triples; inputs it refused (an
  ``id`` with a character an IRI may not contain, a nested pinned ``@context``) now expand.
"""

# Relative IRIs and undefined terms are resolved against this instead of pyld's default
# (http://example.org/base/), so that they can be recognised and rejected.
_RELATIVE_BASE: Final = "lae-relative:/"

LA_EQUIVALENT: Final = "https://linked.art/ns/terms/equivalent"
"""Predicate from a derived node IRI to the id the embedded node has in the JSON-LD (ADR-0021)."""

ENTITY_TYPES: Final = frozenset(
    {
        "Type",
        "Language",
        "Material",
        "Currency",
        "MeasurementUnit",
        "Person",
        "Group",
        "Place",
        "HumanMadeObject",
        "Set",
    }
)
"""Classes Linked Art never embeds: an object of one with a foreign id is always a reference."""

EMBEDDED_TYPES: Final = frozenset(
    {
        "Name",
        "Identifier",
        "TimeSpan",
        "Dimension",
        "MonetaryAmount",
        "AttributeAssignment",
        "Production",
        "Destruction",
        "Creation",
        "Birth",
        "Death",
        "Formation",
        "Dissolution",
        "Joining",
        "Leaving",
        "Acquisition",
        "Payment",
        "TransferOfCustody",
        "Move",
        "Encounter",
        "PartAddition",
        "PartRemoval",
        "Modification",
        "Transformation",
        "Addition",
        "Removal",
        "RightAcquisition",
        "Right",
        "DigitalService",
    }
)
"""Classes that exist only inside a record: one with a foreign id and any content is embedded."""

_ID_KEYS: Final = ("id", "@id")
_TYPE_KEYS: Final = ("type", "@type")
_STUB_KEYS: Final = frozenset({"id", "@id", "type", "@type", "_label", "@context"})
_CLASSIFIED_STUB_KEYS: Final = _STUB_KEYS | {"classified_as"}
# What the Linked Art JSON Schema allows on a reference (``core.json``, the ``*Ref`` definitions).
_SCHEMA_REFERENCE_KEYS: Final = _CLASSIFIED_STUB_KEYS | {"equivalent"}
# pyld's test for an absolute IRI refuses any Unicode whitespace (U+00A0, U+2028, ...), which
# N-Triples allows in an IRI; pyld would drop the triple without a word.
_WHITESPACE: Final = re.compile(r"\s")
# What ``repair_iri`` percent-encodes: every character ``_is_writable_iri`` refuses, and no other.
# That is the set N-Triples forbids in an IRI (``lae.rdf.sanitize._IRI_CHAR``) plus whitespace.
_UNWRITABLE_CHAR: Final = re.compile(r'[\x00-\x20<>"{}|^`\\]|\s')
_NESTED_CONTEXT_ERROR: Final = (
    "a node of the record carries its own @context; only the pinned context at the root is allowed"
)
_BLANK_PREFIX: Final = "_:"
_LITERAL_KEYWORDS: Final = frozenset({"@value", "@language", "@direction", "@index"})

_LANGUAGE_TAG: Final = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*")
# Lexical forms of the datatypes pyld produces from JSON numbers and booleans. Non-finite doubles
# (INF, NaN) have no JSON source and are rejected.
_LEXICAL_FORMS: Final = {
    XSD + "integer": re.compile(r"[+-]?\d+"),
    XSD + "decimal": re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"),
    XSD + "double": re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"),
    XSD + "boolean": re.compile(r"true|false|1|0"),
}
_XSD_DATETIME: Final = XSD + "dateTime"

# One private context cache: only ``document_loader`` ever fills it, so a context cached by other
# pyld users in this process cannot leak in. pyld's caches are not thread-safe, and this module
# runs in worker threads, hence the lock (pyld holds the GIL anyway).
_RESOLVER: Final = ContextResolver({}, document_loader)
_PYLD_LOCK: Final = threading.Lock()


class ExpansionError(ValueError):
    """The JSON-LD cannot be expanded to triples that are safe to store and sync."""


class BlankNodeError(ExpansionError):
    """A node has no IRI (it lacks an internal node IRI), so its triples could not be deleted."""


class OwnershipError(ExpansionError):
    """A triple's subject is neither the record URI nor one of the record's node IRIs."""


class InvalidIRIError(ExpansionError):
    """An IRI is relative, malformed, or a term the pinned context does not define."""


class InvalidLiteralError(ExpansionError):
    """A literal's lexical form is not valid for its datatype, or is not valid Unicode."""


def _identifier_key(obj: dict[str, Any]) -> str | None:
    """Return the key (``id`` or ``@id``) under which a JSON-LD object has a string id."""
    for key in _ID_KEYS:
        if isinstance(obj.get(key), str):
            return key
    return None


def _identifier(obj: dict[str, Any]) -> str | None:
    """Return the ``id`` (or ``@id``) of a JSON-LD object if it is a string."""
    key = _identifier_key(obj)
    return None if key is None else obj[key]


def _is_writable_iri(value: object) -> bool:
    """True if ``value`` is an absolute IRI that N-Triples can hold and pyld will not drop."""
    return isinstance(value, str) and clean_iri(value) == value and not _WHITESPACE.search(value)


def _percent_encode(match: re.Match[str]) -> str:
    return "".join(f"%{byte:02X}" for byte in match.group().encode("utf-8"))


def repair_iri(value: str) -> str | None:
    """Return ``value`` as an IRI that can be written, or ``None`` if it cannot be made one.

    A writable IRI is returned unchanged. Otherwise each character an IRI may not contain is
    replaced by the percent-encoding of its UTF-8 bytes in upper-case hex, and the result is
    returned if it is now a writable absolute IRI (module docstring, ADR-0028). The function is
    pure and idempotent. It is not injective: ``a b`` and ``a%20b`` give the same IRI.
    """
    if _is_writable_iri(value):
        return value
    try:
        repaired = _UNWRITABLE_CHAR.sub(_percent_encode, value)
    except UnicodeEncodeError:  # a lone surrogate
        return None
    return repaired if _is_writable_iri(repaired) else None


def _is_pinned_context(value: object, context_url: str | None) -> bool:
    """True if a nested ``@context`` is just the record's pinned context again."""
    return context_url is not None and (value == context_url or value == [context_url])


def _is_owned(iri: str, record_uri: str) -> bool:
    """True if ``iri`` is the record URI or one of that record's internal node IRIs."""
    return iri == record_uri or is_internal_node_iri(iri, record_uri)


def _types(obj: dict[str, Any]) -> set[str]:
    """Return the compact class names of a JSON-LD object (``type`` / ``@type``, one or a list)."""
    found: set[str] = set()
    for key in _TYPE_KEYS:
        value = obj.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                found.add(item)
    return found


def _is_embedded(obj: dict[str, Any]) -> bool:
    """Decide, for an object with a foreign ``id``: embedded node (True) or reference (False).

    The rule of ADR-0021, in order: a stub (``id``, ``type``, ``_label`` only) is a reference; a
    class Linked Art never embeds is a reference whatever it carries; a class that exists only
    inside a record is an embedded node; any other object is embedded exactly when it has a
    property other than ``classified_as`` (a reference may repeat its target's classification).
    A blank node identifier is left alone, so that expansion rejects it.
    """
    identifier = _identifier(obj)
    if identifier is None or identifier.startswith(_BLANK_PREFIX) or obj.keys() <= _STUB_KEYS:
        return False
    types = _types(obj)
    if types & ENTITY_TYPES:
        return False
    if types & EMBEDDED_TYPES:
        return True
    return not obj.keys() <= _CLASSIFIED_STUB_KEYS


def _collect_embedded(value: Any, record_uri: str, found: set[str]) -> None:
    """Add to ``found`` the foreign ids of the embedded nodes in ``value``.

    Only what expansion keeps is searched: nothing inside a reference counts.
    """
    if isinstance(value, list):
        for item in value:
            _collect_embedded(item, record_uri, found)
    elif isinstance(value, dict):
        identifier = _identifier(value)
        if identifier is not None and not _is_owned(identifier, record_uri):
            if not _is_embedded(value):
                return
            found.add(identifier)
        for key, item in value.items():
            if key != "@context":
                _collect_embedded(item, record_uri, found)


def _strip(
    value: Any,
    record_uri: str,
    embedded: set[str],
    derived: dict[str, str],
    context_url: str | None,
    *,
    root: bool = False,
) -> Any:
    if isinstance(value, list):
        return [_strip(item, record_uri, embedded, derived, context_url) for item in value]
    if not isinstance(value, dict):
        return value
    id_key = _identifier_key(value)
    node_iri: str | None = None
    if id_key is not None and not _is_owned(value[id_key], record_uri):
        if value[id_key] not in embedded:
            return {"id": value[id_key]}
        node_iri = derived_node_iri(record_uri, value[id_key])
        derived[node_iri] = value[id_key]
        if not _is_embedded(value):
            return {"id": node_iri}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "@context":
            if root:
                out[key] = item
            elif not _is_pinned_context(item, context_url):
                raise ExpansionError(_NESTED_CONTEXT_ERROR)
        elif node_iri is not None and key == id_key:
            out[key] = node_iri
        else:
            out[key] = _strip(item, record_uri, embedded, derived, context_url)
    return out


def _rewrite(
    jsonld: dict[str, Any], record_uri: str, context_url: str | None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return the document expansion sees, and ``{derived node IRI: original id}`` for it.

    ``context_url`` is the pinned context a nested node may repeat (``None``: the root's own).
    """
    if context_url is None:
        declared = jsonld.get("@context")
        context_url = declared if isinstance(declared, str) else None
    root_id = _identifier(jsonld)
    if root_id is not None and root_id != record_uri:
        raise OwnershipError(f"root id {root_id!r} is not the record URI {record_uri!r}")
    embedded: set[str] = set()
    _collect_embedded(jsonld, record_uri, embedded)
    derived: dict[str, str] = {}
    stripped = _strip(jsonld, record_uri, embedded, derived, context_url, root=True)
    assert isinstance(stripped, dict)
    return stripped, derived


def strip_stub_attributes(
    jsonld: dict[str, Any], record_uri: str, *, context_version: int | None = None
) -> dict[str, Any]:
    """Return a copy in which every subject is the record's own; the input is not modified.

    An object is owned if its ``id`` is ``record_uri`` or an internal node IRI of that record, or
    if it has no ``id`` (expansion then rejects it as a blank node). Any other object is foreign
    and is rewritten (module docstring, ADR-0021):

    - a reference becomes ``{"id": uri}``, whatever else it carried: ``type``, ``_label``, or a
      whole nested description, including owned-looking nodes inside it;
    - an embedded node with an id of its own keeps its content and gets the derived node IRI
      ``<record_uri>/node/<uuidv5(id)>`` as its ``id``; every other object with the same id
      becomes ``{"id": <derived node IRI>}``.

    The ``la:equivalent`` triple from a derived node to its original id is added by
    ``expand_to_ntriples``, not here, and no ``id`` is repaired here (``rdf_triples`` does that),
    so every ``id`` that is kept is the one in ``jsonld``. Raises ``OwnershipError`` if the root
    ``id`` is not ``record_uri``, and ``ExpansionError`` if a node other than the root has an
    ``@context`` of its own (it could rename ``@id`` or any property; a reference's is dropped
    with the rest). The one exception is a nested ``@context`` that repeats the record's pinned
    context, which is dropped (ADR-0028): the pinned URL of ``context_version``, or without one
    the root's own ``@context``.
    """
    context_url = None if context_version is None else _context_url(context_version)
    return _rewrite(jsonld, record_uri, context_url)[0]


def _is_foreign(obj: dict[str, Any], record_uri: str) -> bool:
    identifier = _identifier(obj)
    return identifier is not None and not _is_owned(identifier, record_uri)


def _is_schema_embedded(obj: dict[str, Any], record_uri: str) -> bool:
    """True if ``obj`` is an embedded node with a foreign ``id`` that the schema has no place for.

    That is ``_is_embedded``, less one case: the schema's reference definitions allow
    ``equivalent`` on a reference, so ``{"id", "type", "_label", "equivalent"}`` of a class that
    can be a record (a ``VisualItem``, a ``LinguisticObject``, an ``Activity``) must keep the
    ``id`` the schema requires there, although expansion writes it as a node of the record.
    """
    if not (_is_foreign(obj, record_uri) and _is_embedded(obj)):
        return False
    return bool(_types(obj) & EMBEDDED_TYPES) or not obj.keys() <= _SCHEMA_REFERENCE_KEYS


def strip_embedded_ids(jsonld: dict[str, Any], record_uri: str) -> dict[str, Any]:
    """Return a copy without the ``id`` of any embedded node that brought one of its own.

    For the JSON Schema check only (ADR-0028): the Linked Art schema does not allow ``id`` on a
    ``Name``, ``Identifier``, ``TimeSpan``, ``Dimension``, a statement or an embedded
    ``VisualItem``, but ADR-0021 makes such a node part of the record. Every object below the
    root is judged alone by ``_is_embedded``, the split expansion uses: an embedded node loses
    its ``id`` (and ``@id``); a reference, an object with an internal node IRI, and the root keep
    theirs. So does an object that the schema reads as a reference although expansion does not:
    one whose only property besides ``classified_as`` is ``equivalent`` (``_is_schema_embedded``).

    "Alone" includes objects inside a reference. Expansion drops what a reference carries, but
    the schema reads it, and there the same split holds: a ``Name`` with an id inside a concept
    reference is still a node the schema allows no ``id`` on, and a meta-type under the
    reference's ``classified_as`` is still a reference, which keeps its ``id``.

    Nothing else changes, so every JSON Pointer into the result is valid in ``jsonld``. Nothing
    inside ``@context`` is touched.
    """
    return {
        key: item if key == "@context" else _without_embedded_ids(item, record_uri)
        for key, item in jsonld.items()
    }


def _without_embedded_ids(value: Any, record_uri: str) -> Any:
    if isinstance(value, list):
        return [_without_embedded_ids(item, record_uri) for item in value]
    if not isinstance(value, dict):
        return value
    drop = _ID_KEYS if _is_schema_embedded(value, record_uri) else ()
    return {
        key: item if key == "@context" else _without_embedded_ids(item, record_uri)
        for key, item in value.items()
        if key not in drop
    }


def strip_nested_contexts(
    jsonld: dict[str, Any], *, context_version: int | None = None
) -> dict[str, Any]:
    """Return a copy without the nested ``@context`` entries that repeat the pinned context.

    For the JSON Schema check only (ADR-0028). Expansion ignores a ``@context`` below the root
    that is the record's pinned context URL (or the one-element array of it), so the schema,
    which allows ``@context`` on few nodes, must not report it either. The pinned URL is that of
    ``context_version``, or without one the root's own ``@context`` if that is a pinned URL. Any
    other nested ``@context`` is kept: the schema reports it where it does not allow one, and
    expansion refuses it everywhere outside a reference. The root's ``@context`` is kept, and
    only keys are removed, so every JSON Pointer into the result is valid in ``jsonld``.
    """
    if context_version is not None:
        context_url: str | None = _context_url(context_version)
    else:
        declared = jsonld.get("@context")
        context_url = declared if isinstance(declared, str) and declared in CONTEXT_URLS else None
    if context_url is None:
        return jsonld
    return {
        key: item if key == "@context" else _without_pinned_contexts(item, context_url)
        for key, item in jsonld.items()
    }


def _without_pinned_contexts(value: Any, context_url: str) -> Any:
    if isinstance(value, list):
        return [_without_pinned_contexts(item, context_url) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: item if key == "@context" else _without_pinned_contexts(item, context_url)
        for key, item in value.items()
        if not (key == "@context" and _is_pinned_context(item, context_url))
    }


def repaired_iris(
    jsonld: dict[str, Any], record_uri: str
) -> list[tuple[tuple[str | int, ...], str, str]]:
    """Return ``(path, original id, repaired IRI)`` for every ``id`` expansion writes repaired.

    ``path`` is the keys and positions from the root to the ``id``. Only what
    ``expand_to_ntriples`` keeps is searched, as in ``_collect_embedded``: the ``id`` of a
    reference counts, nothing inside the reference does. An ``id`` that cannot be repaired is
    not listed; expansion raises for it.
    """
    found: list[tuple[tuple[str | int, ...], str, str]] = []
    for key, item in jsonld.items():
        if key != "@context" and key not in _ID_KEYS:
            _collect_repairs(item, record_uri, (key,), found)
    return found


def _collect_repairs(
    value: Any,
    record_uri: str,
    path: tuple[str | int, ...],
    found: list[tuple[tuple[str | int, ...], str, str]],
) -> None:
    if isinstance(value, list):
        for position, item in enumerate(value):
            _collect_repairs(item, record_uri, (*path, position), found)
    elif isinstance(value, dict):
        id_key = _identifier_key(value)
        if id_key is not None and not _is_owned(value[id_key], record_uri):
            repaired = repair_iri(value[id_key])
            if repaired is not None and repaired != value[id_key]:
                found.append(((*path, id_key), value[id_key], repaired))
            if not _is_embedded(value):
                return
        for key, item in value.items():
            if key != "@context":
                _collect_repairs(item, record_uri, (*path, key), found)


def _walk_identifiers(value: Any) -> Iterator[str]:
    if isinstance(value, list):
        for item in value:
            yield from _walk_identifiers(item)
    elif isinstance(value, dict):
        identifier = _identifier(value)
        if identifier is not None:
            yield identifier
        for key, item in value.items():
            if key != "@context":
                yield from _walk_identifiers(item)


def owned_subjects(jsonld: dict[str, Any], record_uri: str) -> set[str]:
    """Return every subject this record could have emitted: a superset of what it does emit.

    That is ``record_uri``, every internal node IRI of the record used as an ``id``, and the
    derived node IRI of every other ``id`` in the document. These are the subjects
    ``DELETE WHERE`` may be issued for (decision A13, ADR-0007). All of them lie in the record's
    own namespace (ADR-0003, ADR-0021), so none can be a subject of another record, and a
    ``DELETE WHERE`` for one that has no triples does nothing. The set is deliberately wider than
    the subjects of ``expand_to_ntriples``: it includes ids that occur only inside a reference,
    and the derived IRI of ids that are references today, because the fallback exists for the
    case where an earlier expander version decided differently.
    """
    owned = {record_uri}
    for identifier in _walk_identifiers(jsonld):
        if _is_owned(identifier, record_uri):
            owned.add(identifier)
        elif not identifier.startswith(_BLANK_PREFIX):
            owned.add(derived_node_iri(record_uri, identifier))
    return owned


def _context_url(context_version: int) -> str:
    for url, version in CONTEXT_URLS.items():
        if version == context_version:
            return url
    raise ExpansionError(f"no pinned context URL for context version {context_version}")


def _check_iri(value: object, *, allow_blank_nodes: bool) -> None:
    """Raise unless ``value`` is an absolute IRI that can be written as is."""
    if isinstance(value, str):
        if value.startswith(_BLANK_PREFIX):
            if allow_blank_nodes:
                return
            raise BlankNodeError(f"blank node identifier {value!r}")
        if value.startswith(_RELATIVE_BASE):
            raise InvalidIRIError(
                "relative IRI, or a term the pinned context does not define: "
                f"{value[len(_RELATIVE_BASE) :]!r}"
            )
    if not _is_writable_iri(value):
        raise InvalidIRIError(f"not a valid absolute IRI: {value!r}")


def _check_expanded(value: Any, *, allow_blank_nodes: bool) -> None:
    """Check every IRI of an expanded document: ``@id``, ``@type`` and property keys.

    pyld's RDF conversion silently skips a triple with an IRI it does not consider absolute, so
    this has to happen on the expanded form, where the IRIs are all still there.
    """
    if isinstance(value, list):
        for item in value:
            _check_expanded(item, allow_blank_nodes=allow_blank_nodes)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "@id":
                _check_iri(item, allow_blank_nodes=allow_blank_nodes)
            elif key == "@type":
                for type_iri in item if isinstance(item, list) else [item]:
                    if type_iri != "@json":
                        _check_iri(type_iri, allow_blank_nodes=allow_blank_nodes)
            elif key in _LITERAL_KEYWORDS:
                continue
            elif key.startswith("@"):
                _check_expanded(item, allow_blank_nodes=allow_blank_nodes)
            else:
                _check_iri(key, allow_blank_nodes=allow_blank_nodes)
                _check_expanded(item, allow_blank_nodes=allow_blank_nodes)


def _prepare(value: Any, context_url: str, *, root: bool = False) -> Any:
    """Return ``value`` as pyld should see it: the two repairs of ADR-0028 (module docstring).

    A nested ``@context`` equal to the pinned one is dropped and any other raises; an ``id`` that
    ``repair_iri`` can repair is replaced. An ``id`` it cannot repair is left for
    ``_check_expanded``, which says what is wrong with it. So is the root's ``id``: the record URI
    is never repaired, or a record could be written under a subject other than its URI.
    """
    if isinstance(value, list):
        return [_prepare(item, context_url) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "@context":
            # The root's own was checked by the caller, which sets the pinned URL in its place.
            if not root and not _is_pinned_context(item, context_url):
                raise ExpansionError(_NESTED_CONTEXT_ERROR)
        elif key in _ID_KEYS and isinstance(item, str):
            out[key] = item if root else repair_iri(item) or item
        else:
            out[key] = _prepare(item, context_url)
    return out


def rdf_triples(
    jsonld: dict[str, Any], context_version: int, *, allow_blank_nodes: bool = False
) -> list[dict[str, Any]]:
    """Expand ``jsonld`` with the pinned context and return pyld's RDF triples, IRIs checked.

    Each triple is pyld's ``{"subject", "predicate", "object"}`` dict of terms. Triples from every
    graph are returned and the graph name is dropped. The document's own root ``@context`` must be
    absent or the pinned URL of ``context_version``; a nested one must repeat it and is ignored,
    and each ``id`` with characters an IRI may not contain is percent-encoded (``_prepare``).
    Shared by ``expand_to_ntriples`` and ``lae.rdf.serialize``; it does no ownership stripping
    or checking.
    """
    url = _context_url(context_version)
    declared = jsonld.get("@context")
    if declared is not None and declared != url:
        raise ExpansionError(
            f"the record's @context {declared!r} is not the pinned context {url!r} "
            f"of context version {context_version}"
        )
    options = {"documentLoader": document_loader, "contextResolver": _RESOLVER}
    document = _prepare(jsonld, url, root=True)
    document["@context"] = url
    try:
        with _PYLD_LOCK:
            expanded = pyld_jsonld.expand(document, {**options, "base": _RELATIVE_BASE})
            _check_expanded(expanded, allow_blank_nodes=allow_blank_nodes)
            dataset = pyld_jsonld.to_rdf(expanded, options)
    except pyld_jsonld.JsonLdError as error:
        raise ExpansionError(f"JSON-LD processing failed: {error}") from error
    return [triple for graph in dataset.values() for triple in graph]


def literal_parts(term: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Return ``(lexical form, datatype IRI or None, language tag or None)`` of a pyld literal.

    Plain strings have neither datatype nor language; language tags are lower-cased. Raises
    ``InvalidLiteralError`` for a lexical form that is not valid for its datatype
    (``xsd:dateTime`` through ``clean_datetime``, so signed BCE years are accepted).
    """
    value = term["value"]
    if not isinstance(value, str):
        raise InvalidLiteralError(f"literal value is not a string: {value!r}")
    language = term.get("language")
    if language is not None:
        if not isinstance(language, str) or not _LANGUAGE_TAG.fullmatch(language):
            raise InvalidLiteralError(f"not a language tag: {language!r}")
        return value, None, language.lower()
    datatype = term.get("datatype")
    if datatype is None or datatype == XSD_STRING:
        return value, None, None
    if not _is_writable_iri(datatype):
        raise InvalidIRIError(f"not a valid datatype IRI: {datatype!r}")
    if datatype == _XSD_DATETIME:
        valid = clean_datetime(value) == value
    else:
        pattern = _LEXICAL_FORMS.get(datatype)
        valid = pattern is None or pattern.fullmatch(value) is not None
    if not valid:
        raise InvalidLiteralError(f"{value!r} is not a valid {datatype}")
    return value, datatype, None


def _iri_term(term: dict[str, Any]) -> str:
    """Return a pyld IRI term as ``<iri>``; reject blank nodes and anything unwritable."""
    value = term["value"]
    if term["type"] == "blank node":
        raise BlankNodeError(
            f"blank node {value!r}: an embedded node has no internal node IRI (or no id at all)"
        )
    if not _is_writable_iri(value):
        raise InvalidIRIError(f"not a valid absolute IRI: {value!r}")
    return f"<{value}>"


def _object_term(term: dict[str, Any]) -> str:
    if term["type"] != "literal":
        return _iri_term(term)
    lexical, datatype, language = literal_parts(term)
    text = f'"{escape_literal(lexical)}"'
    if language is not None:
        return f"{text}@{language}"
    if datatype is not None:
        return f"{text}^^<{datatype}>"
    return text


def expand_to_ntriples(
    jsonld: dict[str, Any], context_version: int, *, record_uri: str | None = None
) -> frozenset[str]:
    """Return the record's triples as canonical N-Triples lines (see ``lae.rdf.canonical``).

    ``record_uri`` defaults to the root ``id``. Foreign objects are rewritten first
    (``strip_stub_attributes``), so callers pass the stored JSON-LD with its internal node IRIs.
    Each embedded node that has an id of its own is emitted under its derived node IRI, with one
    ``la:equivalent`` triple to that id (ADR-0021).
    An ``id`` with characters an IRI may not contain is written percent-encoded, and a nested
    ``@context`` that repeats the pinned one is ignored (ADR-0028).
    Raises ``BlankNodeError`` for a node without an IRI, ``OwnershipError`` for a subject the
    record does not own, ``InvalidIRIError`` / ``InvalidLiteralError`` for a term that cannot be
    written safely, and ``ExpansionError`` for anything else pyld rejects.
    """
    root_id = _identifier(jsonld)
    if root_id is None:
        raise BlankNodeError("the record has no root id")
    if record_uri is None:
        record_uri = root_id
    if not _is_writable_iri(record_uri):
        raise InvalidIRIError(f"not a valid absolute IRI: {record_uri!r}")
    doc, derived = _rewrite(jsonld, record_uri, _context_url(context_version))

    lines: set[str] = set()
    for node_iri, original in derived.items():
        # The original id no longer passes through pyld, so it is checked (and repaired) here.
        equivalent = repair_iri(original)
        if equivalent is None:
            raise InvalidIRIError(f"not a valid absolute IRI: {original!r}")
        lines.add(f"<{node_iri}> <{LA_EQUIVALENT}> <{equivalent}> .")
    owned: set[str] = set()
    for triple in rdf_triples(doc, context_version):
        subject = _iri_term(triple["subject"])
        if subject not in owned:
            value = triple["subject"]["value"]
            if not (_is_owned(value, record_uri) or value in derived):
                raise OwnershipError(f"subject {subject} is not owned by the record {record_uri!r}")
            owned.add(subject)
        predicate = _iri_term(triple["predicate"])
        lines.add(f"{subject} {predicate} {_object_term(triple['object'])} .")
    for line in lines:
        if not line.isascii():
            try:
                line.encode("utf-8")
            except UnicodeEncodeError as error:
                raise InvalidLiteralError(f"not valid Unicode: {line!r}") from error
    return frozenset(lines)
