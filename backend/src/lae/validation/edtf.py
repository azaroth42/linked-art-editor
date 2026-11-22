"""Syntax check for Extended Date/Time Format strings (EDTF, ISO 8601-2:2019), levels 0 to 2.

Used by the rule ``LAE-RULE-002``. It answers one question, "is this string EDTF?", and builds no
date values. Covered: dates and date-times, intervals (open ``..`` and unknown ends), seasons and
sub-year groupings (21-41), qualifiers (``?``, ``~``, ``%``, on the whole date or on one
component), unspecified digits (``X``), long years (``Y17000``), exponential years (``Y-17E7``),
significant digits (``1950S2``), and sets (``[1667,1668,1670..1672]``, ``{1960,1961-12}``).
Months and days must be possible ones (no ``2004-13``, no ``2004-02-30``). It does not check that
an interval's start is before its end.
"""

from __future__ import annotations

import re
from typing import Final

_Q: Final = r"[?~%]?"
_DATE: Final = re.compile(
    rf"{_Q}(?P<year>-?[0-9X]{{4}}){_Q}"
    rf"(?:-{_Q}(?P<month>[0-9X]{{2}}){_Q}(?:-{_Q}(?P<day>[0-9X]{{2}}){_Q})?)?"
)
_LONG_YEAR: Final = re.compile(r"Y-?(?:\d{5,}|\d+E\d+)(?:S\d+)?")
_SIGNIFICANT_YEAR: Final = re.compile(r"-?\d{4}S\d+")
_DATETIME: Final = re.compile(
    r"(?P<date>-?\d{4}-\d\d-\d\d)T(?P<hour>\d\d):(?P<minute>\d\d):(?P<second>\d\d)"
    r"(?:Z|[+-](?P<zone_hour>\d\d)(?::(?P<zone_minute>\d\d))?)?"
)
_DAYS: Final = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_FIRST_GROUPING: Final = 21
_LAST_GROUPING: Final = 41
_OPEN: Final = ".."


def _candidates(digits: str) -> list[int]:
    """Every number a two-character field with unspecified digits (``X``) can stand for."""
    tens = range(10) if digits[0] == "X" else [int(digits[0])]
    units = range(10) if digits[1] == "X" else [int(digits[1])]
    return [ten * 10 + unit for ten in tens for unit in units]


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _is_date(text: str) -> bool:
    """A year, year-month or year-month-day, with qualifiers and unspecified digits."""
    if _LONG_YEAR.fullmatch(text) or _SIGNIFICANT_YEAR.fullmatch(text):
        return True
    match = _DATE.fullmatch(text)
    if match is None:
        return False
    year, month, day = match["year"], match["month"], match["day"]
    if month is None:
        return True
    months = [m for m in _candidates(month) if 1 <= m <= 12]
    if "X" not in month and _FIRST_GROUPING <= int(month) <= _LAST_GROUPING:
        return day is None
    if not months:
        return False
    if day is None:
        return True
    longest = max(_DAYS[m - 1] for m in months)
    if months == [2] and "X" not in year and not _is_leap(int(year)):
        longest = 28
    return any(1 <= d <= longest for d in _candidates(day))


def _is_datetime(text: str) -> bool:
    match = _DATETIME.fullmatch(text)
    if match is None or not _is_date(match["date"]):
        return False
    hour, minute, second = int(match["hour"]), int(match["minute"]), int(match["second"])
    if minute > 59 or second > 59 or hour > 24 or (hour == 24 and (minute or second)):
        return False
    zone_hour, zone_minute = match["zone_hour"], match["zone_minute"]
    return (zone_hour is None or int(zone_hour) <= 14) and (
        zone_minute is None or int(zone_minute) <= 59
    )


def _is_interval(text: str) -> bool:
    start, _, end = text.partition("/")
    if "/" in end:
        return False
    ends = (start, end)
    if all(part in ("", _OPEN) for part in ends):
        return False
    return all(part in ("", _OPEN) or _is_date(part) for part in ends)


def _is_set_member(text: str) -> bool:
    if _OPEN not in text:
        return _is_date(text)
    start, _, end = text.partition(_OPEN)
    if not start and not end:
        return False
    return (not start or _is_date(start)) and (not end or _is_date(end))


def _is_set(text: str) -> bool:
    if len(text) < 3 or (text[0], text[-1]) not in (("[", "]"), ("{", "}")):
        return False
    return all(_is_set_member(member) for member in text[1:-1].split(","))


def is_edtf(value: object) -> bool:
    """True if ``value`` is a syntactically valid EDTF string (levels 0, 1 or 2)."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if value[0] in "[{":
        return _is_set(value)
    if "/" in value:
        return _is_interval(value)
    return _is_date(value) or _is_datetime(value)
