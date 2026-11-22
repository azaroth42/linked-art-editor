"""Hypothesis strategies for random Linked-Art-shaped JSON-LD documents."""

from __future__ import annotations

from typing import Any

from hypothesis import strategies as st

from s3_projection.projection import RESERVED_KEYS

RECORD_URI = "https://example.org/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f"

NODE_TYPES = [
    "Name",
    "Identifier",
    "LinguisticObject",
    "Type",
    "Dimension",
    "TimeSpan",
    "Production",
    "AttributeAssignment",
    "MonetaryAmount",
    "Person",
]
ARRAY_PROPS = [
    "identified_by",
    "classified_as",
    "referred_to_by",
    "part",
    "language",
    "carried_out_by",
    "equivalent",
    "member_of",
    "notation",
    "x_unprofiled",
]
SINGLE_PROPS = ["timespan", "produced_by", "unit", "currency", "took_place_at", "x_single"]
SCALAR_PROPS = ["value", "begin_of_the_begin", "end_of_the_end", "format", "x_flag", "x_null"]

# Any Unicode scalar value (no lone surrogates: they are not valid JSON text, rule R16).
text = st.text(st.characters(exclude_categories=["Cs"]), max_size=12)
# A small pool of ids so that duplicates and key collisions actually happen.
ids = st.one_of(
    st.sampled_from(
        [
            "http://vocab.getty.edu/aat/300404670",
            "http://vocab.getty.edu/aat/300027221",
            "https://example.org/id/a",
            "https://example.org/id/a #2",
            "_:b0",
            "=s:x",
            "[",
            '["a","b"]',
            "type",
            "_order",
            "",
        ]
    ),
    text,
)
numbers = st.one_of(
    st.integers(min_value=-(2**63), max_value=2**64 - 1),
    st.integers(min_value=-1000, max_value=1000),
    st.floats(allow_nan=False, allow_infinity=False),
    st.floats(min_value=-1000, max_value=1000).map(lambda f: float(round(f))),
    st.sampled_from([0.0, -0.0, 33.0, 33.5, 1e21, 2.0**53, 2.0**53 + 2, 5e-324, 0.1]),
)
scalars = st.one_of(
    text, st.sampled_from(["x", "x #2", "true", "1"]), numbers, st.booleans(), st.none()
)
# Keys that start with "[" share a prefix with entry keys and must be escaped (rule R6).
KEY_LIKE = ["[", "[]", '["a"]', '["identified_by","x"]']
keys = st.one_of(
    st.sampled_from(ARRAY_PROPS + SINGLE_PROPS + SCALAR_PROPS), st.sampled_from(KEY_LIKE), text
).filter(lambda k: k not in RESERVED_KEYS and k not in {"id", "type", "@context"})

# Arbitrary JSON, including objects that carry reserved keys or `type` deep inside (opaque).
any_json = st.recursive(
    scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(text, inner, max_size=3)),
    max_leaves=6,
)
# Objects without `type` that are not references: stored opaquely.
plain_objects = st.dictionaries(
    st.one_of(text, st.sampled_from(["id", "_order", "_value", "_json", "@value", "@language"])),
    any_json,
    max_size=3,
).filter(
    lambda d: (
        "type" not in d and not (isinstance(d.get("id"), str) and d.keys() <= {"id", "_label"})
    )
)


@st.composite
def references(draw: st.DrawFn) -> dict[str, Any]:
    ref: dict[str, Any] = {"id": draw(ids)}
    if draw(st.booleans()):
        ref["type"] = draw(st.sampled_from(NODE_TYPES))
    if draw(st.booleans()):
        ref["_label"] = draw(text)
    return ref


def _with_duplicates(items: st.SearchStrategy[Any]) -> st.SearchStrategy[list[Any]]:
    """Lists that often repeat an element, in arbitrary positions."""

    @st.composite
    def build(draw: st.DrawFn) -> list[Any]:
        base = draw(st.lists(items, max_size=4))
        if base and draw(st.booleans()):
            for _ in range(draw(st.integers(1, 2))):
                base.insert(draw(st.integers(0, len(base))), draw(st.sampled_from(base)))
        return base

    return build()


def nodes(depth: int, *, min_props: int = 0) -> st.SearchStrategy[dict[str, Any]]:
    """Embedded nodes nested at most ``depth`` levels deep."""

    @st.composite
    def build(draw: st.DrawFn) -> dict[str, Any]:
        node: dict[str, Any] = {
            "type": draw(
                st.one_of(
                    st.sampled_from(NODE_TYPES),
                    st.sampled_from(NODE_TYPES),
                    st.sampled_from(NODE_TYPES),
                    st.lists(st.sampled_from(NODE_TYPES), max_size=2),
                    scalars,
                )
            )
        }
        if draw(st.integers(0, 3)) == 0:
            node["id"] = draw(st.one_of(ids, ids, st.integers(0, 3)))
        if draw(st.booleans()):
            node["_label"] = draw(text)
        if draw(st.booleans()):
            node["content"] = draw(st.one_of(text, text, text, any_json))
        for key in draw(st.lists(keys, min_size=min_props, max_size=4, unique=True)):
            node[key] = draw(values(depth))
        return node

    return build()


def values(depth: int) -> st.SearchStrategy[Any]:
    """Property values: scalars, single objects, and arrays of every mixture."""
    leaves = [scalars, references(), plain_objects, _with_duplicates(scalars), st.just([])]
    if depth <= 0:
        return st.one_of(*leaves, _with_duplicates(references()))
    inner = nodes(depth - 1)
    mixed = st.one_of(inner, inner, references(), references(), scalars, plain_objects, any_json)
    return st.one_of(
        *leaves,
        inner,
        _with_duplicates(inner),
        _with_duplicates(inner),
        _with_duplicates(references()),
        _with_duplicates(st.one_of(inner, references())),
        _with_duplicates(mixed),
    )


@st.composite
def records(draw: st.DrawFn) -> dict[str, Any]:
    """A top-level record with a root ``id``, ``type`` and (usually) ``@context``."""
    record = dict(draw(nodes(3, min_props=2)))
    record["id"] = RECORD_URI
    if not isinstance(record["type"], str):
        record["type"] = "HumanMadeObject"
    context = draw(
        st.one_of(
            st.just("https://linked.art/ns/v1/linked-art.json"),
            st.lists(st.one_of(text, plain_objects), max_size=3),
            plain_objects,
            st.none(),
        )
    )
    if context is not None or draw(st.booleans()):
        record["@context"] = context
    return record
