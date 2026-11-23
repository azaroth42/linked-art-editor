"""The typed semantic diff of two versions of one record (spec §4.3; decisions B10, ADR-0029).

``semantic_diff(old, new)`` compares two **projected** JSON-LD documents (internal node IRIs
present, as ``records.data``, a stored revision and ``lae.projection.project`` give them) and
returns a list of typed ``Change`` objects, never prose. It is a pure function: no database, no
clock, no randomness, and the inputs are not modified. The publish dialog, the history browser,
the graph overlay, the offline rebase modal and the agent tools all read this one list, so what
follows is a contract. Fields may be added later; nothing here is reshaped.

Vocabulary (rules R3, R6-R8 of ``docs/spikes/S3-projection.md``)
----------------------------------------------------------------
An **object** is the root, a node or a reference. A property of an object holds one of three
**shapes**, decided from the JSON alone exactly as hydration decides it:

- an **array**: a non-empty JSON array. Each item has an **item key**, computed in array order
  as hydration computes it (R7): the ``id`` of a node or reference, a type-tagged text for an
  opaque item (``=s:…``, ``=n:…``, ``=b:…``, ``=z``, ``=j:…``), and `` #2``, `` #3``, … appended
  for a repeat within the same array;
- a **single** object: a node or reference that is not inside an array;
- a **literal**: everything else (a scalar, the ``Text`` of a ``content``, an empty array, an
  opaque object, and ``@context`` whatever its form).

Matching
--------
- The two roots are always matched.
- Array items are matched **by item key**: nodes by internal node IRI, references by target
  URI, opaque items by value. The *n*-th occurrence of an ``id`` in ``old`` is therefore matched
  with the *n*-th occurrence in ``new`` (the `` #n`` suffix). The item keys of ``old`` are the
  entry keys of a ``Doc`` freshly hydrated from ``old`` (``rules.entry_key(property, key)``).
- A single object is matched with the single object of the same property.
- A matched pair is **the same object** only if both are nodes or references with equal ``id``
  and equal ``type`` (two objects without ``id`` only if they are equal altogether). The same
  object is compared property by property (a reference and a node are both just objects here:
  a reference that gains ``classified_as`` yields an ``add_ref`` inside it). Any other pair is
  a **replacement**: the old value is reported removed and the new one added, both whole. A
  changed ``type`` is always a replacement, because ``type`` is set when a node is created (R20).
- Opaque items matched by key are equal by construction; if their values differ after all (only
  possible when a string itself ends in `` #n``) they are a replacement too.
- A property whose shape differs between the two sides is not matched at all: everything on
  the old side is removed, then everything on the new side added.
- Nothing is matched across properties or across parents. A node that **moves** to another
  property or another parent is a ``remove_node`` and an ``add_node`` with the same
  ``node_id``; a consumer that wants to show a move pairs them by ``node_id``.

Equality of JSON values is exact: ``1`` and ``1.0`` differ, as do ``true`` and ``1``; object
key order never matters and array order always does (R16).

The six kinds
-------------
Every change has ``kind``, ``path``, ``parent`` and ``property``:

- ``parent`` is the chain of ``Step``s from the root to the object that has the property (empty
  for the root). A step holds the property it goes through, the item ``key`` (``null`` for a
  single object), and the ``id`` and ``type`` of the object it reaches, as they are in ``new``.
  This is the exact address; software uses it and never parses ``path``.
- ``path`` is the same address for people, in profile path syntax with a qualifier: a step into
  an array item is ``key[Type]``, or ``key[Type:Label]`` when the item has a classification
  (the label of its first ``classified_as``: ``labels[id]``, else that object's own ``_label``;
  no label, no qualifier); a step into a single object is the bare key; the last segment is
  ``property``. Example: ``identified_by[Name:Primary Name].content``. Labels are not escaped.

``key`` on a change is the item key of the item concerned, or ``null`` when the property holds
(or gets) a single value rather than an array.

======================= ========================================================================
``add_node``            a node was added: ``node_id``, ``node_type``, ``summary_fields``, the
                        whole node as ``value``, and ``before``
``remove_node``         a node was removed: the same fields with the old node, without ``before``
``add_ref``             a reference was added: ``uri``, ``type``, ``label``, ``value``, ``before``
``remove_ref``          a reference was removed: the same without ``before``
``update_literal``      a literal changed: ``op``, ``old``, ``new``. ``op`` is ``"set"`` (there was
                        no value; ``old`` is ``null`` and means nothing), ``"clear"`` (there is no
                        value any more; ``new`` means nothing) or ``"change"`` (both are values,
                        and ``null`` is then the JSON value null). An opaque array item that was
                        added or removed is a ``set`` or ``clear`` with its ``key`` (and
                        ``before`` when added), since its value is its identity
``reorder``             the items of an array that are in both versions changed their relative
                        order: ``order`` is every item key of the property in ``new``'s order,
                        ``previous`` the keys present in both, in ``old``'s order
======================= ========================================================================

A "node" is anything R3 classifies as a node, including an object with a foreign ``id`` that
carries more than ``id``, ``type`` and ``_label`` (a ``Type`` with its meta-type inline); a
"reference" has at most those three keys. ``node_type`` / ``type`` are ``null`` when the
object's ``type`` is not a string, and ``node_id`` when it has no ``id``.

``before`` on an addition to an array is the item key of the nearest following item of ``new``
that is in both versions, or ``null`` to append. Additions are listed in ``new``'s order, so
inserting each one just before its ``before`` reproduces ``new``'s order whenever no ``reorder``
of that property follows; when one follows, its ``order`` is the final order.

``summary_fields`` is what a client needs to describe a node in one line, taken from the node's
own properties (not from nested nodes), in key order, without ``id``, ``type`` and ``@context``:
a string, number or boolean as it is; a reference, or any object with an ``id`` that is not one
of this record's internal node IRIs, as ``{uri, type, label}``; an array as the list of its
items of those two sorts. Everything else is left out; the whole node is in ``value``.

**Text edits.** A changed ``content`` of a ``Name``, ``Identifier`` or ``LinguisticObject`` (a
shared ``Text``, R8.2) is one ``update_literal`` with the whole ``old`` and ``new`` strings and
``text_edit = {index, delete, insert}``: the single splice of R23 (longest common prefix, then
longest common suffix of the rest), counted in Unicode code points (R19). A client shows the
splice, and ``set_scalar`` applies exactly that splice to a live ``Text``. Several separate
edits in one text are reported as the one splice that spans them.

Order of the list
-----------------
Depth-first from the root. The properties of an object are visited in code-point order of
their keys (JSON key order is not significant). For one property:

1. removals, in ``old``'s order (for a shape change: everything on the old side);
2. the changes inside each object that is in both versions, in ``new``'s order (recursively);
3. additions, in ``new``'s order;
4. at most one ``reorder``.

The list is empty exactly when the two documents are equal. Applying every change in list order
to a ``Doc`` hydrated from ``old`` yields a document that projects to ``new`` (property-tested
over the corpus, ``tests/diff/test_property.py``; ``tests/diff/applier.py`` is the reference
applier until ``lae.crdt.ops.apply_change`` exists, Phase 3 Step 3.6). A subset can be applied
too, with two things to respect: the removal and the addition of a replacement belong together,
and a ``reorder`` may name keys that were not added (skip them).

Two notes for an applier on a live draft. The path operations never create a repeat (R7), so an
addition whose ``key`` carries a `` #n`` suffix cannot be made through ``add_reference``. And
``update_literal`` on the root's ``id`` or ``type`` (two versions of one record never differ
there, but the function is total) is not a write the path operations accept (R20).

Labels
------
The backend stores no ``_label`` on resolvable references (spec §3.2), so ``labels`` maps a URI
to the label to show for it (``lae.labels.transclude.resolve_labels``). It only feeds the
display fields ``label`` and ``path``; ``value``, ``old`` and ``new`` are always the stored
JSON, so a change can be applied as it is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from lae.ids import is_internal_node_iri
from lae.projection.canonical_json import canonical_json
from lae.projection.hydrate import is_text_content
from lae.projection.rules import (
    OPAQUE_KEYS,
    is_reference,
    is_structured,
    text_change,
    unique_key,
    value_key,
)

CLASSIFIED_AS: Final = "classified_as"
LABEL_KEY: Final = "_label"
SUMMARY_SKIPPED_KEYS: Final = frozenset({"id", "type", "@context"})
"""Keys that never appear in ``summary_fields``."""

Scalar = str | bool | int | float
"""A JSON scalar other than null, as ``summary_fields`` carries it."""


class Step(BaseModel):
    """One step from an object to an object inside it, on the way to a change."""

    property: str
    """The JSON key that holds the object."""
    key: str | None
    """Item key of the object in the array ``property`` (R7); ``None`` for a single object."""
    id: str | None
    """The object's ``id`` (internal node IRI, or the target URI of a reference)."""
    type: str | None
    """The object's ``type`` when it is a string."""
    classified_as: str | None
    """``id`` of the object's first ``classified_as``."""
    label: str | None
    """Label of that classification: the qualifier in ``path``."""


class RefSummary(BaseModel):
    """A reference inside ``summary_fields``."""

    uri: str
    type: str | None
    label: str | None


SummaryValue = Scalar | RefSummary | list[Scalar | RefSummary]


class TextEdit(BaseModel):
    """The single splice that turns ``old`` into ``new`` (R23), in Unicode code points (R19)."""

    index: int
    delete: int
    insert: str


class _ChangeBase(BaseModel):
    # Every field is always serialized, so the OpenAPI (serialization) schema marks every field
    # required, ``kind`` included: generated clients get a discriminated union.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    path: str
    """Display path with qualifiers, e.g. ``identified_by[Name:Primary Name].content``."""
    parent: list[Step]
    """Steps from the root to the object that has ``property``; empty for the root."""
    property: str
    """The JSON key the change is about."""


class AddNode(_ChangeBase):
    """A node was added to ``property``."""

    kind: Literal["add_node"] = "add_node"
    key: str | None
    before: str | None = None
    node_type: str | None
    node_id: str | None
    summary_fields: dict[str, SummaryValue]
    value: dict[str, Any]
    """The whole node as it is in ``new``, internal node IRIs included."""


class RemoveNode(_ChangeBase):
    """A node was removed from ``property``."""

    kind: Literal["remove_node"] = "remove_node"
    key: str | None
    node_type: str | None
    node_id: str | None
    summary_fields: dict[str, SummaryValue]
    value: dict[str, Any]
    """The whole node as it was in ``old``."""


class AddRef(_ChangeBase):
    """A reference was added to ``property``."""

    kind: Literal["add_ref"] = "add_ref"
    key: str | None
    before: str | None = None
    uri: str
    type: str | None
    label: str | None
    value: dict[str, Any]
    """The reference as stored (``id`` and, if present, ``type`` and ``_label``)."""


class RemoveRef(_ChangeBase):
    """A reference was removed from ``property``."""

    kind: Literal["remove_ref"] = "remove_ref"
    key: str | None
    uri: str
    type: str | None
    label: str | None
    value: dict[str, Any]


class UpdateLiteral(_ChangeBase):
    """A literal was set, changed or cleared."""

    kind: Literal["update_literal"] = "update_literal"
    op: Literal["set", "change", "clear"]
    """``set``: no value before (``old`` means nothing); ``clear``: none after; else ``change``."""
    key: str | None = None
    """Item key when the literal is an opaque array item that was added or removed."""
    before: str | None = None
    old: Any = None
    new: Any = None
    text_edit: TextEdit | None = None


class Reorder(_ChangeBase):
    """The items of ``property`` that are in both versions changed their relative order."""

    kind: Literal["reorder"] = "reorder"
    order: list[str]
    """Every item key of the property, in ``new``'s order."""
    previous: list[str]
    """The item keys present in both versions, in ``old``'s order."""


Change = Annotated[
    AddNode | RemoveNode | UpdateLiteral | AddRef | RemoveRef | Reorder,
    Field(discriminator="kind"),
]
"""One typed change (decision B10)."""

CHANGE_KINDS: Final = (
    "add_node",
    "remove_node",
    "update_literal",
    "add_ref",
    "remove_ref",
    "reorder",
)
"""Every ``kind``; the frontend has one i18n key per kind (``editor.diff.<kind>``)."""


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


def same_json(a: Any, b: Any) -> bool:
    """Exact JSON equality (R16): key order ignored, ``1`` / ``1.0`` / ``true`` all distinct."""
    return a is b or canonical_json(a) == canonical_json(b)


def item_keys(items: list[Any]) -> list[str]:
    """The item key of each item of an array, as hydration assigns them (R7).

    A node without an ``id`` (never the case in a projected document) is keyed by its value.
    """
    used: set[str] = set()
    keys: list[str] = []
    for item in items:
        item_id = item.get("id") if is_structured(item) else None
        key = unique_key(item_id if isinstance(item_id, str) else value_key(item), used)
        used.add(key)
        keys.append(key)
    return keys


def format_path(parent: Sequence[Step], prop: str) -> str:
    """The display path of ``prop`` on the object ``parent`` leads to (module docstring)."""
    segments: list[str] = []
    for step in parent:
        qualifier = ""
        if step.key is not None:
            qualifier = step.type or ""
            if step.label:
                qualifier += f":{step.label}"
        segments.append(f"{step.property}[{qualifier}]" if qualifier else step.property)
    segments.append(prop)
    return ".".join(segments)


@dataclass(frozen=True, slots=True)
class _Context:
    labels: Mapping[str, str]
    record_uri: str | None

    def label_of(self, obj: Any) -> str | None:
        """The label of a reference-like object: the looked-up one, else its own ``_label``.

        The same precedence as read-time transclusion (``hydrate_labels``): a stored ``_label``
        is only what there is for a target that has no ``entity_labels`` row.
        """
        if not isinstance(obj, dict):
            return None
        uri = obj.get("id")
        looked_up = self.labels.get(uri) if isinstance(uri, str) else None
        return looked_up if looked_up is not None else _string(obj.get(LABEL_KEY))

    def is_internal(self, iri: str) -> bool:
        return self.record_uri is not None and is_internal_node_iri(iri, self.record_uri)

    def step(self, prop: str, key: str | None, obj: dict[str, Any]) -> Step:
        classification = obj.get(CLASSIFIED_AS)
        if isinstance(classification, list):
            classification = classification[0] if classification else None
        classified_as = classification.get("id") if isinstance(classification, dict) else None
        return Step(
            property=prop,
            key=key,
            id=_string(obj.get("id")),
            type=_string(obj.get("type")),
            classified_as=_string(classified_as),
            label=self.label_of(classification),
        )

    def ref_summary(self, value: Any) -> RefSummary | None:
        """``{uri, type, label}`` for an object with an ``id`` outside this record's nodes."""
        if not is_structured(value):
            return None
        uri = value.get("id")
        if not isinstance(uri, str) or self.is_internal(uri):
            return None
        return RefSummary(uri=uri, type=_string(value.get("type")), label=self.label_of(value))

    def summary_fields(self, node: dict[str, Any]) -> dict[str, SummaryValue]:
        fields: dict[str, SummaryValue] = {}
        for key in sorted(node):
            if key in SUMMARY_SKIPPED_KEYS:
                continue
            value = node[key]
            if isinstance(value, list):
                listed = [s for s in map(self._summary_item, value) if s is not None]
                if listed:
                    fields[key] = listed
            else:
                single = self._summary_item(value)
                if single is not None:
                    fields[key] = single
        return fields

    def _summary_item(self, value: Any) -> Scalar | RefSummary | None:
        if isinstance(value, str | bool | int | float):
            return value
        return self.ref_summary(value)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


_ABSENT: Final = object()
"""Marks a property that an object does not have (``None`` is a JSON value)."""

_LITERAL: Final = "literal"
_SINGLE: Final = "single"
_ARRAY: Final = "array"


def _shape(key: str, value: Any) -> str | None:
    """The shape of a property value (module docstring); ``None`` if the property is absent."""
    if value is _ABSENT:
        return None
    if key in OPAQUE_KEYS:
        return _LITERAL
    if is_structured(value):
        return _SINGLE
    if isinstance(value, list) and value:
        return _ARRAY
    return _LITERAL


def _same_object(old: Any, new: Any) -> bool:
    """Whether a matched pair is one object to compare inside, rather than a replacement."""
    if not (is_structured(old) and is_structured(new)):
        return False
    if "id" not in old or "id" not in new:
        return same_json(old, new)
    if old["id"] != new["id"] or ("type" in old) != ("type" in new):
        return False
    return "type" not in old or same_json(old["type"], new["type"])


# --------------------------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------------------------


class _Differ:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.changes: list[Change] = []

    # -- emitting ----------------------------------------------------------------------------

    def _removed(self, parent: tuple[Step, ...], prop: str, key: str | None, item: Any) -> None:
        common: dict[str, Any] = {
            "path": format_path(parent, prop),
            "parent": list(parent),
            "property": prop,
            "key": key,
        }
        if not is_structured(item):
            self.changes.append(UpdateLiteral(**common, op="clear", old=item))
        elif is_reference(item):
            self.changes.append(
                RemoveRef(
                    **common,
                    uri=item["id"],
                    type=_string(item.get("type")),
                    label=self.context.label_of(item),
                    value=item,
                )
            )
        else:
            self.changes.append(
                RemoveNode(
                    **common,
                    node_type=_string(item.get("type")),
                    node_id=_string(item.get("id")),
                    summary_fields=self.context.summary_fields(item),
                    value=item,
                )
            )

    def _added(
        self,
        parent: tuple[Step, ...],
        prop: str,
        key: str | None,
        item: Any,
        before: str | None = None,
    ) -> None:
        common: dict[str, Any] = {
            "path": format_path(parent, prop),
            "parent": list(parent),
            "property": prop,
            "key": key,
            "before": before,
        }
        if not is_structured(item):
            self.changes.append(UpdateLiteral(**common, op="set", new=item))
        elif is_reference(item):
            self.changes.append(
                AddRef(
                    **common,
                    uri=item["id"],
                    type=_string(item.get("type")),
                    label=self.context.label_of(item),
                    value=item,
                )
            )
        else:
            self.changes.append(
                AddNode(
                    **common,
                    node_type=_string(item.get("type")),
                    node_id=_string(item.get("id")),
                    summary_fields=self.context.summary_fields(item),
                    value=item,
                )
            )

    def _remove_all(self, parent: tuple[Step, ...], prop: str, shape: str, value: Any) -> None:
        if shape == _ARRAY:
            for key, item in zip(item_keys(value), value, strict=True):
                self._removed(parent, prop, key, item)
        else:
            self._removed(parent, prop, None, value)

    def _add_all(self, parent: tuple[Step, ...], prop: str, shape: str, value: Any) -> None:
        if shape == _ARRAY:
            for key, item in zip(item_keys(value), value, strict=True):
                self._added(parent, prop, key, item)
        else:
            self._added(parent, prop, None, value)

    # -- comparing ---------------------------------------------------------------------------

    def object(self, old: dict[str, Any], new: dict[str, Any], parent: tuple[Step, ...]) -> None:
        """Compare two versions of the same object, property by property."""
        for prop in sorted(old.keys() | new.keys()):
            old_value, new_value = old.get(prop, _ABSENT), new.get(prop, _ABSENT)
            old_shape, new_shape = _shape(prop, old_value), _shape(prop, new_value)
            if old_shape != new_shape:
                if old_shape is not None:
                    self._remove_all(parent, prop, old_shape, old_value)
                if new_shape is not None:
                    self._add_all(parent, prop, new_shape, new_value)
            elif old_shape == _LITERAL:
                self._literal(old, new, parent, prop)
            elif old_shape == _SINGLE:
                if _same_object(old_value, new_value):
                    step = self.context.step(prop, None, new_value)
                    self.object(old_value, new_value, (*parent, step))
                else:
                    self._removed(parent, prop, None, old_value)
                    self._added(parent, prop, None, new_value)
            else:
                self._array(old_value, new_value, parent, prop)

    def _literal(
        self, old: dict[str, Any], new: dict[str, Any], parent: tuple[Step, ...], prop: str
    ) -> None:
        old_value, new_value = old[prop], new[prop]
        if same_json(old_value, new_value):
            return
        text_edit = None
        if is_text_content(prop, old_value, old.get("type")) and is_text_content(
            prop, new_value, new.get("type")
        ):
            index, delete, insert = text_change(old_value, new_value)
            text_edit = TextEdit(index=index, delete=delete, insert=insert)
        self.changes.append(
            UpdateLiteral(
                path=format_path(parent, prop),
                parent=list(parent),
                property=prop,
                op="change",
                old=old_value,
                new=new_value,
                text_edit=text_edit,
            )
        )

    def _array(self, old: list[Any], new: list[Any], parent: tuple[Step, ...], prop: str) -> None:
        old_keys, new_keys = item_keys(old), item_keys(new)
        old_items = dict(zip(old_keys, old, strict=True))
        new_items = dict(zip(new_keys, new, strict=True))

        def kept(key: str) -> bool:
            """In both versions and not a replacement."""
            if key not in old_items or key not in new_items:
                return False
            before, after = old_items[key], new_items[key]
            if is_structured(before) or is_structured(after):
                return _same_object(before, after)
            return same_json(before, after)

        kept_keys = {key for key in new_keys if kept(key)}
        for key in old_keys:
            if key not in kept_keys:
                self._removed(parent, prop, key, old_items[key])
        for key in new_keys:
            if key in kept_keys and is_structured(new_items[key]):
                step = self.context.step(prop, key, new_items[key])
                self.object(old_items[key], new_items[key], (*parent, step))
        anchor: str | None = None
        anchors: dict[str, str | None] = {}
        for key in reversed(new_keys):
            if key in kept_keys:
                anchor = key
            else:
                anchors[key] = anchor
        for key in new_keys:
            if key not in kept_keys:
                self._added(parent, prop, key, new_items[key], anchors[key])
        previous = [key for key in old_keys if key in kept_keys]
        if previous != [key for key in new_keys if key in kept_keys]:
            self.changes.append(
                Reorder(
                    path=format_path(parent, prop),
                    parent=list(parent),
                    property=prop,
                    order=new_keys,
                    previous=previous,
                )
            )


def semantic_diff(
    old: dict[str, Any], new: dict[str, Any], *, labels: Mapping[str, str] | None = None
) -> list[Change]:
    """Return the typed changes that turn ``old`` into ``new`` (see the module docstring).

    Both are projected JSON-LD documents of one record. ``labels`` maps URIs to display labels
    for ``path`` qualifiers and ``label`` fields. Raises ``TypeError`` if either is not a JSON
    object.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise TypeError("semantic_diff compares two JSON objects")
    record_uri = _string(new.get("id")) or _string(old.get("id"))
    differ = _Differ(_Context(labels or {}, record_uri))
    differ.object(old, new, ())
    return differ.changes
