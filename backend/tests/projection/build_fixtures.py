"""Regenerates ``fixtures/*.json``: ``uv run python -m tests.projection.build_fixtures``.

The cases are written here by hand (initial record, clients, steps). Running this module
executes each one with ``lae.projection.ops`` and records what happened (``returns``, ``error``,
``expected``, ``expected_state``, ``initial_update``, ``initial_state``) into the JSON files,
whose format ``fixture_runner`` documents. Review the diff of the JSON after regenerating:
``test_fixtures.py`` only checks that the implementation still matches the files, plus a set of
hand-written expectations per case. The files are shared with the TypeScript mirror
(Step 1.17); a changed expectation is a change to the wire behaviour of both.
"""

from __future__ import annotations

import json
from typing import Any

from lae.projection.rules import entry_key
from tests.projection.fixture_runner import FIXTURE_DIR, run_fixture

RECORD = "https://example.org/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f"
AAT = "http://vocab.getty.edu/aat/"
A, B = 101, 202


def n(number: int) -> str:
    """A fixed internal node IRI of the fixture record (a canonical UUIDv7)."""
    return f"{RECORD}/node/0192f0c1-7b1e-7000-8000-{number:012x}"


def ek(prop: str, item_key: str) -> str:
    return entry_key(prop, item_key)


def op(client: str, name: str, **args: Any) -> dict[str, Any]:
    return {"client": client, "op": name, "args": args}


def sync(*clients: str) -> dict[str, Any]:
    return {"sync": list(clients)}


def name(number: int, content: str, **more: Any) -> dict[str, Any]:
    return {"id": n(number), "type": "Name", "content": content, **more}


def ref(uri: str, cls: str = "Type") -> dict[str, Any]:
    return {"id": uri, "type": cls}


def record(**properties: Any) -> dict[str, Any]:
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": RECORD,
        "type": "HumanMadeObject",
        **properties,
    }


def case(
    description: str,
    rules: list[str],
    initial: dict[str, Any],
    steps: list[dict[str, Any]],
    clients: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "description": description,
        "rules": rules,
        "record_uri": RECORD,
        "initial": initial,
        "clients": clients or {"a": A},
        "steps": steps,
    }


IDENT = "identified_by"
CLASS = "classified_as"

CASES: dict[str, dict[str, Any]] = {}

CASES["order-append-midpoint-first"] = case(
    "Append takes last + 1, an insert between two entries takes their midpoint, an insert "
    "before the first takes first - 1, and the first entry of a property takes 0. Nested nodes "
    "are minted before the node that contains them.",
    ["R4", "R6", "R12", "R14", "R22"],
    record(identified_by=[name(1, "A"), name(2, "B")]),
    [
        op("a", "add_node", path="identified_by[Name]", node={"content": "C"}, mint=[n(3)]),
        op(
            "a",
            "add_node",
            path=IDENT,
            node={"type": "Identifier", "content": "ID"},
            before_key=ek(IDENT, n(2)),
            mint=[n(4)],
        ),
        op(
            "a",
            "add_node",
            path="identified_by[Name]",
            node={"content": "First"},
            before_key=ek(IDENT, n(1)),
            mint=[n(5)],
        ),
        op(
            "a",
            "add_node",
            path=IDENT,
            node={
                "type": "Name",
                "content": "D",
                "part": [{"type": "Name", "content": "D1"}, {"type": "Name", "content": "D2"}],
                "language": [ref(AAT + "300388277", "Language")],
            },
            before_key=ek(IDENT, n(4)),
            mint=[n(6), n(7), n(8)],
        ),
        op("a", "add_reference", path=CLASS, uri=AAT + "300033618", type="Type", label="Painting"),
        op("a", "add_reference", path="classified_as[Type]", uri=AAT + "300133025"),
        op(
            "a",
            "add_reference",
            path=CLASS,
            uri=AAT + "300404024",
            type="Type",
            before_key=ek(CLASS, AAT + "300133025"),
        ),
        op("a", "list_entries", path="identified_by[Name]"),
        op("a", "list_entries", path=IDENT),
        op("a", "get_path", path="identified_by[Name].content"),
        op("a", "get_path", path="identified_by[Name].part[Name].content"),
        op("a", "get_path", path=CLASS),
    ],
)

CASES["rebalance-after-tie"] = case(
    "Two clients insert at the same position and both get the midpoint 0.5; the entry key "
    "breaks the tie. An insert between the two tied entries renumbers the property to 0, 1, 2, "
    "... (writing only the entries whose _order changes) and then takes the midpoint. A move "
    "into a tie does the same.",
    ["R12", "R13", "R18"],
    record(
        identified_by=[name(1, "one"), name(2, "two"), name(3, "three")],
        classified_as=[ref(AAT + "1"), ref(AAT + "2")],
    ),
    [
        op(
            "a",
            "add_node",
            path=IDENT,
            node={"type": "Name", "content": "from a"},
            before_key=ek(IDENT, n(2)),
            mint=[n(10)],
        ),
        op(
            "b",
            "add_node",
            path=IDENT,
            node={"type": "Name", "content": "from b"},
            before_key=ek(IDENT, n(2)),
            mint=[n(11)],
        ),
        sync("a", "b"),
        op("a", "get_path", path="identified_by[Name].content"),
        op(
            "a",
            "add_node",
            path=IDENT,
            node={"type": "Name", "content": "between"},
            before_key=ek(IDENT, n(11)),
            mint=[n(12)],
        ),
        sync("a", "b"),
        op("b", "get_path", path="identified_by[Name].content"),
        # A tie made by two concurrent moves, then a move between the tied entries.
        op("a", "add_reference", path=CLASS, uri=AAT + "3", type="Type"),
        op("a", "add_reference", path=CLASS, uri=AAT + "4", type="Type"),
        sync("a", "b"),
        op(
            "a", "move_entry", path=CLASS, key=ek(CLASS, AAT + "3"), before_key=ek(CLASS, AAT + "2")
        ),
        op(
            "b", "move_entry", path=CLASS, key=ek(CLASS, AAT + "4"), before_key=ek(CLASS, AAT + "2")
        ),
        sync("a", "b"),
        op("b", "list_entries", path=CLASS),
        op(
            "b", "move_entry", path=CLASS, key=ek(CLASS, AAT + "1"), before_key=ek(CLASS, AAT + "4")
        ),
        sync("a", "b"),
        op("a", "list_entries", path=CLASS),
    ],
    {"a": A, "b": B},
)

CASES["rebalance-exhausted-gap"] = case(
    "Inserting again and again just before the last entry halves the gap below 1.0. The 53rd "
    "insert takes 1 - 2^-53, the last double below 1; the 54th finds no double strictly "
    "between, so the property is renumbered to 0, 1, ..., 54 and the insert takes 53.5.",
    ["R12"],
    record(identified_by=[name(1, "first"), name(2, "last")]),
    [
        op(
            "a",
            "add_node",
            path=IDENT,
            node={"type": "Name", "content": f"insert {i}"},
            before_key=ek(IDENT, n(2)),
            mint=[n(100 + i)],
        )
        for i in range(1, 55)
    ]
    + [op("a", "get_path", path="identified_by[Name].content")],
)

X, Y, Z = AAT + "300027221", AAT + "300404670", "https://example.org/thing/z"

CASES["repeated-ids"] = case(
    "An id that occurs twice in one array keeps both entries: the second is keyed '<id> #2'. "
    "Operations never add a duplicate: adding an item whose id is present under its base item "
    "key returns that entry. When the base item key is held by a different item the new entry "
    "takes the next unused suffix.",
    ["R6", "R7"],
    record(
        classified_as=[ref(X), ref(Y), {"id": X, "type": "Type", "_label": "again"}],
        member_of=[
            {"id": Z, "type": "Set", "identified_by": [name(1, "first")]},
            {"id": Z, "type": "Set", "identified_by": [name(2, "second")]},
            ref(Z, "Set"),
        ],
    ),
    [
        op("a", "list_entries", path=CLASS),
        op("a", "list_entries", path="member_of"),
        op("a", "add_reference", path=CLASS, uri=X, type="Type", label="not added"),
        op("a", "remove_entry", path=CLASS, key=ek(CLASS, X)),
        op("a", "get_path", path=CLASS),
        op("a", "add_reference", path=CLASS, uri=X, type="Type", label="not added: held as #2"),
        op("a", "add_reference", path=CLASS, uri=X + " #2", type="Type"),
        op("a", "add_reference", path=CLASS, uri=X + " #2", type="Type", label="not added"),
        op("a", "list_entries", path=CLASS),
        op("a", "remove_entry", path=CLASS, key=ek(CLASS, X + " #2")),
        op("a", "remove_entry", path=CLASS, key=ek(CLASS, X + " #2")),
        op("a", "add_node", path="member_of", node={"id": Z, "type": "Set", "_label": "no"}),
        op(
            "a",
            "add_node",
            path="member_of[Set]",
            node={"id": Z + "/other", "identified_by": [{"type": "Name", "content": "kept id"}]},
            mint=[n(3)],
        ),
        op("a", "add_node", path="member_of", node={"id": Z + "/other", "type": "Set"}),
        op("a", "list_entries", path="member_of"),
    ],
)

NAME = "identified_by[Name].content"
NOTE = "referred_to_by[LinguisticObject].content"

CASES["text-code-points"] = case(
    "Text positions count Unicode code points, whatever the library counts (UTF-8 bytes in "
    "pycrdt, UTF-16 code units in Yjs). set_scalar on an existing Text applies the one splice "
    "between the longest common prefix and suffix, so concurrent edits elsewhere in the text "
    "survive. content is a Text only on Name, Identifier and LinguisticObject.",
    ["R8", "R19", "R23"],
    record(
        identified_by=[name(1, "a😀b𝒳c")],
        referred_to_by=[
            {"id": n(2), "type": "LinguisticObject", "content": "naïve café 🎨 — 終わり"}
        ],
        classified_as=[{"id": AAT + "300033618", "type": "Type", "content": "plain 😀"}],
    ),
    [
        op("a", "splice_text", path=NAME, index=2, delete=0, insert="é"),
        op("b", "splice_text", path=NAME, index=4, delete=0, insert="🎨🎨"),
        sync("a", "b"),
        op("a", "get_path", path=NAME),
        op("a", "set_scalar", path=NAME, value="a😀éB𝒳🎨🎨c"),
        op("b", "splice_text", path=NAME, index=7, delete=1, insert="𝒴!"),
        sync("a", "b"),
        op("b", "get_path", path=NAME),
        op("a", "splice_text", path=NAME, index=1, delete=2, insert=""),
        op("a", "splice_text", path=NAME, index=7, delete=0, insert="."),
        op("a", "splice_text", path=NAME, index=8, delete=1, insert="x"),
        op("a", "splice_text", path=NAME, index=9, delete=0, insert="x"),
        op("a", "splice_text", path=NAME, index=-1, delete=0, insert="x"),
        op("a", "splice_text", path="classified_as[Type].content", index=0, delete=0, insert="x"),
        op(
            "a",
            "splice_text",
            path="identified_by[Identifier].content",
            index=0,
            delete=0,
            insert="x",
        ),
        # Concurrent set_scalar calls on one Text: each is a splice, so both survive.
        op("a", "set_scalar", path=NOTE, value="Naïve café 🎨 — 終わり"),
        op("b", "set_scalar", path=NOTE, value="naïve café 🎨 — 終わり。"),
        sync("a", "b"),
        op("a", "get_path", path=NOTE),
        op("a", "set_scalar", path=NOTE, value="Naïve 🎨🎨 — 終わり。"),
        op("a", "set_scalar", path=NOTE, value="Naïve 🎨🎨 — 終わり。"),
        op("a", "set_scalar", path=NOTE, value=""),
        op("a", "set_scalar", path=NOTE, value="𝒳"),
        # A plain string: replaced whole. A new Identifier: its content is created as a Text.
        op("a", "set_scalar", path="classified_as[Type].content", value="plain 𝒳"),
        op(
            "a",
            "set_scalar",
            path="identified_by[Identifier].content",
            value="INV 😀 1",
            mint=[n(3)],
        ),
        op(
            "a",
            "splice_text",
            path="identified_by[Identifier].content",
            index=5,
            delete=0,
            insert="7",
        ),
        sync("a", "b"),
    ],
    {"a": A, "b": B},
)

P1, P2 = "https://example.org/id/person-1", "https://example.org/id/person-2"
S1, S2 = "https://example.org/id/set-1", "https://example.org/id/set-2"
INSIDE_NAME = [{"path": IDENT, "key": ek(IDENT, n(2))}]

CASES["concurrent-first-inserts"] = case(
    "Two clients each add the first value of a property that has none: at the root, inside a "
    "single-valued node, inside an array entry, and into an array that was empty in the "
    "source. Every insert is kept, the reference both of them add exists once, and the entries "
    "(all with _order 0) are ordered by entry key.",
    ["R6", "R7", "R13", "R14", "R18"],
    record(
        identified_by=[name(2, "title")],
        produced_by={"id": n(1), "type": "Production"},
        member_of=[],
    ),
    [
        op(
            "a",
            "add_node",
            path="referred_to_by[LinguisticObject]",
            node={"content": "from a"},
            mint=[n(11)],
        ),
        op(
            "b",
            "add_node",
            path="referred_to_by[LinguisticObject]",
            node={"content": "from b"},
            mint=[n(10)],
        ),
        op("a", "add_reference", path="produced_by.carried_out_by", uri=P1, type="Person"),
        op("b", "add_reference", path="produced_by.carried_out_by", uri=P2, type="Person"),
        op("b", "add_reference", path="produced_by.carried_out_by", uri=P1, type="Person"),
        op("a", "add_reference", path="member_of", uri=S2, type="Set"),
        op("b", "add_reference", path="member_of", uri=S1, type="Set"),
        op(
            "a",
            "add_node",
            path="part[Name]",
            node={"content": "a"},
            base=INSIDE_NAME,
            mint=[n(12)],
        ),
        op(
            "b",
            "add_node",
            path="part[Name]",
            node={"content": "b"},
            base=INSIDE_NAME,
            mint=[n(13)],
        ),
        op("a", "project"),
        op("b", "project"),
        sync("a", "b"),
        op("a", "get_path", path="referred_to_by.content"),
        op("b", "list_entries", path="produced_by.carried_out_by"),
        op("b", "list_entries", path="member_of"),
        op("a", "get_path", path="identified_by.part.content"),
    ],
    {"a": A, "b": B},
)

PART = "produced_by.part[Production]"
SECOND_NAME = [{"path": IDENT, "key": ek(IDENT, n(2))}]

CASES["intermediate-nodes"] = case(
    "A write follows one chain of nodes and creates the nodes that are missing: a "
    "single-valued one for the properties whose class the model fixes (produced_by, timespan, "
    "...), otherwise an array entry typed by the segment's class filter. A path that addresses "
    "two nodes, or that would have to overwrite a value it does not address, is rejected and "
    "changes nothing.",
    ["R5", "R14", "R20", "R21", "R22", "R24"],
    record(identified_by=[name(1, "one"), name(2, "two")]),
    [
        op(
            "a",
            "set_scalar",
            path="produced_by.timespan.begin_of_the_begin",
            value="1503-01-01T00:00:00Z",
            mint=[n(10), n(11)],
        ),
        op(
            "a",
            "set_scalar",
            path="produced_by.timespan.end_of_the_end",
            value="1506-12-31T23:59:59Z",
        ),
        op(
            "a", "add_reference", path=PART + ".carried_out_by", uri=P1, type="Person", mint=[n(12)]
        ),
        op("a", "add_reference", path=PART + ".technique", uri=AAT + "300054216", type="Type"),
        op("a", "add_node", path=PART, node={}, mint=[n(13)]),
        op("a", "add_reference", path=PART + ".carried_out_by", uri=P2, type="Person"),
        op(
            "a",
            "add_reference",
            path="carried_out_by",
            uri=P2,
            type="Person",
            base=[{"path": "produced_by"}, {"path": "part", "key": ek("part", n(13))}],
        ),
        op("a", "set_scalar", path=NAME, value="ambiguous"),
        op("a", "set_scalar", path="content", value="Two", base=SECOND_NAME),
        op("a", "set_scalar", path="shows.format", value="no class for shows"),
        op("a", "set_scalar", path="identified_by.content", value="two nodes"),
        op(
            "a", "set_scalar", path="produced_by.timespan.begin_of_the_begin.x", value="in a scalar"
        ),
        op("a", "set_scalar", path="produced_by[Activity].x_note", value="would overwrite"),
        op("a", "add_node", path="produced_by", node={"type": "Production"}),
        op(
            "a",
            "add_node",
            path="produced_by.timespan[TimeSpan].identified_by",
            node={"content": "x"},
        ),
        op("a", "add_node", path="identified_by[Name]", node={"type": "Identifier"}),
        op(
            "a",
            "set_object",
            path="produced_by.timespan",
            obj={
                "type": "TimeSpan",
                "identified_by": [{"type": "Name", "content": "about 1503"}],
                "begin_of_the_begin": "1503-01-01T00:00:00Z",
            },
            mint=[n(14), n(15)],
        ),
        op(
            "a",
            "set_object",
            path="current_location",
            obj=ref("https://example.org/id/place-1", "Place"),
        ),
        op(
            "a",
            "set_object",
            path="current_location",
            obj=ref("https://example.org/id/place-2", "Place"),
        ),
        op(
            "a",
            "set_object",
            path="created_by[Creation].timespan[TimeSpan]",
            obj={},
            mint=[n(16), n(17)],
        ),
        op("a", "set_object", path="identified_by", obj={"type": "Name"}),
        op("a", "set_object", path="x_note", obj={"no": "type"}),
        op("a", "set_scalar", path="created_by.timespan", value="over a node"),
        op("a", "remove_value", path="created_by.timespan[Period]"),
        op("a", "remove_value", path="created_by.timespan[TimeSpan]"),
        op("a", "remove_value", path="created_by.timespan"),
        op("a", "remove_value", path="destroyed_by.timespan"),
        op("a", "remove_value", path="identified_by"),
        # A write path never names id or type (R20): nothing is overwritten, and no
        # intermediate node is left behind by the calls that would have had to create one.
        op("a", "set_scalar", path="id", value="https://example.org/id/another"),
        op("a", "set_scalar", path="produced_by.type", value="Activity"),
        op("a", "remove_value", path="produced_by.id"),
        op("a", "set_object", path="produced_by.timespan.id", obj=ref(AAT + "1")),
        op("a", "add_reference", path="identified_by[Identifier].type", uri=AAT + "1"),
        op("a", "add_reference", path="destroyed_by.type[Type].technique", uri=AAT + "1"),
        op("a", "set_scalar", path="destroyed_by.id.x_note", value="x"),
        op("a", "get_path", path="identified_by[Identifier]"),
        op("a", "get_path", path="destroyed_by"),
        op("a", "get_path", path="produced_by.part[Production].carried_out_by"),
        op("a", "get_path", path="produced_by.timespan.identified_by.content"),
    ],
)

REFS = [AAT + str(i) for i in range(1, 6)]
K = [ek(CLASS, uri) for uri in REFS]
NEW = AAT + "9"

CASES["move-and-remove"] = case(
    "move_entry writes only the moved entry's _order (the candidate for its new neighbours) and "
    "nothing when the entry is already there; remove_entry deletes one key. A move and an "
    "insert made concurrently both apply; an entry added while another client removes every "
    "entry is the one that remains; removing the last entry leaves the property absent.",
    ["R12", "R14", "R18"],
    record(
        classified_as=[ref(uri) for uri in REFS],
        member_of=[ref(S1, "Set")],
    ),
    [
        op("a", "move_entry", path=CLASS, key=K[0], before_key=None),
        op("a", "move_entry", path=CLASS, key=K[4], before_key=K[1]),
        op("a", "move_entry", path=CLASS, key=K[3], before_key=K[2]),
        op("a", "list_entries", path=CLASS),
        op("a", "move_entry", path=CLASS, key=K[3], before_key=K[2]),
        op("a", "move_entry", path=CLASS, key=K[0], before_key=None),
        op("a", "move_entry", path=CLASS, key=K[1], before_key=K[1]),
        op("a", "move_entry", path="classified_as[Type]", key=K[1], before_key=K[4]),
        op("a", "list_entries", path=CLASS),
        op("a", "move_entry", path=CLASS, key=ek(CLASS, NEW), before_key=None),
        op("a", "move_entry", path=CLASS, key=K[1], before_key=ek(CLASS, NEW)),
        op("a", "move_entry", path=CLASS, key=ek("member_of", S1), before_key=None),
        op("a", "move_entry", path=CLASS, key=CLASS, before_key=None),
        op("a", "remove_entry", path=CLASS, key=ek("member_of", S1)),
        op("a", "remove_entry", path="produced_by.classified_as", key=K[1]),
        sync("a", "b"),
        op("a", "move_entry", path=CLASS, key=K[2], before_key=K[1]),
        op("b", "add_reference", path=CLASS, uri=NEW, type="Type", before_key=K[2]),
        sync("a", "b"),
        op("b", "list_entries", path=CLASS),
        op("a", "remove_entry", path=CLASS, key=K[0]),
        op("a", "remove_entry", path=CLASS, key=K[1]),
        op("a", "remove_entry", path=CLASS, key=K[2]),
        op("a", "remove_entry", path=CLASS, key=K[3]),
        op("a", "remove_entry", path=CLASS, key=K[4]),
        op("a", "remove_entry", path=CLASS, key=ek(CLASS, NEW)),
        op("a", "remove_entry", path=CLASS, key=K[0]),
        op("a", "project"),
        op("b", "add_reference", path=CLASS, uri=AAT + "10", type="Type", before_key=K[0]),
        op("a", "remove_entry", path="member_of", key=ek("member_of", S1)),
        sync("a", "b"),
    ],
    {"a": A, "b": B},
)

DIMENSION = [{"path": "dimension", "key": ek("dimension", n(1))}]

CASES["scalars-last-writer-wins"] = case(
    "Scalars are plain values under the property key and each is a last-writer-wins register: "
    "of two concurrent writes the one from the client with the higher client id remains, "
    "whole. Integers and non-integral numbers are Y numbers. A scalar never replaces a node, "
    "a reference or an array.",
    ["R8", "R9", "R14", "R18", "R24"],
    record(
        dimension=[
            {
                "id": n(1),
                "type": "Dimension",
                "value": 10,
                "unit": ref(AAT + "300379098", "MeasurementUnit"),
            }
        ],
    ),
    [
        op("a", "set_scalar", path="dimension[Dimension].value", value=12.5),
        op("a", "set_scalar", path="value", value=77, base=DIMENSION),
        op("a", "set_scalar", path="lower_value_limit", value=-3, base=DIMENSION),
        op("a", "set_scalar", path="upper_value_limit", value=0.1, base=DIMENSION),
        op("a", "set_scalar", path="x_flag", value=True),
        op("a", "set_scalar", path="x_none", value=None),
        op("a", "set_scalar", path="x_text", value='𝒳 "quoted" [not a key]'),
        op("a", "set_scalar", path="content", value="a plain string: the root is no Name"),
        op("a", "set_scalar", path="dimension", value="an array"),
        op("a", "set_scalar", path="dimension[Dimension].unit", value="a reference"),
        op("a", "set_scalar", path="dimension[Dimension]", value="a class filter"),
        sync("a", "b"),
        op("a", "set_scalar", path="dimension.value", value=20),
        op("b", "set_scalar", path="dimension.value", value=30),
        op("a", "set_scalar", path="x_text", value="from a"),
        op("b", "remove_value", path="x_text"),
        op(
            "b",
            "set_object",
            path="timespan",
            obj={"type": "TimeSpan", "begin_of_the_begin": "1500-01-01T00:00:00Z"},
            mint=[n(20)],
        ),
        op(
            "a",
            "set_object",
            path="timespan",
            obj={"type": "TimeSpan", "end_of_the_end": "1599-12-31T23:59:59Z"},
            mint=[n(21)],
        ),
        sync("a", "b"),
        op("a", "get_path", path="dimension.value"),
        op("a", "get_path", path="timespan"),
        op("a", "get_path", path="x_text"),
    ],
    {"a": A, "b": B},
)

CASES["single-value-and-entries"] = case(
    "One client writes a single value for a property while another inserts an entry of it: "
    "the entries win on every replica and the single value is ignored. The next insert deletes "
    "the ignored value. An array that was empty in the source loses its marker on the first "
    "insert.",
    ["R11", "R14", "R18"],
    record(shows=[]),
    [
        op("a", "set_scalar", path="x_note", value="single"),
        op("b", "add_reference", path="x_note", uri=AAT + "1", type="Type"),
        op("a", "add_reference", path="x_note", uri=AAT + "2", type="Type"),
        op("a", "set_object", path="x_other", obj=ref(AAT + "3")),
        op("b", "add_node", path="x_other", node={"type": "Name", "content": "entry"}, mint=[n(1)]),
        sync("a", "b"),
        op("a", "project"),
        op("a", "get_path", path="x_note"),
        op("a", "set_scalar", path="x_note", value="rejected"),
        op("a", "set_object", path="x_other", obj=ref(AAT + "4")),
        op("a", "remove_value", path="x_other"),
        op("b", "add_reference", path="x_note", uri=AAT + "5", type="Type"),
        sync("a", "b"),
        op(
            "a",
            "add_reference",
            path="shows",
            uri="https://example.org/id/visual-1",
            type="VisualItem",
        ),
        op(
            "b",
            "add_reference",
            path="shows",
            uri="https://example.org/id/visual-2",
            type="VisualItem",
        ),
        sync("a", "b"),
    ],
    {"a": A, "b": B},
)

ONE, TWO, THREE = (
    [{"path": IDENT, "key": ek(IDENT, n(1))}],
    [{"path": IDENT, "key": ek(IDENT, n(2))}],
    [{"path": IDENT, "key": ek(IDENT, n(3))}],
)

CASES["text-surrogates-and-combining"] = case(
    "The splice of set_scalar is computed on code points, never on UTF-16 code units or on "
    "normalized text. U+1F600 and U+1F601 share their high surrogate and U+1F600 and U+1FA00 "
    "their low surrogate: a prefix or suffix counted in code units would cut a character in "
    "half. 'e' + U+0301 and U+00E9 are different strings: both code points are replaced. Each "
    "case has a concurrent insert next to the replaced character, which lands where it would "
    "only if the splice is exactly (index, delete, insert) as R23 gives it. splice_text takes "
    "integers and a string.",
    ["R19", "R23"],
    record(
        identified_by=[
            name(1, "x\U0001f600y"),
            name(2, "\U0001f600z"),
            name(3, "cafe\u0301"),
        ]
    ),
    [
        # (1, 1, U+1F601): the common prefix is "x", not "x" + a high surrogate.
        op("a", "set_scalar", path="content", value="x\U0001f601y", base=ONE),
        op("b", "splice_text", path="content", index=2, delete=0, insert="!", base=ONE),
        # (0, 1, U+1FA00): the common suffix is "z", not a low surrogate + "z".
        op("a", "set_scalar", path="content", value="\U0001fa00z", base=TWO),
        op("b", "splice_text", path="content", index=1, delete=0, insert="!", base=TWO),
        # (3, 2, U+00E9): no normalization, the combining mark is one position of its own.
        op("a", "set_scalar", path="content", value="caf\u00e9", base=THREE),
        op("b", "splice_text", path="content", index=5, delete=0, insert="s", base=THREE),
        sync("a", "b"),
        op("a", "get_path", path="identified_by[Name].content"),
        # Between a base letter and its combining mark is a valid position.
        op("b", "set_scalar", path="content", value="cafe\u0301s", base=THREE),
        op("b", "splice_text", path="content", index=4, delete=1, insert="\u0300", base=THREE),
        op("b", "splice_text", path="content", index=1.5, delete=0, insert="x", base=THREE),
        op("b", "splice_text", path="content", index=0, delete=1, insert=5, base=THREE),
        op("b", "splice_text", path="content", index=0, delete=1, insert=None, base=THREE),
        sync("a", "b"),
    ],
    {"a": A, "b": B},
)


def main() -> None:
    FIXTURE_DIR.mkdir(exist_ok=True)
    for stem, fixture in CASES.items():
        run_fixture(fixture, record=True)
        ordered = {
            key: fixture[key]
            for key in (
                "description",
                "rules",
                "record_uri",
                "clients",
                "initial",
                "initial_update",
                "initial_state",
                "steps",
                "expected",
                "expected_state",
            )
        }
        text = json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"
        (FIXTURE_DIR / f"{stem}.json").write_text(text, encoding="utf-8")
        print(f"{stem}: {len(fixture['steps'])} steps")


if __name__ == "__main__":
    main()
