"""Canonical N-Triples documents, zstd round trip, and the triple diff."""

from __future__ import annotations

import pyoxigraph as ox
import pytest
import zstandard
from hypothesis import given
from hypothesis import strategies as st

from lae.rdf.canonical import (
    canonical_ntriples,
    compress,
    decompress,
    escape_literal,
    is_canonical_line,
    parse_ntriples,
)
from lae.rdf.diff import diff_triples

A = '<https://example.org/a> <https://example.org/p> "one" .'
B = "<https://example.org/a> <https://example.org/p> <https://example.org/b> ."
C = '<https://example.org/b> <https://example.org/p> "é 日本"@en-us .'
D = '<https://example.org/b> <https://example.org/p> "5"^^<http://www.w3.org/2001/XMLSchema#int> .'


def test_canonical_document_is_sorted_deduplicated_and_newline_terminated() -> None:
    data = canonical_ntriples([C, A, B, A, D])
    assert data == "".join(f"{line}\n" for line in sorted({A, B, C, D})).encode("utf-8")
    assert data.count(b"\n") == 4
    assert b"\r" not in data
    assert canonical_ntriples([]) == b""


def test_canonical_document_does_not_depend_on_input_order() -> None:
    assert canonical_ntriples([A, B, C, D]) == canonical_ntriples(frozenset({D, C, B, A}))


def test_sort_order_is_utf8_byte_order() -> None:
    lines = [f'<https://example.org/a> <https://example.org/p> "{c}" .' for c in "aé￿\U0001f600Z"]
    data = canonical_ntriples(lines)
    assert data.splitlines() == sorted(line.encode("utf-8") for line in lines)


def test_parse_round_trip() -> None:
    lines = frozenset({A, B, C, D})
    assert parse_ntriples(canonical_ntriples(lines)) == lines
    assert parse_ntriples(b"") == frozenset()


@pytest.mark.parametrize(
    "line",
    [
        "",
        A + "\n",
        A + " ",
        A.replace(" .", "."),
        A.replace("> <", ">  <", 1),
        "# comment",
        '_:b0 <https://example.org/p> "x" .',
        "<https://example.org/a> <https://example.org/p> _:b0 .",
        '<https://example.org/a> <https://example.org/p> "raw \t tab" .',
        '<https://example.org/a> <https://example.org/p> "line\nbreak" .',
        '<https://example.org/a> <https://example.org/p> "unescaped " quote" .',
        '<https://example.org/a> <https://example.org/p> "trailing backslash\\" .',
        '<https://example.org/a> <https://example.org/p> "\\u0022" .',
        '<https://example.org/a> <https://example.org/p> "\\u000A" .',
        '<https://example.org/a> <https://example.org/p> "\\x41" .',
        '<https://example.org/a> <https://example.org/p> "x"@EN .',
        "<https://example.org/a b> <https://example.org/p> <https://example.org/b> .",
        f"{A} {A}",
        '<https://example.org/a> <https://example.org/p> "x" . } ; DROP ALL ; INSERT DATA {',
    ],
)
def test_non_canonical_lines_are_rejected(line: str) -> None:
    assert not is_canonical_line(line)
    with pytest.raises(ValueError):
        canonical_ntriples([A, line])


@pytest.mark.parametrize(
    "data",
    [
        A.encode(),  # no final newline
        f"{A}\n\n".encode(),  # blank line
        f"{A}\r\n".encode(),
        f"# comment\n{A}\n".encode(),
        b"\xff\xfe",
    ],
)
def test_parse_rejects_documents_it_did_not_write(data: bytes) -> None:
    with pytest.raises(ValueError):
        parse_ntriples(data)


def test_lone_surrogate_cannot_be_written() -> None:
    with pytest.raises(ValueError):
        canonical_ntriples(['<https://example.org/a> <https://example.org/p> "\ud800" .'])


@given(st.text())
def test_escaped_literal_is_canonical_and_parses_back(value: str) -> None:
    line = f'<https://example.org/a> <https://example.org/p> "{escape_literal(value)}" .'
    assert is_canonical_line(line)
    try:
        data = canonical_ntriples([line])
    except UnicodeEncodeError:
        return  # lone surrogates: rejected, see the test above
    (triple,) = ox.parse(data, format=ox.RdfFormat.N_TRIPLES)
    assert triple.object.value == value


def test_zstd_round_trip_at_level_9() -> None:
    data = canonical_ntriples([A, B, C, D] + [A.replace("one", str(i)) for i in range(500)])
    packed = compress(data)
    assert decompress(packed) == data
    assert len(packed) < len(data)
    assert compress(data) == packed, "compression is deterministic"
    params = zstandard.get_frame_parameters(packed)
    assert params.content_size == len(data)
    assert packed == zstandard.ZstdCompressor(level=9).compress(data)
    assert decompress(compress(b"")) == b""


def test_revision_storage_round_trip() -> None:
    lines = frozenset({A, B, C, D})
    assert parse_ntriples(decompress(compress(canonical_ntriples(lines)))) == lines


def test_diff_triples() -> None:
    old = frozenset({A, B, C})
    new = frozenset({B, C, D})
    assert diff_triples(old, new) == (frozenset({A}), frozenset({D}))
    assert diff_triples(old, old) == (frozenset(), frozenset())
    assert diff_triples(frozenset(), new) == (frozenset(), new)
    assert diff_triples(old, frozenset()) == (old, frozenset())


@given(
    st.frozensets(st.integers(0, 30)),
    st.frozensets(st.integers(0, 30)),
)
def test_diff_applied_to_old_gives_new(old_ids: frozenset[int], new_ids: frozenset[int]) -> None:
    def line(i: int) -> str:
        return f'<https://example.org/s> <https://example.org/p> "{i}" .'

    old = frozenset(map(line, old_ids))
    new = frozenset(map(line, new_ids))
    to_delete, to_insert = diff_triples(old, new)
    assert to_delete <= old and to_insert <= new
    assert not to_delete & to_insert and not to_delete & new and not to_insert & old
    assert (old - to_delete) | to_insert == new
