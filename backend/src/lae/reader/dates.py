"""Dates of ``TimeSpan`` bounds for the Public Reader, formatted with ``babel`` (spec §6.6).

A ``TimeSpan`` is shown as the period its outer bounds (``begin_of_the_begin``,
``end_of_the_end``; the inner ones when an outer one is missing) describe, at the coarsest
precision they allow: a whole year (``1880``, ``1880 – 1885``), a whole month (``January 1880``),
else days (``Jan 5, 1880``). Times of day are not shown. An end bound at midnight is read as
exclusive when it is the first of a month or the day after the start (``1881-01-01T00:00:00``
ends 1880, ``1606-07-16T00:00:00`` after ``1606-07-15T00:00:00`` ends July 15), else as that
day (``1997-12-31T00:00:00`` ends 1997): published data uses both conventions.

**Years before 1** (decision for Step 0.9 in ``docs/questions.md``): bounds are ``xsd:dateTime``
with astronomical years, ``0000`` being 1 BCE and ``-0499`` 500 BCE. Python's ``date`` and so
``babel`` stop at year 1, so such a date is formatted through a stand-in year in the same
position of the 400-year Gregorian cycle (same leap years and weekdays), with the year and era
fields written from the real year. When either end of a period is outside 1–9999, both ends
carry the era in the locale's words (``500 BC – 400 BC``, ``100 BC – 50 AD``); otherwise none
does. The lexical form is the one ``lae.validation.datetimes`` accepts; anything else is ignored.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

from babel import Locale
from babel.dates import (
    DateTimeFormat,
    format_interval,
    format_skeleton,
    get_era_names,
    match_skeleton,
)

from lae.validation.datetimes import is_datetime

Unit = Literal["year", "month", "day"]

_FIELDS: Final = re.compile(
    r"(?P<year>-?\d{4,})-(?P<month>\d\d)-(?P<day>\d\d)"
    r"T(?P<hour>\d\d):(?P<minute>\d\d):(?P<second>\d\d)(?P<fraction>\.\d+)?"
)

SKELETONS: Final[Mapping[Unit, tuple[str, str]]] = {
    "year": ("y", "Gy"),
    "month": ("yMMMM", "GyMMM"),
    "day": ("yMMMd", "GyMMMd"),
}
"""CLDR skeletons per precision: without the era, and with it."""

_CYCLE_YEARS: Final = 400
"""The Gregorian calendar repeats every 400 years, weekdays included (146,097 days)."""

_STAND_IN_BASE: Final = 1600


@dataclass(frozen=True, order=True)
class Day:
    """A proleptic Gregorian calendar day with an astronomical year (``0`` is 1 BCE)."""

    year: int
    month: int
    day: int

    @property
    def stand_in(self) -> date:
        """A ``date`` in the same position of the 400-year cycle (the real one when in range)."""
        year = self.year
        if not 1 <= year <= 9999:
            year = _STAND_IN_BASE + (year - _STAND_IN_BASE) % _CYCLE_YEARS
        return date(year, self.month, self.day)

    def previous(self) -> Day:
        """The day before."""
        if self.day > 1:
            return Day(self.year, self.month, self.day - 1)
        if self.month > 1:
            return Day(self.year, self.month - 1, _days_in_month(self.year, self.month - 1))
        return Day(self.year - 1, 12, 31)


_DAYS_IN_MONTH: Final = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _days_in_month(year: int, month: int) -> int:
    # calendar.isleap is the proleptic Gregorian rule and works for any integer year.
    return 29 if month == 2 and calendar.isleap(year) else _DAYS_IN_MONTH[month - 1]


@dataclass(frozen=True)
class Bound:
    """One parsed bound: its day, and whether it is the first instant of that day."""

    day: Day
    starts_day: bool


def parse_bound(value: Any) -> Bound | None:
    """Return the day of an ``xsd:dateTime`` bound (signed years allowed), else ``None``."""
    if not is_datetime(value):
        return None
    match = _FIELDS.match(value)
    if match is None:  # pragma: no cover - is_datetime accepted the same lexical form
        return None
    day = Day(int(match["year"]), int(match["month"]), int(match["day"]))
    clock = (int(match["hour"]), int(match["minute"]), int(match["second"]))
    return Bound(day=day, starts_day=clock == (0, 0, 0) and not match["fraction"])


def _bound(timespan: Mapping[str, Any], *keys: str) -> Bound | None:
    for key in keys:
        bound = parse_bound(timespan.get(key))
        if bound is not None:
            return bound
    return None


def period(timespan: Mapping[str, Any]) -> tuple[Unit, Day | None, Day | None]:
    """Return the precision and the first and last day of a ``TimeSpan``'s bounds.

    Both days are ``None`` when the ``TimeSpan`` has no usable bound. The last day is the same
    as the first when the period is one unit long.
    """
    begin = _bound(timespan, "begin_of_the_begin", "end_of_the_begin")
    end = _bound(timespan, "end_of_the_end", "begin_of_the_end")
    first = begin.day if begin else None
    last = end.day if end else None
    if end is not None and last is not None and end.starts_day:
        # Data uses midnight both ways: "1606-07-16T00:00:00" closing July 15, and
        # "1997-12-31T00:00:00" closing 1997. Read it as exclusive only when the day before
        # is the start, or when it is the first of a month (the close of the month before).
        if (first is None or last > first) and (last.day == 1 or last.previous() == first):
            last = last.previous()
    unit: Unit = "day"
    if (first is None or (first.month, first.day) == (1, 1)) and (
        last is None or (last.month, last.day) == (12, 31)
    ):
        unit = "year"
    elif (first is None or first.day == 1) and (
        last is None or last.day == _days_in_month(last.year, last.month)
    ):
        unit = "month"
    return unit, first, last


_ERA_WIDTHS: Final[Mapping[int, Literal["abbreviated", "wide", "narrow"]]] = {
    3: "abbreviated",
    4: "wide",
    5: "narrow",
}


class _RealYearFormat(DateTimeFormat):
    """``babel``'s field formatter, with the year and era taken from an astronomical year."""

    def __init__(self, stand_in: date, locale: Locale, year: int) -> None:
        super().__init__(stand_in, locale)
        self.real_year = year

    def format_era(self, char: str, num: int) -> str:
        width = _ERA_WIDTHS[max(3, num)]
        return str(get_era_names(width, self.locale)[int(self.real_year > 0)])

    def format_year(self, char: str, num: int) -> str:
        year = self.real_year if self.real_year > 0 else 1 - self.real_year
        text = self.format(year, num)
        return text[-2:] if num == 2 else text


def _with_era(day: Day, skeleton: str, locale: Locale) -> str:
    skeletons = locale.datetime_skeletons
    pattern = skeletons[match_skeleton(skeleton, skeletons) or skeleton.removeprefix("G")]
    return str(pattern % _RealYearFormat(day.stand_in, locale, day.year))


def _unit_start(day: Day, unit: Unit) -> Day:
    if unit == "year":
        return Day(day.year, 1, 1)
    if unit == "month":
        return Day(day.year, day.month, 1)
    return day


def _same(first: Day, last: Day, unit: Unit) -> bool:
    return _unit_start(first, unit) == _unit_start(last, unit)


def format_day(day: Day, unit: Unit, locale: Locale, *, era: bool) -> str:
    """Format one day at ``unit`` precision, with the era if asked (always outside 1-9999)."""
    plain, with_era = SKELETONS[unit]
    if era or not 1 <= day.year <= 9999:
        return _with_era(_unit_start(day, unit), with_era, locale)
    return str(format_skeleton(plain, _unit_start(day, unit).stand_in, locale=locale))


@dataclass(frozen=True)
class PeriodText:
    """A formatted period: ``exact`` (both ends known), or only its start or its end."""

    kind: Literal["exact", "from", "until"]
    text: str


def interval_pattern(locale: Locale) -> str:
    """The locale's fallback pattern for a range, ``{0} – {1}`` in English."""
    pattern = locale.interval_formats.get(None)
    return pattern if isinstance(pattern, str) else "{0} – {1}"


def describe(timespan: Mapping[str, Any], locale: Locale) -> PeriodText | None:
    """Return the period of a ``TimeSpan``'s bounds in ``locale``, or ``None`` without bounds."""
    unit, first, last = period(timespan)
    era = any(day is not None and not 1 <= day.year <= 9999 for day in (first, last))
    if first is not None and last is not None:
        if _same(first, last, unit):
            return PeriodText("exact", format_day(first, unit, locale, era=era))
        if not era and last > first:
            start, end = _unit_start(first, unit).stand_in, _unit_start(last, unit).stand_in
            text = format_interval(start, end, SKELETONS[unit][0], locale=locale)
            return PeriodText("exact", str(text))
        start_text = format_day(first, unit, locale, era=era)
        end_text = format_day(last, unit, locale, era=era)
        joined = interval_pattern(locale).replace("{0}", start_text).replace("{1}", end_text)
        return PeriodText("exact", joined)
    if first is not None:
        return PeriodText("from", format_day(first, unit, locale, era=era))
    if last is not None:
        return PeriodText("until", format_day(last, unit, locale, era=era))
    return None
