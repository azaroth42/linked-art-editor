#!/usr/bin/env python3
"""Spike S4: pyshacl latency per HumanMadeObject record (Phase 0, Step 0.9).

Usage (from the repository root; needs the backend environment for ``lae``, pyld, and pyshacl):

    uv run --project backend python scripts/spikes/s4_shacl/bench.py \
        [--iterations 50] [--warmup 5] [--config NAME ...] [--out /tmp/lae-i9/s4-results.json]

What it does:

1. Collects every ``HumanMadeObject`` in the golden corpus (``lae.corpus.iter_corpus``), both
   ``linkedart/`` and ``institutional/``.
2. For each record, each configuration below in turn (interleaved, so background load falls on
   all of them alike): ``--warmup`` untimed runs, then ``--iterations`` timed runs of the whole
   per-record pipeline, timing three stages separately: ``expand`` (pyld ``to_rdf`` with the
   pinned context through ``lae.rdf.context``'s offline loader), ``build`` (the data graph from
   the expanded triples), and ``validate`` (pyshacl with ``HumanMadeObject.s4.ttl``, the shapes
   file as it was when the spike ran).
3. Checks that every configuration reports the same results (per record: conformance and the
   multiset of ``(lae:code, severity)``), that every configuration gives the expected results for
   the broken-record cases in ``hmo_shape_cases.s4.json``, and that a
   validator shared across threads agrees with the sequential run.
4. Times the data-graph preparation alternatives on their own (pyld dataset vs N-Quads text, into
   rdflib vs pyoxigraph).
5. Scale probe: the largest corpus object with its embedded array items repeated ``--scale``
   times, to see how latency grows beyond the corpus's record sizes.
6. Formulation probe: each shape's cost on its own, and the cost of an earlier SHACL Core
   (``sh:or``) formulation of LAE-SHACL-001/002 that the shipped file replaced with SHACL-SPARQL.
7. Prints Markdown tables (p50/p95/max per configuration, the slowest records, the corpus
   diagnostics, the probes) and writes the raw samples to ``--out`` as JSON.

Configurations (the guide asks for advanced=False, a pre-loaded shapes graph, and pyoxigraph):

- ``baseline``: ``pyshacl.validate(graph, shacl_graph=<path>, advanced=True)``; the Turtle shapes
  are parsed on every call and SHACL-AF is on (which forces a data-graph copy).
- ``advanced_off``: as ``baseline`` with ``advanced=False``.
- ``preloaded_shapes``: shapes parsed once into an rdflib ``Graph`` and passed to
  ``pyshacl.validate``; pyshacl still re-harvests the shapes from that graph on every call.
- ``cached_shapes``: a ``pyshacl.ShapesGraph`` harvested once and reused by a ``Validator`` per
  record, so no shape parsing or harvesting happens per call.
- ``oxigraph``: as ``preloaded_shapes`` with a ``pyoxigraph.Store`` as the data graph.
- ``oxigraph_cached``: as ``cached_shapes`` with a ``pyoxigraph.Store`` as the data graph.

This is throwaway spike code; Phase 1 Step 1.9 builds ``lae.validation.shacl`` from the report.
"""

from __future__ import annotations

import argparse
import copy
import gc
import json
import logging
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pyoxigraph as ox
import pyshacl
from pyld import jsonld
from pyshacl import ShapesGraph, Validator
from pyshacl.graph_abstraction import DataGraph
from pyshacl.monkey import apply_patches
from pyshacl.validator import assign_baked_in
from rdflib import RDF, BNode, Graph, Literal, Namespace, URIRef
from rdflib.term import Node

from lae.corpus import CORPUS_DIR, iter_corpus
from lae.rdf.context import document_loader

REPO_ROOT = Path(__file__).resolve().parents[3]
# The ten shapes and their cases as the spike measured them. Step 1.9 changed the live files
# (profiles/shapes/, backend/tests/validation/data/shape_cases.json): LAE-SHACL-009 and -010
# became Python rules and the shared declarations moved to common.ttl.
SHAPES_PATH = Path(__file__).resolve().parent / "HumanMadeObject.s4.ttl"
CASES_PATH = Path(__file__).resolve().parent / "hmo_shape_cases.s4.json"
DEFAULT_OUT = Path("/tmp/lae-i9/s4-results.json")
SH = Namespace("http://www.w3.org/ns/shacl#")
LAE = Namespace("urn:lae:ns:")
RDF_LANG_STRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
GO_P95_MS = 100.0

Triple = dict[str, dict[str, str]]
Signature = tuple[bool, tuple[tuple[str, str, int], ...]]


# ----------------------------------------------------------------------------------------------
# Data graph preparation
# ----------------------------------------------------------------------------------------------


def expand(doc: dict[str, Any]) -> list[Triple]:
    """Expand one record to pyld's RDF dataset (default graph only) with the pinned context."""
    dataset = jsonld.to_rdf(doc, {"documentLoader": document_loader})
    triples: list[Triple] = dataset["@default"]
    return triples


def expand_nquads(doc: dict[str, Any]) -> str:
    """Expand one record to N-Quads text (what Phase 1's ``expand_to_ntriples`` will produce)."""
    text: str = jsonld.to_rdf(
        doc, {"documentLoader": document_loader, "format": "application/n-quads"}
    )
    return text


def _rdflib_term(term: dict[str, str]) -> Node:
    kind = term["type"]
    if kind == "IRI":
        return URIRef(term["value"])
    if kind == "blank node":
        return BNode(term["value"][2:])
    if term.get("language"):
        return Literal(term["value"], lang=term["language"])
    return Literal(term["value"], datatype=URIRef(term["datatype"]))


def to_rdflib(triples: list[Triple]) -> Graph:
    """Build an rdflib Graph directly from pyld triples (no serialization round trip)."""
    graph = Graph()
    add = graph.add
    for t in triples:
        add((_rdflib_term(t["subject"]), _rdflib_term(t["predicate"]), _rdflib_term(t["object"])))
    return graph


def _ox_term(term: dict[str, str]) -> Any:
    kind = term["type"]
    if kind == "IRI":
        return ox.NamedNode(term["value"])
    if kind == "blank node":
        return ox.BlankNode(term["value"][2:])
    if term.get("language"):
        return ox.Literal(term["value"], language=term["language"])
    return ox.Literal(term["value"], datatype=ox.NamedNode(term["datatype"]))


def to_oxigraph(triples: list[Triple]) -> ox.Store:
    """Build an in-memory pyoxigraph Store directly from pyld triples."""
    store = ox.Store()
    store.extend(
        ox.Quad(_ox_term(t["subject"]), _ox_term(t["predicate"]), _ox_term(t["object"]))
        for t in triples
    )
    return store


def parse_rdflib(nquads: str) -> Graph:
    """Parse N-Quads text (default graph only, so N-Triples) into an rdflib Graph."""
    return Graph().parse(data=nquads, format="nt")


def parse_oxigraph(nquads: str) -> ox.Store:
    """Load N-Quads text into an in-memory pyoxigraph Store."""
    store = ox.Store()
    store.load(nquads, format=ox.RdfFormat.N_QUADS)
    return store


# ----------------------------------------------------------------------------------------------
# Validation configurations
# ----------------------------------------------------------------------------------------------

# rdflib logs a traceback for every ill-typed literal (one broken-record case has one).
logging.getLogger("rdflib.term").setLevel(logging.ERROR)
apply_patches()
assign_baked_in()
SHAPES_GRAPH = Graph().parse(SHAPES_PATH, format="turtle")
CACHED_SHAPES = ShapesGraph(SHAPES_GRAPH)
_ = CACHED_SHAPES.shapes  # harvest once, up front
CACHED_OPTIONS: dict[str, Any] = {"advanced": False, "inference": "none", "inplace": True}


class CachedShapesValidator(Validator):
    """A pyshacl Validator that reuses an already-harvested ShapesGraph."""

    def __init__(
        self, data_graph: DataGraph, shapes: ShapesGraph, use_shapes: list[str] | None = None
    ) -> None:
        options = dict(CACHED_OPTIONS)
        if use_shapes:
            options["use_shapes"] = use_shapes
        super().__init__(data_graph, shacl_graph=shapes.graph, options=options)
        self.shacl_graph = shapes


def v_baseline(data: Any) -> tuple[bool, Graph]:
    conforms, report, _ = pyshacl.validate(data, shacl_graph=str(SHAPES_PATH), advanced=True)
    return conforms, report


def v_advanced_off(data: Any) -> tuple[bool, Graph]:
    conforms, report, _ = pyshacl.validate(data, shacl_graph=str(SHAPES_PATH), advanced=False)
    return conforms, report


def v_preloaded(data: Any) -> tuple[bool, Graph]:
    conforms, report, _ = pyshacl.validate(data, shacl_graph=SHAPES_GRAPH, advanced=False)
    return conforms, report


def v_cached_rdflib(data: Graph) -> tuple[bool, Graph]:
    conforms, report, _ = CachedShapesValidator(DataGraph.from_rdflib(data), CACHED_SHAPES).run()
    return conforms, report


def v_cached_oxigraph(data: ox.Store) -> tuple[bool, Graph]:
    validator = CachedShapesValidator(DataGraph.from_oxigraph_store(data), CACHED_SHAPES)
    conforms, report, _ = validator.run()
    return conforms, report


# The first formulation of LAE-SHACL-001/002, in SHACL Core (sh:or over a closed reference-stub
# shape and over qualified value shapes). The shipped file uses SHACL-SPARQL instead; the
# formulation probe below times both.
CORE_OR_SHAPES = """
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix crm:  <http://www.cidoc-crm.org/cidoc-crm/> .
@prefix aat:  <http://vocab.getty.edu/aat/> .
@prefix lae:  <urn:lae:ns:> .
@prefix laes: <urn:lae:shape:core-or:> .

laes:ReferenceStub a sh:NodeShape ; sh:closed true ; sh:ignoredProperties ( rdf:type rdfs:label ) .

laes:HumanMadeObject-name a sh:NodeShape ;
    lae:code "LAE-SHACL-001" ;
    sh:targetClass crm:E22_Human-Made_Object ;
    sh:severity sh:Warning ;
    sh:or (
        laes:ReferenceStub
        [ sh:path crm:P1_is_identified_by ;
          sh:qualifiedValueShape [ sh:class crm:E33_E41_Linguistic_Appellation ] ;
          sh:qualifiedMinCount 1 ]
    ) .

laes:HumanMadeObject-primary-name a sh:NodeShape ;
    lae:code "LAE-SHACL-002" ;
    sh:targetClass crm:E22_Human-Made_Object ;
    sh:severity sh:Warning ;
    sh:or (
        [ sh:path crm:P1_is_identified_by ;
          sh:qualifiedValueShape [ sh:class crm:E33_E41_Linguistic_Appellation ] ;
          sh:qualifiedMaxCount 0 ]
        [ sh:path crm:P1_is_identified_by ;
          sh:qualifiedValueShape [
              sh:class crm:E33_E41_Linguistic_Appellation ;
              sh:property [ sh:path crm:P2_has_type ; sh:hasValue aat:300404670 ] ] ;
          sh:qualifiedMinCount 1 ]
    ) .
"""


@dataclass(frozen=True)
class Config:
    name: str
    shapes: str
    advanced: bool
    store: str
    build: Callable[[list[Triple]], Any]
    validate: Callable[[Any], tuple[bool, Graph]]


CONFIGS: tuple[Config, ...] = (
    Config("baseline", "Turtle file parsed per call", True, "rdflib", to_rdflib, v_baseline),
    Config(
        "advanced_off", "Turtle file parsed per call", False, "rdflib", to_rdflib, v_advanced_off
    ),
    Config("preloaded_shapes", "rdflib Graph parsed once", False, "rdflib", to_rdflib, v_preloaded),
    Config(
        "cached_shapes", "ShapesGraph harvested once", False, "rdflib", to_rdflib, v_cached_rdflib
    ),
    Config("oxigraph", "rdflib Graph parsed once", False, "pyoxigraph", to_oxigraph, v_preloaded),
    Config(
        "oxigraph_cached",
        "ShapesGraph harvested once",
        False,
        "pyoxigraph",
        to_oxigraph,
        v_cached_oxigraph,
    ),
)


def signature(conforms: bool, report: Graph) -> Signature:
    """Conformance plus the multiset of (lae:code, severity) in a validation report."""
    counts: Counter[tuple[str, str]] = Counter()
    for result in report.subjects(RDF.type, SH.ValidationResult):
        source = report.value(result, SH.sourceShape)
        code = SHAPES_GRAPH.value(source, LAE.code)
        severity = report.value(result, SH.resultSeverity)
        counts[(str(code), str(severity).rsplit("#", 1)[-1])] += 1
    return conforms, tuple(sorted((c, s, n) for (c, s), n in counts.items()))


# ----------------------------------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------------------------------


@dataclass
class Record:
    name: str
    subset: str
    doc: dict[str, Any]
    triples: int


@dataclass
class Samples:
    expand: list[float] = field(default_factory=list)
    build: list[float] = field(default_factory=list)
    validate: list[float] = field(default_factory=list)

    @property
    def total(self) -> list[float]:
        return [a + b + c for a, b, c in zip(self.expand, self.build, self.validate, strict=True)]


def ms(start: int, end: int) -> float:
    return (end - start) / 1e6


def run_config(
    config: Config, records: Sequence[Record], iterations: int, warmup: int
) -> tuple[dict[str, Samples], dict[str, Signature]]:
    """Time one configuration over every record; return samples and result signatures."""
    samples: dict[str, Samples] = {}
    signatures: dict[str, Signature] = {}
    clock = time.perf_counter_ns
    for record in records:
        for _ in range(warmup):
            config.validate(config.build(expand(record.doc)))
        s = Samples()
        for _ in range(iterations):
            t0 = clock()
            triples = expand(record.doc)
            t1 = clock()
            data = config.build(triples)
            t2 = clock()
            conforms, report = config.validate(data)
            t3 = clock()
            s.expand.append(ms(t0, t1))
            s.build.append(ms(t1, t2))
            s.validate.append(ms(t2, t3))
        signatures[record.name] = signature(conforms, report)
        samples[record.name] = s
    return samples, signatures


def time_prep(records: Sequence[Record], iterations: int) -> dict[str, list[float]]:
    """Time each data-graph preparation route on its own, pooled over every record."""
    stages: dict[str, Callable[[dict[str, Any]], Any]] = {
        "pyld to_rdf (dataset)": expand,
        "pyld to_rdf (N-Quads text)": expand_nquads,
    }
    pooled: dict[str, list[float]] = {}
    clock = time.perf_counter_ns
    for record in records:
        triples = expand(record.doc)
        nquads = expand_nquads(record.doc)
        routes: dict[str, Callable[[], Any]] = {
            **{label: (lambda f=f, d=record.doc: f(d)) for label, f in stages.items()},
            "dataset -> rdflib Graph": lambda t=triples: to_rdflib(t),
            "N-Quads -> rdflib Graph (parse)": lambda n=nquads: parse_rdflib(n),
            "dataset -> pyoxigraph Store": lambda t=triples: to_oxigraph(t),
            "N-Quads -> pyoxigraph Store (parse)": lambda n=nquads: parse_oxigraph(n),
        }
        for label, fn in routes.items():
            for _ in range(3):
                fn()
            bucket = pooled.setdefault(label, [])
            for _ in range(iterations):
                t0 = clock()
                fn()
                bucket.append(ms(t0, clock()))
    return pooled


def apply_case(base: dict[str, Any], operations: list[list[Any]]) -> dict[str, Any]:
    """Apply a case's JSON Pointer 'set' operations to a copy of base (null removes the key)."""
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


def case_check(configs: Sequence[Config]) -> dict[str, list[str]]:
    """Run the broken-record cases through every configuration; list cases that disagree."""
    spec = json.loads(CASES_PATH.read_text())
    failures: dict[str, list[str]] = {}
    for config in configs:
        for case in spec["cases"]:
            doc = apply_case(spec["base"], case["set"])
            conforms, report = config.validate(config.build(expand(doc)))
            expected = Counter((code, severity) for code, severity in case["expect"])
            found = Counter({(c, s): n for c, s, n in signature(conforms, report)[1]})
            if found != expected:
                failures.setdefault(config.name, []).append(case["name"])
    return failures


def inflate(doc: dict[str, Any], factor: int) -> dict[str, Any]:
    """Repeat every embedded (id-less) array item of the record and its Production factor times."""
    out = copy.deepcopy(doc)
    for holder in (out, out.get("produced_by", {})):
        for key, value in holder.items():
            if isinstance(value, list):
                embedded = [item for item in value if isinstance(item, dict) and "id" not in item]
                holder[key] = value + [
                    copy.deepcopy(i) for i in embedded for _ in range(factor - 1)
                ]
    return out


def scale_probe(
    record: Record, configs: Sequence[Config], factors: Sequence[int], iterations: int
) -> list[dict[str, Any]]:
    """Time the largest record inflated by each factor; median and p95 end-to-end per config."""
    rows = []
    for factor in factors:
        doc = inflate(record.doc, factor)
        row: dict[str, Any] = {"factor": factor, "triples": len(expand(doc))}
        for config in configs:
            single = Record(record.name, record.subset, doc, row["triples"])
            samples, _ = run_config(config, [single], iterations, warmup=3)
            s = samples[record.name]
            row[config.name] = {
                "p50": pct(s.total, 50),
                "p95": pct(s.total, 95),
                "expand_p50": pct(s.expand, 50),
                "validate_p50": pct(s.validate, 50),
            }
        rows.append(row)
    return rows


STORES: dict[str, tuple[Callable[[list[Triple]], Any], Callable[[Any], DataGraph]]] = {
    "rdflib": (to_rdflib, DataGraph.from_rdflib),
    "pyoxigraph": (to_oxigraph, DataGraph.from_oxigraph_store),
}


def shape_costs(
    shapes: ShapesGraph, triples: list[Triple], store: str, iterations: int
) -> dict[str, float]:
    """Median ms to validate one data graph against each targeted shape on its own."""
    build, wrap = STORES[store]
    targeted = {
        shape
        for target in (SH.targetClass, SH.targetObjectsOf, SH.targetSubjectsOf, SH.targetNode)
        for shape in shapes.graph.subjects(target, None)
    }
    costs: dict[str, float] = {}
    clock = time.perf_counter_ns
    for shape in targeted:
        code = str(shapes.graph.value(shape, LAE.code))
        times = []
        for i in range(iterations + 3):
            data = wrap(build(triples))
            t0 = clock()
            CachedShapesValidator(data, shapes, use_shapes=[str(shape)]).run()
            if i >= 3:
                times.append(ms(t0, clock()))
        costs[code] = statistics.median(times)
    return costs


def formulation_probe(
    record: Record, factors: Sequence[int], iterations: int
) -> dict[str, dict[str, dict[str, float]]]:
    """Per-shape cost of the shipped shapes and of the Core sh:or formulation of 001/002."""
    core_or = ShapesGraph(Graph().parse(data=CORE_OR_SHAPES, format="turtle"))
    _ = core_or.shapes
    out: dict[str, dict[str, dict[str, float]]] = {}
    for factor in factors:
        triples = expand(inflate(record.doc, factor))
        for store in STORES:
            key = f"{len(triples)} triples, {store}"
            out[key] = {
                "shipped": shape_costs(CACHED_SHAPES, triples, store, iterations),
                "core_or": shape_costs(core_or, triples, store, iterations),
            }
    return out


def thread_check(
    records: Sequence[Record], expected: dict[str, Signature], workers: int, config: Config
) -> tuple[int, float, float]:
    """Validate every record 5x on a thread pool sharing one ShapesGraph.

    Returns the number of results that disagree with the sequential run, and the wall-clock
    seconds of the same work sequentially and on the pool (the GIL bounds any speed-up).
    """

    def one(record: Record) -> bool:
        conforms, report = config.validate(config.build(expand(record.doc)))
        return signature(conforms, report) == expected[record.name]

    work = [r for r in records for _ in range(5)]
    started = time.perf_counter()
    for record in work:
        one(record)
    sequential = time.perf_counter() - started
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(one, work))
    threaded = time.perf_counter() - started
    return outcomes.count(False), sequential, threaded


# ----------------------------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------------------------


def pct(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(values: Sequence[float]) -> dict[str, float]:
    return {
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "p99": pct(values, 99),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def fmt(x: float) -> str:
    return f"{x:.2f}"


def environment() -> dict[str, str]:
    env = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "loadavg_at_start": " ".join(f"{x:.2f}" for x in os.getloadavg()),
    }
    for pkg in ("pyshacl", "rdflib", "pyld", "pyoxigraph"):
        env[pkg] = version(pkg)
    if sys.platform == "darwin":
        for key, name in (
            ("cpu", "machdep.cpu.brand_string"),
            ("cores", "hw.ncpu"),
            ("memory_bytes", "hw.memsize"),
        ):
            out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
            env[key] = out.stdout.strip()
    return env


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--config", action="append", choices=[c.name for c in CONFIGS])
    parser.add_argument("--threads", type=int, default=8, help="workers for the thread check")
    parser.add_argument("--scale", default="1,2,4,8,16", help="inflation factors for the probe")
    parser.add_argument("--scale-iterations", type=int, default=20)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    records = [
        Record(path.name, path.parent.name, doc, len(expand(doc)))
        for path, doc in iter_corpus()
        if doc.get("type") == "HumanMadeObject"
    ]
    by_subset = Counter(r.subset for r in records)
    configs = [c for c in CONFIGS if not args.config or c.name in args.config]
    env = environment()
    print(f"# Spike S4 run: {len(records)} HumanMadeObject records {dict(by_subset)}")
    print(f"# corpus: {CORPUS_DIR}; shapes: {SHAPES_PATH}")
    print(f"# {json.dumps(env)}")
    print(f"# iterations={args.iterations} warmup={args.warmup}\n")

    # Global warm-up: import-time and first-call costs of every code path.
    for config in configs:
        for record in records:
            config.validate(config.build(expand(record.doc)))

    # Configurations are interleaved per record, so background load (other processes on the
    # machine) falls on all of them alike.
    results: dict[str, dict[str, Samples]] = {c.name: {} for c in configs}
    signatures: dict[str, dict[str, Signature]] = {c.name: {} for c in configs}
    wall: dict[str, float] = dict.fromkeys(results, 0.0)
    for n, record in enumerate(records, 1):
        for config in configs:
            gc.collect()
            started = time.perf_counter()
            samples, sigs = run_config(config, [record], args.iterations, args.warmup)
            wall[config.name] += time.perf_counter() - started
            results[config.name].update(samples)
            signatures[config.name].update(sigs)
        print(f"# {n}/{len(records)} {record.name}", file=sys.stderr)

    reference = signatures[configs[0].name]
    mismatches = {
        c.name: sorted(r for r in reference if signatures[c.name][r] != reference[r])
        for c in configs
    }

    # Table 1: per configuration, pooled samples (records x iterations).
    print("## Validation latency per configuration (ms, all samples pooled)\n")
    print(
        "| Configuration | Shapes | advanced | Data store | validate p50 | validate p95 | "
        "validate max | end-to-end p50 | end-to-end p95 | end-to-end max | "
        "per-record median p95 | results agree |"
    )
    print("| :-- | :-- | :-- | :-- | --: | --: | --: | --: | --: | --: | --: | :-- |")
    summary: dict[str, Any] = {}
    for config in configs:
        samples = results[config.name]
        validate = [x for s in samples.values() for x in s.validate]
        total = [x for s in samples.values() for x in s.total]
        medians = [statistics.median(s.total) for s in samples.values()]
        v, t, m = summarize(validate), summarize(total), summarize(medians)
        summary[config.name] = {"validate": v, "total": t, "per_record_median_total": m}
        agree = "yes" if not mismatches[config.name] else f"NO ({len(mismatches[config.name])})"
        print(
            f"| `{config.name}` | {config.shapes} | {config.advanced} | {config.store} | "
            f"{fmt(v['p50'])} | {fmt(v['p95'])} | {fmt(v['max'])} | {fmt(t['p50'])} | "
            f"{fmt(t['p95'])} | {fmt(t['max'])} | {fmt(m['p95'])} | {agree} |"
        )

    # Table 2: stage breakdown per configuration.
    print("\n## Stage breakdown (ms, p50 / p95)\n")
    print("| Configuration | expand (pyld) | build data graph | validate (pyshacl) |")
    print("| :-- | --: | --: | --: |")
    for config in configs:
        samples = results[config.name]
        cols = []
        for stage in ("expand", "build", "validate"):
            pooled = [x for s in samples.values() for x in getattr(s, stage)]
            cols.append(f"{fmt(pct(pooled, 50))} / {fmt(pct(pooled, 95))}")
        print(f"| `{config.name}` | " + " | ".join(cols) + " |")

    # Table 3: slowest records under the fastest configuration and the baseline.
    best = min(configs, key=lambda c: summary[c.name]["total"]["p95"])
    print(f"\n## Slowest records (median of {args.iterations} runs, ms)\n")
    print(f"Fastest configuration by end-to-end p95: `{best.name}`.\n")
    print(
        f"| Record | Subset | Triples | `{best.name}` end-to-end | `{best.name}` end-to-end p95 "
        f"| `{best.name}` validate | `{configs[0].name}` end-to-end |"
    )
    print("| :-- | :-- | --: | --: | --: | --: | --: |")
    ranked = sorted(
        records,
        key=lambda r: statistics.median(results[best.name][r.name].total),
        reverse=True,
    )
    for record in ranked[:8]:
        b = results[best.name][record.name]
        base = results[configs[0].name][record.name]
        print(
            f"| `{record.name}` | {record.subset} | {record.triples} | "
            f"{fmt(statistics.median(b.total))} | {fmt(pct(b.total, 95))} | "
            f"{fmt(statistics.median(b.validate))} | "
            f"{fmt(statistics.median(base.total))} |"
        )

    # Table 4: data graph preparation routes.
    prep = time_prep(records, args.iterations)
    print("\n## Data graph preparation routes (ms, pooled)\n")
    print("| Route | p50 | p95 | max |")
    print("| :-- | --: | --: | --: |")
    for label, values in prep.items():
        s = summarize(values)
        print(f"| {label} | {fmt(s['p50'])} | {fmt(s['p95'])} | {fmt(s['max'])} |")

    # Corpus diagnostics and the thread check.
    diagnostics: Counter[tuple[str, str]] = Counter()
    nonconforming = 0
    for sig in reference.values():
        nonconforming += not sig[0]
        for code, severity, n in sig[1]:
            diagnostics[(code, severity)] += n
    print(f"\n## Corpus diagnostics ({len(records)} records, {nonconforming} non-conforming)\n")
    print("| Code | Severity | Results |")
    print("| :-- | :-- | --: |")
    for (code, severity), n in sorted(diagnostics.items()):
        print(f"| `{code}` | {severity} | {n} |")

    case_failures = case_check(configs)
    print(
        f"\nBroken-record cases ({CASES_PATH.name}): "
        + (f"disagreements {case_failures}" if case_failures else "every configuration agrees")
    )

    thread_config = next((c for c in configs if c.name == "oxigraph_cached"), configs[-1])
    thread_mismatches, sequential_s, threaded_s = thread_check(
        records, signatures[configs[0].name], args.threads, thread_config
    )
    print(
        f"\nThread check (`{thread_config.name}`): {len(records) * 5} validations on "
        f"{args.threads} threads sharing one ShapesGraph, {thread_mismatches} disagreed with the "
        f"sequential results; wall clock {sequential_s:.2f} s sequential, {threaded_s:.2f} s "
        "threaded."
    )
    if any(mismatches.values()):
        print(f"\nResult mismatches between configurations: {mismatches}")

    # Scale probe: the largest record, inflated.
    largest = max(records, key=lambda r: r.triples)
    probe_configs = [c for c in configs if c.name in ("baseline", "cached_shapes", best.name)]
    factors = [int(f) for f in args.scale.split(",") if f]
    probe = scale_probe(largest, probe_configs, factors, args.scale_iterations)
    print(
        f"\n## Scale probe: `{largest.name}` with embedded array items repeated "
        f"(ms over {args.scale_iterations} runs)\n"
    )
    print(
        "| Factor | Triples | expand p50 | "
        + " | ".join(
            f"`{c.name}` validate p50 | `{c.name}` end-to-end p50 / p95" for c in probe_configs
        )
        + " |"
    )
    print("| --: | --: | --: | " + " | ".join("--: | --:" for _ in probe_configs) + " |")
    for row in probe:
        cells = [
            f"{fmt(row[c.name]['validate_p50'])} | "
            f"{fmt(row[c.name]['p50'])} / {fmt(row[c.name]['p95'])}"
            for c in probe_configs
        ]
        expand_p50 = statistics.median(row[c.name]["expand_p50"] for c in probe_configs)
        print(
            f"| {row['factor']} | {row['triples']} | {fmt(expand_p50)} | "
            + " | ".join(cells)
            + " |"
        )

    # Formulation probe: what each shape costs on its own.
    shape_probe = formulation_probe(largest, [1, 8], args.scale_iterations)
    codes = sorted(next(iter(shape_probe.values()))["shipped"])
    print(
        f"\n## Cost per shape on its own (`cached_shapes` validator, `{largest.name}`, "
        f"median ms of {args.scale_iterations} runs)\n"
    )
    print("| Data graph | " + " | ".join(c.removeprefix("LAE-SHACL-") for c in codes) + " |")
    print("| :-- | " + " | ".join("--:" for _ in codes) + " |")
    for key, row in shape_probe.items():
        print(f"| {key} | " + " | ".join(fmt(row["shipped"][c]) for c in codes) + " |")
    print(
        "\nSame, with LAE-SHACL-001/002 written as SHACL Core `sh:or` (closed stub / qualified):\n"
    )
    print("| Data graph | 001 SPARQL | 001 Core `sh:or` | 002 SPARQL | 002 Core `sh:or` |")
    print("| :-- | --: | --: | --: | --: |")
    for key, row in shape_probe.items():
        cells = [
            fmt(row[variant][f"LAE-SHACL-00{n}"])
            for n in (1, 2)
            for variant in ("shipped", "core_or")
        ]
        print(f"| {key} | " + " | ".join(cells) + " |")

    recommended = summary[best.name]["total"]["p95"]
    verdict = "GO" if recommended < GO_P95_MS else "NO-GO"
    load = " ".join(f"{x:.2f}" for x in os.getloadavg())
    print(f"\nLoad average at the end of the run: {load}")
    print(f"\nVerdict: {verdict} (`{best.name}` end-to-end p95 = {fmt(recommended)} ms)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "environment": env,
                "iterations": args.iterations,
                "warmup": args.warmup,
                "records": [
                    {"name": r.name, "subset": r.subset, "triples": r.triples} for r in records
                ],
                "wall_seconds": wall,
                "summary": summary,
                "mismatches": mismatches,
                "thread_mismatches": thread_mismatches,
                "thread_wall_seconds": {"sequential": sequential_s, "threaded": threaded_s},
                "case_failures": case_failures,
                "scale_probe": probe,
                "shape_probe": shape_probe,
                "diagnostics": {f"{c} {s}": n for (c, s), n in diagnostics.items()},
                "prep": {label: summarize(v) for label, v in prep.items()},
                "samples": {
                    name: {
                        rec: {"expand": s.expand, "build": s.build, "validate": s.validate}
                        for rec, s in per.items()
                    }
                    for name, per in results.items()
                },
            }
        )
    )
    print(f"\n# raw samples written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
