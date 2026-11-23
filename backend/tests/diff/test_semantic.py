"""``lae.diff.semantic``: hand-made before/after documents for every change kind and rule.

Each test states one sentence of the module's contract. The documents are small projected
records: every embedded node has an internal node IRI, references have none.
"""

from __future__ import annotations

import copy
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter

from lae.diff import CHANGE_KINDS, Change, semantic_diff
from lae.diff.semantic import (
    AddNode,
    AddRef,
    RefSummary,
    RemoveNode,
    RemoveRef,
    Reorder,
    Step,
    TextEdit,
    UpdateLiteral,
    item_keys,
)
from lae.projection import canonical_json, hydrate, project
from tests.diff.applier import apply_changes

URI = "https://data.test/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f"
AAT = "http://vocab.getty.edu/aat/"
PRIMARY = f"{AAT}300404670"
ALTERNATE = f"{AAT}300264273"
PAINTING = f"{AAT}300033618"
ARTWORK = f"{AAT}300133025"
SCULPTURE = f"{AAT}300047090"
ENGLISH = f"{AAT}300388277"
REMBRANDT = "https://data.test/id/0192f0c1-0000-7000-8000-00000000000a"
BOL = "https://data.test/id/0192f0c1-0000-7000-8000-00000000000b"


def nid(n: int) -> str:
    """Internal node IRI number ``n`` of the record."""
    return f"{URI}/node/0192f0c1-7b1e-7c3a-9d2e-{n:012x}"


def name(n: int, content: str, kind: str | None = PRIMARY, **more: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": nid(n), "type": "Name", "content": content}
    if kind is not None:
        node["classified_as"] = [{"id": kind, "type": "Type"}]
    return {**node, **more}


def ref(uri: str, type_: str = "Type", label: str | None = None) -> dict[str, Any]:
    out = {"id": uri, "type": type_}
    return out if label is None else {**out, "_label": label}


def record(**properties: Any) -> dict[str, Any]:
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": URI,
        "type": "HumanMadeObject",
        **properties,
    }


def diff(old: dict[str, Any], new: dict[str, Any], **kwargs: Any) -> list[Any]:
    """The diff, after checking that applying it to ``old`` gives ``new``."""
    changes = semantic_diff(old, new, **kwargs)
    doc = hydrate(old, URI)
    apply_changes(doc, changes)
    assert canonical_json(project(doc)) == canonical_json(new)
    return changes


def kinds(changes: list[Any]) -> list[str]:
    return [change.kind for change in changes]


# --- nothing changed ----------------------------------------------------------------------------


def test_equal_documents_have_no_changes() -> None:
    doc = record(identified_by=[name(1, "Night Watch")], classified_as=[ref(PAINTING)])
    reordered_keys = dict(reversed(list(copy.deepcopy(doc).items())))
    assert diff(doc, copy.deepcopy(doc)) == []
    assert diff(doc, reordered_keys) == []


def test_inputs_must_be_objects() -> None:
    with pytest.raises(TypeError):
        semantic_diff([], {})  # type: ignore[arg-type]


def test_change_kinds_are_the_six_of_decision_b10() -> None:
    models = get_args(get_args(Change)[0])
    assert tuple(m.model_fields["kind"].default for m in models) == (
        "add_node",
        "remove_node",
        "update_literal",
        "add_ref",
        "remove_ref",
        "reorder",
    )
    assert set(CHANGE_KINDS) == {m.model_fields["kind"].default for m in models}


# --- update_literal -----------------------------------------------------------------------------


def test_update_literal_in_a_single_valued_chain() -> None:
    def doc(begin: str) -> dict[str, Any]:
        timespan = {"id": nid(2), "type": "TimeSpan", "begin_of_the_begin": begin}
        return record(produced_by={"id": nid(1), "type": "Production", "timespan": timespan})

    changes = diff(doc("1642-01-01T00:00:00Z"), doc("1640-01-01T00:00:00Z"))
    assert changes == [
        UpdateLiteral(
            # Steps into single objects are bare keys: there is only one to address.
            path="produced_by.timespan.begin_of_the_begin",
            parent=[
                Step(
                    property="produced_by",
                    key=None,
                    id=nid(1),
                    type="Production",
                    classified_as=None,
                    label=None,
                ),
                Step(
                    property="timespan",
                    key=None,
                    id=nid(2),
                    type="TimeSpan",
                    classified_as=None,
                    label=None,
                ),
            ],
            property="begin_of_the_begin",
            op="change",
            old="1642-01-01T00:00:00Z",
            new="1640-01-01T00:00:00Z",
        )
    ]
    assert changes[0].text_edit is None  # not a Text: a date is a last-writer-wins scalar


def test_text_edit_is_one_splice_in_code_points_with_the_qualified_path() -> None:
    old = record(identified_by=[name(1, "The 😀 Night Watch!")])
    new = record(identified_by=[name(1, "The 😀 Nachtwacht!")])
    (change,) = diff(old, new, labels={PRIMARY: "Primary Name"})
    assert change.kind == "update_literal"
    assert change.op == "change"
    assert change.path == "identified_by[Name:Primary Name].content"
    assert change.parent == [
        Step(
            property="identified_by",
            key=nid(1),
            id=nid(1),
            type="Name",
            classified_as=PRIMARY,
            label="Primary Name",
        )
    ]
    assert (change.old, change.new) == ("The 😀 Night Watch!", "The 😀 Nachtwacht!")
    # "The 😀 N" is 7 code points (9 UTF-16 units, 10 UTF-8 bytes); the common suffix "!" stays.
    assert change.text_edit == TextEdit(index=7, delete=10, insert="achtwacht")


def test_several_edits_in_one_text_are_the_one_splice_that_spans_them() -> None:
    old = record(referred_to_by=[{"id": nid(1), "type": "LinguisticObject", "content": "a-b-c"}])
    new = record(referred_to_by=[{"id": nid(1), "type": "LinguisticObject", "content": "A-b-C"}])
    (change,) = diff(old, new)
    assert change.text_edit == TextEdit(index=0, delete=5, insert="A-b-C")
    assert change.path == "referred_to_by[LinguisticObject].content"


def test_content_of_another_type_is_not_a_text() -> None:
    old = record(dimension=[{"id": nid(1), "type": "Dimension", "content": "a"}])
    new = record(dimension=[{"id": nid(1), "type": "Dimension", "content": "b"}])
    (change,) = diff(old, new)
    assert change.text_edit is None


def test_a_literal_that_appears_is_set_and_one_that_goes_is_clear() -> None:
    old = record(_label="Night Watch", x_note=None)
    new = record(x_note=None, x_flag=None)
    changes = diff(old, new)
    # Code-point order of the keys: "_label" < "x_flag".
    assert [(c.property, c.op, c.old, c.new) for c in changes] == [
        ("_label", "clear", "Night Watch", None),
        ("x_flag", "set", None, None),  # set to the JSON value null; x_note stayed null
    ]
    assert [c.path for c in changes] == ["_label", "x_flag"]


def test_json_equality_is_exact() -> None:
    old = record(dimension=[{"id": nid(1), "type": "Dimension", "value": 33, "x": True}])
    new = record(dimension=[{"id": nid(1), "type": "Dimension", "value": 33.0, "x": 1}])
    changes = diff(old, new)
    assert [(c.property, c.old, c.new) for c in changes] == [("value", 33, 33.0), ("x", True, 1)]
    assert isinstance(changes[0].new, float) and isinstance(changes[1].new, int)
    wire = TypeAdapter(list[Change]).dump_json(changes)
    assert b'"old":33,"new":33.0' in wire and b'"old":true,"new":1' in wire


def test_context_and_opaque_values_are_literals_whatever_their_form() -> None:
    old = record(x_opaque={"@value": "a"}, x_empty=[])
    new = record(x_opaque={"@value": "b"}, x_empty=[])
    new["@context"] = ["https://linked.art/ns/v1/linked-art.json", {"x": "https://x.test/"}]
    changes = diff(old, new)
    assert [(c.kind, c.property, c.op) for c in changes] == [
        ("update_literal", "@context", "change"),
        ("update_literal", "x_opaque", "change"),
    ]
    assert changes[1].new == {"@value": "b"}


def test_opaque_array_items_are_set_and_cleared_by_key() -> None:
    old = record(x_tags=["a", "b", "c"])
    new = record(x_tags=["a", "d", "c"])
    changes = diff(old, new)
    assert [(c.kind, c.op, c.key, c.before, c.old, c.new) for c in changes] == [
        ("update_literal", "clear", "=s:b", None, "b", None),
        ("update_literal", "set", "=s:d", "=s:c", None, "d"),
    ]
    assert changes[0].path == "x_tags"


# --- add_ref / remove_ref -----------------------------------------------------------------------


def test_add_and_remove_references_matched_by_uri() -> None:
    old = record(classified_as=[ref(PAINTING), ref(ARTWORK, label="stored label")])
    new = record(classified_as=[ref(SCULPTURE), ref(PAINTING)])
    changes = diff(old, new, labels={SCULPTURE: "Sculpture", PAINTING: "Painting"})
    assert changes == [
        RemoveRef(
            path="classified_as",
            parent=[],
            property="classified_as",
            key=ARTWORK,
            uri=ARTWORK,
            type="Type",
            label="stored label",  # no looked-up label: the reference's own _label
            value=ref(ARTWORK, label="stored label"),
        ),
        AddRef(
            path="classified_as",
            parent=[],
            property="classified_as",
            key=SCULPTURE,
            before=PAINTING,  # the nearest following item that is in both versions
            uri=SCULPTURE,
            type="Type",
            label="Sculpture",
            value=ref(SCULPTURE),  # as stored: the looked-up label is not written into it
        ),
    ]


def test_references_inside_a_node_carry_the_parent_steps() -> None:
    old = record(identified_by=[name(1, "Night Watch", language=[ref(ENGLISH, "Language")])])
    new = record(identified_by=[name(1, "Night Watch")])
    (change,) = diff(old, new, labels={PRIMARY: "Primary Name", ENGLISH: "English"})
    assert isinstance(change, RemoveRef)
    assert change.path == "identified_by[Name:Primary Name].language"
    assert (change.uri, change.type, change.label) == (ENGLISH, "Language", "English")
    assert [step.key for step in change.parent] == [nid(1)]


def test_a_single_reference_pointed_elsewhere_is_removed_and_added() -> None:
    def doc(place: str) -> dict[str, Any]:
        return record(current_location={"id": place, "type": "Place"})

    changes = diff(doc(REMBRANDT), doc(BOL))
    assert [(c.kind, c.key, c.uri) for c in changes] == [
        ("remove_ref", None, REMBRANDT),  # key null: the property holds a single value
        ("add_ref", None, BOL),
    ]


def test_a_reference_without_type_or_label() -> None:
    (change,) = diff(record(), record(equivalent=[{"id": BOL}]))
    assert isinstance(change, AddRef)
    assert (change.type, change.label, change.before, change.value) == (
        None,
        None,
        None,
        {"id": BOL},
    )


# --- add_node / remove_node ---------------------------------------------------------------------


def test_add_and_remove_nodes_matched_by_internal_iri() -> None:
    dimension = {
        "id": nid(3),
        "type": "Dimension",
        "value": 363,
        "unit": ref(f"{AAT}300379098", "MeasurementUnit"),
        "classified_as": [ref(f"{AAT}300055644")],
        "assigned_by": [{"id": nid(4), "type": "AttributeAssignment"}],
    }
    old = record(identified_by=[name(1, "Night Watch"), name(2, "De Nachtwacht", ALTERNATE)])
    new = record(identified_by=[name(1, "Night Watch")], dimension=[dimension])
    changes = diff(old, new, labels={ALTERNATE: "Alternate Name", f"{AAT}300055644": "Height"})
    assert changes == [
        AddNode(
            path="dimension",
            parent=[],
            property="dimension",
            key=nid(3),
            before=None,
            node_type="Dimension",
            node_id=nid(3),
            summary_fields={
                # The nested AttributeAssignment (an internal node) is not summarized.
                "classified_as": [RefSummary(uri=f"{AAT}300055644", type="Type", label="Height")],
                "unit": RefSummary(uri=f"{AAT}300379098", type="MeasurementUnit", label=None),
                "value": 363,
            },
            value=dimension,
        ),
        RemoveNode(
            path="identified_by",
            parent=[],
            property="identified_by",
            key=nid(2),
            node_type="Name",
            node_id=nid(2),
            summary_fields={
                "classified_as": [RefSummary(uri=ALTERNATE, type="Type", label="Alternate Name")],
                "content": "De Nachtwacht",
            },
            value=name(2, "De Nachtwacht", ALTERNATE),
        ),
    ]


def test_a_single_valued_node_added_with_everything_inside_it() -> None:
    production = {
        "id": nid(1),
        "type": "Production",
        "carried_out_by": [ref(REMBRANDT, "Person")],
        "timespan": {"id": nid(2), "type": "TimeSpan", "begin_of_the_begin": "1642"},
    }
    (change,) = diff(record(), record(produced_by=production), labels={REMBRANDT: "Rembrandt"})
    assert isinstance(change, AddNode)
    assert (change.path, change.key, change.before) == ("produced_by", None, None)
    assert change.summary_fields == {
        "carried_out_by": [RefSummary(uri=REMBRANDT, type="Person", label="Rembrandt")]
    }
    assert change.value == production  # nested nodes are part of the one change


def test_a_changed_type_is_a_replacement_of_the_node() -> None:
    old = record(identified_by=[name(1, "RM-1"), name(2, "other")])
    new = copy.deepcopy(old)
    new["identified_by"][0]["type"] = "Identifier"
    changes = diff(old, new)
    assert [(c.kind, c.key, c.node_type) for c in changes] == [
        ("remove_node", nid(1), "Name"),
        ("add_node", nid(1), "Identifier"),
    ]
    assert changes[1].before == nid(2)  # it keeps its place


def test_a_node_moved_to_another_parent_is_removed_and_added_with_the_same_id() -> None:
    part = name(3, "van Rijn", None)
    old = record(identified_by=[name(1, "Rembrandt", part=[part]), name(2, "R.")])
    new = record(identified_by=[name(1, "Rembrandt"), name(2, "R.", part=[part])])
    changes = diff(old, new)
    assert [(c.kind, c.node_id, [s.key for s in c.parent]) for c in changes] == [
        ("remove_node", nid(3), [nid(1)]),
        ("add_node", nid(3), [nid(2)]),
    ]
    # No classification on the Names here would be wrong: they have one, but no label is known.
    assert changes[0].path == "identified_by[Name].part"


def test_a_reference_that_gains_a_property_is_compared_inside() -> None:
    old = record(classified_as=[ref(PAINTING)])
    new = record(classified_as=[{**ref(PAINTING), "classified_as": [ref(ARTWORK)]}])
    (change,) = diff(old, new, labels={ARTWORK: "Type of Work"})
    assert isinstance(change, AddRef)
    assert change.uri == ARTWORK
    assert [(s.property, s.key, s.id) for s in change.parent] == [
        ("classified_as", PAINTING, PAINTING)
    ]
    assert change.path == "classified_as[Type:Type of Work].classified_as"


def test_an_object_with_a_foreign_id_and_content_is_a_node() -> None:
    inline = {**ref(PAINTING, label="Painting"), "classified_as": [ref(ARTWORK)]}
    (change,) = diff(record(), record(classified_as=[inline]))
    assert isinstance(change, AddNode)  # R3: more than id, type, _label
    assert (change.node_id, change.key) == (PAINTING, PAINTING)
    assert change.summary_fields == {
        "_label": "Painting",
        "classified_as": [RefSummary(uri=ARTWORK, type="Type", label=None)],
    }


# --- reorder ------------------------------------------------------------------------------------


def test_reorder_reports_the_new_order_once_per_property() -> None:
    names = [name(1, "a"), name(2, "b"), name(3, "c")]
    old = record(identified_by=names, classified_as=[ref(PAINTING), ref(ARTWORK)])
    new = record(
        identified_by=[names[2], names[0], names[1]],
        classified_as=[ref(ARTWORK), ref(PAINTING)],
    )
    assert diff(old, new) == [
        Reorder(
            path="classified_as",
            parent=[],
            property="classified_as",
            order=[ARTWORK, PAINTING],
            previous=[PAINTING, ARTWORK],
        ),
        Reorder(
            path="identified_by",
            parent=[],
            property="identified_by",
            order=[nid(3), nid(1), nid(2)],
            previous=[nid(1), nid(2), nid(3)],
        ),
    ]


def test_additions_and_removals_alone_are_not_a_reorder() -> None:
    old = record(identified_by=[name(1, "a"), name(2, "b"), name(3, "c")])
    new = record(identified_by=[name(4, "new first"), name(1, "a"), name(3, "c"), name(5, "last")])
    changes = diff(old, new)
    assert [(c.kind, c.key, getattr(c, "before", None)) for c in changes] == [
        ("remove_node", nid(2), None),
        ("add_node", nid(4), nid(1)),
        ("add_node", nid(5), None),
    ]


def test_order_of_the_changes_of_one_property() -> None:
    old = record(identified_by=[name(1, "a"), name(2, "b"), name(3, "c")])
    new = record(identified_by=[name(4, "d"), name(3, "c"), name(1, "A")])
    changes = diff(old, new)
    # Removals, then changes inside kept items (new's order), then additions, then the reorder.
    assert [(c.kind, c.key if c.kind != "reorder" else None) for c in changes] == [
        ("remove_node", nid(2)),
        ("update_literal", None),
        ("add_node", nid(4)),
        ("reorder", None),
    ]
    assert changes[1].parent[0].key == nid(1)
    assert changes[2].before == nid(3)
    assert changes[3].order == [nid(4), nid(3), nid(1)]
    assert changes[3].previous == [nid(1), nid(3)]


def test_reorder_inside_a_nested_node() -> None:
    def doc(*languages: str) -> dict[str, Any]:
        return record(
            identified_by=[name(1, "a", language=[ref(u, "Language") for u in languages])]
        )

    (change,) = diff(doc(ENGLISH, BOL), doc(BOL, ENGLISH), labels={PRIMARY: "Primary Name"})
    assert isinstance(change, Reorder)
    assert change.path == "identified_by[Name:Primary Name].language"


# --- repeats (R7) -------------------------------------------------------------------------------


def test_item_keys_are_the_keys_hydration_assigns() -> None:
    items = [ref(PAINTING), "x", ref(PAINTING), "x", 1, 1.0, None, {"a": 1}, name(1, "n")]
    assert item_keys(items) == [
        PAINTING,
        "=s:x",
        f"{PAINTING} #2",
        "=s:x #2",
        "=n:1",
        "=n:1.0",
        "=z",
        '=j:{"a":1}',
        nid(1),
    ]


def test_repeated_references_are_matched_by_occurrence() -> None:
    old = record(classified_as=[ref(PAINTING), ref(ARTWORK), ref(PAINTING)])
    new = record(classified_as=[ref(ARTWORK), ref(PAINTING)])
    changes = diff(old, new)
    # The second occurrence is the one that is gone; the first is kept and has moved.
    assert [(c.kind, getattr(c, "key", None)) for c in changes] == [
        ("remove_ref", f"{PAINTING} #2"),
        ("reorder", None),
    ]
    assert changes[1].order == [ARTWORK, PAINTING]


def test_a_repeat_that_appears_is_added_under_its_suffixed_key() -> None:
    old = record(classified_as=[ref(PAINTING)])
    new = record(classified_as=[ref(PAINTING), ref(PAINTING, label="again")])
    (change,) = diff(old, new)
    assert isinstance(change, AddRef)
    assert (change.key, change.uri, change.before) == (f"{PAINTING} #2", PAINTING, None)


# --- shapes -------------------------------------------------------------------------------------


def test_a_shape_change_removes_the_old_side_then_adds_the_new_side() -> None:
    old = record(classified_as=ref(PAINTING), x_note="text", member_of=[ref(BOL, "Set")])
    new = record(classified_as=[ref(PAINTING), ref(ARTWORK)], x_note=[name(1, "n")], member_of=[])
    changes = diff(old, new)
    assert [(c.kind, c.property, c.key) for c in changes] == [
        ("remove_ref", "classified_as", None),
        ("add_ref", "classified_as", PAINTING),
        ("add_ref", "classified_as", ARTWORK),
        ("remove_ref", "member_of", BOL),
        ("update_literal", "member_of", None),  # the empty array is a literal (R8.5)
        ("update_literal", "x_note", None),
        ("add_node", "x_note", nid(1)),
    ]
    assert (changes[4].op, changes[4].new) == ("set", [])
    assert (changes[5].op, changes[5].old) == ("clear", "text")


# --- qualifiers ---------------------------------------------------------------------------------


def test_path_qualifier_is_the_first_classification_label() -> None:
    both = [ref(ALTERNATE, label="Own Label"), ref(PRIMARY)]
    old = record(identified_by=[name(1, "a", None, classified_as=both)])
    new = record(identified_by=[name(1, "b", None, classified_as=both)])
    labels = {PRIMARY: "Primary Name"}
    assert diff(old, new, labels=labels)[0].path == "identified_by[Name:Own Label].content"
    labels[ALTERNATE] = "Looked Up"  # as on read, the looked-up label wins over a stored one
    assert diff(old, new, labels=labels)[0].path == "identified_by[Name:Looked Up].content"
    del both[0]["_label"]  # the two versions share the list
    (change,) = diff(old, new)
    assert change.path == "identified_by[Name].content"  # no label known: no qualifier
    assert change.parent[0].classified_as == ALTERNATE  # the client can still resolve it


def test_the_steps_describe_the_new_version() -> None:
    old = record(identified_by=[name(1, "a", PRIMARY)])
    new = record(identified_by=[name(1, "b", ALTERNATE)])
    changes = diff(old, new, labels={PRIMARY: "Primary Name", ALTERNATE: "Alternate Name"})
    assert kinds(changes) == ["remove_ref", "add_ref", "update_literal"]
    assert changes[2].path == "identified_by[Name:Alternate Name].content"


# --- totality -----------------------------------------------------------------------------------


def test_root_id_and_type_are_literals() -> None:
    old = record()
    new = {**record(), "type": "DigitalObject"}
    (change,) = semantic_diff(old, new)
    assert (change.kind, change.path, change.old, change.new) == (
        "update_literal",
        "type",
        "HumanMadeObject",
        "DigitalObject",
    )


def test_nodes_without_ids_are_compared_by_value() -> None:
    def doc(content: str) -> dict[str, Any]:
        stripped = {"type": "Name", "content": content}
        return record(identified_by=[stripped, {"type": "Name", "content": "same"}])

    assert semantic_diff(doc("a"), doc("a")) == []
    changes = semantic_diff(doc("a"), doc("b"))
    assert [(c.kind, c.node_id, c.value["content"]) for c in changes] == [
        ("remove_node", None, "a"),
        ("add_node", None, "b"),
    ]


def test_changes_survive_json() -> None:
    old = record(identified_by=[name(1, "a")], classified_as=[ref(PAINTING)], x=1)
    new = record(identified_by=[name(2, "b")], classified_as=[ref(ARTWORK)], x=None)
    changes = semantic_diff(old, new)
    adapter = TypeAdapter(list[Change])
    assert adapter.validate_json(adapter.dump_json(changes)) == changes
    assert {c["kind"] for c in adapter.dump_python(changes)} == {
        "add_node",
        "remove_node",
        "add_ref",
        "remove_ref",
        "update_literal",
    }
