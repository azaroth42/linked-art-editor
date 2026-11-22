"""Convergence of concurrent edits made through the keyed-entry + ``_order`` representation."""

from __future__ import annotations

import itertools
import os
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from pycrdt import Doc, Map, Text

from lae.corpus import CORPUS_DIR, load_record
from s3_projection import projection
from s3_projection.projection import (
    ORDER_KEY,
    ROOT_MAP,
    canonical_json,
    encode_plain,
    entry_order,
    hydrate,
    insert_entry,
    move_entry,
    project,
    remove_entry,
    sorted_entries,
    strip_internal_iris,
)

URI = "https://example.org/id/rec"
MAX_EXAMPLES = int(os.environ.get("S3_MAX_EXAMPLES", "300"))


def root(doc: Doc) -> Map:
    return doc.get(ROOT_MAP, type=Map)


def fork(doc: Doc) -> Doc:
    """A second client that received the full state of ``doc``."""
    other: Doc = Doc()
    other.apply_update(doc.get_update())
    assert other.client_id != doc.client_id
    return other


def sync(*docs: Doc) -> None:
    """Exchange updates until every replica has every other replica's changes."""
    for _ in range(2):
        for source in docs:
            for target in docs:
                if source is not target:
                    target.apply_update(source.get_update(target.get_state()))


def name(content: str) -> dict[str, Any]:
    return {"type": "Name", "content": content}


def labels(items: list[Any]) -> list[Any]:
    return [n.get("content", n.get("id")) for n in items]


def contents(doc: Doc, prop: str = "identified_by") -> list[Any]:
    return labels(project(doc).get(prop, []))


def record(n: int = 3) -> dict[str, Any]:
    return {"id": URI, "type": "Person", "identified_by": [name(f"n{i}") for i in range(n)]}


def add(doc: Doc, item: Any, index: int | None = None, prop: str = "identified_by") -> str:
    return insert_entry(doc, root(doc), prop, item, index, record_uri=URI)


def orders(doc: Doc, prop: str = "identified_by") -> list[float | None]:
    return [entry_order(e) for _, e in sorted_entries(root(doc), prop)]


# --- the guide's test ---


def test_two_clients_insert_a_name_at_position_1() -> None:
    a = hydrate(record())
    b = fork(a)
    add(a, name("from A"), 1)
    add(b, name("from B"), 1)
    assert contents(a) == ["n0", "from A", "n1", "n2"]
    assert contents(b) == ["n0", "from B", "n1", "n2"]

    sync(a, b)

    assert canonical_json(project(a)) == canonical_json(project(b))
    merged = contents(a)
    assert sorted(merged[1:3]) == ["from A", "from B"]  # neither insert was lost
    assert merged[0] == "n0" and merged[3:] == ["n1", "n2"]  # and nothing else moved
    # Both took the same midpoint; the entry key (it ends in the node IRI) breaks the tie.
    assert orders(a) == [0.0, 0.5, 0.5, 1.0, 2.0]
    entries = sorted_entries(root(a), "identified_by")
    assert entries[1][0] < entries[2][0]


def test_same_on_a_corpus_record_in_both_sync_orders() -> None:
    source = load_record(CORPUS_DIR / "linkedart/person-rembrandt-4.json")
    for first_a in (True, False):
        a = hydrate(source)
        b = fork(a)
        uri = source["id"]
        insert_entry(a, root(a), "identified_by", name("from A"), 1, record_uri=uri)
        insert_entry(b, root(b), "identified_by", name("from B"), 1, record_uri=uri)
        if first_a:
            b.apply_update(a.get_update(b.get_state()))
            a.apply_update(b.get_update(a.get_state()))
        else:
            a.apply_update(b.get_update(a.get_state()))
            b.apply_update(a.get_update(b.get_state()))
        assert canonical_json(project(a)) == canonical_json(project(b))
        names = strip_internal_iris(project(a))["identified_by"]
        assert len(names) == len(source["identified_by"]) + 2
        assert names[0] == source["identified_by"][0]
        assert {n["content"] for n in names[1:3]} == {"from A", "from B"}
        assert names[3:] == source["identified_by"][1:]


# --- creating an array: the hole the review closed (rules R6, R14) ---


def test_a_nested_container_created_concurrently_loses_one() -> None:
    """Why arrays are not nested maps: two maps created under one key are last-writer-wins."""
    a: Doc = Doc()
    a.get(ROOT_MAP, type=Map)["id"] = URI
    b = fork(a)
    for doc, label in ((a, "from A"), (b, "from B")):
        with doc.transaction():
            root(doc)["identified_by"] = Map({label: Map({"content": label, ORDER_KEY: 0.0})})
    sync(a, b)
    assert root(a)["identified_by"].to_py() == root(b)["identified_by"].to_py()
    assert len(root(a)["identified_by"]) == 1  # the other client's container and entry are gone


def _parents() -> dict[str, Any]:
    """Where an absent array property can be: the root, an entry, a single-valued node."""
    return {
        "root": lambda doc: root(doc),
        "entry": lambda doc: sorted_entries(root(doc), "identified_by")[0][1],
        "single": lambda doc: root(doc)["timespan"],
        "nested": lambda doc: sorted_entries(root(doc)["timespan"], "identified_by")[0][1],
    }


FIRST_INSERT_RECORD: dict[str, Any] = {
    "id": URI,
    "type": "Activity",
    "identified_by": [name("n0")],
    "timespan": {"type": "TimeSpan", "identified_by": [name("t0")]},
}


@pytest.mark.parametrize("where", list(_parents()))
@pytest.mark.parametrize("prop", ["classified_as", "x_unprofiled", "[", '["a","b"]'])
@pytest.mark.parametrize("clients", [2, 3])
def test_concurrent_first_inserts_into_an_absent_property_are_all_kept(
    where: str, prop: str, clients: int
) -> None:
    parent = _parents()[where]
    for delivery in itertools.permutations(range(clients)):
        first = hydrate(FIRST_INSERT_RECORD)
        docs = [first, *(fork(first) for _ in range(clients - 1))]
        for i, doc in enumerate(docs):
            assert prop not in project(doc) and not sorted_entries(parent(doc), prop)
            insert_entry(doc, parent(doc), prop, name(f"from {i}"), record_uri=URI)
            # A reference too: the same one from every client, plus one of its own.
            insert_entry(doc, parent(doc), prop, {"id": "shared"}, record_uri=URI)
            insert_entry(doc, parent(doc), prop, {"id": f"own {i}"}, record_uri=URI)
        sync(*(docs[i] for i in delivery))
        assert len({canonical_json(project(d)) for d in docs}) == 1
        got = labels(project_of(docs[0], where)[prop])
        expected = [f"from {i}" for i in range(clients)] + [f"own {i}" for i in range(clients)]
        assert sorted(got) == sorted([*expected, "shared"])


def project_of(doc: Doc, where: str) -> dict[str, Any]:
    out = project(doc)
    if where == "entry":
        return out["identified_by"][0]  # type: ignore[no-any-return]
    if where == "single":
        return out["timespan"]  # type: ignore[no-any-return]
    if where == "nested":
        return out["timespan"]["identified_by"][0]  # type: ignore[no-any-return]
    return out


def test_concurrent_first_inserts_into_an_empty_source_array_are_both_kept() -> None:
    a = hydrate({"id": URI, "type": "Person", "identified_by": []})
    b = fork(a)
    add(a, name("from A"))
    add(b, name("from B"))
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert sorted(contents(a)) == ["from A", "from B"]
    assert "identified_by" not in root(a)  # the empty-array marker is gone


def test_removing_every_entry_concurrently_with_an_insert_keeps_the_insert() -> None:
    a = hydrate(record(2))
    b = fork(a)
    for key, _ in sorted_entries(root(a), "identified_by"):
        remove_entry(a, root(a), key)
    assert "identified_by" not in project(a)
    add(b, name("from B"), 1)
    sync(a, b)
    assert contents(a) == contents(b) == ["from B"]


def test_concurrent_creation_of_a_single_valued_node_keeps_one_whole_value() -> None:
    """A single-valued property is a register: one client's whole value wins, never a mixture."""
    a = hydrate({"id": URI, "type": "Activity"})
    b = fork(a)
    for doc, label in ((a, "A"), (b, "B")):
        with doc.transaction():
            root(doc)["timespan"] = Map({"id": f"{URI}/ts-{label}", "type": "TimeSpan"})
        insert_entry(doc, root(doc)["timespan"], "identified_by", name(label), record_uri=URI)
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    timespan = project(a)["timespan"]
    assert labels(timespan["identified_by"]) == [timespan["id"][-1]]


def test_a_single_value_written_concurrently_with_an_insert_yields_the_array() -> None:
    """Cardinality conflict: entries win in the projection, on every replica."""
    a = hydrate({"id": URI, "type": "Person"})
    b = fork(a)
    add(a, name("from A"), prop="x")
    root(b)["x"] = encode_plain("single")
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert contents(a, "x") == ["from A"]
    # The shadowed single value blocks nothing: the next insert clears it.
    assert "x" in root(a)
    add(a, name("second"), prop="x")
    sync(a, b)
    assert contents(b, "x") == ["from A", "second"]
    assert "x" not in root(a) and "x" not in root(b)


# --- other concurrent cases the rules must settle ---


def test_concurrent_add_of_the_same_reference_yields_one_entry() -> None:
    ref = {"id": "http://vocab.getty.edu/aat/300404670", "type": "Type", "_label": "Primary"}
    a = hydrate({"id": URI, "type": "Person", "classified_as": [{"id": "x"}]})
    b = fork(a)
    add(a, ref, prop="classified_as")
    add(b, dict(ref), 0, prop="classified_as")
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert sorted(contents(a, "classified_as")) == sorted(["x", ref["id"]])
    # Inserting it again, anywhere, is a no-op: the entry is neither replaced nor moved.
    before = canonical_json(project(a))
    for index in (0, 1, 2):
        add(a, {"id": ref["id"]}, index, prop="classified_as")
    assert canonical_json(project(a)) == before


def test_the_same_reference_in_two_properties_of_one_node_is_two_entries() -> None:
    a = hydrate({"id": URI, "type": "Person"})
    add(a, {"id": "x"}, prop="classified_as")
    add(a, {"id": "x"}, prop="member_of")
    assert contents(a, "classified_as") == contents(a, "member_of") == ["x"]


def test_concurrent_text_edits_merge() -> None:
    a = hydrate(record(1))
    b = fork(a)

    def text(doc: Doc) -> Text:
        ((_, entry),) = sorted_entries(root(doc), "identified_by")
        value = entry["content"]
        assert isinstance(value, Text)
        return value

    text(a).insert(0, "A-")
    text(b).insert(2, "-B")
    sync(a, b)
    assert contents(a) == contents(b) == ["A-n0-B"]


def test_remove_wins_over_concurrent_edit_of_the_removed_entry() -> None:
    a = hydrate(record())
    b = fork(a)
    key, _ = sorted_entries(root(a), "identified_by")[1]
    remove_entry(a, root(a), key)
    root(b)[key]["_label"] = "edited concurrently"
    insert_entry(b, root(b)[key], "part", name("inserted inside"), record_uri=URI)
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert contents(a) == ["n0", "n2"]


def test_concurrent_move_and_insert_converge() -> None:
    a = hydrate(record(4))
    b = fork(a)
    key = sorted_entries(root(a), "identified_by")[3][0]
    move_entry(a, root(a), key, 0)
    add(b, name("new"), 2)
    sync(a, b)
    assert contents(a) == contents(b) == ["n3", "n0", "n1", "new", "n2"]


# --- precision limits and rebalance ---


def _count_rebalances(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = projection.rebalance

    def counting(parent: Map, prop: str) -> None:
        calls.append(len(sorted_entries(parent, prop)))
        original(parent, prop)

    monkeypatch.setattr(projection, "rebalance", counting)
    return calls


def test_repeated_insert_after_the_first_entry_rebalances_at_1075(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Halving towards 0.0 lasts 1074 inserts (down to the smallest subnormal double)."""
    calls = _count_rebalances(monkeypatch)
    doc = hydrate(record(2))
    expected = ["n0", "n1"]
    for i in range(1200):
        add(doc, name(f"i{i}"), 1)
        expected.insert(1, f"i{i}")
        if i == 1073:
            assert calls == []
            assert orders(doc)[1] == 5e-324
    assert contents(doc) == expected
    assert calls == [1076]  # the 1075th insert found no midpoint; 2 + 1074 entries renumbered
    assert len(set(orders(doc))) == len(orders(doc))


def test_repeated_insert_before_the_last_entry_rebalances_after_53(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Halving towards the upper neighbour is the worst case: 53 inserts, then fewer.

    After a rebalance the gap sits between larger integers (53.0 and 54.0, ...), which leaves
    fewer mantissa bits for fractions: 52 - floor(log2(lower neighbour)) halvings.
    """
    calls = _count_rebalances(monkeypatch)
    doc = hydrate(record(2))
    expected = ["n0", "n1"]
    for i in range(200):
        add(doc, name(f"i{i}"), len(expected) - 1)
        expected.insert(len(expected) - 1, f"i{i}")
        assert contents(doc) == expected
    assert calls == [55, 102, 148, 193]  # array sizes at each rebalance


def test_rebalance_touches_only_its_own_property() -> None:
    doc = hydrate({**record(2), "classified_as": [{"id": "a"}, {"id": "b"}]})
    for _, entry in sorted_entries(root(doc), "classified_as"):
        entry[ORDER_KEY] = 7.0  # a tie in another property of the same map
    first, second = (e for _, e in sorted_entries(root(doc), "identified_by"))
    second[ORDER_KEY] = 5e-324
    add(doc, name("forces rebalance"), 1)
    assert orders(doc) == [0.0, 0.5, 1.0]
    assert orders(doc, "classified_as") == [7.0, 7.0]


def test_insert_between_tied_entries_rebalances() -> None:
    a = hydrate(record(2))
    b = fork(a)
    add(a, name("A"), 1)
    add(b, name("B"), 1)
    sync(a, b)
    tied = contents(a)[1:3]
    add(a, name("between"), 2)
    assert contents(a) == ["n0", tied[0], "between", tied[1], "n1"]
    assert orders(a) == [0.0, 1.0, 1.5, 2.0, 3.0]
    sync(a, b)
    assert contents(b) == contents(a)


def test_rebalance_concurrent_with_insert_converges_without_loss() -> None:
    a = hydrate(record(2))
    # Exhaust the gap between the first two entries.
    first, second = (e for _, e in sorted_entries(root(a), "identified_by"))
    second[ORDER_KEY] = 5e-324
    b = fork(a)
    add(a, name("A forces rebalance"), 1)
    assert orders(a) == [0.0, 0.5, 1.0]
    add(b, name("B appends"))
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert sorted(contents(a)) == sorted(["n0", "n1", "A forces rebalance", "B appends"])
    assert contents(a)[0] == "n0"


def test_two_concurrent_rebalances_converge_without_loss() -> None:
    """Each ``_order`` is its own register, so two renumberings can interleave: still no loss."""
    a = hydrate(record(4))
    for _, entry in sorted_entries(root(a), "identified_by"):
        entry[ORDER_KEY] = 1.0  # all tied
    b = fork(a)
    add(a, name("A first"), 0)  # no rebalance: 0.0 is free before the tie
    add(a, name("A"), 2)  # rebalances with five entries
    add(b, name("B"), 3)  # rebalances with four entries
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert sorted(contents(a)) == sorted(["n0", "n1", "n2", "n3", "A first", "A", "B"])
    assert orders(a) == sorted(orders(a))  # type: ignore[type-var]


# --- randomized convergence ---

OPS = st.lists(
    st.tuples(
        st.integers(0, 2),  # client
        st.sampled_from(
            ["insert", "insert", "new", "new", "inner", "move", "remove", "text", "ref", "sync"]
        ),
        st.integers(0, 8),
        st.integers(0, 8),
    ),
    max_size=30,
)


@settings(max_examples=MAX_EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(OPS, st.permutations([0, 1, 2]))
def test_random_concurrent_ops_converge(
    ops: list[tuple[int, str, int, int]], order: list[int]
) -> None:
    first = hydrate(
        {
            "id": URI,
            "type": "Person",
            "identified_by": [name(f"n{i}") for i in range(3)],
            "classified_as": [{"id": "ref0"}],
        }
    )
    docs = [first, fork(first), fork(first)]
    inserted: set[str] = set()  # entry keys added to the root
    inner: set[tuple[str, str]] = set()  # (root entry key, entry key added inside it)
    removed: set[str] = set()
    ever_synced = concurrent_new = False
    new_by: dict[str, set[int]] = {}
    for step, (client, op, x, y) in enumerate(ops):
        doc = docs[client]
        entries = sorted_entries(root(doc), "identified_by")
        label = f"c{client}-s{step}"
        if op == "insert":
            inserted.add(add(doc, name(label), x))
        elif op == "new":
            # A property that no replica has at the start: every first insert must survive.
            prop = f"x_new{x % 2}"
            if not sorted_entries(root(doc), prop):
                new_by.setdefault(prop, set()).add(client)
            inserted.add(add(doc, name(label), y, prop=prop))
        elif op == "ref":
            add(doc, {"id": f"ref{x % 3}"}, y, prop="classified_as")
        elif op == "sync":
            ever_synced = True
            sync(docs[client], docs[x % 3])
        elif entries:
            key, entry = entries[x % len(entries)]
            if op == "inner":
                inner.add((key, insert_entry(doc, entry, "part", name(label), y, record_uri=URI)))
            elif op == "move":
                move_entry(doc, root(doc), key, y)
            elif op == "remove":
                remove_entry(doc, root(doc), key)
                removed.add(key)
            else:
                entry["content"].insert(min(y, len(str(entry["content"]))), "*")
    concurrent_new = any(len(clients) > 1 for clients in new_by.values())
    event(f"synced mid-run: {ever_synced}")
    event(
        f"two or more clients made a first insert into the same absent property: {concurrent_new}"
    )

    # Deliver everything, in an arbitrary replica order.
    sync(*(docs[i] for i in order))

    projections = [canonical_json(project(d)) for d in docs]
    assert projections[0] == projections[1] == projections[2]
    final = root(docs[0])
    for prop in ("identified_by", "x_new0", "x_new1"):
        sorted_orders = orders(docs[0], prop)
        assert sorted_orders == sorted(sorted_orders)  # type: ignore[type-var]
    # No insert is lost unless some client removed it (or its parent), and removal always wins.
    keys = set(final.keys())
    assert inserted - removed <= keys
    assert not (removed & keys)
    for parent_key, key in inner:
        if parent_key not in removed:
            assert key in final[parent_key]
    refs = contents(docs[0], "classified_as")
    assert len(refs) == len(set(refs))
