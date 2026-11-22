"""Checks for the S6 prototype: offline loader, sanitation, generator, N-Triples output.

cd backend && uv run pytest -c pyproject.toml ../scripts/spikes/s6_bootstrap -q
"""

from __future__ import annotations

import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import orjson
import pyoxigraph
import pytest

from lae.ids import record_id_from_uri
from lae.rdf.context import UnpinnedContextError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s6_bootstrap.generate import (  # noqa: E402
    CONTEXT_URL,
    DEFAULT_BASE_URI,
    TYPE_MIX,
    TYPES,
    Generator,
    type_codes,
)
from s6_bootstrap.ntriples import (  # noqa: E402
    TO_RDF_OPTIONS,
    RecordRejected,
    enable_context_c14n_memo,
    expand_batch,
    expand_record,
    sanitize_ids,
    static_document_loader,
)
from s6_bootstrap.sanitize import clean_datetime, clean_iri, pad_datetime  # noqa: E402

SEED = 7


@pytest.fixture(scope="module")
def generator() -> Generator:
    return Generator(SEED, 2_000)


def _parse_ntriples(data: bytes) -> list[pyoxigraph.Triple]:
    """Strict N-Triples parse: raises on any malformed line."""
    return list(pyoxigraph.parse(data, format=pyoxigraph.RdfFormat.N_TRIPLES))


def test_static_loader_wraps_the_pinned_loader() -> None:
    doc = static_document_loader(CONTEXT_URL)
    assert "@context" in doc["document"]
    assert doc["tag"] == "static"
    with pytest.raises(UnpinnedContextError):
        static_document_loader("https://example.org/other-context.json")


def test_vendored_sanitizers() -> None:
    assert (
        clean_iri("http://vocab.getty.edu/aat/300033618") == "http://vocab.getty.edu/aat/300033618"
    )
    assert clean_iri("http://example.org/a b") == "http://example.org/a%20b"
    assert clean_iri('http://example.org/q"uote') == "http://example.org/q%22uote"
    assert clean_iri("https:/no-host") is None
    assert clean_iri("not an iri") is None
    assert clean_datetime("1850-01-01T00:00:00Z") == "1850-01-01T00:00:00Z"
    assert clean_datetime("c. 1850") is None
    assert pad_datetime("1850", end=False) == "1850-01-01T00:00:00"
    assert pad_datetime("1850-02", end=True) == "1850-02-28T23:59:59"


def test_generator_is_reproducible(generator: Generator) -> None:
    again = Generator(SEED, 2_000)
    other = Generator(SEED + 1, 2_000)
    for index in (0, 1, 17, 999, 1_999):
        assert orjson.dumps(generator.record(index)[0]) == orjson.dumps(again.record(index)[0])
    assert generator.record(5)[0] != other.record(5)[0]


def test_type_codes_follow_the_mix_and_are_prefix_stable() -> None:
    codes = type_codes(SEED, 40_000)
    assert type_codes(SEED, 1_000) == codes[:1_000]
    counts = Counter(TYPES[code] for code in codes)
    total = sum(TYPE_MIX.values())
    for name, weight in TYPE_MIX.items():
        assert abs(counts[name] / len(codes) - weight / total) < 0.01, name


def _nodes(value: Any, depth: int = 0) -> list[tuple[int, dict[str, Any]]]:
    found: list[tuple[int, dict[str, Any]]] = []
    if isinstance(value, dict):
        found.append((depth, value))
        for item in value.values():
            found.extend(_nodes(item, depth + 1))
    elif isinstance(value, list):
        for item in value:
            found.extend(_nodes(item, depth))
    return found


def test_records_have_uuid7_uris_and_node_iris(generator: Generator) -> None:
    for index in range(300):
        record, _ = generator.record(index)
        uri = record["id"]
        assert record["@context"] == CONTEXT_URL
        assert uri.startswith(DEFAULT_BASE_URI)
        assert uuid.UUID(uri[-36:]).version == 7
        for depth, node in _nodes(record):
            if depth and "type" in node:
                assert isinstance(node.get("id"), str), node
            ident = node.get("id")
            if isinstance(ident, str) and ident.startswith(uri + "/node/"):
                assert record_id_from_uri(ident, base_uri=DEFAULT_BASE_URI) == uuid.UUID(uri[-36:])


def test_records_expand_to_valid_ntriples(generator: Generator) -> None:
    texts = [orjson.dumps(generator.record(i)[0]).decode() for i in range(300)]
    data, stats = expand_batch(texts)
    assert stats["records"] == 300
    assert stats.get("records_failed", 0) == 0
    assert len(_parse_ntriples(data)) == stats["triples"] == data.count(b"\n")


def test_injected_defects_are_counted_and_output_stays_valid() -> None:
    gen = Generator(SEED, 400, defect_rate=1.0)
    injected: Counter[str] = Counter()
    texts = []
    for index in range(400):
        record, defect = gen.record(index)
        assert defect is not None
        injected[defect] += 1
        texts.append(orjson.dumps(record).decode())
    data, stats = expand_batch(texts)
    assert set(injected) == {
        "iri_space",
        "iri_unfixable",
        "date_partial",
        "date_garbage",
        "literal_special",
    }
    assert stats.get("iri_repaired", 0) == injected["iri_space"]
    assert stats.get("iri_rejected", 0) == injected["iri_unfixable"]
    assert stats.get("datetime_repaired", 0) == injected["date_partial"]
    assert stats.get("datetime_rejected", 0) == injected["date_garbage"]
    assert len(_parse_ntriples(data)) == stats["triples"]


def test_unusable_ids() -> None:
    stats: Counter[str] = Counter()
    record = {
        "id": "https://example.org/r/1",
        "type": "HumanMadeObject",
        "classified_as": [
            {"id": "https:/broken", "type": "Type"},
            {"id": "http://vocab.getty.edu/aat/300033618", "type": "Type"},
        ],
        "produced_by": {"id": "not an iri", "type": "Production"},
    }
    sanitize_ids(record, stats)
    assert record["classified_as"] == [
        {"id": "http://vocab.getty.edu/aat/300033618", "type": "Type"}
    ]
    assert "produced_by" not in record
    assert stats["iri_rejected"] == 2
    with pytest.raises(RecordRejected):
        sanitize_ids({"id": "https:/x", "type": "Type"}, Counter())


def test_blank_nodes_are_unique_per_record() -> None:
    def record(n: int) -> dict[str, Any]:
        return {
            "@context": CONTEXT_URL,
            "id": f"https://example.org/r/{n}",
            "type": "HumanMadeObject",
            "produced_by": {"type": "Production", "_label": f"production {n}"},
        }

    stats: Counter[str] = Counter()
    lines = expand_record(record(1), stats) + expand_record(record(2), stats)
    bnodes = {term for line in lines for term in line.split() if term.startswith("_:")}
    assert len(bnodes) == 2
    triples = _parse_ntriples("".join(lines).encode())
    assert len({t.subject for t in triples if isinstance(t.subject, pyoxigraph.BlankNode)}) == 2


def test_tuning_experiments_give_identical_output(generator: Generator) -> None:
    texts = [orjson.dumps(generator.record(i)[0]).decode() for i in range(300)]
    baseline, _ = expand_batch(texts)
    loader = TO_RDF_OPTIONS["documentLoader"]
    TO_RDF_OPTIONS["documentLoader"] = static_document_loader
    undo = enable_context_c14n_memo()
    try:
        tuned, _ = expand_batch(texts)
    finally:
        undo()
        TO_RDF_OPTIONS["documentLoader"] = loader
    assert tuned == baseline
