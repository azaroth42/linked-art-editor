"""Expansion of a record's JSON-LD to canonical N-Triples under strict subject ownership.

This is what the sync worker diffs and what revisions store (spec §3.3; decisions A7, A9, A12,
A13), so three properties are enforced here rather than assumed:

**Ownership.** A record emits only triples whose subject is its own URI or one of its internal
node IRIs (``<record_uri>/node/<uuidv7>``). ``strip_stub_attributes`` reduces every object with
any other ``id`` to ``{"id": uri}`` before expansion, so a reference to a local record, a sidecar
entity or an unresolved external URI contributes the linking triple and nothing else: no
``rdf:type``, no label (also when the reference kept its ``_label``, ADR-0006), and nothing
nested inside it. After expansion every subject is checked again, and ``OwnershipError`` is raised
for one the record does not own, whatever JSON-LD construct produced it.

**No blank nodes.** ``DELETE DATA`` cannot name a blank node, so every embedded node must carry
an internal node IRI (assigned by the projection, Step 1.4). ``BlankNodeError`` otherwise.

**Nothing silently wrong or missing.** pyld drops a triple whose IRI is malformed, resolves a
relative ``id`` or an undefined ``type`` against a default base, and writes ``INF`` for an
overflowing float. Every IRI is therefore checked on the expanded document, before pyld can drop
it, and every literal when it is written; a bad one raises (``InvalidIRIError``,
``InvalidLiteralError``) instead of being repaired or skipped. Properties that the pinned context
does not define produce no triple; that is ordinary JSON-LD and is not an error.

Expansion is deterministic: the same JSON-LD and context version give the same set of lines in
every process. ``EXPANDER_VERSION`` changes whenever that output could change for some input
(ADR-0007 compares it, with the context version, before trusting a recomputed diff).

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

from lae.ids import is_internal_node_iri
from lae.rdf.canonical import XSD, XSD_STRING, escape_literal
from lae.rdf.context import CONTEXT_URLS, document_loader
from lae.rdf.sanitize import clean_datetime, clean_iri

EXPANDER_VERSION: Final = 1
"""Bump when the triples produced for some input change (see the module docstring)."""

# Relative IRIs and undefined terms are resolved against this instead of pyld's default
# (http://example.org/base/), so that they can be recognised and rejected.
_RELATIVE_BASE: Final = "lae-relative:/"

_ID_KEYS: Final = ("id", "@id")
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


def _identifier(obj: dict[str, Any]) -> str | None:
    """Return the ``id`` (or ``@id``) of a JSON-LD object if it is a string."""
    for key in _ID_KEYS:
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return None


def _is_owned(iri: str, record_uri: str) -> bool:
    """True if ``iri`` is the record URI or one of that record's internal node IRIs."""
    return iri == record_uri or is_internal_node_iri(iri, record_uri)


def _strip(value: Any, record_uri: str) -> Any:
    if isinstance(value, list):
        return [_strip(item, record_uri) for item in value]
    if not isinstance(value, dict):
        return value
    identifier = _identifier(value)
    if identifier is not None and not _is_owned(identifier, record_uri):
        return {"id": identifier}
    return {k: v if k == "@context" else _strip(v, record_uri) for k, v in value.items()}


def strip_stub_attributes(jsonld: dict[str, Any], record_uri: str) -> dict[str, Any]:
    """Return a copy in which every object the record does not own is reduced to ``{"id": uri}``.

    An object is owned if its ``id`` is ``record_uri`` or an internal node IRI of that record, or
    if it has no ``id`` (expansion then rejects it as a blank node). Everything else is foreign,
    whatever else it carries: ``type``, ``_label``, or a whole nested description, including
    owned-looking nodes inside it. Raises ``OwnershipError`` if the root ``id`` is not
    ``record_uri``. The input is not modified; ``@context`` values are passed through untouched.
    """
    root_id = _identifier(jsonld)
    if root_id is not None and root_id != record_uri:
        raise OwnershipError(f"root id {root_id!r} is not the record URI {record_uri!r}")
    stripped = _strip(jsonld, record_uri)
    assert isinstance(stripped, dict)
    return stripped


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
    """Return ``record_uri`` and every internal node IRI of that record used as an ``id``.

    These are the subjects ``DELETE WHERE`` may be issued for (decision A13, ADR-0007). Node IRIs
    of the record that occur only inside a foreign object are included: they are never emitted,
    but the ``<record_uri>/node/`` namespace belongs to the record (ADR-0003).
    """
    owned = {iri for iri in _walk_identifiers(jsonld) if is_internal_node_iri(iri, record_uri)}
    owned.add(record_uri)
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
    if clean_iri(value) != value:
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
    if clean_iri(datatype) != datatype:
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
    if clean_iri(value) != value:
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

    ``record_uri`` defaults to the root ``id``. Foreign references are stripped first
    (``strip_stub_attributes``), so callers pass the stored JSON-LD with its internal node IRIs.
    Raises ``BlankNodeError`` for a node without an IRI, ``OwnershipError`` for a subject the
    record does not own, ``InvalidIRIError`` / ``InvalidLiteralError`` for a term that cannot be
    written safely, and ``ExpansionError`` for anything else pyld rejects.
    """
    root_id = _identifier(jsonld)
    if root_id is None:
        raise BlankNodeError("the record has no root id")
    if record_uri is None:
        record_uri = root_id
    if clean_iri(record_uri) != record_uri:
        raise InvalidIRIError(f"not a valid absolute IRI: {record_uri!r}")
    doc = strip_stub_attributes(jsonld, record_uri)

    lines: set[str] = set()
    owned: set[str] = set()
    for triple in rdf_triples(doc, context_version):
        subject = _iri_term(triple["subject"])
        if subject not in owned:
            if not _is_owned(triple["subject"]["value"], record_uri):
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
