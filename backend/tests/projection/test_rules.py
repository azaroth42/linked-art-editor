"""One focused test per rule decision that the guide's draft rules left open."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pycrdt import Doc, Map, Text

from lae.corpus import CORPUS_DIR, load_record
from lae.ids import is_internal_node_iri
from lae.projection.canonical_json import canonical_json
from lae.projection.hydrate import assign_node_iris, hydrate
from lae.projection.ops import insert_item
from lae.projection.project import project, sorted_entries, strip_internal_iris
from lae.projection.rules import (
    JSON_KEY,
    ORDER_KEY,
    ROOT_MAP,
    VALUE_KEY,
    ProjectionError,
    entry_key,
    is_reference,
    order_between,
    parse_key,
)

URI = "https://example.org/id/rec"
AAT = "http://vocab.getty.edu/aat/"


def roundtrip(record: dict[str, Any]) -> dict[str, Any]:
    return strip_internal_iris(project(hydrate(record)))


def assert_roundtrips(record: dict[str, Any]) -> Doc:
    doc = hydrate(record)
    assert canonical_json(strip_internal_iris(project(doc))) == canonical_json(record)
    return doc


def root(doc: Doc) -> Map:
    return doc.get(ROOT_MAP, type=Map)


def corpus(name: str) -> dict[str, Any]:
    return load_record(CORPUS_DIR / name)


def item_keys(parent: Map, prop: str) -> list[str]:
    """The item keys (the second part of each entry key) of one array property, in order."""
    return [parse_key(k)[1] or "" for k, _ in sorted_entries(parent, prop)]


# --- embedded nodes that already carry an id ---


def test_embedded_node_with_id_is_keyed_by_it_and_never_minted_over() -> None:
    record = corpus("linkedart/object-0.json")
    doc = assert_roundtrips(record)
    source = record["classified_as"]
    with_id = [n for n in source if "id" in n and not is_reference(n)]
    assert with_id, "corpus example changed"
    for node in with_id:
        assert root(doc)[entry_key("classified_as", node["id"])]["id"] == node["id"]
    keys = item_keys(root(doc), "classified_as")
    assert not any(is_internal_node_iri(k, record["id"]) for k in keys)


def test_embedded_node_without_id_gets_internal_iri_kept_by_project() -> None:
    record = {"id": URI, "type": "Person", "identified_by": [{"type": "Name", "content": "A"}]}
    projected = project(hydrate(record))
    node_id = projected["identified_by"][0]["id"]
    assert is_internal_node_iri(node_id, URI)
    assert strip_internal_iris(projected) == record


def test_single_valued_embedded_node_gets_internal_iri() -> None:
    record = corpus("institutional/ycba-activity-2a201e0b-e18e-4040-a55b-9bda016e804f.json")
    assert isinstance(record["timespan"], dict)
    doc = assert_roundtrips(record)
    timespan = root(doc)["timespan"]
    assert isinstance(timespan, Map) and ORDER_KEY not in timespan
    assert is_internal_node_iri(timespan["id"], record["id"])


def test_internal_iri_pattern_is_scoped_to_the_record() -> None:
    uuid7 = "0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f"
    assert is_internal_node_iri(f"{URI}/node/{uuid7}", URI)
    assert not is_internal_node_iri(f"{URI}x/node/{uuid7}", URI)
    assert not is_internal_node_iri(f"{URI}/node/{uuid7.upper()}", URI)
    assert not is_internal_node_iri(f"{URI}/node/{uuid7}/x", URI)
    assert not is_internal_node_iri(f"{URI}/node/0192f0c1-7b1e-4c3a-9d2e-5a1b2c3d4e5f", URI)
    # A record whose own URI contains /node/ (Yale Library) keeps its root id and nested ids.
    record = corpus("institutional/yul-node-07e71913-f2be-479c-a388-65025173b4fb.json")
    assert "/node/" in record["id"]
    assert roundtrip(record)["id"] == record["id"]


def test_assign_node_iris_skips_references_opaque_values_and_context() -> None:
    record = {
        "@context": [{"type": "x"}],
        "id": URI,
        "type": "Person",
        "ref": {"id": AAT + "1", "type": "Type", "_label": "x"},
        "opaque": {"nested": {"type": "Name"}},
        "untyped_with_id": {"id": "x", "extra": {"type": "Name"}},
        "list": [{"type": "Name"}, {"id": AAT + "2"}, [{"type": "Name"}], {"k": {"type": "Name"}}],
    }
    out = assign_node_iris(record, URI, mint=iter(["m1", "m2"]).__next__)
    assert out["list"][0]["id"] == "m1"
    del out["list"][0]["id"]
    assert out == record


# --- duplicates in arrays ---


def test_duplicate_reference_is_preserved_with_suffixed_key() -> None:
    record = corpus("institutional/yuag-lin-176060.json")
    ids = [item["id"] for item in record["classified_as"]]
    dup = AAT + "300027221"
    assert ids.count(dup) == 2, "corpus example changed"
    doc = assert_roundtrips(record)
    keys = item_keys(root(doc), "classified_as")
    assert {dup, dup + " #2"} <= set(keys)
    assert len(set(keys)) == len(ids)
    # Both occurrences are in the projection, at their original positions.
    assert [item["id"] for item in project(doc)["classified_as"]] == ids
    assert entry_key("classified_as", dup + " #2") in root(doc)


def test_duplicate_scalars_nodes_and_colliding_suffixes_are_preserved() -> None:
    node = {"id": "https://example.org/n", "type": "Type", "_label": "one", "x": 1}
    record = {
        "id": URI,
        "type": "Person",
        "notation": ["a #2", "a", "a", "b", "a", 1, 1.0, "1", True, None, None],
        "same_id_different_body": [node, {**node, "_label": "two"}, {"id": node["id"]}],
        "refs": [{"id": "x #2"}, {"id": "x"}, {"id": "x"}, {"id": "x #2"}],
    }
    doc = assert_roundtrips(record)
    assert set(item_keys(root(doc), "notation")) == {
        "=s:a #2",
        "=s:a",
        "=s:a #3",
        "=s:b",
        "=s:a #4",
        "=n:1",
        "=n:1.0",
        "=s:1",
        "=b:true",
        "=z",
        "=z #2",
    }
    assert item_keys(root(doc), "refs") == ["x #2", "x", "x #3", "x #2 #2"]


def test_insert_is_a_noop_only_for_the_same_item_not_for_a_suffix_collision() -> None:
    record = {"id": URI, "type": "Person", "notation": ["x", "x"]}
    doc = hydrate(record)
    assert item_keys(root(doc), "notation") == ["=s:x", "=s:x #2"]
    # The string "x #2" has base key "=s:x #2", which the duplicate of "x" already holds.
    insert_item(doc, root(doc), "notation", "x #2", record_uri=URI)
    assert project(doc)["notation"] == ["x", "x", "x #2"]
    insert_item(doc, root(doc), "notation", "x", record_uri=URI)  # present: no-op
    assert project(doc)["notation"] == ["x", "x", "x #2"]


# --- mixed arrays, scalars, plain objects ---


def test_array_mixing_references_and_embedded_nodes() -> None:
    record = corpus("linkedart/object-0.json")
    kinds = {is_reference(item) for item in record["member_of"]}
    assert kinds == {True, False}, "corpus example changed"
    doc = assert_roundtrips(record)
    assert len(sorted_entries(root(doc), "member_of")) == len(record["member_of"])
    assert "member_of" not in root(doc)  # no container: the entries are keys of the parent map


def test_arrays_of_scalars_plain_objects_and_nested_arrays() -> None:
    record = {
        "id": URI,
        "type": "Person",
        "notation": ["b", "a", "c"],
        "plain": [{"@value": "x", "@language": "en"}, {"_order": 1, "_value": 2, "_json": 3}],
        "nested": [[1, [2.0, {"type": "Name"}]], []],
        "mixed": ["s", {"id": AAT + "1"}, {"type": "Name", "content": "n"}, {"k": 1}, 2, None],
    }
    doc = assert_roundtrips(record)
    entry = root(doc)[entry_key("notation", "=s:a")]
    assert entry[VALUE_KEY] == "a" and entry[ORDER_KEY] == 1.0
    # Nothing inside an opaque value gets an internal IRI.
    assert project(doc)["nested"] == record["nested"]


def test_single_plain_object_is_opaque_and_exact() -> None:
    record = {
        "id": URI,
        "type": "Person",
        "x": {"b": 1, "a": 1.0, "c": {"type": "Name"}, "_order": 0},
        "empty": {},
        "id_no_type": {"id": "x", "extra": 1},
        "id_not_string": {"id": 5, "type": "Name"},
    }
    doc = assert_roundtrips(record)
    assert root(doc)["x"] == {JSON_KEY: '{"_order":0,"a":1.0,"b":1,"c":{"type":"Name"}}'}
    assert project(doc)["x"] == record["x"]


# --- @context ---


@pytest.mark.parametrize(
    "context",
    [
        "https://linked.art/ns/v1/linked-art.json",
        ["https://linked.art/ns/v1/linked-art.json", {"x": "https://example.org/x"}],
        ["b", "a", "b"],
        {"id": "@id", "type": "@type"},
        [],
        None,
    ],
)
def test_context_is_stored_verbatim(context: Any) -> None:
    record = {"@context": context, "id": URI, "type": "Person"}
    doc = assert_roundtrips(record)
    assert not isinstance(root(doc)["@context"], Map)


def test_corpus_context_is_a_string() -> None:
    record = corpus("linkedart/object-0.json")
    assert isinstance(record["@context"], str)
    assert root(hydrate(record))["@context"] == record["@context"]


# --- numbers and booleans ---


def test_pycrdt_turns_integers_into_floats() -> None:
    """The reason for rule R9: a plain Y.Map value does not keep int versus float."""
    doc: Doc = Doc()
    m = doc.get("m", type=Map)
    m["int"] = 33
    m["nested"] = {"a": 1}
    assert type(m["int"]) is float
    assert type(m["nested"]["a"]) is float


@pytest.mark.parametrize(
    "value",
    [0, 1, -1, 33, 2550, 2**53, -(2**53), 2**53 + 1, 2**63 - 1, -(2**63), 2**64 - 1]
    + [0.0, -0.0, 33.0, 33.5, 0.1, 1e21, 1e300, 5e-324, 2.0**53, 2.0**60, -1.5]
    + [True, False, None, "33", "true", ""],
)
def test_scalar_types_do_not_drift(value: Any) -> None:
    record = {"id": URI, "type": "Dimension", "value": value, "values": [value, "x", value]}
    doc = hydrate(record)
    replica: Doc = Doc()
    replica.apply_update(doc.get_update())
    for d in (doc, replica):
        out = project(d)
        assert type(out["value"]) is type(value)
        assert canonical_json(out) == canonical_json(record)
        assert json.dumps(out, sort_keys=True) == json.dumps(record, sort_keys=True)


def test_corpus_has_both_integers_and_integral_floats() -> None:
    ints = corpus("linkedart/digital-0.json")
    assert type(ints["dimension"][0]["value"]) is int
    assert type(project(hydrate(ints))["dimension"][0]["value"]) is int
    floats = corpus("institutional/ycba-object-5497f1dd-415f-4c05-b593-2a87e00d2ee2.json")
    assert (
        floats["dimension"][3]["value"] == 33.0 and type(floats["dimension"][3]["value"]) is float
    )
    assert type(project(hydrate(floats))["dimension"][3]["value"]) is float


def test_numbers_written_by_another_client_project_deterministically() -> None:
    """A JS client can only write doubles: integral ones project as integers."""
    doc = hydrate({"id": URI, "type": "Dimension", "value": 1.5})
    root(doc)["value"] = 34.0
    assert type(project(doc)["value"]) is int
    root(doc)["value"] = 2.0**60
    assert type(project(doc)["value"]) is float


# --- Text ---


def test_content_is_text_only_on_the_three_types() -> None:
    record = {
        "id": URI,
        "type": "LinguisticObject",
        "content": "root\U0001f600",
        "identified_by": [
            {"type": "Name", "content": "name"},
            {"type": "Identifier", "content": ""},
            {"type": "Type", "content": "plain"},
            {"type": "Name", "content": 5},
            {"type": ["Name"], "content": "list-typed"},
            {"type": "Name", "value": "not content"},
        ],
    }
    doc = assert_roundtrips(record)
    assert isinstance(root(doc)["content"], Text)
    entries = [e for _, e in sorted_entries(root(doc), "identified_by")]
    assert [isinstance(e.get("content"), Text) for e in entries] == [True, True] + [False] * 4
    assert isinstance(entries[5]["value"], str)


def test_pycrdt_text_offsets_are_utf8_bytes() -> None:
    """The reason for rule R19: pycrdt counts UTF-8 bytes, Yjs counts UTF-16 code units."""
    doc = hydrate({"id": URI, "type": "Name", "content": "é\U0001f600z"})
    text = root(doc)["content"]
    assert len(str(text)) == 3 and len(text) == 7
    # Code-point position 2 (before "z") is byte offset 6 in pycrdt and offset 3 in Yjs.
    prefix = str(text)[:2]
    assert len(prefix.encode("utf-8")) == 6 and len(prefix.encode("utf-16-le")) // 2 == 3
    text.insert(len(prefix.encode("utf-8")), "|")
    assert project(doc)["content"] == "é\U0001f600|z"
    # An offset inside a character is not an error: the text silently lands somewhere else.
    text.insert(1, "!")
    assert project(doc)["content"] not in ("é!\U0001f600|z", "é\U0001f600|z")


# --- _label, empty arrays, key order ---


def test_label_on_embedded_nodes_and_references_is_preserved() -> None:
    record = corpus("institutional/ycba-activity-2a201e0b-e18e-4040-a55b-9bda016e804f.json")
    node = record["referred_to_by"][0]["classified_as"][0]
    assert "_label" in node and not is_reference(node), "corpus example changed"
    out = roundtrip(record)
    assert out["referred_to_by"][0]["classified_as"][0]["_label"] == node["_label"]
    assert out["classified_as"][0]["_label"] == record["classified_as"][0]["_label"]


def test_empty_array_roundtrips_and_an_emptied_array_is_omitted() -> None:
    record = {"id": URI, "type": "Person", "identified_by": [], "part": [{"type": "Name"}]}
    doc = assert_roundtrips(record)
    assert root(doc)["identified_by"] == {JSON_KEY: "[]"}
    # An array whose last entry was removed projects to no key at all.
    ((key, _),) = sorted_entries(root(doc), "part")
    del root(doc)[key]
    assert project(doc) == {"id": URI, "type": "Person", "identified_by": []}
    # The first insert into an empty source array replaces the marker.
    insert_item(doc, root(doc), "identified_by", {"id": AAT + "1"}, record_uri=URI)
    assert "identified_by" not in root(doc)
    assert project(doc)["identified_by"] == [{"id": AAT + "1"}]


# --- map keys (R6) ---


def test_array_entries_are_keys_of_the_parent_map() -> None:
    record = {
        "id": URI,
        "type": "Person",
        "identified_by": [{"id": "https://example.org/n", "type": "Name", "part": [{"id": "p"}]}],
        "timespan": {"type": "TimeSpan", "identified_by": [{"id": AAT + "1"}]},
    }
    doc = assert_roundtrips(record)
    keys = set(root(doc).keys())
    assert keys == {"id", "type", '["identified_by","https://example.org/n"]', "timespan"}
    name = root(doc)['["identified_by","https://example.org/n"]']
    assert set(name.keys()) == {"id", "type", '["part","p"]', ORDER_KEY}
    assert f'["identified_by","{AAT}1"]' in root(doc)["timespan"]
    # No shared map anywhere holds only entries: there is no container to create concurrently.
    assert isinstance(root(doc)["timespan"], Map) and ORDER_KEY not in root(doc)["timespan"]


@pytest.mark.parametrize(
    "key",
    ["[", "[]", '["a"]', '["a","b"]', "[x", "a[0]", 'q"\\\n\t\u0001\u007f\u2028', "\U0001f600"],
)
def test_keys_that_look_like_entry_keys_roundtrip(key: str) -> None:
    record = {
        "id": URI,
        "type": "Person",
        key: "single",
        "nested": {"type": "Name", key: [{"id": key}, key, {"type": "Name", key: 1}]},
    }
    doc = assert_roundtrips(record)
    assert parse_key(entry_key(key, key)) == (key, key)
    stored = [k for k in root(doc).keys() if parse_key(k) == (key, None)]
    assert len(stored) == 1
    assert (stored[0] == key) == (not key.startswith("["))


def test_entry_key_text_is_json_stringify_compatible() -> None:
    assert entry_key("classified_as", AAT + "1") == f'["classified_as","{AAT}1"]'
    assert entry_key('a"b\\', "\n\u0001\u007f\u2028é\U0001f600") == (
        '["a\\"b\\\\","\\n\\u0001\u007f\u2028é\U0001f600"]'
    )


@pytest.mark.parametrize("key", ["[", "[1]", '["a",1]', '["a","b","c"]', "[]", '{"a":1}x', "[x"])
def test_a_malformed_map_key_fails_projection(key: str) -> None:
    doc = hydrate({"id": URI, "type": "Person"})
    root(doc)[key] = "written by a faulty client"
    if key.startswith("["):
        with pytest.raises(ProjectionError) as info:
            project(doc)
        assert info.value.code == "LAE-PROJ-005"
    else:
        assert project(doc)[key] == "written by a faulty client"


def test_insert_into_a_single_valued_property_is_rejected() -> None:
    doc = hydrate({"id": URI, "type": "Activity", "timespan": {"type": "TimeSpan"}})
    with pytest.raises(ProjectionError) as info:
        insert_item(doc, root(doc), "timespan", {"type": "TimeSpan"}, record_uri=URI)
    assert info.value.code == "LAE-PROJ-006"
    for reserved in ("_order", "_value", "@context"):
        with pytest.raises(ProjectionError) as info:
            insert_item(doc, root(doc), reserved, "x", record_uri=URI)
        assert info.value.code == "LAE-PROJ-001"


def test_key_order_is_irrelevant_and_array_order_is_exact() -> None:
    a = {"id": URI, "type": "Person", "p": [{"id": "z"}, {"id": "a"}, {"id": "m"}], "q": "x"}
    b = {"q": "x", "p": [{"id": "z"}, {"id": "a"}, {"id": "m"}], "type": "Person", "id": URI}
    reordered = {**a, "p": [{"id": "a"}, {"id": "m"}, {"id": "z"}]}
    assert canonical_json(roundtrip(a)) == canonical_json(roundtrip(b)) == canonical_json(a)
    assert canonical_json(roundtrip(reordered)) != canonical_json(a)
    assert [r["id"] for r in project(hydrate(a))["p"]] == ["z", "a", "m"]


def test_sort_is_by_order_then_key_in_code_point_order() -> None:
    doc = hydrate({"id": URI, "type": "Person", "p": [{"id": "b"}]})
    # U+FF5E sorts before U+1F600 by code point but after it by UTF-16 code unit (JS default).
    for key in ["\U0001f600", "～", "a"]:
        root(doc)[entry_key("p", key)] = Map({"id": key, ORDER_KEY: 0.0})
    assert item_keys(root(doc), "p") == ["a", "b", "～", "\U0001f600"]


# --- invalid input ---


@pytest.mark.parametrize(
    ("record", "code"),
    [
        ({"id": URI, "type": "Person", "_order": 1}, "LAE-PROJ-001"),
        ({"id": URI, "type": "Person", "p": [{"type": "Name", "_value": 1}]}, "LAE-PROJ-001"),
        (
            {
                "id": URI,
                "type": "Person",
                "p": {"id": "x", "type": "T", "q": {"type": "N", "_order": 0}},
            },
            "LAE-PROJ-001",
        ),
        ({"id": URI, "type": "Person", "value": float("nan")}, "LAE-PROJ-002"),
        ({"id": URI, "type": "Person", "x": {"deep": [float("inf")]}}, "LAE-PROJ-002"),
        ({"id": URI, "type": "Person", "value": 2**64}, "LAE-PROJ-002"),
        ({"id": URI, "type": "Person", "_label": "\ud800"}, "LAE-PROJ-003"),
        ({"id": URI, "type": "Person", "\ud800": 1}, "LAE-PROJ-003"),
        ({"id": URI, "type": "Name", "content": "\udfff"}, "LAE-PROJ-003"),
        ({"id": URI, "type": "Person", "x": {"deep": "\ud800"}}, "LAE-PROJ-003"),
        ({"type": "Person"}, "LAE-PROJ-004"),
        ({"id": 5, "type": "Person"}, "LAE-PROJ-004"),
    ],
)
def test_invalid_input_is_rejected_with_a_code(record: dict[str, Any], code: str) -> None:
    with pytest.raises(ProjectionError) as info:
        hydrate(record)
    assert info.value.code == code


def test_record_uri_can_be_supplied_for_a_record_without_id() -> None:
    record = {"type": "Person", "identified_by": [{"type": "Name", "content": "A"}]}
    projected = project(hydrate(record, URI))
    assert is_internal_node_iri(projected["identified_by"][0]["id"], URI)
    assert strip_internal_iris(projected, URI) == record


# --- _order arithmetic ---


def test_order_between() -> None:
    assert order_between(None, None) == 0.0
    assert order_between(None, 0.0) == -1.0
    assert order_between(3.0, None) == 4.0
    assert order_between(0.0, 1.0) == 0.5
    assert order_between(0.5, 1.0) == 0.75
    assert order_between(-1.0, 0.0) == -0.5
    # Exhausted gaps and ties ask for a rebalance.
    assert order_between(1.0, 1.0) is None
    assert order_between(1.0, 1.0000000000000002) is None
    assert order_between(2.0**53, None) is None
    assert order_between(None, -(2.0**53)) is None
    assert order_between(1.7e308, 1.7e308) is None
