"""The HumanMadeObject SHACL shapes from Spike S4 (Phase 0, Step 0.9).

Every targeted shape carries one ``lae:code`` (Step 1.9 reads diagnostic codes from it), each
constraint fires on a minimal broken record, and the golden corpus has no violations.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pyoxigraph as ox
import pyshacl
import pytest
from pyld import jsonld
from rdflib import RDF, Graph, Namespace
from rdflib.term import Node

from lae.corpus import iter_corpus
from lae.rdf.context import document_loader

SHAPES_PATH = Path(__file__).resolve().parents[3] / "profiles" / "shapes" / "HumanMadeObject.ttl"
CASES_PATH = Path(__file__).parent / "data" / "hmo_shape_cases.json"
SH = Namespace("http://www.w3.org/ns/shacl#")
LAE = Namespace("urn:lae:ns:")
CODE = re.compile(r"LAE-SHACL-\d{3}")
TARGETS = (SH.targetClass, SH.targetNode, SH.targetObjectsOf, SH.targetSubjectsOf)
CASES = json.loads(CASES_PATH.read_text())

Result = tuple[str, str]


@pytest.fixture(scope="module")
def shapes() -> Graph:
    return Graph().parse(SHAPES_PATH, format="turtle")


def targeted_shapes(shapes: Graph) -> set[Node]:
    return {s for target in TARGETS for s in shapes.subjects(target, None)}


def nquads(doc: dict[str, Any]) -> str:
    text: str = jsonld.to_rdf(
        doc, {"documentLoader": document_loader, "format": "application/n-quads"}
    )
    return text


def data_graph(doc: dict[str, Any], store: str) -> Any:
    if store == "rdflib":
        return Graph().parse(data=nquads(doc), format="nt")
    oxstore = ox.Store()
    oxstore.load(nquads(doc), format=ox.RdfFormat.N_QUADS)
    return oxstore


def validate(doc: dict[str, Any], shapes: Graph, store: str = "rdflib") -> Counter[Result]:
    _, report, _ = pyshacl.validate(data_graph(doc, store), shacl_graph=shapes, advanced=False)
    found: Counter[Result] = Counter()
    for result in report.subjects(RDF.type, SH.ValidationResult):
        code = shapes.value(report.value(result, SH.sourceShape), LAE.code)
        severity = str(report.value(result, SH.resultSeverity)).rsplit("#", 1)[-1]
        found[(str(code), severity)] += 1
    return found


def apply(base: dict[str, Any], operations: list[list[Any]]) -> dict[str, Any]:
    """Apply JSON Pointer 'set' operations to a copy of base; a null value removes the key."""
    doc = copy.deepcopy(base)
    for pointer, value in operations:
        *parents, last = pointer.lstrip("/").split("/")
        node: Any = doc
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if value is None:
            del node[last]
        else:
            node[int(last) if isinstance(node, list) else last] = value
    return doc


def test_every_targeted_shape_has_one_unique_code(shapes: Graph) -> None:
    codes = []
    for shape in targeted_shapes(shapes):
        values = [str(v) for v in shapes.objects(shape, LAE.code)]
        assert len(values) == 1, f"{shape} has lae:code {values}"
        assert CODE.fullmatch(values[0]), values[0]
        codes.append(values[0])
    assert len(codes) == 10
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("store", ["rdflib", "oxigraph"])
@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_case(case: dict[str, Any], store: str, shapes: Graph) -> None:
    doc = apply(CASES["base"], case["set"])
    assert validate(doc, shapes, store) == Counter(tuple(e) for e in case["expect"])


PYSHACL_DATE_DEFECT = (
    "pyshacl 0.40.2 sh:lessThanOrEquals raises TypeError when a bound is not a datetime rdflib "
    "can parse (ill-typed, or a BCE year); docs/spikes/S4-shacl.md recommends a Python rule"
)


@pytest.mark.xfail(raises=TypeError, strict=True, reason=PYSHACL_DATE_DEFECT)
@pytest.mark.parametrize("store", ["rdflib", "oxigraph"])
def test_ill_typed_end_of_the_end(store: str, shapes: Graph) -> None:
    doc = apply(CASES["base"], [["/produced_by/timespan/end_of_the_end", "1642"]])
    found = validate(doc, shapes, store)
    assert found == Counter({("LAE-SHACL-009", "Violation"): 1, ("LAE-SHACL-010", "Violation"): 1})


@pytest.mark.xfail(raises=TypeError, strict=True, reason=PYSHACL_DATE_DEFECT)
@pytest.mark.parametrize("store", ["rdflib", "oxigraph"])
def test_bce_timespan(store: str, shapes: Graph) -> None:
    doc = apply(
        CASES["base"],
        [
            ["/produced_by/timespan/begin_of_the_begin", "-0500-01-01T00:00:00Z"],
            ["/produced_by/timespan/end_of_the_end", "-0400-12-31T23:59:59Z"],
        ],
    )
    assert validate(doc, shapes, store) == Counter()


def test_cases_cover_every_code(shapes: Graph) -> None:
    expected = {code for case in CASES["cases"] for code, _ in case["expect"]}
    declared = {str(shapes.value(s, LAE.code)) for s in targeted_shapes(shapes)}
    assert expected == declared


def test_corpus_objects_have_no_violations(shapes: Graph) -> None:
    violations = {
        path.name: found
        for path, doc in iter_corpus()
        if doc.get("type") == "HumanMadeObject"
        for found in [validate(doc, shapes)]
        if any(severity == "Violation" for _, severity in found)
    }
    assert violations == {}
