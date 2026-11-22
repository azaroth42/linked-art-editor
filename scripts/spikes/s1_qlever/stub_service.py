"""Spike S1, step 6: a stub SPARQL ``SERVICE`` endpoint that records what QLever sends it.

QLever evaluates ``SERVICE <http://localhost:9806/sparql> { ?x <urn:vec:score> ?score }`` by
POSTing ``SELECT ?x ?score { ?x <urn:vec:score> ?score VALUES (?x) { ... } }`` here, with the
``VALUES`` block present only if it pushed the sibling's result down. The request is parsed with
the vendored LUX parser (``sparql_parser.py``: ``_split_values`` cuts out the ``VALUES`` block,
``_tokens`` / ``_triples`` read the rest), and every request is logged (method, content type,
body size, pushed-down rows) for ``bench.py service`` to read back from ``GET /log``.

The answer is ``application/sparql-results+json``: one row per pushed-down IRI with a
deterministic pseudo-score, or, with nothing pushed down, a fixed "global top 20" (subjects
``s1/99980`` .. ``s1/99999``), which is what a vector-first search would return.

    uv run --project backend python scripts/spikes/s1_qlever/stub_service.py --port 9806
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sparql_parser  # noqa: E402  (vendored, next to this file)

SCORE = "urn:vec:score"
XSD_DOUBLE = "http://www.w3.org/2001/XMLSchema#double"
GLOBAL_TOP = [f"http://localhost:8000/id/s1/{i}" for i in range(99_980, 100_000)]
RESULTS_JSON = "application/sparql-results+json"

app = FastAPI(title="S1 stub SERVICE")
LOG: list[dict[str, Any]] = []


def pseudo_score(value: str) -> float:
    """A stable score in [0, 1) for an IRI."""
    digest = hashlib.blake2b(value.encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big") / 2**32


def read_query(raw: bytes, content_type: str, params: dict[str, str]) -> str:
    """The SPARQL text of a SPARQL-protocol request (POST body, form, or GET parameter)."""
    if "query" in params:
        return params["query"]
    text = raw.decode("utf-8")
    if content_type.startswith("application/x-www-form-urlencoded"):
        return urllib.parse.parse_qs(text).get("query", [""])[0]
    return text


def plan(query: str) -> dict[str, Any]:
    """Select variables, subject and score variables, and pushed-down candidates."""
    remainder, value_vars, rows = sparql_parser._split_values(query)
    tokens = sparql_parser._tokens(remainder)
    prefixes: dict[str, str] = {}
    i = 0
    while i < len(tokens) and tokens[i][1].upper() in ("PREFIX", "BASE"):
        if tokens[i][1].upper() == "PREFIX":
            prefixes[tokens[i + 1][1][:-1]] = tokens[i + 2][1][1:-1]
            i += 3
        else:
            i += 2
    if i >= len(tokens) or tokens[i][1].upper() != "SELECT":
        raise sparql_parser.SparqlError("expected a SELECT query")
    i += 1
    select = []
    while tokens[i][1] != "{" and tokens[i][1].upper() != "WHERE":
        if tokens[i][0] == "var":
            select.append(tokens[i][1][1:])
        i += 1
    while tokens[i][1] != "{":
        i += 1
    close = sparql_parser._matching_brace(tokens, i)
    group = tokens[i + 1 : close]
    while (
        group and group[0][1] == "{" and sparql_parser._matching_brace(group, 0) == len(group) - 1
    ):
        group = group[1:-1]
    triples = sparql_parser._triples(group, prefixes)
    subject = score = None
    for s, p, o in triples:
        if p.kind == "iri" and p.value == SCORE and s.kind == "var" and o.kind == "var":
            subject, score = s.value, o.value
    if subject is None or score is None:
        raise sparql_parser.SparqlError("expected ?x <urn:vec:score> ?score")
    candidates = None
    if value_vars:
        column = value_vars.index(subject)
        candidates = [row[column] for row in rows if row[column] is not None]
    return {"select": select, "subject": subject, "score": score, "candidates": candidates}


@app.api_route("/sparql", methods=["GET", "POST"])
async def sparql(request: Request) -> JSONResponse:
    """Answer the SERVICE request and log what QLever sent."""
    started = time.perf_counter()
    raw = await request.body()
    content_type = request.headers.get("content-type", "")
    entry: dict[str, Any] = {
        "method": request.method,
        "content_type": content_type,
        "accept": request.headers.get("accept"),
        "body_bytes": len(raw),
    }
    try:
        parsed = plan(read_query(raw, content_type, dict(request.query_params)))
    except (sparql_parser.SparqlError, ValueError, IndexError) as error:
        entry["error"] = str(error)
        LOG.append(entry)
        return JSONResponse({"error": str(error)}, status_code=400)
    candidates = parsed["candidates"]
    entry["select"] = parsed["select"]
    entry["values_pushed_down"] = candidates is not None
    entry["values_rows"] = len(candidates) if candidates is not None else 0
    entry["parse_ms"] = round((time.perf_counter() - started) * 1000, 2)
    hits = [c[1:-1] if c.startswith("<") else c for c in candidates] if candidates else GLOBAL_TOP
    bindings = []
    for value in hits:
        row: dict[str, Any] = {}
        if parsed["subject"] in parsed["select"]:
            row[parsed["subject"]] = {"type": "uri", "value": value}
        if parsed["score"] in parsed["select"]:
            row[parsed["score"]] = {
                "type": "literal",
                "datatype": XSD_DOUBLE,
                "value": f"{pseudo_score(value):.6f}",
            }
        bindings.append(row)
    entry["returned_rows"] = len(bindings)
    entry["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    LOG.append(entry)
    body = {"head": {"vars": parsed["select"]}, "results": {"bindings": bindings}}
    return JSONResponse(body, media_type=RESULTS_JSON)


@app.get("/log")
async def log() -> list[dict[str, Any]]:
    """Every request since the last reset."""
    return LOG


@app.post("/reset")
async def reset() -> dict[str, int]:
    """Clear the request log."""
    LOG.clear()
    return {"cleared": 1}


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Spike S1 stub SPARQL SERVICE.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9806)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
