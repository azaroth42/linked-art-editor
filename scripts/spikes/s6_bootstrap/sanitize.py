# Vendored from lux/data-pipeline/lux_pipeline/sources/lux/qlever/mapper2.py @ 443793ee3b7aadb9716c45777d5a606348ef30e7  # noqa: E501
# (VALID_IRI, IRI_ESCAPE_TABLE, VALID_DATETIME, clean_iri, clean_datetime, PARTIAL_DATE,
# _days_in_month, pad_datetime), with type hints added. Logic unchanged.
"""IRI and xsd:dateTime sanitation applied to every N-Triples export (docs/reuse_map.md).

A ``"`` or ``\\`` in an IRI fails the QLever index build, and a bad literal can make the serial
parser drop every triple after it, so each IRI and dateTime literal is checked before it is written.
"""

from __future__ import annotations

import re

# An http(s) IRI needs a host, so `https:/` is rejected; any other IRI needs at least a scheme.
_IRI_CHAR = r'[^\x00-\x20<>"{}|^`\\]'
VALID_IRI = re.compile(
    rf"(?i:https?)://[^/?#\x00-\x20<>\"{{}}|^`\\]{_IRI_CHAR}*"
    rf"|(?!(?i:https?):)[A-Za-z][A-Za-z0-9+.\-]*:{_IRI_CHAR}+"
)
# Line breaks inside a source IRI are wrapping, so delete them; the other characters N-Triples
# forbids get percent-encoded.
IRI_ESCAPE_TABLE = str.maketrans(
    {
        **{chr(c): f"%{c:02X}" for c in range(0x21)},
        **{c: f"%{ord(c):02X}" for c in '<>"{}|^`\\'},
        "\n": None,
        "\r": None,
        "\t": None,
    }
)

VALID_DATETIME = re.compile(
    r"-?(?:[1-9]\d{4,}|\d{4})-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?"
)


def clean_iri(value: object) -> str | None:
    """The value as a writable IRI (percent-encoding where it can), or None."""
    if type(value) is not str:
        return None
    if VALID_IRI.fullmatch(value):
        return value
    value = value.strip().translate(IRI_ESCAPE_TABLE)
    if VALID_IRI.fullmatch(value):
        return value
    return None


def clean_datetime(value: object) -> str | None:
    """The value if it is a valid xsd:dateTime, else None."""
    if type(value) is not str:
        return None
    value = value.strip()
    if VALID_DATETIME.fullmatch(value):
        return value
    return None


# year, year-month or year-month-day with no time
PARTIAL_DATE = re.compile(r"(-?(?:[1-9]\d{4,}|\d{4}))(?:-(\d\d)(?:-(\d\d))?)?")


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        # proleptic Gregorian; % is floored in python, so this holds for negative years too
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def pad_datetime(value: object, end: bool) -> str | None:
    """Pad a year, year-month or date out to an xsd:dateTime (start or end of the period)."""
    if type(value) is not str:
        return None
    m = PARTIAL_DATE.fullmatch(value.strip())
    if not m:
        return None
    year, month_s, day_s = m.groups()
    if month_s is None:
        month = 12 if end else 1
    elif not 1 <= int(month_s) <= 12:
        return None
    else:
        month = int(month_s)
    last = _days_in_month(int(year), month)
    if day_s is None:
        day = last if end else 1
    elif not 1 <= int(day_s) <= last:
        return None
    else:
        day = int(day_s)
    time = "23:59:59" if end else "00:00:00"
    return f"{year}-{month:02d}-{day:02d}T{time}"
