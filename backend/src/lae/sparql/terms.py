# Vendored from lux/qlever-middletier/qleverlux/sparql/terms.py
# @ 937ea8bf2c4023a8bb37b655ed02d81da285fab3
# (`iri` and `string_literal` with what they need, reformatted only; the query-builder terms in
# that file arrive with builder.py in Phase 2). Changed here: `string_literal` also escapes a
# "u" or "U" after a backslash, as `lae.rdf.canonical` does (Step 1.13). Tests:
# tests/sparql/test_terms.py.
"""SPARQL leaf terms that are safe to build from untrusted values.

Derived from SPARQL Burger, created by Panos Mitzias (http://pmitzias.com/SPARQLBurger)
and powered by Catalink Ltd (http://catalink.eu).
Rewritten for qleverlux by Rob Sanderson (robert.sanderson@yale.edu).
"""

from __future__ import annotations

__all__ = ["iri", "string_literal"]

# Characters SPARQL forbids inside an IRIREF, plus whitespace. Letting any of
# them through would let a value close the IRI and inject query text.
_IRI_FORBIDDEN = frozenset('<>"{}|^`\\')


def iri(value: str) -> str:
    """
    Render ``value`` as a bracketed IRI, rejecting anything that cannot be one.

    Raises ``ValueError`` for characters forbidden in an IRIREF, so user input
    can be passed straight in.
    """
    if not value or any(c in _IRI_FORBIDDEN or c.isspace() or ord(c) < 0x20 for c in value):
        raise ValueError(f"Not a valid IRI: {value!r}")
    return f"<{value}>"


_LITERAL_ESCAPES = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"})

# SPARQL decodes codepoint escapes (backslash, "u", four hex digits; backslash, "U", eight) in the
# whole query text before it tokenizes (SPARQL 1.1 §19.2), and QLever does so even when the
# backslash is itself escaped. So a "u" or "U" that follows a backslash of the value is written as
# its own codepoint escape (0075, 0055) after the doubled backslash, exactly as
# lae.rdf.canonical.escape_literal does; both readings then give backslash, "u". Otherwise the
# value changes on the way in: backslash "u0041" reaches the tokenizer as backslash "A", an
# invalid escape, and the query is refused; backslash "u0022" becomes an escaped quote, a
# different literal. Once every backslash is doubled, an escaped backslash followed by u/U is the
# only way "\\u" or "\\U" can appear in the output.
_BACKSLASH_U = (("\\\\u", "\\\\\\u0075"), ("\\\\U", "\\\\\\u0055"))


def string_literal(value: str) -> str:
    """Render ``value`` as a double-quoted SPARQL string literal, escaped."""
    escaped = value.translate(_LITERAL_ESCAPES)
    for sequence, replacement in _BACKSLASH_U:
        escaped = escaped.replace(sequence, replacement)
    return f'"{escaped}"'
