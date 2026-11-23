"""SHACL validation of one record, with results mapped back to JSON paths (spec §5; ADR-0008).

``validate_shacl`` runs every shape in ``profiles/shapes/*.ttl`` (loaded once) with pyshacl over
the record's data graph, as Spike S4 recommends (``docs/spikes/S4-shacl.md`` §7): an in-memory
pyoxigraph store, ``advanced=False``, no inference.

**Data graph.** The record's canonical N-Triples (``lae.rdf.expand_to_ntriples``, so the JSON-LD
must carry its internal node IRIs) plus the ``rdf:type`` of every referenced entity
(``reference_classes``). The class comes from ``referenced_types`` (``entity_labels.type``); a
reference whose target has no row there falls back to the ``type`` the reference itself declares,
so that a record can be validated before its targets are known (import, the CLI, the golden
corpus). Two refinements follow from the CIDOC-CRM hierarchy, because the graph has no
``rdfs:subClassOf`` triples and no inference: a target known only as a ``Type`` (every AAT
concept that ``lae bootstrap aat-labels`` seeds) also has the class a reference declares for it
when that is one of ``Type``'s subclasses (``Material``, ``Language``, ``MeasurementUnit``,
``Currency``), and a reference of one of those classes is a ``Type`` as well.

**Diagnostics.** For each ``sh:ValidationResult``, one per place it is found (see Paths):
``code`` from the ``lae:code`` of the result's ``sh:sourceShape``,
``severity`` from ``sh:resultSeverity`` (never from pyshacl's ``conforms``,
which is ``False`` for a warning), ``params.value`` from ``sh:value`` when that is a literal or
a reference (a node of the record is named by ``path``, and its internal IRI stays out). A
result whose focus node is not a node of this record is dropped: of a referenced entity the
graph holds only its class, so anything a shape concludes about it is a statement about data the
record does not have. That is safe only because every constraint on what a property may refer
to is a property shape whose focus node is the *referring* node (``profiles/shapes/README.md``,
rule 6). A focus node in the record's own namespace is never dropped, indexed or not.

**Paths.** Expansion replaces JSON structure by IRIs, so the way back is an index built from the
same document: subject IRI (the record URI, an internal node IRI, or the derived node IRI of an
embedded node that has an id of its own, ADR-0021) to the JSON Pointer of that node. A result is
located as precisely as the report allows:

1. If it has a value, the shape's ``sh:path`` is walked through the data graph from the focus
   node to that value, and the last step ``(subject, predicate, value)`` gives the place: the
   pointer of ``subject``, then the JSON key that the context maps to ``predicate`` for a node of
   that type, then the index of the item whose ``id`` is the value (if it is a node or a
   reference). SHACL reports a value once per focus node however many ways lead to it (the same
   wrong actor on two parts of a production), so one result yields one diagnostic for **every**
   place the walk finds, in path order: nothing depends on the order the store iterates in.
2. Without a value (``sh:minCount``, ``sh:maxCount``) and with a path of one predicate, the
   pointer of the focus node plus that key, whether or not the key is present.
3. Otherwise the pointer of the focus node.

The key for a predicate depends on the node's class, because the Linked Art context scopes
``part``, ``part_of``, ``member`` and ``member_of`` by class; ``_predicate_keys`` asks pyld
rather than guessing.

pyshacl is synchronous: call this through ``asyncio.to_thread`` (``lae.validation.engine`` does).
Sharing the shapes graph between threads is safe (S4 §3.7).
"""

from __future__ import annotations

import re
import time
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import pyoxigraph as ox
import pyshacl
from rdflib import RDF, XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection as RdfList
from rdflib.term import Node

from lae.ids import NODE_PATH, is_internal_node_iri
from lae.logging import get_logger
from lae.rdf import ExpansionError
from lae.rdf.expand import rdf_triples, repair_iri
from lae.settings import get_settings
from lae.validation.diagnostics import Diagnostic, Severity, child, diagnostic, ordered
from lae.validation.expansion import expand_or_diagnose

SHAPES_DIR: Final = Path(__file__).resolve().parents[4] / "profiles" / "shapes"
SH: Final = Namespace("http://www.w3.org/ns/shacl#")
LAE: Final = Namespace("urn:lae:ns:")

SHACL_ENGINE_FAILURE: Final = "LAE-SHACL-000"
"""SHACL validation could not be completed (pyshacl raised, or a result has no ``lae:code``)."""

SHACL_BUDGET_SECONDS: Final = 0.050
"""Per-record budget from Spike S4 (expansion, data graph and pyshacl): logged when exceeded."""

SHAPE_CODE: Final = re.compile(r"LAE-SHACL-\d{3}")
_TARGETS: Final = (SH.targetClass, SH.targetNode, SH.targetObjectsOf, SH.targetSubjectsOf)
_SEVERITIES: Final[Mapping[URIRef, Severity]] = {
    SH.Violation: "violation",
    SH.Warning: "warning",
    SH.Info: "info",
}
_RDF_TYPE: Final = str(RDF.type)
_PROBE: Final = "urn:lae:probe"
_ID_KEYS: Final = ("id", "@id")
_TYPE_KEYS: Final = ("type", "@type")
_CONTEXT_KEY: Final = "@context"

TYPE_CLASS: Final = "Type"
TYPE_SUBCLASSES: Final = frozenset({"Material", "Language", "MeasurementUnit", "Currency"})
"""The Linked Art classes that are subclasses of ``Type`` (``crm:E55_Type``) in CIDOC-CRM."""

logger = get_logger(__name__)


class ShapesError(ValueError):
    """The shapes in ``profiles/shapes`` break an authoring rule of its README."""


# --------------------------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------------------------


def shape_codes(shapes: Graph) -> dict[URIRef, str]:
    """Return ``{shape IRI: lae:code}`` for every targeted shape, checking the authoring rules.

    Every shape with a target must be an IRI and carry exactly one ``lae:code`` of the form
    ``LAE-SHACL-NNN``, unique across all files (``profiles/shapes/README.md``).
    """
    codes: dict[URIRef, str] = {}
    targeted = {shape for target in _TARGETS for shape in shapes.subjects(target, None)}
    for shape in sorted(targeted, key=str):
        values = [str(value) for value in shapes.objects(shape, LAE.code)]
        if not isinstance(shape, URIRef):
            raise ShapesError(f"a targeted shape is a blank node (lae:code {values})")
        if len(values) != 1 or not SHAPE_CODE.fullmatch(values[0]):
            raise ShapesError(f"{shape} must have exactly one lae:code LAE-SHACL-NNN: {values}")
        if values[0] in codes.values() or values[0] == SHACL_ENGINE_FAILURE:
            raise ShapesError(f"{shape}: lae:code {values[0]} is already used")
        codes[shape] = values[0]
    return codes


def load_shapes(shapes_dir: Path = SHAPES_DIR) -> Graph:
    """Parse every ``*.ttl`` in ``shapes_dir`` into one graph (``ShapesError`` if a rule fails)."""
    shapes = Graph()
    for path in sorted(shapes_dir.glob("*.ttl")):
        shapes.parse(path, format="turtle")
    shape_codes(shapes)
    return shapes


@lru_cache(maxsize=1)
def _shapes() -> tuple[Graph, dict[URIRef, str]]:
    shapes = load_shapes()
    return shapes, shape_codes(shapes)


def shapes_graph() -> Graph:
    """Return the shapes graph of ``profiles/shapes``, loaded once per process."""
    return _shapes()[0]


def all_shape_codes() -> frozenset[str]:
    """Return every ``lae:code`` in ``profiles/shapes``, plus ``LAE-SHACL-000``."""
    return frozenset(_shapes()[1].values()) | {SHACL_ENGINE_FAILURE}


# --------------------------------------------------------------------------------------------
# The context, asked through pyld
# --------------------------------------------------------------------------------------------


def _identifier(obj: Mapping[str, Any]) -> str | None:
    for key in _ID_KEYS:
        if isinstance(obj.get(key), str):
            identifier: str = obj[key]
            return identifier
    return None


def _types(obj: Mapping[str, Any]) -> tuple[str, ...]:
    found: set[str] = set()
    for key in _TYPE_KEYS:
        value = obj.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                found.add(item)
    return tuple(sorted(found))


@lru_cache(maxsize=1024)
def class_iri(type_name: str, context_version: int) -> str | None:
    """Return the class IRI the pinned context gives a compact ``type``; ``None`` if undefined."""
    try:
        triples = rdf_triples({"id": _PROBE, "type": type_name}, context_version)
    except ExpansionError:
        return None
    for triple in triples:
        if triple["predicate"]["value"] == _RDF_TYPE:
            iri: str = triple["object"]["value"]
            return iri
    return None


@lru_cache(maxsize=8)
def _terms(context_version: int) -> tuple[str, ...]:
    """Every term the pinned context defines with an expanded definition, at any depth."""
    from lae.rdf.context import load_context

    found: set[str] = set()

    def collect(context: Any) -> None:
        if isinstance(context, list):
            for item in context:
                collect(item)
        elif isinstance(context, dict):
            for term, definition in context.items():
                if term.startswith("@") or not isinstance(definition, dict):
                    continue
                found.add(term)
                collect(definition.get(_CONTEXT_KEY))

    collect(load_context(context_version)[_CONTEXT_KEY])
    return tuple(sorted(found))


@lru_cache(maxsize=512)
def _predicate_keys(types: tuple[str, ...], context_version: int) -> Mapping[str, tuple[str, ...]]:
    """Return ``{predicate IRI: JSON keys}`` for a node of the given classes.

    One probe node that uses every term of the context as a key is expanded with pyld, so
    type-scoped definitions (``part`` on an ``Activity`` against ``part`` on a ``Type``) come out
    as the context defines them. Keys are sorted; several keys can share a predicate.
    """
    terms = _terms(context_version)
    probe: dict[str, Any] = {term: {"id": f"{_PROBE}:{index}"} for index, term in enumerate(terms)}
    probe["id"] = _PROBE
    if types:
        probe["type"] = list(types)
    try:
        triples = rdf_triples(probe, context_version)
    except ExpansionError:
        if not types:
            return {}
        return _predicate_keys((), context_version)
    keys: dict[str, list[str]] = {}
    prefix = _PROBE + ":"
    for triple in triples:
        target = triple["object"]["value"]
        if triple["subject"]["value"] == _PROBE and target.startswith(prefix):
            suffix = target[len(prefix) :]
            if suffix.isdigit():
                keys.setdefault(triple["predicate"]["value"], []).append(terms[int(suffix)])
    return {predicate: tuple(sorted(names)) for predicate, names in keys.items()}


# --------------------------------------------------------------------------------------------
# Node index
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Located:
    """A node of the record: its JSON Pointer and its object as expansion sees it."""

    path: str
    obj: Mapping[str, Any]


@dataclass
class NodeIndex:
    """Where the subjects and references of one record are in its JSON-LD."""

    nodes: dict[str, _Located] = field(default_factory=dict)
    """Subject IRI (record URI, internal or derived node IRI) -> the node."""
    reference_types: dict[str, set[str]] = field(default_factory=dict)
    """Referenced URI -> every ``type`` the references to it declare (empty if none does)."""

    def path_of(self, iri: str) -> str | None:
        """Return the JSON Pointer of the node with this subject IRI, if it is in the record."""
        located = self.nodes.get(iri)
        return None if located is None else located.path


def build_index(
    jsonld: dict[str, Any], record_uri: str, *, context_version: int | None = None
) -> NodeIndex:
    """Index the nodes and references of ``jsonld`` (which has its internal node IRIs).

    ``context_version`` is passed on to ``strip_stub_attributes`` (which nested ``@context`` is
    the pinned one, ADR-0028).

    The document is walked together with ``strip_stub_attributes(jsonld)``, the form expansion
    reads: there a reference is exactly ``{"id": <its own id>}``, and an embedded node that has
    a foreign id carries its derived node IRI, so the two cases need not be decided again here.
    When an IRI occurs more than once (a node and pointers to it), the occurrence with the most
    properties is the node, and the first of those in document order.
    """
    from lae.rdf import strip_stub_attributes

    index = NodeIndex()
    stripped = strip_stub_attributes(jsonld, record_uri, context_version=context_version)
    index.nodes[record_uri] = _Located("", stripped)
    for key, value in stripped.items():
        if key != _CONTEXT_KEY and key in jsonld:
            _index(jsonld[key], value, child("", key), record_uri, index)
    return index


def _index(original: Any, stripped: Any, path: str, record_uri: str, index: NodeIndex) -> None:
    if isinstance(original, list) and isinstance(stripped, list):
        for position, (before, after) in enumerate(zip(original, stripped, strict=False)):
            _index(before, after, child(path, position), record_uri, index)
        return
    if not isinstance(original, dict) or not isinstance(stripped, dict):
        return
    identifier = _identifier(stripped)
    if identifier is not None:
        owned = identifier == record_uri or is_internal_node_iri(identifier, record_uri)
        if not owned and identifier == _identifier(original):
            index.reference_types.setdefault(identifier, set()).update(_types(original))
            return
        known = index.nodes.get(identifier)
        if identifier != record_uri and (known is None or len(stripped) > len(known.obj)):
            index.nodes[identifier] = _Located(path, stripped)
    for key, value in stripped.items():
        if key != _CONTEXT_KEY and key in original:
            _index(original[key], value, child(path, key), record_uri, index)


# --------------------------------------------------------------------------------------------
# Data graph
# --------------------------------------------------------------------------------------------


def reference_classes(known: str | None, declared: Collection[str]) -> set[str]:
    """Return the class names a referenced entity has in the data graph.

    ``known`` is ``entity_labels.type`` (``None`` without a row) and ``declared`` the ``type`` of
    every reference to the entity in the record.

    - ``known`` wins: what the references declare is ignored when the registry says otherwise.
    - Except that ``known == "Type"`` does not contradict a declared subclass of ``Type``. The
      registry holds every AAT concept as a plain ``Type`` (``lae.bootstrap.aat``), so without
      this a correct ``made_of`` (a ``Material``) or ``language`` (a ``Language``) would fail its
      ``sh:class`` as soon as the AAT labels are loaded.
    - Without a row the declared classes stand.
    - A subclass of ``Type`` is a ``Type`` too, since there is no inference to say so.
    """
    if known is None:
        names = set(declared)
    else:
        names = {known}
        if known == TYPE_CLASS:
            names |= TYPE_SUBCLASSES.intersection(declared)
    if names & TYPE_SUBCLASSES:
        names.add(TYPE_CLASS)
    return names


def reference_type_triples(
    index: NodeIndex, referenced_types: Mapping[str, str], context_version: int
) -> set[str]:
    """Return the ``rdf:type`` N-Triples lines of every reference whose class is known.

    ``referenced_types`` (``entity_labels.type``) wins over the ``type`` a reference declares,
    as ``reference_classes`` describes.
    """
    lines: set[str] = set()
    for uri, declared in index.reference_types.items():
        # The subject is the IRI expansion wrote for the reference (ADR-0028); the registry and
        # the index know it by the id in the JSON-LD.
        subject = repair_iri(uri)
        if subject is None:
            # Not an IRI N-Triples can hold. Expansion has refused it already if it is the
            # object of a triple; under a property the context does not define it is inert.
            continue
        for type_name in reference_classes(referenced_types.get(uri), declared):
            iri = class_iri(type_name, context_version)
            if iri is not None:
                lines.add(f"<{subject}> <{_RDF_TYPE}> <{iri}> .")
    return lines


def _store(lines: set[str] | frozenset[str]) -> ox.Store:
    store = ox.Store()
    store.load("\n".join(lines).encode("utf-8"), format=ox.RdfFormat.N_TRIPLES)
    return store


# --------------------------------------------------------------------------------------------
# SHACL property paths, walked through the data graph
# --------------------------------------------------------------------------------------------

_Hop = tuple[str, str, ox.NamedNode | ox.Literal | ox.BlankNode | ox.Triple]
_Trail = tuple[_Hop, ...]


def _walk(
    store: ox.Store, shapes: Graph, start: str, path: Node, trail: _Trail = ()
) -> Iterator[tuple[Any, _Trail]]:
    """Yield ``(end term, hops)`` for every way ``path`` leads from the IRI ``start``.

    Supports predicate, sequence, alternative, zero-or-more, one-or-more and zero-or-one paths.
    An inverse path yields nothing (the caller then falls back to the focus node).
    """
    if isinstance(path, URIRef):
        predicate = str(path)
        for quad in store.quads_for_pattern(ox.NamedNode(start), ox.NamedNode(predicate), None):
            yield quad.object, (*trail, (start, predicate, quad.object))
        return
    if shapes.value(path, RDF.first) is not None:
        yield from _walk_sequence(store, shapes, start, list(RdfList(shapes, path)), trail)
        return
    alternatives = shapes.value(path, SH.alternativePath)
    if alternatives is not None:
        for member in RdfList(shapes, alternatives):
            yield from _walk(store, shapes, start, member, trail)
        return
    for keyword, include_start, repeat in (
        (SH.zeroOrMorePath, True, True),
        (SH.oneOrMorePath, False, True),
        (SH.zeroOrOnePath, True, False),
    ):
        inner = shapes.value(path, keyword)
        if inner is not None:
            yield from _walk_repeated(store, shapes, start, inner, trail, include_start, repeat)
            return


def _walk_sequence(
    store: ox.Store, shapes: Graph, start: str, steps: list[Node], trail: _Trail
) -> Iterator[tuple[Any, _Trail]]:
    if not steps:
        yield ox.NamedNode(start), trail
        return
    for end, hops in _walk(store, shapes, start, steps[0], trail):
        if len(steps) == 1:
            yield end, hops
        elif isinstance(end, ox.NamedNode):
            yield from _walk_sequence(store, shapes, end.value, steps[1:], hops)


def _walk_repeated(
    store: ox.Store,
    shapes: Graph,
    start: str,
    inner: Node,
    trail: _Trail,
    include_start: bool,
    repeat: bool,
) -> Iterator[tuple[Any, _Trail]]:
    if include_start:
        yield ox.NamedNode(start), trail
    seen = {start}
    frontier: list[tuple[str, _Trail]] = [(start, trail)]
    while frontier:
        node, hops = frontier.pop()
        for end, extended in _walk(store, shapes, node, inner, hops):
            if isinstance(end, ox.NamedNode):
                if end.value in seen:
                    continue
                seen.add(end.value)
                if repeat:
                    frontier.append((end.value, extended))
            yield end, extended


def _literal_key(literal: Literal) -> tuple[str, str | None, str | None]:
    datatype = None if literal.datatype in (None, XSD.string) else str(literal.datatype)
    return str(literal), literal.language, datatype


def _same_term(term: Any, value: Node) -> bool:
    """True if a term of the store is the ``sh:value`` of a result (an rdflib term)."""
    if isinstance(value, URIRef):
        return isinstance(term, ox.NamedNode) and term.value == str(value)
    if isinstance(value, Literal) and isinstance(term, ox.Literal):
        # rdflib may normalise a lexical form on the way into the report ("007" -> "7"), so the
        # store's literal is built the same way before the two are compared.
        if term.language:
            same = Literal(term.value, lang=term.language)
        else:
            same = Literal(term.value, datatype=URIRef(term.datatype.value))
        return _literal_key(same) == _literal_key(value)
    return False


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    store: ox.Store
    shapes: Graph
    index: NodeIndex
    context_version: int
    record_uri: str

    @property
    def node_prefix(self) -> str:
        return self.record_uri + NODE_PATH


def _key_for(node: _Located, predicate: str, context_version: int) -> str | None:
    """Return the JSON key of ``predicate`` on this node: one it has, else the first defined."""
    keys = _predicate_keys(_types(node.obj), context_version).get(predicate, ())
    return next((key for key in keys if key in node.obj), keys[0] if keys else None)


def _hop_path(hop: _Hop, ctx: _Context) -> str | None:
    """Return the JSON Pointer of the value of one step ``(subject, predicate, value)``."""
    subject, predicate, value = hop
    node = ctx.index.nodes.get(subject)
    if node is None:
        return None
    key = _key_for(node, predicate, ctx.context_version)
    if key is None:
        return node.path
    path = child(node.path, key)
    items = node.obj.get(key)
    if isinstance(value, ox.NamedNode) and isinstance(items, list):
        for position, item in enumerate(items):
            if isinstance(item, dict) and _identifier(item) == value.value:
                return child(path, position)
    return path


def _locate(focus: str, shape_path: Node | None, value: Node | None, ctx: _Context) -> list[str]:
    """Return the JSON Pointers for a result, sorted; at least one (module docstring, "Paths")."""
    focus_node = ctx.index.nodes.get(focus)
    if focus_node is None:
        return [""]  # see ``_is_ours``: kept, at the record
    if shape_path is None:
        return [focus_node.path]
    if value is not None:
        places: set[str] = set()
        for end, hops in _walk(ctx.store, ctx.shapes, focus, shape_path):
            if hops and _same_term(end, value):
                located = _hop_path(hops[-1], ctx)
                if located is not None:
                    places.add(located)
        if places:
            return sorted(places)
    if isinstance(shape_path, URIRef):
        key = _key_for(focus_node, str(shape_path), ctx.context_version)
        if key is not None:
            return [child(focus_node.path, key)]
    return [focus_node.path]


def _value_param(value: Node) -> Any:
    if isinstance(value, Literal):
        python = value.toPython()
        return python if isinstance(python, bool | int | float | str) else str(value)
    return str(value)


def _is_ours(focus: Node | None, ctx: _Context) -> bool:
    """True if a term is a node of the record (and so is named by a path, never by its IRI)."""
    if not isinstance(focus, URIRef):
        return False
    iri = str(focus)
    if iri in ctx.index.nodes:
        return True
    # Not indexed, yet a subject in the record's namespace and not one of its references: no
    # input is known to produce one, but such a finding must not be lost.
    return iri.startswith(ctx.node_prefix) and iri not in ctx.index.reference_types


def _result_diagnostics(
    report: Graph, result: Node, codes: Mapping[URIRef, str], ctx: _Context
) -> list[Diagnostic]:
    """Return the diagnostics of one ``sh:ValidationResult``: one per place, none if dropped."""
    focus = report.value(result, SH.focusNode)
    if not _is_ours(focus, ctx):
        return []
    shape = report.value(result, SH.sourceShape)
    reported = report.value(result, SH.resultSeverity)
    severity = (
        _SEVERITIES.get(reported, "violation") if isinstance(reported, URIRef) else "violation"
    )
    message = report.value(result, SH.resultMessage)
    fallback = None if message is None else str(message)
    code = codes.get(shape) if isinstance(shape, URIRef) else None
    if code is None:
        # README rule 2 was broken (a nested shape): still a finding, never silently dropped.
        detail = {"detail": f"result of a shape without lae:code: {shape}"}
        path = _locate(str(focus), None, None, ctx)[0]
        return [diagnostic(SHACL_ENGINE_FAILURE, path, "violation", detail, fallback=fallback)]
    shape_path = ctx.shapes.value(shape, SH.path)
    if shape_path is None:
        # A SHACL-SPARQL constraint on a node shape can name the property with ?path.
        result_path = report.value(result, SH.resultPath)
        shape_path = result_path if isinstance(result_path, URIRef) else None
    value = report.value(result, SH.value)
    if value == focus:
        # pyshacl reports the focus node as the value of a SHACL-SPARQL result without ?value.
        value = None
    params: dict[str, Any] = {}
    if value is not None and not isinstance(value, BNode) and not _is_ours(value, ctx):
        # A node of the record is named by ``path``; its IRI is internal and stays out of params.
        params["value"] = _value_param(value)
    return [
        diagnostic(code, path, severity, params, fallback=fallback)
        for path in _locate(str(focus), shape_path, value, ctx)
    ]


def run_shapes(
    triples: frozenset[str],
    index: NodeIndex,
    referenced_types: Mapping[str, str],
    context_version: int,
    record_uri: str,
) -> list[Diagnostic]:
    """Validate expanded triples against the shapes and return the mapped diagnostics."""
    shapes, codes = _shapes()
    store = _store(triples | reference_type_triples(index, referenced_types, context_version))
    # pyshacl takes a pyoxigraph Store as the data graph (S4 §2.1); its annotations do not say so.
    _, report, _ = pyshacl.validate(
        store,  # type: ignore[arg-type]
        shacl_graph=shapes,
        advanced=False,
        inference="none",
    )
    ctx = _Context(store, shapes, index, context_version, record_uri)
    found: list[Diagnostic] = []
    for result in report.objects(None, SH.result):
        found.extend(_result_diagnostics(report, result, codes, ctx))
    return ordered(found)


def validate_shacl(
    jsonld: dict[str, Any],
    record_uri: str,
    referenced_types: Mapping[str, str],
    *,
    context_version: int | None = None,
) -> list[Diagnostic]:
    """Return the SHACL diagnostics of a record that passed JSON Schema.

    ``jsonld`` carries its internal node IRIs; ``referenced_types`` maps referenced URIs to
    their class name (``entity_labels.type``). A record that cannot be expanded yields the
    ``LAE-RDF-*`` violation for the reason, and a failure inside pyshacl yields
    ``LAE-SHACL-000``: neither raises. Exceeding ``SHACL_BUDGET_SECONDS`` is logged, not an
    error.
    """
    version = get_settings().context_version if context_version is None else context_version
    try:
        _shapes()  # loaded before the clock starts: a one-off cost, not this record's
    except Exception as error:
        logger.exception("shacl_shapes_failed")
        return [diagnostic(SHACL_ENGINE_FAILURE, "", "violation", {"detail": repr(error)})]
    started = time.perf_counter()
    triples, failures = expand_or_diagnose(jsonld, record_uri, version)
    if triples is None:
        return failures
    try:
        index = build_index(jsonld, record_uri, context_version=version)
        found = run_shapes(triples, index, referenced_types, version, record_uri)
    except Exception as error:
        logger.exception("shacl_validation_failed", record_uri=record_uri)
        return [diagnostic(SHACL_ENGINE_FAILURE, "", "violation", {"detail": repr(error)})]
    elapsed = time.perf_counter() - started
    if elapsed > SHACL_BUDGET_SECONDS:
        logger.warning(
            "shacl_budget_exceeded",
            record_uri=record_uri,
            seconds=round(elapsed, 4),
            budget_seconds=SHACL_BUDGET_SECONDS,
            triples=len(triples),
        )
    return found
