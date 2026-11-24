"""Path operations (``lae.projection.ops``): every function, its rejections, and R19-R25."""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pycrdt import Doc, Map, Text

from lae.corpus import CORPUS_DIR, corpus_files, load_record
from lae.ids import is_internal_node_iri
from lae.profiles.paths import PathSyntaxError, parse_path
from lae.projection import ops
from lae.projection.canonical_json import canonical_json
from lae.projection.hydrate import hydrate
from lae.projection.project import project, record_map, sorted_entries, strip_internal_iris
from lae.projection.rules import (
    ORDER_KEY,
    SINGLE_VALUED_CLASSES,
    ProjectionError,
    entry_key,
    parse_key,
    text_change,
    utf8_offset,
)

URI = "https://example.org/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f"
AAT = "http://vocab.getty.edu/aat/"
REPO = CORPUS_DIR.parents[1]


def record(**properties: Any) -> dict[str, Any]:
    return {"id": URI, "type": "HumanMadeObject", **properties}


def names(*contents: str) -> list[dict[str, Any]]:
    return [{"type": "Name", "content": c} for c in contents]


def fork(doc: Doc[Any]) -> Doc[Any]:
    other: Doc[Any] = Doc()
    other.apply_update(doc.get_update())
    return other


def sync(*docs: Doc[Any]) -> None:
    for _ in range(2):
        for source in docs:
            for target in docs:
                if source is not target:
                    target.apply_update(source.get_update(target.get_state()))


def contents(doc: Doc[Any], path: str = "identified_by.content") -> list[Any]:
    return [ops.value_to_json(v) for v in ops.get_path(doc, path)]


def keys(doc: Doc[Any], path: str) -> list[str]:
    return [key for key, _ in ops.list_entries(doc, path)]


def code(excinfo: pytest.ExceptionInfo[ProjectionError]) -> str:
    return excinfo.value.code


def assert_rejected(doc: Doc[Any], expected: str, call: Any) -> None:
    """``call`` raises ``expected`` and leaves no trace in the document."""
    before = doc.get_update()
    with pytest.raises(ProjectionError) as excinfo:
        call()
    assert code(excinfo) == expected
    assert doc.get_update() == before


# --- get_path -------------------------------------------------------------------------------


def evaluate(value: Any, path: str) -> list[Any]:
    """The "Reading" rules of profiles/README.md applied to plain JSON."""
    current = [value]
    for segment in parse_path(path):
        found: list[Any] = []
        for item in current:
            if isinstance(item, dict) and segment.key in item:
                held = item[segment.key]
                found.extend(held if isinstance(held, list) else [held])
        if segment.type_filter is not None:
            found = [
                f for f in found if isinstance(f, dict) and f.get("type") == segment.type_filter
            ]
        current = found
    return current


def profile_paths() -> list[str]:
    paths = {
        "identified_by[Name].content",
        "identified_by[Identifier].content",
        "identified_by[Name].part[Name].classified_as",
        "identified_by.language.notation",
        "dimension.unit",
        "dimension.value",
        "produced_by.timespan.begin_of_the_begin",
        "produced_by.part[Production].carried_out_by",
        "referred_to_by.classified_as.classified_as",
        "member_of[Set]",
        "equivalent",
        "born.timespan.end_of_the_end",
        "no_such_key.content",
        "type.content",
    }
    for profile in sorted((REPO / "profiles" / "ui").glob("*.yaml")):
        for section in yaml.safe_load(profile.read_text())["sections"]:
            paths.update(slot["path"] for slot in section["slots"])
    return sorted(paths)


def test_get_path_equals_evaluating_the_path_on_the_projection_for_the_whole_corpus() -> None:
    paths = profile_paths()
    assert len(paths) > 15
    addressed = 0
    for file in corpus_files():
        doc = hydrate(load_record(file))
        projected = project(doc)
        for path in paths:
            got = [ops.value_to_json(v) for v in ops.get_path(doc, path)]
            assert canonical_json(got) == canonical_json(evaluate(projected, path)), (file, path)
            addressed += len(got)
    assert addressed > 800


def test_get_path_on_the_mona_lisa_matches_the_examples_in_the_profile_readme() -> None:
    doc = hydrate(load_record(CORPUS_DIR / "linkedart" / "object-0.json"))
    assert contents(doc, "identified_by[Name].content") == [
        "Mona Lisa",
        "La Joconde",
        "La Gioconda",
    ]
    assert contents(doc, "identified_by[Identifier].content") == ["INV. 779"]
    assert len(ops.get_path(doc, "classified_as")) == 2
    assert [v["type"] for v in contents(doc, "dimension.unit")] == ["MeasurementUnit"] * 2
    (begin,) = contents(doc, "produced_by.timespan.begin_of_the_begin")
    assert isinstance(begin, str)


def test_get_path_returns_live_values_and_reads_inside_opaque_values() -> None:
    doc = hydrate(
        record(
            identified_by=names("a"),
            timespan={"type": "TimeSpan", "begin_of_the_begin": "1500"},
            notation=["x", 1, {"k": [{"type": "T", "v": 1}, {"v": 2}]}],
            x_opaque={"inner": {"deep": [1, 2.5, None]}, "id": 5},
            x_empty=[],
            x_number=33.0,
        )
    )
    (entry,) = ops.get_path(doc, "identified_by[Name]")
    assert isinstance(entry, Map) and ORDER_KEY in entry
    assert ops.value_to_json(entry) == {"id": entry["id"], "type": "Name", "content": "a"}
    (text,) = ops.get_path(doc, "identified_by.content")
    assert isinstance(text, Text)
    (span,) = ops.get_path(doc, "timespan")
    assert isinstance(span, Map) and ORDER_KEY not in span
    assert ops.get_path(doc, "timespan.begin_of_the_begin") == ["1500"]
    assert ops.get_path(doc, "notation") == ["x", 1, {"k": [{"type": "T", "v": 1}, {"v": 2}]}]
    assert ops.get_path(doc, "notation.k.v") == [1, 2]
    assert ops.get_path(doc, "notation.k[T].v") == [1]
    assert ops.get_path(doc, "x_opaque.inner.deep") == [1, 2.5, None]
    assert ops.get_path(doc, "x_empty") == []
    values = ops.get_path(doc, "x_number")
    assert values == [33.0] and isinstance(values[0], float)
    assert ops.get_path(doc, "identified_by.content.more") == []
    assert [str(v) for v in ops.get_path(doc, "content", base=entry)] == [str(text)] == ["a"]
    assert ops.get_path(doc, "identified_by[Type]") == []


def test_get_path_prefers_entries_over_a_single_value() -> None:
    doc = hydrate(record())
    other = fork(doc)
    ops.set_scalar(doc, "x_both", "single")
    ops.insert_item(other, record_map(other), "x_both", "entry")
    sync(doc, other)
    assert record_map(doc)["x_both"] == "single"
    assert ops.get_path(doc, "x_both") == ["entry"]
    assert project(doc)["x_both"] == ["entry"]


@pytest.mark.parametrize("path", ["", "identified_by[0]", "a..b", "_note", "a.[B]", "A"])
def test_a_malformed_path_is_a_syntax_error(path: str) -> None:
    doc = hydrate(record())
    with pytest.raises(PathSyntaxError):
        ops.get_path(doc, path)
    with pytest.raises(PathSyntaxError):
        ops.set_scalar(doc, path, "x")


@pytest.mark.parametrize(
    "path", ["_label", "identified_by[Name]._label", "_label[Name].content", "a._label"]
)
def test_a_write_path_naming_label_is_refused_before_it_is_parsed(path: str) -> None:
    """Step 1.19a: ``_label`` is ``LAE-PROJ-007`` for a write, as in ``paths.ts``; reading the
    same path is still a syntax error (the path grammar has no ``_label``)."""
    doc = hydrate(record(_label="Kept", identified_by=names("a")))
    before = doc.get_update()
    with pytest.raises(ProjectionError) as refused:
        ops.set_scalar(doc, path, "x")
    assert refused.value.code == "LAE-PROJ-007"
    with pytest.raises(ProjectionError) as refused:
        ops.remove_value(doc, path)
    assert refused.value.code == "LAE-PROJ-007"
    assert doc.get_update() == before
    assert project(doc)["_label"] == "Kept"
    with pytest.raises(PathSyntaxError):
        ops.get_path(doc, path)


# --- list_entries, get_entry ----------------------------------------------------------------


def test_list_entries_and_get_entry() -> None:
    doc = hydrate(
        record(
            identified_by=[*names("a"), {"type": "Identifier", "content": "i"}, *names("b")],
            notation=["x", "x"],
        )
    )
    every = ops.list_entries(doc, "identified_by")
    assert [e["type"] for _, e in every] == ["Name", "Identifier", "Name"]
    only = ops.list_entries(doc, "identified_by[Identifier]")
    assert [k for k, _ in only] == [every[1][0]]
    assert ops.get_entry(doc, "identified_by", every[2][0])["content"].to_py() == "b"
    assert ops.get_entry(doc, "identified_by[Type]", every[2][0])["type"] == "Name"
    assert keys(doc, "notation") == [
        entry_key("notation", "=s:x"),
        entry_key("notation", "=s:x #2"),
    ]
    assert ops.list_entries(doc, "notation[Name]") == []
    assert ops.list_entries(doc, "produced_by.part") == []
    assert ops.list_entries(doc, "x_absent") == []
    for path, key in [
        ("identified_by", entry_key("identified_by", "missing")),
        ("classified_as", every[0][0]),
        ("identified_by", "identified_by"),
        ("produced_by.identified_by", every[0][0]),
    ]:
        assert_rejected(doc, "LAE-PROJ-008", lambda p=path, k=key: ops.get_entry(doc, p, k))
    assert_rejected(doc, "LAE-PROJ-007", lambda: ops.list_entries(doc, "identified_by.part"))


# --- add_node -------------------------------------------------------------------------------


def test_add_node_mints_iris_and_appends() -> None:
    doc = hydrate(record(identified_by=names("a", "b")))
    key = ops.add_node(
        doc,
        "identified_by",
        {"type": "Name", "content": "c", "part": [{"type": "Name", "content": "p"}]},
    )
    prop, node_id = parse_key(key)
    assert prop == "identified_by" and is_internal_node_iri(node_id, URI)
    entry = ops.get_entry(doc, "identified_by", key)
    assert entry[ORDER_KEY] == 2.0 and isinstance(entry["content"], Text)
    assert contents(doc) == ["a", "b", "c"]
    (part,) = project(doc)["identified_by"][2]["part"]
    assert is_internal_node_iri(part["id"], URI) and part["id"] != node_id
    public = strip_internal_iris(project(doc))
    assert public["identified_by"][2] == {
        "type": "Name",
        "content": "c",
        "part": [{"type": "Name", "content": "p"}],
    }


def test_add_node_positions_and_types() -> None:
    doc = hydrate(record(identified_by=names("a", "b")))
    first, second = keys(doc, "identified_by")
    ops.add_node(doc, "identified_by[Name]", {"content": "mid"}, before_key=second)
    ops.add_node(doc, "identified_by[Identifier]", {"content": "front"}, before_key=first)
    ops.add_node(doc, "identified_by[Name]", {"type": "Name", "content": "end"})
    assert contents(doc) == ["front", "a", "mid", "b", "end"]
    assert [e[ORDER_KEY] for _, e in ops.list_entries(doc, "identified_by")] == [
        -1.0,
        0.0,
        0.5,
        1.0,
        2.0,
    ]
    assert [e["type"] for _, e in ops.list_entries(doc, "identified_by")][0] == "Identifier"


def test_add_node_keeps_an_id_it_is_given_and_never_duplicates_it() -> None:
    doc = hydrate(record())
    minted: list[str] = []

    def mint() -> str:
        minted.append(f"{URI}/node/{len(minted)}")
        return minted[-1]

    node = {"id": "https://example.org/own", "type": "Type", "part": [{"type": "Type"}]}
    key = ops.add_node(doc, "classified_as", node, mint=mint)
    assert key == entry_key("classified_as", "https://example.org/own")
    assert len(minted) == 1
    before = doc.get_update()
    assert ops.add_node(doc, "classified_as", {**node, "_label": "again"}, mint=mint) == key
    assert doc.get_update() == before and len(minted) == 1
    assert node == {"id": "https://example.org/own", "type": "Type", "part": [{"type": "Type"}]}


def test_add_node_rejections_change_nothing_and_mint_nothing() -> None:
    doc = hydrate(record(identified_by=names("a"), timespan={"type": "TimeSpan"}, x_single="value"))

    def never() -> str:
        raise AssertionError("minted")

    (first,) = keys(doc, "identified_by")
    cases: list[tuple[str, Any]] = [
        ("LAE-PROJ-007", lambda: ops.add_node(doc, "identified_by", {"content": "no type"})),
        ("LAE-PROJ-007", lambda: ops.add_node(doc, "identified_by[Name]", {"type": "Type"})),
        ("LAE-PROJ-007", lambda: ops.add_node(doc, "identified_by", {"type": "Name", "id": 5})),
        ("LAE-PROJ-007", lambda: ops.add_node(doc, "identified_by", ["not", "an", "object"])),  # type: ignore[arg-type]
        ("LAE-PROJ-006", lambda: ops.add_node(doc, "timespan", {"type": "TimeSpan"}, mint=never)),
        ("LAE-PROJ-006", lambda: ops.add_node(doc, "x_single", {"type": "Name"}, mint=never)),
        (
            "LAE-PROJ-001",
            lambda: ops.add_node(doc, "identified_by", {"type": "Name", "_order": 1}, mint=never),
        ),
        (
            "LAE-PROJ-001",
            lambda: ops.add_node(
                doc,
                "identified_by",
                {"type": "Name", "part": [{"type": "Name", "_value": 1}]},
                mint=never,
            ),
        ),
        (
            "LAE-PROJ-002",
            lambda: ops.add_node(
                doc, "identified_by", {"type": "Name", "x": float("nan")}, mint=never
            ),
        ),
        (
            "LAE-PROJ-003",
            lambda: ops.add_node(
                doc, "identified_by", {"type": "Name", "content": "\ud800"}, mint=never
            ),
        ),
        (
            "LAE-PROJ-008",
            lambda: ops.add_node(
                doc,
                "identified_by[Name]",
                {},
                before_key=entry_key("identified_by", "x"),
                mint=never,
            ),
        ),
        (
            "LAE-PROJ-008",
            lambda: ops.add_node(doc, "classified_as[Type]", {}, before_key=first, mint=never),
        ),
        (
            "LAE-PROJ-008",
            lambda: ops.add_node(
                doc,
                "produced_by.part[Production]",
                {},
                before_key=entry_key("part", "x"),
                mint=never,
            ),
        ),
        ("LAE-PROJ-007", lambda: ops.add_node(doc, "shows.part[Name]", {}, mint=never)),
        ("LAE-PROJ-006", lambda: ops.add_node(doc, "x_single[Name].part[Name]", {}, mint=never)),
    ]
    for expected, call in cases:
        assert_rejected(doc, expected, call)


def test_without_a_root_id_minting_needs_the_record_uri() -> None:
    doc = hydrate({"type": "HumanMadeObject"}, URI)
    assert_rejected(doc, "LAE-PROJ-004", lambda: ops.add_node(doc, "identified_by[Name]", {}))
    key = ops.add_node(doc, "identified_by[Name]", {}, record_uri=URI)
    assert is_internal_node_iri(parse_key(key)[1], URI)
    # A reference needs no IRI, so no record URI either.
    ops.add_reference(doc, "classified_as", AAT + "1", "Type")


# --- add_reference --------------------------------------------------------------------------


def test_add_reference() -> None:
    doc = hydrate(record(classified_as=[{"id": AAT + "1", "type": "Type"}]))
    first = entry_key("classified_as", AAT + "1")
    key = ops.add_reference(doc, "classified_as", AAT + "2", "Type", label="two")
    assert key == entry_key("classified_as", AAT + "2")
    assert ops.add_reference(doc, "classified_as[Type]", AAT + "3", before_key=first) == entry_key(
        "classified_as", AAT + "3"
    )
    assert ops.add_reference(doc, "x_untyped", "https://example.org/u") == entry_key(
        "x_untyped", "https://example.org/u"
    )
    assert project(doc)["classified_as"] == [
        {"id": AAT + "3", "type": "Type"},
        {"id": AAT + "1", "type": "Type"},
        {"id": AAT + "2", "type": "Type", "_label": "two"},
    ]
    assert project(doc)["x_untyped"] == [{"id": "https://example.org/u"}]
    before = doc.get_update()
    assert ops.add_reference(doc, "classified_as", AAT + "1", "Type", label="ignored") == first
    assert ops.add_reference(doc, "classified_as", AAT + "1", "Other", before_key=key) == first
    assert doc.get_update() == before
    assert_rejected(
        doc, "LAE-PROJ-007", lambda: ops.add_reference(doc, "classified_as[Type]", AAT + "4", "Set")
    )
    assert_rejected(doc, "LAE-PROJ-007", lambda: ops.add_reference(doc, "classified_as", 5))  # type: ignore[arg-type]
    assert_rejected(
        doc, "LAE-PROJ-003", lambda: ops.add_reference(doc, "classified_as", "\udc00", "Type")
    )


def test_two_clients_adding_the_same_reference_converge_on_one_entry() -> None:
    a = hydrate(record())
    b = fork(a)
    ops.add_reference(a, "classified_as", AAT + "1", "Type", label="from a")
    ops.add_reference(b, "classified_as", AAT + "1", "Type", label="from b")
    ops.add_reference(b, "classified_as", AAT + "2", "Type")
    sync(a, b)
    assert project(a) == project(b)
    found = project(a)["classified_as"]
    assert sorted(r["id"] for r in found) == [AAT + "1", AAT + "2"]
    assert found[[r["id"] for r in found].index(AAT + "1")]["_label"] in ("from a", "from b")


# --- concurrent inserts across two documents (R14, R18) -------------------------------------


@pytest.mark.parametrize("a_first", [True, False])
def test_two_clients_insert_a_name_at_position_1(a_first: bool) -> None:
    a = hydrate(record(identified_by=names("n0", "n1", "n2")))
    b = fork(a)
    second = keys(a, "identified_by")[1]
    key_a = ops.add_node(a, "identified_by[Name]", {"content": "A"}, before_key=second)
    key_b = ops.add_node(b, "identified_by[Name]", {"content": "B"}, before_key=second)
    if a_first:
        b.apply_update(a.get_update(b.get_state()))
        a.apply_update(b.get_update(a.get_state()))
    else:
        a.apply_update(b.get_update(a.get_state()))
        b.apply_update(a.get_update(b.get_state()))
    assert canonical_json(project(a)) == canonical_json(project(b))
    order = contents(a)
    assert order[0] == "n0" and order[3:] == ["n1", "n2"]
    assert order[1:3] == (["A", "B"] if key_a < key_b else ["B", "A"])
    entries = dict(ops.list_entries(a, "identified_by"))
    assert entries[key_a][ORDER_KEY] == entries[key_b][ORDER_KEY] == 0.5
    assert [e[ORDER_KEY] for e in entries.values()] == [0.0, 0.5, 0.5, 1.0, 2.0]


FIRST_INSERT_PATHS = [
    "referred_to_by[LinguisticObject]",
    "produced_by.part[Production]",
    "identified_by[Name].part[Name]",
    "timespan.identified_by[Name]",
    "x_empty[Name]",
]


@pytest.mark.parametrize("path", FIRST_INSERT_PATHS)
@pytest.mark.parametrize("clients", [2, 3])
def test_concurrent_first_inserts_through_a_path_are_all_kept(path: str, clients: int) -> None:
    source = hydrate(
        record(
            identified_by=names("title"),
            produced_by={"type": "Production"},
            timespan={"type": "TimeSpan"},
            x_empty=[],
        )
    )
    docs = [source, *[fork(source) for _ in range(clients - 1)]]
    assert ops.list_entries(source, path) == []
    added = []
    for number, doc in enumerate(docs):
        added.append(ops.add_node(doc, path, {"_label": f"from {number}"}))
        ops.add_reference(doc, path.rsplit("[", 1)[0], "https://example.org/shared")
    assert len(set(added)) == clients
    sync(*reversed(docs))
    projections = [canonical_json(project(doc)) for doc in docs]
    assert len(set(projections)) == 1
    for doc in docs:
        found = keys(doc, path.rsplit("[", 1)[0])
        assert set(added) < set(found) and len(found) == clients + 1
        labels = [v.get("_label") for v in ops.get_path(doc, path)]
        assert sorted(labels) == [f"from {i}" for i in range(clients)]


def test_concurrent_creation_of_a_missing_intermediate_follows_the_kind_of_property() -> None:
    a = hydrate(record())
    b = fork(a)
    # Single-valued intermediate (timespan): one whole value wins.
    ops.set_scalar(a, "timespan.begin_of_the_begin", "from a")
    ops.set_scalar(b, "timespan.end_of_the_end", "from b")
    # Array intermediate (part): both entries are kept.
    ops.add_reference(a, "part[Production].carried_out_by", "https://example.org/a")
    ops.add_reference(b, "part[Production].carried_out_by", "https://example.org/b")
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    span = project(a)["timespan"]
    assert span["type"] == "TimeSpan"
    assert ("begin_of_the_begin" in span) != ("end_of_the_end" in span)
    parts = project(a)["part"]
    assert sorted(p["carried_out_by"][0]["id"] for p in parts) == [
        "https://example.org/a",
        "https://example.org/b",
    ]
    assert_rejected(
        a,
        "LAE-PROJ-007",
        lambda: ops.add_reference(a, "part[Production].carried_out_by", "https://example.org/c"),
    )


# --- remove_entry, move_entry ---------------------------------------------------------------


def test_remove_entry() -> None:
    doc = hydrate(record(identified_by=names("a", "b"), classified_as=[{"id": AAT + "1"}]))
    first, second = keys(doc, "identified_by")
    assert ops.remove_entry(doc, "identified_by[Name]", first) is True
    assert ops.remove_entry(doc, "identified_by", first) is False
    assert contents(doc) == ["b"]
    assert ops.get_entry(doc, "identified_by", second)[ORDER_KEY] == 1.0  # nothing else touched
    assert ops.remove_entry(doc, "identified_by", second) is True
    assert "identified_by" not in project(doc)
    assert ops.remove_entry(doc, "produced_by.identified_by", second) is False
    assert_rejected(
        doc, "LAE-PROJ-008", lambda: ops.remove_entry(doc, "identified_by", "classified_as")
    )
    assert_rejected(
        doc,
        "LAE-PROJ-008",
        lambda: ops.remove_entry(doc, "identified_by", entry_key("classified_as", AAT + "1")),
    )
    assert project(doc)["classified_as"] == [{"id": AAT + "1"}]


def test_removal_wins_over_a_concurrent_edit_inside_the_entry() -> None:
    a = hydrate(record(identified_by=names("a", "b")))
    b = fork(a)
    first = keys(a, "identified_by")[0]
    ops.remove_entry(a, "identified_by", first)
    inside = ops.get_entry(b, "identified_by", first)
    ops.set_scalar(b, "content", "edited", base=inside)
    ops.add_node(b, "part[Name]", {"content": "new"}, base=inside)
    sync(a, b)
    assert contents(a) == contents(b) == ["b"]


def test_move_entry() -> None:
    doc = hydrate(record(identified_by=names("a", "b", "c", "d")))
    a, b, c, d = keys(doc, "identified_by")
    ops.move_entry(doc, "identified_by", d, a)
    assert contents(doc) == ["d", "a", "b", "c"]
    ops.move_entry(doc, "identified_by", d, None)
    assert contents(doc) == ["a", "b", "c", "d"]
    ops.move_entry(doc, "identified_by[Name]", a, c)
    assert contents(doc) == ["b", "a", "c", "d"]
    orders = {k: e[ORDER_KEY] for k, e in ops.list_entries(doc, "identified_by")}
    assert orders == {b: 1.0, a: 1.5, c: 2.0, d: 3.0}
    for key, before in [(a, c), (d, None), (b, b), (b, a)]:
        state = doc.get_update()
        ops.move_entry(doc, "identified_by", key, before)
        assert doc.get_update() == state, "already in place: nothing is written"
    missing = entry_key("identified_by", "missing")
    assert_rejected(doc, "LAE-PROJ-008", lambda: ops.move_entry(doc, "identified_by", missing, a))
    assert_rejected(doc, "LAE-PROJ-008", lambda: ops.move_entry(doc, "identified_by", a, missing))
    assert_rejected(doc, "LAE-PROJ-008", lambda: ops.move_entry(doc, "classified_as", a, None))
    assert_rejected(doc, "LAE-PROJ-008", lambda: ops.move_entry(doc, "produced_by.part", a, None))


def test_move_entry_writes_one_order_unless_it_must_rebalance() -> None:
    doc = hydrate(record(identified_by=names("a", "b", "c")))
    a, b, c = keys(doc, "identified_by")
    entries = dict(ops.list_entries(doc, "identified_by"))
    entries[b][ORDER_KEY] = 0.0  # a tie, as two concurrent inserts leave it
    assert keys(doc, "identified_by") == sorted([a, b]) + [c]
    low, high = sorted([a, b])
    ops.move_entry(doc, "identified_by", c, high)
    assert keys(doc, "identified_by") == [low, c, high]
    assert [e[ORDER_KEY] for _, e in ops.list_entries(doc, "identified_by")] == [0.0, 0.5, 1.0]


def test_the_53rd_insert_before_the_last_entry_is_the_last_without_a_rebalance() -> None:
    doc = hydrate(record(identified_by=names("first", "last")))
    last = keys(doc, "identified_by")[1]
    for i in range(1, 54):
        ops.add_node(doc, "identified_by[Name]", {"content": str(i)}, before_key=last)
    entries = ops.list_entries(doc, "identified_by")
    assert entries[-2][1][ORDER_KEY] == 1.0 - 2.0**-53
    assert entries[-1][1][ORDER_KEY] == 1.0
    ops.add_node(doc, "identified_by[Name]", {"content": "54"}, before_key=last)
    assert [e[ORDER_KEY] for _, e in ops.list_entries(doc, "identified_by")] == [
        *[float(i) for i in range(54)],
        53.5,
        54.0,
    ]
    assert contents(doc) == ["first", *[str(i) for i in range(1, 55)], "last"]


# --- set_scalar -----------------------------------------------------------------------------


def test_set_scalar_values_round_trip_exactly() -> None:
    doc = hydrate(record(dimension=[{"type": "Dimension", "value": 1}]))
    values: list[Any] = [5, -7, 33.0, 33.5, 2**60, True, False, None, "text", "", 0, -0.0, 1e21]
    for value in values:
        ops.set_scalar(doc, "dimension.value", value)
        (got,) = ops.get_path(doc, "dimension.value")
        assert got == value and type(got) is type(value)
        assert json.dumps(project(doc)["dimension"][0]["value"]) == json.dumps(value)
    for bad, expected in [
        (float("inf"), "LAE-PROJ-002"),
        (2**70, "LAE-PROJ-002"),
        ("\ud800", "LAE-PROJ-003"),
        ({"a": 1}, "LAE-PROJ-007"),
        ([1], "LAE-PROJ-007"),
        (object(), "LAE-PROJ-005"),
    ]:
        assert_rejected(doc, expected, lambda b=bad: ops.set_scalar(doc, "dimension.value", b))


def test_set_scalar_content_is_text_only_on_the_three_types() -> None:
    doc = hydrate(
        record(
            identified_by=[{"type": "Identifier"}],
            referred_to_by=[{"type": "LinguisticObject", "content": "old"}],
            classified_as=[{"id": AAT + "1", "type": "Type"}],
        )
    )
    ops.set_scalar(doc, "identified_by[Identifier].content", "INV 1")
    ops.set_scalar(doc, "classified_as.content", "plain")
    ops.set_scalar(doc, "content", "root")
    ops.set_scalar(doc, "identified_by[Name].content", "created")
    assert isinstance(ops.get_path(doc, "identified_by[Identifier].content")[0], Text)
    assert isinstance(ops.get_path(doc, "identified_by[Name].content")[0], Text)
    assert ops.get_path(doc, "classified_as.content") == ["plain"]
    assert ops.get_path(doc, "content") == ["root"]
    text = ops.get_path(doc, "referred_to_by.content")[0]
    ops.set_scalar(doc, "referred_to_by.content", "new")
    assert ops.get_path(doc, "referred_to_by.content")[0] is not None and str(text) == "new"
    # A non-string replaces the Text with a plain value; a string makes a Text again.
    ops.set_scalar(doc, "referred_to_by.content", 5)
    assert ops.get_path(doc, "referred_to_by.content") == [5]
    ops.set_scalar(doc, "referred_to_by.content", "again")
    assert isinstance(ops.get_path(doc, "referred_to_by.content")[0], Text)
    # What set_scalar stores is what hydrating the projection stores.
    again = hydrate(project(doc))
    assert canonical_json(project(again)) == canonical_json(project(doc))
    assert isinstance(ops.get_path(again, "identified_by[Name].content")[0], Text)


def test_set_scalar_on_a_text_keeps_concurrent_edits_elsewhere() -> None:
    a = hydrate(record(identified_by=names("The quick brown fox 🦊 jumps")))
    b = fork(a)
    ops.set_scalar(a, "identified_by.content", "A quick brown fox 🦊 jumps")
    ops.set_scalar(b, "identified_by.content", "The quick brown fox 🦊 leaps!")
    sync(a, b)
    assert contents(a) == contents(b) == ["A quick brown fox 🦊 leaps!"]
    state = a.get_update()
    ops.set_scalar(a, "identified_by.content", "A quick brown fox 🦊 leaps!")
    assert a.get_update() == state, "an unchanged Text is not written"


def test_set_scalar_rejections() -> None:
    doc = hydrate(
        record(
            identified_by=names("a", "b"),
            timespan={"type": "TimeSpan"},
            current_location={"id": "https://example.org/place"},
            x_scalar="s",
        )
    )
    for path in [
        "identified_by",  # an array
        "identified_by.content",  # two nodes
        "identified_by[Name].content",  # two nodes
        "timespan",  # a node
        "current_location",  # a reference
        "timespan[TimeSpan]",  # a class filter on a scalar
        "x_scalar.inner",  # through a scalar
        "shows.content",  # an intermediate of unknown class
        "timespan[Period].begin_of_the_begin",  # would overwrite the TimeSpan
    ]:
        assert_rejected(doc, "LAE-PROJ-007", lambda p=path: ops.set_scalar(doc, p, "x"))
    assert_rejected(doc, "LAE-PROJ-006", lambda: ops.set_scalar(doc, "x_scalar[Name].content", "x"))


def test_missing_intermediates_are_created_with_the_model_class_or_the_filter() -> None:
    doc = hydrate(record())
    for key, cls in SINGLE_VALUED_CLASSES.items():
        ops.set_scalar(doc, f"{key}.x_note", "v")
        node = project(doc)[key]
        assert node["type"] == cls and is_internal_node_iri(node["id"], URI)
        assert ORDER_KEY not in ops.get_path(doc, key)[0]
    ops.set_scalar(doc, "created_by.part[Creation].timespan.begin_of_the_begin", "1500")
    ops.set_scalar(doc, "dissolved_by.timespan[Period].x_note", "filter wins")
    projected = project(doc)
    assert projected["created_by"]["part"][0]["type"] == "Creation"
    assert projected["created_by"]["part"][0]["timespan"]["type"] == "TimeSpan"
    assert projected["dissolved_by"]["timespan"]["type"] == "Period"
    assert projected["created_by"]["x_note"] == "v"
    public = canonical_json(strip_internal_iris(projected)).decode()
    assert "/node/" not in public


def test_the_single_valued_classes_are_single_valued_in_the_pinned_context_and_the_corpus() -> None:
    context = json.loads((REPO / "profiles" / "context" / "v1.json").read_text())["@context"]
    for key in SINGLE_VALUED_CLASSES:
        assert context[key]["@type"] == "@id" and "@container" not in context[key], key
    seen: dict[str, set[str]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in SINGLE_VALUED_CLASSES:
                    assert isinstance(child, dict), key
                    seen.setdefault(key, set()).add(child["type"])
                if key != "@context":
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for file in corpus_files():
        walk(load_record(file))
    assert seen == {key: {cls} for key, cls in SINGLE_VALUED_CLASSES.items()}


# --- set_object, remove_value ---------------------------------------------------------------


def test_set_object_and_remove_value() -> None:
    doc = hydrate(record(identified_by=names("a"), x_scalar=1))
    span = {"type": "TimeSpan", "identified_by": names("about 1500")}
    span_id = ops.set_object(doc, "produced_by.timespan", span)
    assert is_internal_node_iri(span_id, URI)
    assert span == {"type": "TimeSpan", "identified_by": names("about 1500")}
    assert contents(doc, "produced_by.timespan.identified_by.content") == ["about 1500"]
    assert ops.set_object(doc, "produced_by.timespan[TimeSpan]", {"end_of_the_end": "1"}) != span_id
    assert "identified_by" not in project(doc)["produced_by"]["timespan"]
    place = {"id": "https://example.org/place", "type": "Place", "_label": "x"}
    assert ops.set_object(doc, "current_location", place) == place["id"]
    assert ops.set_object(doc, "x_scalar", {"id": "https://example.org/over-a-scalar"})
    assert project(doc)["current_location"] == place
    assert ORDER_KEY not in ops.get_path(doc, "current_location")[0]
    for path, obj in [
        ("identified_by", {"type": "Name"}),
        ("x_new", {"no": "type"}),
        ("x_new", {"id": 5, "type": "T"}),
        ("x_new[Name]", {"type": "Type"}),
        ("x_new", "scalar"),
    ]:
        assert_rejected(doc, "LAE-PROJ-007", lambda p=path, o=obj: ops.set_object(doc, p, o))  # type: ignore[misc]
    assert_rejected(
        doc, "LAE-PROJ-001", lambda: ops.set_object(doc, "x_new", {"type": "T", "_order": 0})
    )

    assert ops.remove_value(doc, "produced_by.timespan[Period]") is False
    assert ops.remove_value(doc, "produced_by.timespan[TimeSpan]") is True
    assert ops.remove_value(doc, "produced_by.timespan") is False
    assert ops.remove_value(doc, "x_scalar") is True
    assert ops.remove_value(doc, "created_by.timespan") is False
    assert "created_by" not in project(doc)
    assert_rejected(doc, "LAE-PROJ-007", lambda: ops.remove_value(doc, "identified_by"))
    assert project(doc)["produced_by"] == {
        "id": project(doc)["produced_by"]["id"],
        "type": "Production",
    }


def test_concurrent_set_object_keeps_one_whole_value() -> None:
    a = hydrate(record(produced_by={"type": "Production"}))
    b = fork(a)
    ops.set_object(a, "produced_by.timespan", {"type": "TimeSpan", "begin_of_the_begin": "a"})
    ops.set_object(b, "produced_by.timespan", {"type": "TimeSpan", "end_of_the_end": "b"})
    sync(a, b)
    assert project(a) == project(b)
    span = project(a)["produced_by"]["timespan"]
    assert set(span) in ({"id", "type", "begin_of_the_begin"}, {"id", "type", "end_of_the_end"})


# --- splice_text and R19 --------------------------------------------------------------------


def test_splice_text_counts_code_points() -> None:
    doc = hydrate(record(identified_by=names("a😀b𝒳c")))
    path = "identified_by.content"
    ops.splice_text(doc, path, 2, 0, "é")
    assert contents(doc) == ["a😀éb𝒳c"]
    ops.splice_text(doc, path, 1, 2, "")
    assert contents(doc) == ["ab𝒳c"]
    ops.splice_text(doc, path, 2, 1, "🎨🎨")
    assert contents(doc) == ["ab🎨🎨c"]
    ops.splice_text(doc, path, 5, 0, "!")
    ops.splice_text(doc, path, 0, 0, "¡")
    assert contents(doc) == ["¡ab🎨🎨c!"]
    state = doc.get_update()
    ops.splice_text(doc, path, 3, 0, "")
    assert doc.get_update() == state
    for index, delete in [(8, 0), (7, 1), (-1, 0), (0, -1), (0, 8)]:
        assert_rejected(
            doc, "LAE-PROJ-008", lambda i=index, d=delete: ops.splice_text(doc, path, i, d, "x")
        )
    assert_rejected(doc, "LAE-PROJ-003", lambda: ops.splice_text(doc, path, 0, 0, "\udfff"))
    for bad in ["identified_by", "identified_by[Name]", "type", "x_absent", "produced_by.content"]:
        assert_rejected(doc, "LAE-PROJ-007", lambda p=bad: ops.splice_text(doc, p, 0, 0, "x"))


def test_utf8_offset() -> None:
    assert [utf8_offset("aé😀𝒳", i) for i in range(5)] == [0, 1, 3, 7, 11]
    assert utf8_offset("", 0) == 0


characters = st.characters(exclude_categories=["Cs"])
strings = st.one_of(
    st.text(characters, max_size=12),
    st.text(st.sampled_from("ab😀𝒳é"), max_size=10),
)


@settings(max_examples=300, deadline=None)
@given(strings, strings)
def test_text_change_is_the_one_splice_between_two_strings(old: str, new: str) -> None:
    index, delete, insert = text_change(old, new)
    assert old[:index] + insert + old[index + delete :] == new
    assert 0 <= index and 0 <= delete and index + delete <= len(old)
    if old == new:
        assert (delete, insert) == (0, "")
    else:
        # Minimal: the splice does not start or end with an unchanged character.
        removed = old[index : index + delete]
        assert not (removed and insert and removed[0] == insert[0])
        assert not (removed and insert and removed[-1] == insert[-1])


@settings(max_examples=200, deadline=None)
@given(strings, strings, strings)
def test_set_scalar_on_text_reaches_the_new_string_on_every_replica(
    start: str, first: str, second: str
) -> None:
    a = hydrate(record(identified_by=names(start)))
    b = fork(a)
    ops.set_scalar(a, "identified_by.content", first)
    assert contents(a) == [first]
    ops.set_scalar(b, "identified_by.content", second)
    assert contents(b) == [second]
    sync(a, b)
    assert contents(a) == contents(b)
    ops.set_scalar(a, "identified_by.content", start)
    sync(a, b)
    assert contents(b) == [start]


# --- transactions ---------------------------------------------------------------------------


def test_every_operation_is_one_transaction_and_takes_the_callers_origin() -> None:
    doc = hydrate(record(identified_by=names("a", "b")))
    events: list[Any] = []
    subscription = doc.observe(lambda event: events.append(event))
    ops.set_scalar(doc, "produced_by.part[Production].timespan.begin_of_the_begin", "1500")
    ops.add_node(doc, "produced_by.part[Production].identified_by[Name]", {"content": "x"})
    first, second = keys(doc, "identified_by")
    ops.move_entry(doc, "identified_by", second, first)
    ops.remove_entry(doc, "identified_by", first)
    assert len(events) == 4
    with doc.transaction(origin="agent-turn-1"):
        ops.add_reference(doc, "classified_as", AAT + "1", "Type")
        ops.set_scalar(doc, "identified_by.content", "B")
    assert len(events) == 5
    doc.unobserve(subscription)


def test_sorted_entries_ignore_other_properties_and_escaped_keys() -> None:
    doc = hydrate(record(identified_by=names("a"), classified_as=[{"id": AAT + "1"}]))
    root = record_map(doc)
    assert len(sorted_entries(root, "identified_by")) == 1
    assert sorted_entries(root, "identified") == []
    assert sorted_entries(root, "type") == []


@pytest.mark.parametrize("key", ["id", "type"])
def test_a_write_path_never_names_id_or_type(key: str) -> None:
    """R5, R20: ``id`` and ``type`` are not written through paths (profiles/README.md).

    Found in review: the node a path creates already holds both, so
    ``add_reference(doc, "identified_by[Identifier].type", ...)`` created the Identifier and
    then failed, leaving it behind; and ``set_scalar(doc, "id", ...)`` replaced the record URI.
    """
    doc = hydrate(record(identified_by=names("a"), timespan={"type": "TimeSpan"}))
    (first,) = keys(doc, "identified_by")
    entry = ops.get_entry(doc, "identified_by", first)

    def never() -> str:
        raise AssertionError("minted")

    ref = {"id": AAT + "1", "type": "Type"}
    calls: list[Any] = [
        lambda: ops.set_scalar(doc, key, "x"),
        lambda: ops.set_scalar(doc, key, "x", base=entry),
        lambda: ops.set_scalar(doc, f"timespan.{key}", "x"),
        lambda: ops.set_scalar(doc, f"produced_by.{key}", "x", mint=never),
        lambda: ops.set_scalar(doc, f"produced_by.{key}[Type].x_note", "x", mint=never),
        lambda: ops.set_object(doc, key, ref),
        lambda: ops.set_object(doc, f"produced_by.timespan.{key}", ref, mint=never),
        lambda: ops.remove_value(doc, key),
        lambda: ops.remove_value(doc, f"timespan.{key}"),
        lambda: ops.splice_text(doc, key, 0, 0, "x", base=entry),
        lambda: ops.add_node(doc, key, {"type": "Name"}, mint=never),
        lambda: ops.add_node(doc, f"identified_by[Identifier].{key}", {"type": "T"}, mint=never),
        lambda: ops.add_reference(doc, f"identified_by[Identifier].{key}", AAT + "1", mint=never),
        lambda: ops.add_reference(doc, f"produced_by.{key}[Type].technique", AAT + "1", mint=never),
        lambda: ops.remove_entry(doc, key, entry_key(key, "x")),
        lambda: ops.move_entry(doc, key, entry_key(key, "x"), None),
    ]
    for call in calls:
        assert_rejected(doc, "LAE-PROJ-007", call)
    assert project(doc)["id"] == URI
    assert "produced_by" not in project(doc)
    assert [i["type"] for i in project(doc)["identified_by"]] == ["Name"]
    # Reading is not restricted.
    assert ops.get_path(doc, key) == [project(doc)[key]]
    assert ops.get_path(doc, f"timespan.{key}") == [project(doc)["timespan"][key]]


def test_splice_text_takes_integers_and_a_string() -> None:
    """Found in review: a non-string ``insert`` deleted the range and then failed."""
    doc = hydrate(record(identified_by=names("abc")))
    path = "identified_by.content"
    calls: list[Any] = [
        lambda: ops.splice_text(doc, path, 0, 1, 5),  # type: ignore[arg-type]
        lambda: ops.splice_text(doc, path, 0, 1, None),  # type: ignore[arg-type]
        lambda: ops.splice_text(doc, path, 0.0, 1, "x"),  # type: ignore[arg-type]
        lambda: ops.splice_text(doc, path, 0, 1.0, "x"),  # type: ignore[arg-type]
        lambda: ops.splice_text(doc, path, True, 0, "x"),
        lambda: ops.splice_text(doc, path, 0, False, "x"),
    ]
    for call in calls:
        assert_rejected(doc, "LAE-PROJ-007", call)
    assert_rejected(doc, "LAE-PROJ-003", lambda: ops.splice_text(doc, path, 0, 1, "\ud800"))
    assert contents(doc) == ["abc"]


@pytest.mark.parametrize(
    ("old", "new", "splice"),
    [
        ("x\U0001f600y", "x\U0001f601y", (1, 1, "\U0001f601")),  # same high surrogate
        ("\U0001f600z", "\U0001fa00z", (0, 1, "\U0001fa00")),  # same low surrogate
        ("cafe\u0301", "caf\u00e9", (3, 2, "\u00e9")),  # not normalized
        ("", "", (0, 0, "")),
        ("", "a", (0, 0, "a")),
        ("a", "", (0, 1, "")),
        ("aaa", "aa", (2, 1, "")),  # the prefix is taken first
        ("aXa", "aa", (1, 1, "")),
    ],
)
def test_text_change_cuts_between_code_points(old: str, new: str, splice: Any) -> None:
    assert text_change(old, new) == splice
