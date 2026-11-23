"""``TimeSpan`` display with ``babel``, BCE dates included (``lae.reader.dates``)."""

from __future__ import annotations

import pytest
from babel import Locale

from lae.reader.dates import Day, describe, parse_bound, period

THIN = " "  # CLDR's English range pattern is "{0} – {1}" with thin spaces
RANGE = f"{THIN}–{THIN}"


def span(begin: str | None, end: str | None) -> dict[str, str]:
    timespan: dict[str, str] = {}
    if begin is not None:
        timespan["begin_of_the_begin"] = begin
    if end is not None:
        timespan["end_of_the_end"] = end
    return timespan


@pytest.mark.parametrize(
    ("begin", "end", "kind", "text"),
    [
        # Common Era: the coarsest precision the bounds allow.
        ("1880-01-01T00:00:00Z", "1880-12-31T23:59:59Z", "exact", "1880"),
        ("1880-01-01T00:00:00Z", "1885-12-31T23:59:59Z", "exact", f"1880{RANGE}1885"),
        ("1880-03-01T00:00:00Z", "1880-03-31T23:59:59Z", "exact", "March 1880"),
        ("1880-03-01T00:00:00Z", "1880-05-31T23:59:59Z", "exact", f"March{RANGE}May 1880"),
        ("1880-03-05T00:00:00Z", "1880-03-05T23:59:59Z", "exact", "Mar 5, 1880"),
        ("1880-03-05T10:00:00Z", "1880-03-05T12:00:00Z", "exact", "Mar 5, 1880"),
        ("1880-03-05T00:00:00Z", "1880-04-07T23:59:59Z", "exact", f"Mar 5{RANGE}Apr 7, 1880"),
        ("1880-02-01T00:00:00Z", "1880-02-29T23:59:59Z", "exact", "February 1880"),
        # Midnight ends: exclusive on the first of a month or the day after the start.
        ("1880-01-01T00:00:00Z", "1881-01-01T00:00:00Z", "exact", "1880"),
        ("1606-07-15T00:00:00Z", "1606-07-16T00:00:00Z", "exact", "Jul 15, 1606"),
        ("1997-01-01T00:00:00Z", "1997-12-31T00:00:00Z", "exact", "1997"),
        ("1300-01-01T00:00:00Z", "1399-12-31T00:00:00Z", "exact", f"1300{RANGE}1399"),
        # Before the Common Era: astronomical years, with the era on both ends.
        ("-0499-01-01T00:00:00Z", "-0499-12-31T23:59:59Z", "exact", "500 BC"),
        ("-0479-01-01T00:00:00Z", "-0469-12-31T23:59:59Z", "exact", f"480 BC{RANGE}470 BC"),
        ("-0099-01-01T00:00:00Z", "0050-12-31T23:59:59Z", "exact", f"100 BC{RANGE}50 AD"),
        ("0000-01-01T00:00:00Z", "0000-12-31T23:59:59Z", "exact", "1 BC"),
        ("0001-01-01T00:00:00Z", "0001-12-31T23:59:59Z", "exact", "1"),
        ("-0043-03-15T00:00:00Z", "-0043-03-15T23:59:59Z", "exact", "Mar 15, 44 BC"),
        ("-0044-02-01T00:00:00Z", "-0044-02-29T23:59:59Z", "exact", "Feb 45 BC"),
        ("-12000-01-01T00:00:00Z", "-10000-12-31T23:59:59Z", "exact", f"12001 BC{RANGE}10001 BC"),
        ("12000-01-01T00:00:00Z", "12000-12-31T23:59:59Z", "exact", "12000 AD"),
        # One end only.
        ("1880-01-01T00:00:00Z", None, "from", "1880"),
        (None, "1880-12-31T23:59:59Z", "until", "1880"),
        ("-0499-06-01T00:00:00Z", None, "from", "Jun 500 BC"),
    ],
)
def test_describe_in_english(begin: str | None, end: str | None, kind: str, text: str) -> None:
    described = describe(span(begin, end), Locale.parse("en"))
    assert described is not None
    assert (described.kind, described.text) == (kind, text)


@pytest.mark.parametrize(
    ("locale", "begin", "end", "text"),
    [
        ("fr", "1880-03-01T00:00:00Z", "1880-03-31T23:59:59Z", "mars 1880"),
        ("fr", "-0499-01-01T00:00:00Z", "-0499-12-31T23:59:59Z", "500 av. J.-C."),
        ("de", "1880-03-05T00:00:00Z", "1880-03-05T23:59:59Z", "5. März 1880"),
        ("de", "-0043-03-15T00:00:00Z", "-0043-03-15T23:59:59Z", "15. März 44 v. Chr."),
        ("ar", "1880-01-01T00:00:00Z", "1880-12-31T23:59:59Z", "1880"),
    ],
)
def test_describe_in_other_locales(locale: str, begin: str, end: str, text: str) -> None:
    described = describe(span(begin, end), Locale.parse(locale))
    assert described is not None
    assert described.text == text


def test_inner_bounds_stand_in_for_missing_outer_ones() -> None:
    timespan = {
        "end_of_the_begin": "1880-01-01T00:00:00Z",
        "begin_of_the_end": "1880-12-31T23:59:59Z",
    }
    described = describe(timespan, Locale.parse("en"))
    assert described is not None
    assert described.text == "1880"


@pytest.mark.parametrize(
    "value",
    ["1880", "1880-01-01", "1880-13-01T00:00:00Z", "-0043-02-29T00:00:00Z", 1880, None, "soon"],
)
def test_unusable_bounds_are_ignored(value: object) -> None:
    assert parse_bound(value) is None
    assert describe({"begin_of_the_begin": value}, Locale.parse("en")) is None


def test_period_reports_the_precision_and_days() -> None:
    assert period(span("-0043-03-15T00:00:00Z", "-0043-03-15T23:59:59Z")) == (
        "day",
        Day(-43, 3, 15),
        Day(-43, 3, 15),
    )
    assert period({}) == ("year", None, None)


def test_stand_in_keeps_leap_years() -> None:
    assert Day(-44, 2, 29).stand_in.year % 400 == (-44) % 400
    assert Day(-44, 2, 29).previous() == Day(-44, 2, 28)
    assert Day(-44, 3, 1).previous() == Day(-44, 2, 29)
    assert Day(0, 1, 1).previous() == Day(-1, 12, 31)
