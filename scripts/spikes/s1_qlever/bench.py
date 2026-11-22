"""Spike S1: measure SPARQL Update, text freshness, persistence, SERVICE pushdown, and query
latency during a rebuild against a running QLever server (Phase 0 Step 0.6).

Every sub-command prints a JSON document with its measurements and, with ``--out``, writes it to a
file. ``run_s1.sh`` runs them in order; see ``docs/spikes/S1-qlever.md`` for the results.

    uv run --project backend python scripts/spikes/s1_qlever/bench.py --port 7006 updates
    uv run --project backend python scripts/spikes/s1_qlever/bench.py --port 7006 text
    uv run --project backend python scripts/spikes/s1_qlever/bench.py --port 7006 persist
    uv run --project backend python scripts/spikes/s1_qlever/bench.py --port 7006 service
    uv run --project backend python scripts/spikes/s1_qlever/bench.py --port 7006 probe --until F

The access token comes from ``LAE_QLEVER_ACCESS_TOKEN`` (as for ``deploy/Qleverfile``).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

BASE = "http://localhost:8000/id/"
S1 = BASE + "s1/"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"
EX = "https://example.org/s1/"
XSD = "http://www.w3.org/2001/XMLSchema#"
PREFIXES = (
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
    "PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/>\n"
    f"PREFIX crm: <{CRM}>\nPREFIX ex: <{EX}>\n"
)
QLEVER_JSON = "application/qlever-results+json"
SPARQL_JSON = "application/sparql-results+json"
INSERTED_LITERAL = "zebra-crossing-xyz"
CONTROL_LITERAL = "zebra-crossing-control"
TEXT_SUBJECT = S1 + "text-fresh"  # keeps the inserted literal for the persistence check


class Qlever:
    """A minimal QLever HTTP client: queries, updates, metrics, runtime parameters."""

    def __init__(self, port: int, token: str | None, timeout: float = 600.0) -> None:
        self.url = f"http://localhost:{port}"
        self.token = token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.Client(timeout=timeout, headers=headers)

    def query(self, sparql: str, accept: str = QLEVER_JSON) -> tuple[dict[str, Any], float]:
        """Run a query; return the parsed JSON result and the client-side seconds."""
        started = time.perf_counter()
        response = self.http.post(
            self.url,
            content=sparql.encode(),
            headers={"Content-Type": "application/sparql-query", "Accept": accept},
        )
        elapsed = time.perf_counter() - started
        if response.status_code != 200:
            raise RuntimeError(f"query failed ({response.status_code}): {response.text[:500]}")
        return response.json(), elapsed

    def rows(self, sparql: str) -> list[list[str]]:
        """Run a query and return its result rows (QLever JSON)."""
        result, _ = self.query(sparql)
        return [list(row) for row in result["res"]]

    def count(self, where: str) -> int:
        """``SELECT (COUNT(*) AS ?n) WHERE { where }``."""
        rows = self.rows(f"{PREFIXES}SELECT (COUNT(*) AS ?n) WHERE {{ {where} }}")
        return int(rows[0][0].split('"')[1])

    def update(self, sparql: str) -> tuple[dict[str, Any], float]:
        """Run a SPARQL Update; return the parsed JSON response and the client-side seconds."""
        started = time.perf_counter()
        response = self.http.post(
            self.url,
            content=sparql.encode(),
            headers={"Content-Type": "application/sparql-update", "Accept": "application/json"},
        )
        elapsed = time.perf_counter() - started
        if response.status_code != 200:
            raise RuntimeError(f"update failed ({response.status_code}): {response.text[:500]}")
        try:
            body = trim_update_response(response.json())
        except ValueError:
            body = {"raw": response.text[:500]}
        return body, elapsed

    def metrics(self) -> dict[str, str]:
        """Prometheus lines from ``/metrics`` whose name mentions delta triples or the index."""
        response = self.http.get(self.url + "/metrics")
        if response.status_code != 200:
            return {"error": f"{response.status_code}: {response.text[:200]}"}
        found = {}
        for line in response.text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            if "delta" in line or "num_triples" in line or "rebuild" in line:
                name, _, value = line.rpartition(" ")
                found[name] = value
        return found

    def stats(self) -> dict[str, Any]:
        """``cmd=stats`` (index and delta-triple counts)."""
        response = self.http.get(self.url, params={"cmd": "stats"})
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def set_runtime_parameter(self, name: str, value: str) -> dict[str, Any]:
        """Set a runtime parameter (needs the access token)."""
        response = self.http.get(self.url, params={name: value})
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


def trim_update_response(body: Any) -> dict[str, Any]:
    """Keep the delta-triple counts, status, warnings, and total time of each operation."""
    operations = body.get("operations", []) if isinstance(body, dict) else []
    kept = []
    for op in operations:
        time_info = op.get("time", {})
        kept.append(
            {
                "status": op.get("status"),
                "delta-triples": op.get("delta-triples"),
                "time_total_ms": time_info.get("total"),
                "warnings": op.get("warnings"),
            }
        )
    return {"operations": kept} if operations else {"raw": body}


def iri(value: str) -> str:
    """An IRI in SPARQL syntax."""
    return f"<{value}>"


def lit(value: str, datatype: str | None = None) -> str:
    """A literal in SPARQL syntax (the benchmark's values never need escaping)."""
    return f'"{value}"' + (f"^^<{XSD}{datatype}>" if datatype else "")


def record_triples(subject: str, label: str, n: int = 100) -> list[str]:
    """``n`` triples about ``subject``, one of them ``rdfs:label label``."""
    s = iri(subject)
    triples = [
        f"{s} {iri(RDF_TYPE)} {iri(CRM + 'E22_Human-Made_Object')} .",
        f"{s} {iri(RDFS_LABEL)} {lit(label)} .",
    ]
    k = 0
    while len(triples) < n:
        kind = k % 3
        if kind == 0:
            triples.append(f"{s} {iri(CRM + 'P3_has_note')} {lit(f'inserted note {k} plain')} .")
        elif kind == 1:
            triples.append(f"{s} {iri(CRM + 'P67_refers_to')} {iri(S1 + str(k))} .")
        else:
            triples.append(f"{s} {iri(EX + f'dim{k}')} {lit(f'{k}.5', 'decimal')} .")
        k += 1
    return triples


def percentile(values: list[float], q: float) -> float:
    """The ``q``-quantile (0..1) by nearest rank."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * len(ordered)) - 1))]


def summary_ms(values: list[float]) -> dict[str, float]:
    """p50 / p95 / p99 / max / mean of a list of seconds, in milliseconds."""
    ms = [v * 1000 for v in values]
    return {
        "n": len(ms),
        "p50_ms": round(percentile(ms, 0.50), 2),
        "p95_ms": round(percentile(ms, 0.95), 2),
        "p99_ms": round(percentile(ms, 0.99), 2),
        "max_ms": round(max(ms), 2),
        "mean_ms": round(statistics.fmean(ms), 2),
    }


def delta(q: Qlever) -> dict[str, Any]:
    """The delta-triple counts from ``/metrics`` and ``cmd=stats``."""
    stats = q.stats()
    keys = [k for k in stats if "delta" in k or k.startswith("num-triples")]
    return {"metrics": q.metrics(), "stats": {k: stats[k] for k in keys}}


# -- sub-commands -------------------------------------------------------------------------------


def cmd_updates(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """Step 3: INSERT DATA / DELETE DATA (100 triples), DELETE WHERE, 1,000 small updates."""
    out: dict[str, Any] = {"delta_before": delta(q)}
    # Each single operation is repeated on --repeat different subjects; the first response is
    # kept, and every repetition is checked (100 triples after INSERT, 0 after the deletes).
    timings: dict[str, list[float]] = {"insert": [], "delete": [], "delete_where": []}
    checks: dict[str, list[int]] = {"insert": [], "delete": [], "where_before": [], "where": []}
    first: dict[str, Any] = {}
    for r in range(args.repeat):
        subject = S1 + f"insert-100-{r}"
        body = "\n".join(record_triples(subject, INSERTED_LITERAL))
        response, seconds = q.update(f"INSERT DATA {{\n{body}\n}}")
        timings["insert"].append(seconds)
        checks["insert"].append(q.count(f"<{subject}> ?p ?o"))
        first.setdefault("insert", response)
        response, seconds = q.update(f"DELETE DATA {{\n{body}\n}}")
        timings["delete"].append(seconds)
        checks["delete"].append(q.count(f"<{subject}> ?p ?o"))
        first.setdefault("delete", response)
        victim = S1 + str(12_345 + r)
        checks["where_before"].append(q.count(f"<{victim}> ?p ?o"))
        response, seconds = q.update(f"DELETE WHERE {{ <{victim}> ?p ?o }}")
        timings["delete_where"].append(seconds)
        checks["where"].append(q.count(f"<{victim}> ?p ?o"))
        first.setdefault("delete_where", response)
    out["insert_data_100"] = {
        "latency": summary_ms(timings["insert"]),
        "triples_after_each": sorted(set(checks["insert"])),
        "first_response": first["insert"],
    }
    out["delete_data_100"] = {
        "latency": summary_ms(timings["delete"]),
        "triples_after_each": sorted(set(checks["delete"])),
        "first_response": first["delete"],
    }
    out["delete_where_subject"] = {
        "subjects": f"{S1}12345 .. {S1}{12_345 + args.repeat - 1}",
        "latency": summary_ms(timings["delete_where"]),
        "triples_before_each": sorted(set(checks["where_before"])),
        "triples_after_each": sorted(set(checks["where"])),
        "first_response": first["delete_where"],
    }
    out["delta_after_single_ops"] = delta(q)

    # 1,000 sequential small updates, each one request with DELETE DATA + INSERT DATA of five
    # triples on a different base subject: the shape of one record edit applied by the sync
    # worker (old triples known from Postgres). The old triples are read before timing starts.
    n = args.n
    subjects = [S1 + str(20_000 + i) for i in range(n)]
    old: dict[str, list[str]] = {}
    values = " ".join(iri(s) for s in subjects)
    rows = q.rows(
        f"{PREFIXES}SELECT ?s ?l ?d0 ?d1 ?d2 ?d3 WHERE {{ VALUES ?s {{ {values} }} "
        "?s rdfs:label ?l ; ex:dim0 ?d0 ; ex:dim1 ?d1 ; ex:dim2 ?d2 ; ex:dim3 ?d3 }"
    )
    for row in rows:
        s, label, *dims = row
        old[s[1:-1]] = [f"{s} <{RDFS_LABEL}> {label} ."] + [
            f"{s} <{EX}dim{k}> {d} ." for k, d in enumerate(dims)
        ]
    latencies: list[float] = []
    server_ms: list[float] = []
    for i, s in enumerate(subjects):
        new = [f"<{s}> <{RDFS_LABEL}> {lit(f'edited label {i}')} ."] + [
            f"<{s}> <{EX}dim{k}> {lit(f'{i}.{k}', 'decimal')} ." for k in range(4)
        ]
        sparql = (
            "DELETE DATA {\n"
            + "\n".join(old[s])
            + "\n} ;\nINSERT DATA {\n"
            + "\n".join(new)
            + "\n}"
        )
        response, seconds = q.update(sparql)
        latencies.append(seconds)
        total = _server_total_ms(response)
        if total is not None:
            server_ms.append(total / 1000)
    out["small_updates"] = {
        "description": "DELETE DATA (5 triples) ; INSERT DATA (5 triples) per request",
        "client": summary_ms(latencies),
        "server_reported": summary_ms(server_ms) if server_ms else None,
        "last_response": response,
        "check_edited_labels": q.count('?s rdfs:label ?l FILTER(STRSTARTS(?l, "edited label"))'),
    }
    out["delta_after_small_updates"] = delta(q)
    return out


def _server_total_ms(response: dict[str, Any]) -> float | None:
    """The server-side total time (ms, summed over the operations) of a trimmed update response."""
    values = [op.get("time_total_ms") for op in response.get("operations", [])]
    numbers = [float(v) for v in values if isinstance(v, int | float)]
    return sum(numbers) if numbers else None


# With `TEXT_INDEX = from_literals` every literal is a text record that "contains" itself as an
# entity, so a literal is found with `?text ql:contains-entity ?literal ; ql:contains-word "w"`.
CONTAINS = "?s rdfs:label ?t . ?text ql:contains-entity ?t . ?text ql:contains-word"
TEXT_QUERIES = {
    "contains_word_prefix_zebra*": f"{CONTAINS} 'zebra*'",
    "contains_word_prefix_zebra-cross*": f"{CONTAINS} 'zebra cross*'",
    "contains_word_xyz": f"{CONTAINS} 'xyz'",
    "contains_word_control": f"{CONTAINS} 'control'",
    "has_word_zebra-crossing-xyz": "?s rdfs:label ?t . ?t ql:has-word 'zebra-crossing-xyz'",
    "has_word_zebra": "?s rdfs:label ?t . ?t ql:has-word 'zebra'",
    "has_word_xyz": "?s rdfs:label ?t . ?t ql:has-word 'xyz'",
    "has_word_control": "?s rdfs:label ?t . ?t ql:has-word 'control'",
    "has_word_prefix_zebra*": "?s rdfs:label ?t . ?t ql:has-word 'zebra*'",
    # Prefix over the words of the ql:has-word triples (sorted by word in the POS permutation).
    "has_word_var_strstarts_zebra": (
        "?s rdfs:label ?t . ?t ql:has-word ?w FILTER(STRSTARTS(STR(?w), 'zebra'))"
    ),
    "filter_strstarts_zebra": "?s rdfs:label ?t FILTER(STRSTARTS(LCASE(STR(?t)), 'zebra'))",
    "filter_regex_word_prefix": "?s rdfs:label ?t FILTER(REGEX(?t, '(^|[^a-z])zebra', 'i'))",
}


def run_text_queries(q: Qlever) -> dict[str, Any]:
    """Every text query: which subjects it finds (control, inserted) and its latency."""
    results: dict[str, Any] = {}
    for name, where in TEXT_QUERIES.items():
        sparql = f"{PREFIXES}SELECT DISTINCT ?s ?t WHERE {{ {where} }} LIMIT 100"
        try:
            result, seconds = q.query(sparql)
        except RuntimeError as error:
            results[name] = {"error": str(error)[:300]}
            continue
        found = [row[0] for row in result["res"]]
        results[name] = {
            "rows": len(found),
            "finds_control": iri(S1 + "0") in found,
            "finds_inserted": iri(TEXT_SUBJECT) in found,
            "ms": round(seconds * 1000, 2),
        }
    return results


def cmd_text(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """Step 4: does text search find a literal inserted by SPARQL Update (without a rebuild)?"""
    out: dict[str, Any] = {"before_insert": run_text_queries(q)}
    triples = record_triples(TEXT_SUBJECT, INSERTED_LITERAL, n=2)
    response, seconds = q.update("INSERT DATA {\n" + "\n".join(triples) + "\n}")
    out["insert"] = {"triples": triples, "ms": round(seconds * 1000, 2), "response": response}
    out["after_insert"] = run_text_queries(q)
    return out


def cmd_persist(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """Step 5: are the updates still there (run after a server restart)?"""
    return {
        "text_subject_triples": q.count(f"<{TEXT_SUBJECT}> ?p ?o"),
        "text_subject_label": q.rows(f"SELECT ?l WHERE {{ <{TEXT_SUBJECT}> <{RDFS_LABEL}> ?l }}"),
        "deleted_subject_12345_triples": q.count(f"<{S1}12345> ?p ?o"),
        "edited_labels": q.count('?s rdfs:label ?l FILTER(STRSTARTS(?l, "edited label"))'),
        "insert_100_subject_triples": q.count(f"<{S1}insert-100> ?p ?o"),
        "total_triples": q.count("?s ?p ?o"),
        "delta": delta(q),
        "text_queries": run_text_queries(q),
    }


SERVICE_IRI_DEFAULT = "http://localhost:9806/sparql"


def service_query(endpoint: str, n_values: int, sibling: str = "values") -> str:
    """The guide's SERVICE query with a sibling of ``n_values`` IRIs (VALUES or a graph pattern)."""
    if sibling == "values":
        values = " ".join(iri(S1 + str(i)) for i in range(n_values))
        constraint = f"VALUES ?x {{ {values} }}"
    else:  # a sibling computed from the index: subjects with ex:dim0 below a threshold
        constraint = f"?x ex:dim0 ?d . FILTER(?d < {n_values})"
    return (
        f"{PREFIXES}SELECT ?x ?score WHERE {{\n  {constraint}\n"
        f"  SERVICE <{endpoint}> {{ ?x <urn:vec:score> ?score }}\n}}"
    )


def find_details(node: dict[str, Any], key: str) -> list[Any]:
    """All values of ``details[key]`` in a QLever runtime-information tree."""
    found = []
    details = node.get("details") or {}
    if key in details:
        found.append(details[key])
    for child in node.get("children", []):
        found.extend(find_details(child, key))
    return found


def cmd_service(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """Step 6: SERVICE with a VALUES sibling; was it pushed down, and when not?"""
    stub = httpx.Client(timeout=30)
    stub_url = args.stub.rsplit("/", 1)[0]
    out: dict[str, Any] = {"service_iri": args.stub}
    cases = [
        ("values_5000_default_limit", 5000, "values", None),
        ("values_5000_limit_4999", 5000, "values", "4999"),
        ("values_20000_default_limit", 20000, "values", None),
        ("values_20000_limit_100000", 20000, "values", "100000"),
        ("graph_sibling_dim0_lt_250_default_limit", 250, "graph", None),
    ]
    for name, n, sibling, limit in cases:
        if limit is not None:
            q.set_runtime_parameter("service-max-value-rows", limit)
        stub.post(stub_url + "/reset")
        sparql = service_query(args.stub, n, sibling)
        try:
            result, seconds = q.query(sparql)
            error = None
        except RuntimeError as exc:
            result, seconds, error = {"res": []}, 0.0, str(exc)[:300]
        finally:
            if limit is not None:
                q.set_runtime_parameter("service-max-value-rows", "10000")
        log = stub.get(stub_url + "/log").json()
        tree = result.get("runtimeInformation", {}).get("query_execution_tree", {})
        out[name] = {
            "query_bytes": len(sparql.encode()),
            "service_max_value_rows": limit or "10000 (default)",
            "result_rows": len(result["res"]),
            "ms": round(seconds * 1000, 2),
            "error": error,
            "warnings": result.get("warnings"),
            "server_time": result.get("time"),
            "optimized_with_sibling_result": find_details(tree, "optimized-with-sibling-result"),
            "stub_requests": log,
        }
    return out


PROBE_QUERIES = [
    f"{PREFIXES}SELECT (COUNT(*) AS ?n) WHERE {{ ?s rdfs:label ?l . ?s crm:P2_has_type ?t }}",
    f"{PREFIXES}SELECT ?s ?t WHERE {{ ?s rdfs:label ?t . ?text ql:contains-entity ?t . "
    "?text ql:contains-word 'blai*' } LIMIT 20",
    f"{PREFIXES}SELECT ?o WHERE {{ <{S1}4242> ?p ?o }}",
]


def latency_probe(
    q: Qlever, seconds: float, interval: float, until: Path | None = None
) -> dict[str, Any]:
    """Run ``PROBE_QUERIES`` round-robin (query cache off) and summarise the latencies."""
    q.set_runtime_parameter("cache-max-num-entries", "0")  # measure evaluation, not the cache
    latencies: list[float] = []
    errors = 0
    started = time.monotonic()
    i = 0
    try:
        while True:
            if until is not None and until.exists():
                break
            if until is None and time.monotonic() - started > seconds:
                break
            try:
                _, elapsed = q.query(PROBE_QUERIES[i % len(PROBE_QUERIES)])
                latencies.append(elapsed)
            except (RuntimeError, httpx.HTTPError):
                errors += 1
            i += 1
            time.sleep(interval)
    finally:
        q.set_runtime_parameter("cache-max-num-entries", "200")
    return {
        "duration_s": round(time.monotonic() - started, 1),
        "queries": len(latencies),
        "errors": errors,
        "latency": summary_ms(latencies) if latencies else None,
    }


def cmd_probe(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """Step 7: query the serving index in a loop until ``--until`` exists; report latencies."""
    until = Path(args.until) if args.until else None
    return latency_probe(q, args.seconds, args.interval, until)


def cmd_bulk(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """Insert ``--triples`` new triples in ``--batch``-sized INSERT DATA requests (replay rate),
    then measure query latency with the large delta."""
    out: dict[str, Any] = {"delta_before": delta(q)}
    out["probe_before"] = latency_probe(q, args.probe_seconds, 0.05)
    latencies: list[float] = []
    started = time.perf_counter()
    for b in range(args.triples // args.batch):
        lines = []
        for k in range(args.batch // 2):
            s = iri(f"{S1}bulk-{b}-{k}")
            lines.append(f"{s} {iri(RDFS_LABEL)} {lit(f'bulk label {b} {k}')} .")
            lines.append(f"{s} {iri(CRM + 'P67_refers_to')} {iri(S1 + str(k))} .")
        _, elapsed = q.update("INSERT DATA {\n" + "\n".join(lines) + "\n}")
        latencies.append(elapsed)
    total = time.perf_counter() - started
    out["bulk_insert"] = {
        "triples": args.triples,
        "batch": args.batch,
        "total_s": round(total, 2),
        "triples_per_s": round(args.triples / total),
        "per_batch": summary_ms(latencies),
    }
    out["delta_after"] = delta(q)
    out["probe_after"] = latency_probe(q, args.probe_seconds, 0.05)
    return out


def cmd_marker(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """Insert ``-n`` acknowledged single-triple updates (crash test: count them after kill -9)."""
    acked = 0
    stamp = str(int(time.time()))
    for k in range(args.n):
        q.update(f'INSERT DATA {{ <{S1}crash-marker> <{EX}marker> "{stamp}-{k}" }}')
        acked += 1
    return {"acked": acked, "stamp": stamp}


def cmd_count_markers(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """How many crash-test markers survived."""
    return {"markers": q.count(f"<{S1}crash-marker> <{EX}marker> ?v")}


def cmd_inplace_rebuild(q: Qlever, args: argparse.Namespace) -> dict[str, Any]:
    """QLever's own ``cmd=rebuild-index`` (folds the delta triples into a new index while
    serving): its duration, query latency meanwhile, and the text queries afterwards."""
    out: dict[str, Any] = {"delta_before": delta(q), "triples_before": q.count("?s ?p ?o")}
    done = Path(args.marker)
    done.unlink(missing_ok=True)
    probe: dict[str, Any] = {}
    prober = Qlever(int(q.url.rsplit(":", 1)[1]), q.token)
    thread = threading.Thread(
        target=lambda: probe.update(latency_probe(prober, 0, 0.05, until=done)), daemon=True
    )
    thread.start()
    started = time.perf_counter()
    response = q.http.get(q.url, params={"cmd": "rebuild-index"})
    out["rebuild_s"] = round(time.perf_counter() - started, 2)
    done.touch()
    thread.join()
    out["status"] = response.status_code
    out["response"] = response.text[:1000]
    out["probe_during"] = probe
    out["delta_after"] = delta(q)
    out["triples_after"] = q.count("?s ?p ?o")
    out["text_queries_after"] = run_text_queries(q)
    return out


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Spike S1 QLever measurements.")
    parser.add_argument("--port", type=int, default=7006)
    parser.add_argument("--out", type=Path, help="also write the JSON result here")
    sub = parser.add_subparsers(dest="command", required=True)
    updates = sub.add_parser("updates")
    updates.add_argument("-n", type=int, default=1000, help="number of small updates")
    updates.add_argument("--repeat", type=int, default=20, help="repetitions of each single op")
    sub.add_parser("text")
    sub.add_parser("persist")
    service = sub.add_parser("service")
    service.add_argument("--stub", default=SERVICE_IRI_DEFAULT)
    probe = sub.add_parser("probe")
    probe.add_argument("--until", help="stop when this file exists")
    probe.add_argument("--seconds", type=float, default=30.0, help="without --until")
    probe.add_argument("--interval", type=float, default=0.2)
    bulk = sub.add_parser("bulk")
    bulk.add_argument("--triples", type=int, default=1_000_000)
    bulk.add_argument("--batch", type=int, default=10_000)
    bulk.add_argument("--probe-seconds", type=float, default=10.0)
    marker = sub.add_parser("marker")
    marker.add_argument("-n", type=int, default=50)
    sub.add_parser("count-markers")
    inplace = sub.add_parser("inplace-rebuild")
    inplace.add_argument("--marker", default="/tmp/lae-i6/inplace.done")
    args = parser.parse_args(argv)

    q = Qlever(args.port, os.environ.get("LAE_QLEVER_ACCESS_TOKEN"))
    handlers = {
        "updates": cmd_updates,
        "text": cmd_text,
        "persist": cmd_persist,
        "service": cmd_service,
        "probe": cmd_probe,
        "bulk": cmd_bulk,
        "marker": cmd_marker,
        "count-markers": cmd_count_markers,
        "inplace-rebuild": cmd_inplace_rebuild,
    }
    result = handlers[args.command](q, args)
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
