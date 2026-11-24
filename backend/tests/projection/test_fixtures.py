"""The shared operation fixtures: Python must match every file, and the files must say what
the rules say (the expectations below are written by hand, not recorded)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lae.ids import is_internal_node_iri
from lae.projection.canonical_json import canonical_json
from lae.projection.hydrate import assign_node_iris
from lae.projection.project import strip_internal_iris
from lae.projection.rules import entry_key, parse_key
from tests.projection.build_fixtures import AAT, CASES, RECORD, n
from tests.projection.fixture_runner import fixture_files, run_fixture

FILES = fixture_files()
IDENT = "identified_by"
CLASS = "classified_as"


def load(stem: str) -> dict[str, Any]:
    (path,) = [p for p in FILES if p.stem == stem]
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def orders(state: dict[str, Any], prop: str) -> dict[str, float]:
    """``item key -> _order`` of the entries of ``prop`` in one dumped object map."""
    found: dict[str, float] = {}
    for key, value in state["map"].items():
        key_prop, item_key = parse_key(key)
        if item_key is not None and key_prop == prop:
            found[item_key] = value["map"]["_order"]["plain"]
    return found


def returns(fixture: dict[str, Any], op: str) -> list[Any]:
    return [s["returns"] for s in fixture["steps"] if s.get("op") == op and "returns" in s]


def errors(fixture: dict[str, Any]) -> list[str]:
    return [s["error"] for s in fixture["steps"] if "error" in s]


def test_the_files_are_what_the_builder_defines() -> None:
    assert sorted(p.stem for p in FILES) == sorted(CASES)
    assert len(FILES) == 12


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_fixture(path: Path) -> None:
    run_fixture(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_fixture_is_portable(path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    # Hydration and the hand-written steps are as the builder has them (the file is not stale).
    case = CASES[path.stem]
    assert fixture["initial"] == case["initial"]
    assert fixture["clients"] == case["clients"]
    strip = ("returns", "error")
    assert [{k: v for k, v in s.items() if k not in strip} for s in fixture["steps"]] == [
        {k: v for k, v in s.items() if k not in strip} for s in case["steps"]
    ]
    # Nothing is minted at hydration: every embedded node of the initial record has an id.
    assert assign_node_iris(fixture["initial"], RECORD, mint=lambda: 1 / 0) == fixture["initial"]  # type: ignore[arg-type,return-value]
    assert fixture["initial"]["id"] == fixture["record_uri"] == RECORD
    # Every id the steps mint is an internal node IRI, so the public form has none of them.
    minted = [iri for s in fixture["steps"] for iri in s.get("args", {}).get("mint", [])]
    assert all(is_internal_node_iri(iri, RECORD) for iri in minted)
    assert len(set(minted)) == len(minted)
    public = canonical_json(strip_internal_iris(fixture["expected"])).decode()
    assert RECORD + "/node/" not in public

    # JSON cannot tell 33.0 from 33 and Python stores them differently (R9): no such argument.
    def floats(value: Any) -> list[float]:
        if isinstance(value, float):
            return [value]
        if isinstance(value, dict):
            return [f for v in value.values() for f in floats(v)]
        if isinstance(value, list):
            return [f for v in value for f in floats(v)]
        return []

    for step in fixture["steps"]:
        assert not [f for f in floats(step.get("args", {})) if f.is_integer()]
    # Every client id is explicit, and every multi-client case ends synchronized.
    assert all(isinstance(i, int) and i > 0 for i in fixture["clients"].values())
    if len(fixture["clients"]) > 1:
        last_sync = max(i for i, s in enumerate(fixture["steps"]) if "sync" in s)
        assert fixture["steps"][last_sync] == {"sync": list(fixture["clients"])}
        read_only = {"project", "get_path", "list_entries"}
        assert all(s["op"] in read_only for s in fixture["steps"][last_sync + 1 :])


def test_order_append_midpoint_first() -> None:
    fixture = load("order-append-midpoint-first")
    assert returns(fixture, "add_node") == [
        entry_key(IDENT, n(3)),
        entry_key(IDENT, n(4)),
        entry_key(IDENT, n(5)),
        entry_key(IDENT, n(8)),  # the two nested parts take n(6) and n(7) first
    ]
    state = fixture["expected_state"]
    assert orders(state, IDENT) == {
        n(1): 0.0,
        n(2): 1.0,
        n(3): 2.0,  # append: last + 1
        n(4): 0.5,  # between 0 and 1
        n(5): -1.0,  # before the first: first - 1
        n(8): 0.25,  # between 0 and 0.5
    }
    assert orders(state, CLASS) == {
        AAT + "300033618": 0.0,  # the first entry of a property
        AAT + "300133025": 1.0,
        AAT + "300404024": 0.5,
    }
    added = state["map"][entry_key(IDENT, n(8))]
    assert orders(added, "part") == {n(6): 0.0, n(7): 1.0}
    assert added["map"]["content"] == {"text": "D"}
    names = [i["content"] for i in fixture["expected"][IDENT]]
    assert names == ["First", "A", "D", "ID", "B", "C"]
    assert [i["type"] for i in fixture["expected"][IDENT]].count("Identifier") == 1
    assert fixture["expected"][CLASS][0]["_label"] == "Painting"
    assert fixture["expected"][CLASS][2] == {"id": AAT + "300133025", "type": "Type"}


def test_rebalance_after_tie() -> None:
    fixture = load("rebalance-after-tie")
    first, second = returns(fixture, "get_path")
    assert first == ["one", "from a", "from b", "two", "three"]
    assert second == ["one", "from a", "between", "from b", "two", "three"]
    assert orders(fixture["expected_state"], IDENT) == {
        n(1): 0.0,
        n(10): 1.0,
        n(12): 1.5,
        n(11): 2.0,
        n(2): 3.0,
        n(3): 4.0,
    }
    # The two moves tied at 0.5; the third rebalanced to 0, 1, 2, 3 and took 1.5.
    assert orders(fixture["expected_state"], CLASS) == {
        AAT + "3": 1.0,
        AAT + "1": 1.5,
        AAT + "4": 2.0,
        AAT + "2": 3.0,
    }


def test_rebalance_exhausted_gap() -> None:
    fixture = load("rebalance-exhausted-gap")
    found = orders(fixture["expected_state"], IDENT)
    expected = {n(1): 0.0, n(2): 54.0, n(154): 53.5}
    expected.update({n(100 + i): float(i) for i in range(1, 54)})
    assert found == expected
    (contents,) = returns(fixture, "get_path")
    assert contents == ["first", *[f"insert {i}" for i in range(1, 55)], "last"]
    assert not errors(fixture)


def test_repeated_ids() -> None:
    fixture = load("repeated-ids")
    x, z = AAT + "300027221", "https://example.org/thing/z"
    listed = returns(fixture, "list_entries")
    assert listed[0] == [
        entry_key(CLASS, x),
        entry_key(CLASS, AAT + "300404670"),
        entry_key(CLASS, x + " #2"),
    ]
    assert listed[1] == [entry_key("member_of", z + s) for s in ("", " #2", " #3")]
    added = returns(fixture, "add_reference")
    assert added == [
        entry_key(CLASS, x),  # present under its base item key
        entry_key(CLASS, x + " #2"),  # present, but only as the suffixed duplicate
        entry_key(CLASS, x + " #2 #2"),  # another item: its base item key is taken
        entry_key(CLASS, x + " #2 #2"),  # and now it is present
    ]
    assert returns(fixture, "remove_entry") == [True, True, False]
    assert returns(fixture, "add_node") == [
        entry_key("member_of", z),
        entry_key("member_of", z + "/other"),
        entry_key("member_of", z + "/other"),
    ]
    assert fixture["expected"][CLASS] == [
        {"id": AAT + "300404670", "type": "Type"},
        {"id": x + " #2", "type": "Type"},
    ]
    members = fixture["expected"]["member_of"]
    assert [m["id"] for m in members] == [z, z, z, z + "/other"]
    assert members[3]["identified_by"] == [{"id": n(3), "type": "Name", "content": "kept id"}]
    assert "_label" not in members[0]


def test_text_code_points() -> None:
    fixture = load("text-code-points")
    assert returns(fixture, "get_path") == [
        ["a😀éb𝒳🎨🎨c"],
        ["a😀éB𝒳🎨🎨𝒴!"],
        ["Naïve café 🎨 — 終わり。"],
    ]
    assert errors(fixture) == ["LAE-PROJ-008"] * 3 + ["LAE-PROJ-007"] * 2
    state = fixture["expected_state"]["map"]
    assert state[entry_key(IDENT, n(1))]["map"]["content"] == {"text": "aB𝒳🎨🎨𝒴!."}
    assert state[entry_key(IDENT, n(3))]["map"]["content"] == {"text": "INV 😀7 1"}
    assert state[entry_key(IDENT, n(3))]["map"]["type"] == {"plain": "Identifier"}
    assert state[entry_key("referred_to_by", n(2))]["map"]["content"] == {"text": "𝒳"}
    assert state[entry_key(CLASS, AAT + "300033618")]["map"]["content"] == {"plain": "plain 𝒳"}


def test_concurrent_first_inserts() -> None:
    fixture = load("concurrent-first-inserts")
    expected = fixture["expected"]
    # Each client saw only its own entries before the sync; afterwards every one is there.
    before_a, before_b = returns(fixture, "project")
    assert [i["content"] for i in before_a["referred_to_by"]] == ["from a"]
    assert [i["content"] for i in before_b["referred_to_by"]] == ["from b"]
    assert [i["id"] for i in expected["referred_to_by"]] == [n(10), n(11)]
    assert [i["content"] for i in expected["referred_to_by"]] == ["from b", "from a"]
    people = [i["id"] for i in expected["produced_by"]["carried_out_by"]]
    assert sorted(people) == ["https://example.org/id/person-1", "https://example.org/id/person-2"]
    assert [i["id"] for i in expected["member_of"]] == [
        "https://example.org/id/set-1",
        "https://example.org/id/set-2",
    ]
    assert [i["content"] for i in expected[IDENT][0]["part"]] == ["a", "b"]
    state = fixture["expected_state"]
    assert "member_of" not in state["map"]  # the marker of the empty source array is gone
    assert set(orders(state, "referred_to_by").values()) == {0.0}
    assert set(orders(state, "member_of").values()) == {0.0}
    assert set(orders(state["map"][entry_key(IDENT, n(2))], "part").values()) == {0.0}
    assert not errors(fixture)


def test_intermediate_nodes() -> None:
    fixture = load("intermediate-nodes")
    expected = fixture["expected"]
    production = expected["produced_by"]
    assert (production["id"], production["type"]) == (n(10), "Production")
    assert production["timespan"] == {
        "id": n(15),
        "type": "TimeSpan",
        "identified_by": [{"id": n(14), "type": "Name", "content": "about 1503"}],
        "begin_of_the_begin": "1503-01-01T00:00:00Z",
    }
    first, second = production["part"]
    assert first == {
        "id": n(12),
        "type": "Production",
        "carried_out_by": [{"id": "https://example.org/id/person-1", "type": "Person"}],
        "technique": [{"id": AAT + "300054216", "type": "Type"}],
    }
    assert second == {
        "id": n(13),
        "type": "Production",
        "carried_out_by": [{"id": "https://example.org/id/person-2", "type": "Person"}],
    }
    assert expected["current_location"] == {"id": "https://example.org/id/place-2", "type": "Place"}
    assert expected["created_by"] == {"id": n(17), "type": "Creation"}
    assert [i["content"] for i in expected[IDENT]] == ["one", "Two"]
    assert "shows" not in expected and "destroyed_by" not in expected
    assert returns(fixture, "set_object") == [
        n(15),
        "https://example.org/id/place-1",
        "https://example.org/id/place-2",
        n(16),
    ]
    assert returns(fixture, "remove_value") == [False, True, False, False]
    assert errors(fixture) == [
        "LAE-PROJ-007",  # two Production parts
        "LAE-PROJ-007",  # two Names
        "LAE-PROJ-007",  # shows: no class to create
        "LAE-PROJ-007",  # two entries, no filter
        "LAE-PROJ-007",  # through a scalar
        "LAE-PROJ-007",  # produced_by holds a Production, not an Activity
        "LAE-PROJ-006",  # an entry of a single-valued property
        "LAE-PROJ-007",  # a node without type
        "LAE-PROJ-007",  # type against the class filter
        "LAE-PROJ-007",  # set_object on an array
        "LAE-PROJ-007",  # neither node nor reference
        "LAE-PROJ-007",  # a scalar over a node
        "LAE-PROJ-007",  # remove_value on an array
        *["LAE-PROJ-007"] * 7,  # a write path that names id or type
    ]
    # ... and none of those left behind the intermediate node it would have created.
    assert returns(fixture, "get_path")[:2] == [[], []]
    assert expected["id"] == RECORD
    assert production["type"] == "Production"


def test_text_surrogates_and_combining() -> None:
    fixture = load("text-surrogates-and-combining")
    # Derived by hand from R23: (1, 1, U+1F601), (0, 1, U+1FA00), (3, 2, U+00E9), with b's
    # concurrent insert after the replaced character each time.
    assert returns(fixture, "get_path") == [["x\U0001f601!y", "\U0001fa00!z", "caf\u00e9s"]]
    state = fixture["expected_state"]["map"]
    assert state[entry_key(IDENT, n(1))]["map"]["content"] == {"text": "x\U0001f601!y"}
    assert state[entry_key(IDENT, n(2))]["map"]["content"] == {"text": "\U0001fa00!z"}
    assert state[entry_key(IDENT, n(3))]["map"]["content"] == {"text": "cafe\u0300s"}
    assert errors(fixture) == ["LAE-PROJ-007"] * 3


def test_move_and_remove() -> None:
    fixture = load("move-and-remove")
    k = [entry_key(CLASS, AAT + str(i)) for i in range(1, 6)]
    new = entry_key(CLASS, AAT + "9")
    listed = returns(fixture, "list_entries")
    assert listed[0] == [k[4], k[1], k[3], k[2], k[0]]
    assert listed[1] == [k[1], k[4], k[3], k[2], k[0]]
    # Concurrent: a moved 3 to the front, b inserted 9 before 3's old place.
    assert listed[2] == [k[2], k[1], k[4], k[3], new, k[0]]
    assert errors(fixture) == ["LAE-PROJ-008"] * 5
    assert returns(fixture, "remove_entry") == [False] + [True] * 6 + [False, True]
    (emptied,) = returns(fixture, "project")
    assert CLASS not in emptied
    assert fixture["expected"][CLASS] == [{"id": AAT + "10", "type": "Type"}]
    assert "member_of" not in fixture["expected"]
    assert "member_of" not in fixture["expected_state"]["map"]


def test_scalars_last_writer_wins() -> None:
    fixture = load("scalars-last-writer-wins")
    assert fixture["clients"] == {"a": 101, "b": 202}
    expected = fixture["expected"]
    (dimension,) = expected["dimension"]
    assert dimension["value"] == 30  # b has the higher client id
    assert (dimension["lower_value_limit"], dimension["upper_value_limit"]) == (-3, 0.1)
    assert dimension["unit"] == {"id": AAT + "300379098", "type": "MeasurementUnit"}
    assert expected["timespan"] == {
        "id": n(20),
        "type": "TimeSpan",
        "begin_of_the_begin": "1500-01-01T00:00:00Z",
    }
    assert expected["x_text"] == "from a"  # a write survives a concurrent removal
    assert expected["x_flag"] is True and expected["x_none"] is None
    state = fixture["expected_state"]["map"]
    assert state["content"] == {"plain": "a plain string: the root is no Name"}
    assert state["x_none"] == {"plain": None}
    assert errors(fixture) == ["LAE-PROJ-007"] * 3


def test_single_value_and_entries() -> None:
    fixture = load("single-value-and-entries")
    state = fixture["expected_state"]["map"]
    expected = fixture["expected"]
    assert [i["id"] for i in expected["x_note"]] == [AAT + "1", AAT + "5"]
    assert "x_note" not in state  # the ignored single value went with the next insert
    assert expected["x_other"] == [{"id": n(1), "type": "Name", "content": "entry"}]
    assert "map" in state["x_other"]  # still stored, ignored by the projection (R11)
    assert "shows" not in state
    assert len(expected["shows"]) == 2
    assert errors(fixture) == ["LAE-PROJ-006"] + ["LAE-PROJ-007"] * 3


def test_write_forbidden_keys() -> None:
    """Step 1.19a: ``_label`` joins ``id`` and ``type`` (``WRITE_FORBIDDEN_KEYS``)."""
    fixture = load("write-forbidden-keys")
    assert errors(fixture) == ["LAE-PROJ-007"] * len(fixture["steps"])
    assert fixture["expected"] == fixture["initial"]
    assert fixture["expected_state"] == fixture["initial_state"]
    paths = [step["args"]["path"] for step in fixture["steps"]]
    assert {"_label", "identified_by[Name]._label", "id", "produced_by.type"} <= set(paths)
