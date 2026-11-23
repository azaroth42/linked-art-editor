"""Property tests of the semantic diff: applying the changes to ``old`` gives ``new``.

For every pair the changes are serialized to JSON and parsed back first (what a client and
Phase 3's ``apply_change`` receive), then applied in list order to ``hydrate(old)`` by the
reference applier (``tests/diff/applier.py``); the projection must equal ``new`` exactly.

Pairs: random edits of every kind to corpus records (``tests/diff/mutations.py``), each corpus
record against the next one, and random documents with the pathological shapes of the
projection's own property test. ``LAE_DIFF_MAX_EXAMPLES`` raises the example count (default
200) for an extended run.
"""

from __future__ import annotations

import copy
import os
import random
from functools import cache
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

from lae.corpus import corpus_files, load_record
from lae.diff.semantic import CHANGE_KINDS, Change, same_json, semantic_diff
from lae.projection import assign_node_iris, canonical_json, hydrate, project
from tests.diff.applier import apply_changes
from tests.diff.mutations import mutate, reorderable
from tests.projection.strategies import RECORD_URI, records

MAX_EXAMPLES = int(os.environ.get("LAE_DIFF_MAX_EXAMPLES", "200"))

CHANGES = TypeAdapter(list[Change])

CORPUS = corpus_files()


@cache
def corpus_record(index: int) -> dict[str, Any]:
    """Corpus record ``index`` with internal node IRIs, as a draft or a revision holds it."""
    data = load_record(CORPUS[index])
    return assign_node_iris(data, data["id"])


def check_pair(old: dict[str, Any], new: dict[str, Any], record_uri: str) -> list[Any]:
    """Assert the contract for one pair and return the changes."""
    before = canonical_json(old), canonical_json(new)
    changes = semantic_diff(old, new)
    assert (canonical_json(old), canonical_json(new)) == before, "an input was modified"
    assert (not changes) == same_json(old, new)
    # Deterministic, and independent of key order.
    shuffled = {key: new[key] for key in reversed(list(new))}
    assert CHANGES.dump_json(semantic_diff(old, shuffled)) == CHANGES.dump_json(changes)

    wire = CHANGES.validate_json(CHANGES.dump_json(changes))
    doc = hydrate(old, record_uri)
    apply_changes(doc, wire)
    assert canonical_json(project(doc)) == canonical_json(new)
    return changes


def test_the_corpus_is_there() -> None:
    assert len(CORPUS) >= 244


@pytest.mark.parametrize("index", range(len(CORPUS)), ids=lambda i: CORPUS[i].name)
def test_a_corpus_record_against_itself_and_the_next_one(index: int) -> None:
    old = corpus_record(index)
    assert semantic_diff(old, copy.deepcopy(old)) == []
    # Another record under the same URI: nearly everything is removed and added.
    other = load_record(CORPUS[(index + 1) % len(CORPUS)])
    other["id"] = old["id"]
    new = assign_node_iris(other, old["id"])
    check_pair(old, new, old["id"])
    check_pair(new, old, old["id"])


REORDER_SHARE = 0.05
"""The share of edited corpus records whose changes must include a ``reorder``."""

REORDERABLE = [index for index in range(len(CORPUS)) if reorderable(corpus_record(index))]
"""Corpus records with an array that a ``reorder`` edit can change (two in five)."""


def test_edited_corpus_records() -> None:
    """Item 12 of the contract over random edits of corpus records, in both directions.

    Every change kind must occur, and a ``reorder`` in at least ``REORDER_SHARE`` of the
    examples: a generator that rarely reorders would leave the ``reorder`` path untested. Three
    in five corpus records have no array that a ``reorder`` edit can change, so half the
    examples start from one of the others (``REORDERABLE``).
    """
    kinds_per_example: list[set[str]] = []

    @settings(
        max_examples=MAX_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        st.one_of(st.sampled_from(REORDERABLE), st.integers(0, len(CORPUS) - 1)),
        st.integers(0, len(CORPUS) - 1),
        st.integers(1, 6),
        st.randoms(use_true_random=True),
    )
    def edited(index: int, donor: int, count: int, rng: random.Random) -> None:
        old = corpus_record(index)
        new, names = mutate(old, rng, corpus_record(donor), count)
        for name in names:
            event(f"edit: {name}")
        changes = check_pair(old, new, old["id"])
        kinds = {change.kind for change in changes}
        for kind in sorted(kinds):
            event(f"change: {kind}")
        check_pair(new, old, old["id"])
        kinds_per_example.append(kinds)

    edited()
    reordered = sum("reorder" in kinds for kinds in kinds_per_example)
    assert reordered >= REORDER_SHARE * len(kinds_per_example), (
        f"{reordered} of {len(kinds_per_example)} examples had a reorder"
    )
    assert set().union(*kinds_per_example) == set(CHANGE_KINDS)


@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(records(), records(), st.integers(0, 4), st.randoms(use_true_random=False))
def test_random_documents(
    first: dict[str, Any], second: dict[str, Any], count: int, rng: random.Random
) -> None:
    old = assign_node_iris(first, RECORD_URI)
    other = assign_node_iris(second, RECORD_URI)
    # Two unrelated documents, then an edited copy that shares most of its nodes with ``old``.
    check_pair(old, other, RECORD_URI)
    edited, _ = mutate(old, rng, other, count)
    check_pair(old, edited, RECORD_URI)
    check_pair(edited, old, RECORD_URI)
