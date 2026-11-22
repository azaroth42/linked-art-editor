"""SPARQL 1.1 update strings for the sync worker (spec §3.3; decision A13, ADR-0007).

Both builders write to the default graph, where all local records live. Their input is checked,
so nothing passed in can end a block early and add operations of its own: triples must be
canonical N-Triples lines (``lae.rdf.canonical``), and subjects go through ``iri``.
"""

from __future__ import annotations

from collections.abc import Iterable

from lae.rdf.canonical import check_lines
from lae.sparql.terms import iri

_SEPARATOR = " ;\n"


def _data_block(keyword: str, lines: list[str]) -> str:
    return f"{keyword} DATA {{\n" + "".join(f"{line}\n" for line in lines) + "}"


def build_update(delete: Iterable[str], insert: Iterable[str]) -> str:
    """Return ``DELETE DATA { ... } ; INSERT DATA { ... }`` for canonical N-Triples lines.

    Lines are deduplicated and sorted. An empty part is omitted; if both are empty the result is
    the empty string and there is nothing to send. Raises ``ValueError`` for a line that is not
    canonical, or one that appears on both sides (the operations run in order, so the triple would
    end up inserted whatever the caller meant; ``diff_triples`` never produces that).
    """
    deletes = check_lines(delete)
    inserts = check_lines(insert)
    both = set(deletes) & set(inserts)
    if both:
        raise ValueError(f"triple in both delete and insert: {min(both)!r}")
    parts = []
    if deletes:
        parts.append(_data_block("DELETE", deletes))
    if inserts:
        parts.append(_data_block("INSERT", inserts))
    return _SEPARATOR.join(parts)


def build_delete_where_subjects(subjects: Iterable[str]) -> str:
    """Return one ``DELETE WHERE { <s> ?p ?o }`` per subject, sorted and joined by ``;``.

    Used instead of a computed diff when the triples in QLever cannot be reproduced exactly
    (ADR-0007). Pass only ``owned_subjects`` of the record: everything about each subject goes.
    Raises ``ValueError`` for a subject that is not a writable IRI.
    """
    return _SEPARATOR.join(
        f"DELETE WHERE {{ {iri(subject)} ?p ?o }}" for subject in sorted(set(subjects))
    )
