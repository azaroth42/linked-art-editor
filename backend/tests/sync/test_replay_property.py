"""Idempotent replay (contract §3 item 17; tests 28-29): no database, a ``pyoxigraph`` store.

For one record with versions ``v0 = ∅, v1, …`` and QLever holding ``v_c`` (or a state between
``v_c`` and ``v_{c+1}`` left by an update that stopped part-way), applying the updates of
events ``a … b`` in order, with ``a − 1 ≤ c ≤ b``, leaves exactly ``v_b``. The updates are built
by the worker's own pure functions (``build_event_update``, ``subjects_of``,
``owned_subjects``), in every mode where it is legal: the first event ``INSERT``, a later one
``DIFF`` or ``REPLACE``, a final one ``RETRACT``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyoxigraph
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lae.rdf import expand_to_ntriples, owned_subjects
from lae.sync.events import Mode, build_event_update, subjects_of
from tests.sync.conftest import expected_store, operations, triples_of

URI = "https://data.test/id/01900000-0000-7000-8000-000000000001"
NODES = [f"{URI}/node/01900000-0000-7000-8000-00000000010{k}" for k in range(3)]
PREDICATES = ["https://x.test/p", "https://x.test/q"]
XSD = "http://www.w3.org/2001/XMLSchema#"
OBJECTS = [
    '"a"',
    '"b"@en',
    f'"2.5E0"^^<{XSD}double>',
    f'"7"^^<{XSD}integer>',
    "<https://x.test/o>",
    f"<{NODES[0]}>",
]
LINES = [f"<{s}> <{p}> {o} ." for s in (URI, *NODES) for p in PREDICATES for o in OBJECTS]


@dataclass(frozen=True)
class Step:
    """One event: the record goes from ``old`` to ``new`` in ``mode``."""

    mode: Mode
    old: frozenset[str]
    new: frozenset[str]
    retract: frozenset[str]

    def update(self) -> str:
        return build_event_update(
            self.mode, old_lines=self.old, new_lines=self.new, retract_subjects=self.retract
        )


def chain(versions: list[frozenset[str]], replace: list[bool], retract: bool) -> list[Step]:
    """The events of a record whose versions are ``versions`` (``v1 …``)."""
    steps: list[Step] = []
    previous: frozenset[str] = frozenset()
    for index, version in enumerate(versions):
        if index == 0:
            mode = Mode.INSERT
        else:
            mode = Mode.REPLACE if replace[index] else Mode.DIFF
        owned = subjects_of(previous) | {URI}
        steps.append(Step(mode, previous, version, frozenset(owned)))
        previous = version
    if retract:
        steps.append(
            Step(Mode.RETRACT, previous, frozenset(), frozenset(subjects_of(previous) | {URI}))
        )
    return steps


versions_strategy = st.lists(
    st.frozensets(st.sampled_from(LINES), max_size=12), min_size=1, max_size=6
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    versions=versions_strategy,
    replace=st.lists(st.booleans(), min_size=6, max_size=6),
    retract=st.booleans(),
    data=st.data(),
)
def test_replaying_any_range_from_any_state_inside_it_ends_at_the_last_version(
    versions: list[frozenset[str]], replace: list[bool], retract: bool, data: st.DataObject
) -> None:
    steps = chain(versions, replace, retract)
    states = [frozenset[str](), *(step.new for step in steps)]  # states[i] = v_i
    count = len(steps)
    b = data.draw(st.integers(1, count), label="b")
    a = data.draw(st.integers(1, b), label="a")
    c = data.draw(st.integers(a - 1, b), label="c")
    store = pyoxigraph.Store()
    if states[c]:
        store.update(
            build_event_update(
                Mode.INSERT, old_lines=frozenset(), new_lines=states[c], retract_subjects=()
            )
        )
    if c < b:  # QLever may hold part of event c + 1: it stopped part-way
        ops = operations(steps[c].update())
        cut = data.draw(st.integers(0, len(ops)), label="cut")
        if cut:
            store.update(" ;\n".join(ops[:cut]))

    for step in steps[a - 1 : b]:
        update = step.update()
        if update:
            store.update(update)

    assert triples_of(store) == expected_store(states[b])


# -- two records --------------------------------------------------------------------------------

SHARED = [f"https://shared.test/node/{k}" for k in range(3)]
"""Foreign ids both records give their embedded nodes: each gets its own derived node IRI."""
RECORDS = [
    "https://data.test/id/01900000-0000-7000-8000-00000000000a",
    "https://data.test/id/01900000-0000-7000-8000-00000000000b",
]


def document(uri: str, label: str, statements: dict[int, str], names: list[int]) -> dict[str, Any]:
    """A record whose statements carry shared foreign ids and whose names are internal nodes."""
    return {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": uri,
        "type": "HumanMadeObject",
        "_label": label,
        "identified_by": [
            {
                "id": f"{uri}/node/01900000-0000-7000-8000-0000000002{k:02d}",
                "type": "Name",
                "content": f"Name {k}",
            }
            for k in names
        ],
        "referred_to_by": [
            {"id": SHARED[k], "type": "LinguisticObject", "content": content}
            for k, content in sorted(statements.items())
        ],
    }


version_strategy = st.tuples(
    st.sampled_from(["One", "Two"]),
    st.dictionaries(st.integers(0, 2), st.sampled_from(["x", "y"]), max_size=3),
    st.lists(st.integers(0, 2), unique=True, max_size=3),
)


@dataclass(frozen=True)
class Event:
    record: str
    update: str


def record_events(
    uri: str, specs: list[Any], replace: list[bool], retract: bool
) -> tuple[list[Event], frozenset[str]]:
    """The updates of one record's events and the lines of its last version."""
    events: list[Event] = []
    old_doc: dict[str, Any] | None = None
    old: frozenset[str] = frozenset()
    for index, (label, statements, names) in enumerate(specs):
        doc = document(uri, label, statements, names)
        new = frozenset(expand_to_ntriples(doc, 1, record_uri=uri))
        if old_doc is None:
            mode, subjects = Mode.INSERT, set[str]()
        else:
            mode = Mode.REPLACE if replace[index] else Mode.DIFF
            subjects = owned_subjects(old_doc, uri) | subjects_of(old)
        events.append(
            Event(
                uri,
                build_event_update(mode, old_lines=old, new_lines=new, retract_subjects=subjects),
            )
        )
        old_doc, old = doc, new
    if retract and old_doc is not None:
        subjects = owned_subjects(old_doc, uri) | subjects_of(old)
        events.append(
            Event(
                uri,
                build_event_update(
                    Mode.RETRACT, old_lines=old, new_lines=frozenset(), retract_subjects=subjects
                ),
            )
        )
        old = frozenset()
    return events, old


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    first=st.lists(version_strategy, min_size=1, max_size=4),
    second=st.lists(version_strategy, min_size=1, max_size=4),
    replace=st.lists(st.booleans(), min_size=4, max_size=4),
    retract=st.tuples(st.booleans(), st.booleans()),
    order=st.lists(st.booleans(), min_size=10, max_size=10),
)
def test_two_records_never_touch_each_others_triples(
    first: list[Any],
    second: list[Any],
    replace: list[bool],
    retract: tuple[bool, bool],
    order: list[bool],
) -> None:
    events_a, last_a = record_events(RECORDS[0], first, replace, retract[0])
    events_b, last_b = record_events(RECORDS[1], second, replace[::-1], retract[1])
    interleaved: list[Event] = []
    for take_b in order:  # an arbitrary interleaving that keeps each record's order
        source = events_b if (take_b and events_b) or not events_a else events_a
        if source:
            interleaved.append(source.pop(0))
    interleaved += events_a + events_b
    store = pyoxigraph.Store()

    for event in interleaved:
        if event.update:
            store.update(event.update)

    assert triples_of(store) == expected_store(last_a | last_b)
    a_subjects = subjects_of(last_a)
    assert all(s.startswith(RECORDS[0]) for s in a_subjects)
