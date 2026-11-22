"""One stored record (JSON-LD text) -> sanitized N-Triples lines: the expansion stage's worker.

``pyld`` expands with the pinned context through ``lae.rdf.context.document_loader`` (offline).
Two opt-in tuning experiments are measured against that baseline: ``enable_static_context`` and
``enable_context_c14n_memo``. Sanitation happens twice:

- before expansion, every ``id`` goes through ``clean_iri``: pyld silently drops IRIs that contain
  whitespace (and the triples that use them) and passes ``"`` through, which breaks QLever. A
  repairable id is percent-encoded; an unusable one removes its node (counted as a reject);
- after expansion, every IRI is checked again and every ``xsd:dateTime`` literal goes through
  ``clean_datetime``, then ``pad_datetime`` (partial dates), else the triple is rejected.

Blank-node labels are made unique per record, since pyld restarts at ``_:b0`` for every document
and one ``.nt`` file holds all of them.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from collections.abc import Callable
from typing import Any, Final

import orjson
from pyld import context_resolver, jsonld

from lae.rdf.context import document_loader
from s6_bootstrap.sanitize import VALID_IRI, clean_datetime, clean_iri, pad_datetime

XSD: Final = "http://www.w3.org/2001/XMLSchema#"
XSD_STRING: Final = XSD + "string"
XSD_DATETIME: Final = XSD + "dateTime"
RDF_LANGSTRING: Final = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
CRM: Final = "http://www.cidoc-crm.org/cidoc-crm/"
END_PREDICATES: Final = frozenset({CRM + "P81a_end_of_the_begin", CRM + "P82b_end_of_the_end"})
"""dateTime predicates whose partial dates pad to the end of the period."""

LANGUAGE_TAG: Final = re.compile(r"[a-zA-Z]+(?:-[a-zA-Z0-9]+)*")
TO_RDF_OPTIONS: Final[dict[str, Any]] = {"documentLoader": document_loader}
_KNOWN_GOOD_IRIS: set[str] = set()
_KNOWN_GOOD_MAX: Final = 200_000


def static_document_loader(url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """``lae.rdf.context.document_loader`` plus ``tag: "static"`` on the remote document.

    pyld keeps a resolved remote context in its shared, cross-document cache only when the loader
    tags it ``static``; untagged, every ``to_rdf`` call reloads, re-resolves and re-canonicalizes
    the whole 79 KB context.
    """
    remote: dict[str, Any] = document_loader(url, options)
    remote["tag"] = "static"
    return remote


def enable_static_context() -> None:
    """Tuning experiment 1 (off by default): expand with ``static_document_loader``."""
    TO_RDF_OPTIONS["documentLoader"] = static_document_loader


class RecordRejected(ValueError):
    """The record's own ``id`` is not a usable IRI, so none of its triples can be written."""


def _clean_node(node: dict[str, Any], stats: Counter[str]) -> bool:
    """Clean the ids in ``node`` and below; return False if ``node``'s own id is unusable."""
    ident = node.get("id")
    if ident is not None:
        cleaned = clean_iri(ident)
        if cleaned is None:
            stats["iri_rejected"] += 1
            return False
        if cleaned != ident:
            stats["iri_repaired"] += 1
            node["id"] = cleaned
    dropped: list[str] = []
    for key, value in node.items():
        if isinstance(value, dict):
            if not _clean_node(value, stats):
                dropped.append(key)
        elif isinstance(value, list):
            kept = [v for v in value if not isinstance(v, dict) or _clean_node(v, stats)]
            if len(kept) != len(value):
                node[key] = kept
    for key in dropped:
        del node[key]
    return True


def sanitize_ids(record: dict[str, Any], stats: Counter[str]) -> None:
    """Repair or remove unusable ``id`` values in place; raise if the root id is unusable."""
    if not _clean_node(record, stats):
        raise RecordRejected(record.get("id"))


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _iri(value: str, stats: Counter[str]) -> str | None:
    if value in _KNOWN_GOOD_IRIS or VALID_IRI.fullmatch(value):
        if len(_KNOWN_GOOD_IRIS) < _KNOWN_GOOD_MAX:
            _KNOWN_GOOD_IRIS.add(value)
        return "<" + value + ">"
    cleaned = clean_iri(value)
    if cleaned is None:
        return None
    stats["iri_repaired_after_expansion"] += 1
    return "<" + cleaned + ">"


def to_ntriples(dataset: dict[str, Any], bnode_prefix: str, stats: Counter[str]) -> list[str]:
    """Serialize a pyld RDF dataset's default graph as sanitized N-Triples lines."""
    lines: list[str] = []
    for triple in dataset.get("@default", ()):
        s, p, o = triple["subject"], triple["predicate"], triple["object"]
        if s["type"] == "IRI":
            subj = _iri(s["value"], stats)
        else:
            subj = bnode_prefix + s["value"][2:]
        pred = _iri(p["value"], stats)
        if subj is None or pred is None:
            stats["triples_rejected_iri"] += 1
            continue
        kind = o["type"]
        if kind == "IRI":
            obj = _iri(o["value"], stats)
            if obj is None:
                stats["triples_rejected_iri"] += 1
                continue
        elif kind == "blank node":
            obj = bnode_prefix + o["value"][2:]
        else:
            value: str = o["value"]
            datatype = o.get("datatype", XSD_STRING)
            if datatype == XSD_DATETIME:
                cleaned = clean_datetime(value)
                if cleaned is None:
                    cleaned = pad_datetime(value, end=p["value"] in END_PREDICATES)
                    if cleaned is None:
                        stats["datetime_rejected"] += 1
                        continue
                    stats["datetime_repaired"] += 1
                value = cleaned
            obj = '"' + _escape(value) + '"'
            if datatype == RDF_LANGSTRING:
                language = o.get("language", "")
                if not LANGUAGE_TAG.fullmatch(language):
                    stats["triples_rejected_language"] += 1
                    continue
                obj += "@" + language
            elif datatype != XSD_STRING:
                obj += "^^<" + datatype + ">"
        lines.append(subj + " " + pred + " " + obj + " .\n")
    return lines


def bnode_prefix(record_id: str) -> str:
    """Blank-node label prefix unique to one record (pyld numbers from ``_:b0`` per record)."""
    return "_:r" + hashlib.blake2b(record_id.encode(), digest_size=8).hexdigest()


def expand_record(record: dict[str, Any], stats: Counter[str]) -> list[str]:
    """Sanitize ids, expand with pyld and the pinned context, return N-Triples lines."""
    sanitize_ids(record, stats)
    dataset = jsonld.to_rdf(record, TO_RDF_OPTIONS)
    return to_ntriples(dataset, bnode_prefix(str(record.get("id"))), stats)


def enable_context_c14n_memo(max_entries: int = 10_000) -> Callable[[], None]:
    """Tuning experiment 2 (off by default): memoize pyld's canonicalization of contexts.

    pyld 3.3 JCS-canonicalizes every context object it resolves, to build its cache key, each
    time a type-scoped context applies (~70 times per record here). Those objects come from the
    pinned context and are the same Python objects every time, so the key can be the identity of
    their values (kept alive in the memo, so ids are never reused). Only safe together with
    ``enable_static_context``: otherwise the loader returns a fresh context object per record and
    the memo would fill with copies. Returns an undo function.
    """
    original = context_resolver.canonicalize
    memo: dict[tuple[tuple[str, int], ...], tuple[bytes, list[Any]]] = {}

    def canonicalize(obj: dict[str, Any]) -> bytes:
        key = tuple((k, id(v)) for k, v in obj.items())
        hit = memo.get(key)
        if hit is None:
            hit = (original(obj), list(obj.values()))
            if len(memo) < max_entries:
                memo[key] = hit
        return hit[0]

    context_resolver.canonicalize = canonicalize

    def undo() -> None:
        context_resolver.canonicalize = original

    return undo


def expand_batch(texts: list[str]) -> tuple[bytes, dict[str, float]]:
    """Worker entry point: expand stored JSON texts; return N-Triples bytes and counters."""
    cpu0 = time.process_time()
    stats: Counter[str] = Counter()
    t_parse = t_rdf = t_ser = 0.0
    chunks: list[str] = []
    for text in texts:
        t0 = time.perf_counter()
        try:
            record = orjson.loads(text)
            t1 = time.perf_counter()
            sanitize_ids(record, stats)
            dataset = jsonld.to_rdf(record, TO_RDF_OPTIONS)
        except RecordRejected:
            stats["records_rejected"] += 1
            continue
        except Exception:  # count and skip, as the bootstrap will
            stats["records_failed"] += 1
            continue
        t2 = time.perf_counter()
        lines = to_ntriples(dataset, bnode_prefix(str(record.get("id"))), stats)
        chunks.append("".join(lines))
        t3 = time.perf_counter()
        t_parse += t1 - t0
        t_rdf += t2 - t1
        t_ser += t3 - t2
        stats["records"] += 1
        stats["triples"] += len(lines)
    data = "".join(chunks).encode()
    out: dict[str, float] = dict(stats)
    out.update(cpu_s=time.process_time() - cpu0, parse_s=t_parse, to_rdf_s=t_rdf, serialize_s=t_ser)
    return data, out
