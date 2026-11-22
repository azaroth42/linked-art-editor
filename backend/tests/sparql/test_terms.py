# Vendored from lux/qlever-middletier/tests/test_terms_escaping.py
# @ 937ea8bf2c4023a8bb37b655ed02d81da285fab3 (the `iri` and `string_literal` tests; type hints
# added, the parse-back test uses pyoxigraph instead of rdflib; the SERVICE rendering test belongs
# to builder.py and arrives with it in Phase 2).
"""
Escaping helpers for user values.

These are what keep a user-supplied value (record URI or free text) from ending up as query text.
"""

from __future__ import annotations

import pyoxigraph as ox
import pytest

from lae.sparql.terms import iri, string_literal


@pytest.mark.parametrize(
    ("raw", "rendered"),
    [
        ("roman coins", '"roman coins"'),
        ('say "hi"', r'"say \"hi\""'),
        ("back\\slash", r'"back\\slash"'),
        # The backslash must be escaped before the quote, or \" becomes \\".
        ('\\"', r'"\\\""'),
        ("two\nlines\r\tx", r'"two\nlines\r\tx"'),
        ("", '""'),
    ],
)
def test_string_literal(raw: str, rendered: str) -> None:
    assert string_literal(raw) == rendered


@pytest.mark.parametrize(
    "raw",
    ['" } ?s ?p ?o { "', "a\\", 'x" . ?s ?p ?o . ?x <y> "', "\n", 'é "ü"'],
)
def test_string_literal_parses_back(raw: str) -> None:
    """The rendered literal must match exactly the original string, and nothing else."""
    store = ox.Store()
    p = ox.NamedNode("http://ex/p")
    store.add(ox.Quad(ox.NamedNode("http://ex/hit"), p, ox.Literal(raw)))
    store.add(ox.Quad(ox.NamedNode("http://ex/miss"), p, ox.Literal(raw + "x")))
    query = f"SELECT ?s WHERE {{ ?s <http://ex/p> {string_literal(raw)} }}"
    assert [row["s"].value for row in store.query(query)] == ["http://ex/hit"]  # type: ignore[union-attr]


def test_iri_accepts_record_uri() -> None:
    uri = "https://lux.collections.yale.edu/data/set/0025ab12-dc14-5a95-bdc7-e4c7e1db2349"
    assert iri(uri) == f"<{uri}>"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "https://x/a> . ?s ?p ?o <b",
        "https://x/a b",
        'https://x/"a',
        "https://x/{a}",
        "https://x/a|b",
        "https://x/a^b",
        "https://x/a`b",
        "https://x/a\\b",
        "https://x/a\nb",
    ],
)
def test_iri_rejects_breakout(bad: str) -> None:
    with pytest.raises(ValueError):
        iri(bad)
