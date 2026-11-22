"""``xsd:dateTime`` with signed years: validity and ordering (``lae.validation.datetimes``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lae.validation.datetimes import is_datetime, parse_datetime


def instant(value: str) -> Fraction:
    parsed = parse_datetime(value)
    assert parsed is not None, value
    return parsed


@pytest.mark.parametrize(
    "value",
    [
        "1642-01-01T00:00:00Z",
        "1642-01-01T00:00:00",
        "1642-12-31T23:59:59.999Z",
        "2024-02-29T12:00:00+05:30",
        "2000-02-29T00:00:00Z",
        "0001-01-01T00:00:00Z",
        "0000-01-01T00:00:00Z",
        "-0500-01-01T00:00:00Z",
        "-0004-02-29T00:00:00Z",
        "-12000-06-15T00:00:00Z",
        "12000-06-15T00:00:00Z",
        "1642-01-01T24:00:00Z",
        "1642-01-01T00:00:00-14:00",
    ],
)
def test_valid(value: str) -> None:
    assert is_datetime(value)


@pytest.mark.parametrize(
    "value",
    [
        "1642",
        "1642-01-01",
        "1642-01-01T00:00",
        "1642-01-01 00:00:00Z",
        "1642-13-01T00:00:00Z",
        "1642-00-10T00:00:00Z",
        "1642-02-30T00:00:00Z",
        "1900-02-29T00:00:00Z",
        "-0001-02-29T00:00:00Z",
        "1642-04-31T00:00:00Z",
        "1642-01-01T25:00:00Z",
        "1642-01-01T24:00:01Z",
        "1642-01-01T12:60:00Z",
        "1642-01-01T12:00:60Z",
        "1642-01-01T12:00:00+15:00",
        "1642-01-01T12:00:00+05:60",
        "642-01-01T00:00:00Z",
        "01642-01-01T00:00:00Z",
        " 1642-01-01T00:00:00Z",
        "",
        None,
        1642,
    ],
)
def test_invalid(value: object) -> None:
    assert not is_datetime(value)
    assert parse_datetime(value) is None


def test_bce_years_order_before_ce_years() -> None:
    assert instant("-0500-01-01T00:00:00Z") < instant("-0400-12-31T23:59:59Z")
    assert instant("-0400-12-31T23:59:59Z") < instant("0000-01-01T00:00:00Z")
    assert instant("-0001-12-31T23:59:59Z") < instant("0000-01-01T00:00:00Z")
    assert instant("0000-12-31T23:59:59Z") < instant("0001-01-01T00:00:00Z")
    assert instant("-12000-01-01T00:00:00Z") < instant("-0500-01-01T00:00:00Z")
    # Lexical order would get these wrong.
    assert "-0500" > "-0400"


def test_year_zero_is_a_leap_year_one_second_before_year_one() -> None:
    assert instant("0000-02-29T00:00:00Z") < instant("0000-03-01T00:00:00Z")
    assert instant("0001-01-01T00:00:00Z") - instant("0000-12-31T23:59:59Z") == 1


def test_time_zones_and_fractions_compare_as_instants() -> None:
    assert instant("1642-01-01T00:00:00+02:00") == instant("1641-12-31T22:00:00Z")
    assert instant("1642-01-01T00:00:00-05:00") > instant("1642-01-01T00:00:00Z")
    assert instant("1642-01-01T00:00:00") == instant("1642-01-01T00:00:00Z")
    assert instant("1642-01-01T24:00:00Z") == instant("1642-01-02T00:00:00Z")
    assert instant("1642-01-01T00:00:00.5Z") - instant("1642-01-01T00:00:00Z") == Fraction(1, 2)


def test_epoch() -> None:
    assert instant("1970-01-01T00:00:00Z") == 0


@given(
    st.datetimes(min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31, 23, 59, 59)),
    st.integers(min_value=-14 * 60, max_value=14 * 60),
)
def test_agrees_with_python_datetime_for_years_1_to_9999(moment: datetime, offset: int) -> None:
    zone = timezone(timedelta(minutes=offset))
    aware = moment.replace(tzinfo=zone)
    text = aware.isoformat(timespec="microseconds" if moment.microsecond else "seconds")
    expected = aware - datetime(1970, 1, 1, tzinfo=UTC)
    assert instant(text) == Fraction(expected // timedelta(microseconds=1), 1_000_000)
