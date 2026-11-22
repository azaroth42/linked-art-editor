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
``InvalidLiteralError``) instead of being repaired or skipped. Properties that the pinned context
does not define produce no triple; that is ordinary JSON-LD and is not an error.

Expansion is deterministic: the same JSON-LD and context version give the same set of lines in
every process. ``EXPANDER_VERSION`` changes whenever that output could change for some input,
which includes any change to ``ENTITY_TYPES`` or ``EMBEDDED_TYPES`` (ADR-0007 compares it, with
the context version, before trusting a recomputed diff).

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

EXPANDER_VERSION: Final = 1
"""Bump when the triples produced for some input change (see the module docstring)."""

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
# pyld's test for an absolute IRI refuses any Unicode whitespace (U+00A0, U+2028, ...), which
# N-Triples allows in an IRI; pyld would drop the triple without a word.
_WHITESPACE: Final = re.compile(r"\s")
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
    value: Any, record_uri: str, embedded: set[str], derived: dict[str, str], *, root: bool = False
) -> Any:
    if isinstance(value, list):
        return [_strip(item, record_uri, embedded, derived) for item in value]
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
            if not root:
                raise ExpansionError(
                    "a node of the record carries its own @context; only the pinned context "
                    "at the root is allowed"
                )
            out[key] = item
        elif node_iri is not None and key == id_key:
            out[key] = node_iri
        else:
            out[key] = _strip(item, record_uri, embedded, derived)
    return out


def _rewrite(jsonld: dict[str, Any], record_uri: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Return the document expansion sees, and ``{derived node IRI: original id}`` for it."""
    root_id = _identifier(jsonld)
    if root_id is not None and root_id != record_uri:
        raise OwnershipError(f"root id {root_id!r} is not the record URI {record_uri!r}")
    embedded: set[str] = set()
    _collect_embedded(jsonld, record_uri, embedded)
    derived: dict[str, str] = {}
    stripped = _strip(jsonld, record_uri, embedded, derived, root=True)
    assert isinstance(stripped, dict)
    return stripped, derived


def strip_stub_attributes(jsonld: dict[str, Any], record_uri: str) -> dict[str, Any]:
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
    ``expand_to_ntriples``, not here. Raises ``OwnershipError`` if the root ``id`` is not
    ``record_uri``, and ``ExpansionError`` if a node other than the root has an ``@context`` of
    its own (it could rename ``@id`` or any property; a reference's is dropped with the rest).
    """
    return _rewrite(jsonld, record_uri)[0]


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


def rdf_triples(
    jsonld: dict[str, Any], context_version: int, *, allow_blank_nodes: bool = False
) -> list[dict[str, Any]]:
    """Expand ``jsonld`` with the pinned context and return pyld's RDF triples, IRIs checked.

    Each triple is pyld's ``{"subject", "predicate", "object"}`` dict of terms. Triples from every
    graph are returned and the graph name is dropped. The document's own root ``@context`` must be
    absent or the pinned URL of ``context_version``. Shared by ``expand_to_ntriples`` and
    ``lae.rdf.serialize``; it does no ownership stripping or checking.
    """
    url = _context_url(context_version)
    declared = jsonld.get("@context")
    if declared is not None and declared != url:
        raise ExpansionError(
            f"the record's @context {declared!r} is not the pinned context {url!r} "
            f"of context version {context_version}"
        )
    options = {"documentLoader": document_loader, "contextResolver": _RESOLVER}
    try:
        with _PYLD_LOCK:
            expanded = pyld_jsonld.expand(
                {**jsonld, "@context": url}, {**options, "base": _RELATIVE_BASE}
            )
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
    doc, derived = _rewrite(jsonld, record_uri)

    lines: set[str] = set()
    for node_iri, original in derived.items():
        # The original id no longer passes through pyld, so it is checked here.
        if not _is_writable_iri(original):
            raise InvalidIRIError(f"not a valid absolute IRI: {original!r}")
        lines.add(f"<{node_iri}> <{LA_EQUIVALENT}> <{original}> .")
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
