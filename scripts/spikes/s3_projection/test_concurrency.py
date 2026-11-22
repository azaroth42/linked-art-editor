"""Convergence of concurrent edits made through the keyed-map + ``_order`` representation."""

from __future__ import annotations

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


def contents(doc: Doc, prop: str = "identified_by") -> list[Any]:
    return [n.get("content", n.get("id")) for n in project(doc).get(prop, [])]


def record(n: int = 3) -> dict[str, Any]:
    return {"id": URI, "type": "Person", "identified_by": [name(f"n{i}") for i in range(n)]}


# --- the guide's test ---


def test_two_clients_insert_a_name_at_position_1() -> None:
    a = hydrate(record())
    b = fork(a)
    insert_entry(a, root(a)["identified_by"], name("from A"), 1, record_uri=URI)
    insert_entry(b, root(b)["identified_by"], name("from B"), 1, record_uri=URI)
    assert contents(a) == ["n0", "from A", "n1", "n2"]
    assert contents(b) == ["n0", "from B", "n1", "n2"]

    sync(a, b)

    assert canonical_json(project(a)) == canonical_json(project(b))
    merged = contents(a)
    assert sorted(merged[1:3]) == ["from A", "from B"]  # neither insert was lost
    assert merged[0] == "n0" and merged[3:] == ["n1", "n2"]  # and nothing else moved
    # Both took the same midpoint; the entry key (the node IRI) breaks the tie.
    entries = sorted_entries(root(a)["identified_by"])
    assert [entry_order(e) for _, e in entries] == [0.0, 0.5, 0.5, 1.0, 2.0]
    assert entries[1][0] < entries[2][0]


def test_same_on_a_corpus_record_in_both_sync_orders() -> None:
    source = load_record(CORPUS_DIR / "linkedart/person-rembrandt-4.json")
    for first_a in (True, False):
        a = hydrate(source)
        b = fork(a)
        uri = source["id"]
        insert_entry(a, root(a)["identified_by"], name("from A"), 1, record_uri=uri)
        insert_entry(b, root(b)["identified_by"], name("from B"), 1, record_uri=uri)
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


# --- other concurrent cases the rules must settle ---


def test_concurrent_add_of_the_same_reference_yields_one_entry() -> None:
    ref = {"id": "http://vocab.getty.edu/aat/300404670", "type": "Type", "_label": "Primary"}
    a = hydrate({"id": URI, "type": "Person", "classified_as": [{"id": "x"}]})
    b = fork(a)
    insert_entry(a, root(a)["classified_as"], ref, record_uri=URI)
    insert_entry(b, root(b)["classified_as"], dict(ref), 0, record_uri=URI)
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert sorted(contents(a, "classified_as")) == sorted(["x", ref["id"]])
    # Inserting it again, anywhere, is a no-op: the entry is neither replaced nor moved.
    before = canonical_json(project(a))
    for index in (0, 1, 2):
        insert_entry(a, root(a)["classified_as"], {"id": ref["id"]}, index, record_uri=URI)
    assert canonical_json(project(a)) == before


def test_concurrent_text_edits_merge() -> None:
    a = hydrate(record(1))
    b = fork(a)

    def text(doc: Doc) -> Text:
        ((_, entry),) = sorted_entries(root(doc)["identified_by"])
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
    key, entry_a = sorted_entries(root(a)["identified_by"])[1]
    remove_entry(a, root(a)["identified_by"], key)
    root(b)["identified_by"][key]["_label"] = "edited concurrently"
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert contents(a) == ["n0", "n2"]


def test_concurrent_move_and_insert_converge() -> None:
    a = hydrate(record(4))
    b = fork(a)
    key = sorted_entries(root(a)["identified_by"])[3][0]
    move_entry(a, root(a)["identified_by"], key, 0)
    insert_entry(b, root(b)["identified_by"], name("new"), 2, record_uri=URI)
    sync(a, b)
    assert contents(a) == contents(b) == ["n3", "n0", "n1", "new", "n2"]


def test_concurrent_first_insert_into_an_absent_property_loses_one() -> None:
    """Documented hazard (rule R14): creating the container concurrently is last-writer-wins."""
    a = hydrate({"id": URI, "type": "Person"})
    b = fork(a)
    for doc, label in ((a, "from A"), (b, "from B")):
        with doc.transaction():
            root(doc)["identified_by"] = Map()
        insert_entry(doc, root(doc)["identified_by"], name(label), record_uri=URI)
    sync(a, b)
    assert contents(a) == contents(b)
    assert len(contents(a)) == 1  # one insert was lost


def test_precreated_empty_container_keeps_both_first_inserts() -> None:
    """The fix: the container exists (empty, projecting to no key) before clients diverge."""
    a = hydrate({"id": URI, "type": "Person"})
    with a.transaction():
        root(a)["identified_by"] = Map()
    assert project(a) == {"id": URI, "type": "Person"}
    b = fork(a)
    insert_entry(a, root(a)["identified_by"], name("from A"), record_uri=URI)
    insert_entry(b, root(b)["identified_by"], name("from B"), record_uri=URI)
    sync(a, b)
    assert contents(a) == contents(b)
    assert sorted(contents(a)) == ["from A", "from B"]


# --- precision limits and rebalance ---


def _count_rebalances(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = projection.rebalance

    def counting(container: Map) -> None:
        calls.append(len(container))
        original(container)

    monkeypatch.setattr(projection, "rebalance", counting)
    return calls


def test_repeated_insert_after_the_first_entry_rebalances_at_1075(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Halving towards 0.0 lasts 1074 inserts (down to the smallest subnormal double)."""
    calls = _count_rebalances(monkeypatch)
    doc = hydrate(record(2))
    container = root(doc)["identified_by"]
    expected = ["n0", "n1"]
    for i in range(1200):
        insert_entry(doc, container, name(f"i{i}"), 1, record_uri=URI)
        expected.insert(1, f"i{i}")
        if i == 1073:
            assert calls == []
            assert sorted_entries(container)[1][1][ORDER_KEY] == 5e-324
    assert contents(doc) == expected
    assert calls == [1076]  # the 1075th insert found no midpoint; 2 + 1074 entries renumbered
    orders = [entry_order(e) for _, e in sorted_entries(container)]
    assert len(set(orders)) == len(orders)


def test_repeated_insert_before_the_last_entry_rebalances_after_53(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Halving towards the upper neighbour is the worst case: 53 inserts, then fewer.

    After a rebalance the gap sits between larger integers (53.0 and 54.0, ...), which leaves
    fewer mantissa bits for fractions: 52 - floor(log2(lower neighbour)) halvings.
    """
    calls = _count_rebalances(monkeypatch)
    doc = hydrate(record(2))
    container = root(doc)["identified_by"]
    expected = ["n0", "n1"]
    for i in range(200):
        insert_entry(doc, container, name(f"i{i}"), len(expected) - 1, record_uri=URI)
        expected.insert(len(expected) - 1, f"i{i}")
        assert contents(doc) == expected
    assert calls == [55, 102, 148, 193]  # container sizes at each rebalance


def test_insert_between_tied_entries_rebalances() -> None:
    a = hydrate(record(2))
    b = fork(a)
    insert_entry(a, root(a)["identified_by"], name("A"), 1, record_uri=URI)
    insert_entry(b, root(b)["identified_by"], name("B"), 1, record_uri=URI)
    sync(a, b)
    tied = contents(a)[1:3]
    insert_entry(a, root(a)["identified_by"], name("between"), 2, record_uri=URI)
    assert contents(a) == ["n0", tied[0], "between", tied[1], "n1"]
    orders = [entry_order(e) for _, e in sorted_entries(root(a)["identified_by"])]
    assert orders == [0.0, 1.0, 1.5, 2.0, 3.0]
    sync(a, b)
    assert contents(b) == contents(a)


def test_rebalance_concurrent_with_insert_converges_without_loss() -> None:
    a = hydrate(record(2))
    container = root(a)["identified_by"]
    # Exhaust the gap between the first two entries.
    first, second = (e for _, e in sorted_entries(container))
    second[ORDER_KEY] = 5e-324
    b = fork(a)
    insert_entry(a, container, name("A forces rebalance"), 1, record_uri=URI)
    assert [entry_order(e) for _, e in sorted_entries(container)] == [0.0, 0.5, 1.0]
    insert_entry(b, root(b)["identified_by"], name("B appends"), record_uri=URI)
    sync(a, b)
    assert canonical_json(project(a)) == canonical_json(project(b))
    assert sorted(contents(a)) == sorted(["n0", "n1", "A forces rebalance", "B appends"])
    assert contents(a)[0] == "n0"


# --- randomized convergence ---

OPS = st.lists(
    st.tuples(
        st.integers(0, 2),  # client
        st.sampled_from(["insert", "insert", "move", "remove", "text", "ref", "sync"]),
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
    inserted: set[str] = set()
    removed: set[str] = set()
    ever_synced = False
    for step, (client, op, x, y) in enumerate(ops):
        doc = docs[client]
        container = root(doc)["identified_by"]
        entries = sorted_entries(container)
        if op == "insert":
            label = f"c{client}-s{step}"
            inserted.add(insert_entry(doc, container, name(label), x, record_uri=URI))
        elif op == "ref":
            insert_entry(doc, root(doc)["classified_as"], {"id": f"ref{x % 3}"}, y, record_uri=URI)
        elif op == "sync":
            ever_synced = True
            sync(docs[client], docs[x % 3])
        elif entries:
            key, entry = entries[x % len(entries)]
            if op == "move":
                move_entry(doc, container, key, y)
            elif op == "remove":
                remove_entry(doc, container, key)
                removed.add(key)
            else:
                entry["content"].insert(min(y, len(str(entry["content"]))), "*")
    event(f"synced mid-run: {ever_synced}")

    # Deliver everything, in an arbitrary replica order.
    sync(*(docs[i] for i in order))

    projections = [canonical_json(project(d)) for d in docs]
    assert projections[0] == projections[1] == projections[2]
    final = root(docs[0])["identified_by"]
    orders = [entry_order(e) for _, e in sorted_entries(final)]
    assert None not in orders and orders == sorted(orders)  # type: ignore[type-var]
    # No insert is lost unless some client removed it, and removal always wins.
    assert inserted - removed <= set(final.keys())
    assert not (removed & set(final.keys()))
    refs = contents(docs[0], "classified_as")
    assert len(refs) == len(set(refs))
