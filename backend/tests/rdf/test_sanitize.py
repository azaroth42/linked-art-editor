# Cases vendored from lux/data-pipeline/tests/test_qlever_mapper_ntriples.py
# @ 443793ee3b7aadb9716c45777d5a606348ef30e7 (test_clean_iri, test_clean_datetime), with BCE cases
# added.
"""Vendored IRI and xsd:dateTime sanitation."""

from __future__ import annotations

import pytest

from lae.rdf.sanitize import clean_datetime, clean_iri

GOOD = "https://lux.collections.yale.edu/data/concept/0025ab12-dc14-5a95-bdc7-e4c7e1db2349"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (GOOD, GOOD),
        ("urn:uuid:1234", "urn:uuid:1234"),
        ("HTTPS://example.org/x", "HTTPS://example.org/x"),
        # forbidden characters are percent-encoded
        ("https://example.org/a b", "https://example.org/a%20b"),
        ('https://example.org/a"b', "https://example.org/a%22b"),
        ("https://example.org/{a}", "https://example.org/%7Ba%7D"),
        ("https://example.org/a\\b", "https://example.org/a%5Cb"),
        # line breaks are wrapping, so they are deleted, as are outer spaces
        ("https://example.org/a\nb", "https://example.org/ab"),
        (" https://example.org/x ", "https://example.org/x"),
        # nothing to repair: dropped
        ("https:/", None),
        ("https:", None),
        ("https://", None),
        ("http:/example.org/x", None),
        ("", None),
        ("relative/path", None),
        (None, None),
        (12345, None),
    ],
)
def test_clean_iri(value: object, expected: str | None) -> None:
    assert clean_iri(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1815-12-10T00:00:00", "1815-12-10T00:00:00"),
        (" 1815-12-10T00:00:00\n", "1815-12-10T00:00:00"),
        ("-10000-01-01T00:00:00Z", "-10000-01-01T00:00:00Z"),
        ("1815", None),
        ("1815-12-10", None),
        ('1815-12-10T00:00:00"', None),
        (None, None),
        (1815, None),
    ],
)
def test_clean_datetime(value: object, expected: str | None) -> None:
    assert clean_datetime(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "-0500-01-01T00:00:00Z",
        "-0001-12-31T23:59:59Z",
        "-0044-03-15T12:00:00+01:00",
        "-4000-01-01T00:00:00",
        "-13000-01-01T00:00:00Z",
        "0000-01-01T00:00:00Z",
    ],
)
def test_clean_datetime_accepts_signed_bce_years(value: str) -> None:
    assert clean_datetime(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "-500-01-01T00:00:00Z",
        "+0500-01-01T00:00:00Z",
        "--0500-01-01T00:00:00Z",
        "-05000-01-01T00:00:00Z",
    ],
)
def test_clean_datetime_rejects_malformed_signed_years(value: str) -> None:
    assert clean_datetime(value) is None
