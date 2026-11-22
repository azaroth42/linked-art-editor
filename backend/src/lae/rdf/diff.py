"""Revision diff of canonical N-Triples (spec §3.3, decision A13)."""

from __future__ import annotations


def diff_triples(old: frozenset[str], new: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(to_delete, to_insert)``: the lines only in ``old`` and the lines only in ``new``.

    Both sets must hold canonical lines (``lae.rdf.canonical``), so that equal triples are equal
    strings. The two results are disjoint, and applying them to ``old`` gives exactly ``new``.
    """
    return old - new, new - old
