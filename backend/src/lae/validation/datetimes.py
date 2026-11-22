"""``xsd:dateTime`` values with signed years, parsed to something that can be compared.

Linked Art ``TimeSpan`` bounds are ``xsd:dateTime``, whose value space includes years before 1
(``-0500-01-01T00:00:00Z``). Python's ``datetime``, rdflib and RFC 3339 all stop at year 1, so
the JSON Schema ``date-time`` format check and the ordering rule ``LAE-RULE-001`` use this module
instead (``docs/questions.md``, Step 0.9; ``docs/spikes/S4-shacl.md``, finding 6).

The lexical form is the one ``lae.rdf.sanitize.VALID_DATETIME`` accepts, so that the schema
check, the rules and RDF expansion agree; on top of it the fields must be in range (month 1-12,
a day the month has, ``24:00:00`` as the only hour 24, no leap second, a zone offset of at most
14:00) and the digits must be ASCII: Python's regular-expression class for digits also matches
the digits of other scripts, which ``VALID_DATETIME`` therefore lets through and XSD does not.
Years are astronomical and the calendar proleptic Gregorian, as in XSD 1.1 and ISO 8601:
``0000`` is 1 BCE, ``-0001`` is 2 BCE.

Order is the order of instants on the UTC timeline: the zone offset is subtracted, fractional
seconds are exact, and ``24:00:00`` is the first instant of the next day. A value without a zone
is read as UTC. (XSD leaves such a value unordered against a zoned one within 14 hours; Linked
Art bounds are written with ``Z``, and a rule has to decide.)
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Final

from lae.rdf.sanitize import VALID_DATETIME

_FIELDS: Final = re.compile(
    r"(?P<year>-?\d{4,})-(?P<month>\d\d)-(?P<day>\d\d)"
    r"T(?P<hour>\d\d):(?P<minute>\d\d):(?P<second>\d\d)(?P<fraction>\.\d+)?"
    r"(?P<zone>Z|[+-]\d\d:\d\d)?"
)
_DAYS_IN_MONTH: Final = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_MAX_ZONE_MINUTES: Final = 14 * 60
_SECONDS_PER_DAY: Final = 86_400


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days from 1970-01-01 to a proleptic Gregorian date; any year, positive or not."""
    if month <= 2:
        year -= 1
    era, year_of_era = divmod(year, 400)
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146_097 + day_of_era - 719_468


def parse_datetime(value: object) -> Fraction | None:
    """Return the instant of an ``xsd:dateTime`` as seconds from 1970-01-01T00:00:00Z, or ``None``.

    ``None`` means the value is not a string, not the lexical form, or not a real date and time.
    A value without a time zone is read as UTC. The result is exact (fractional seconds
    included), so two results compare as the instants do.
    """
    if not isinstance(value, str) or not value.isascii():
        return None
    if not VALID_DATETIME.fullmatch(value):
        return None
    match = _FIELDS.fullmatch(value)
    if match is None:
        return None
    year, month, day = int(match["year"]), int(match["month"]), int(match["day"])
    hour, minute, second = int(match["hour"]), int(match["minute"]), int(match["second"])
    if not 1 <= month <= 12:
        return None
    days = _DAYS_IN_MONTH[month - 1] + (1 if month == 2 and _is_leap(year) else 0)
    if not 1 <= day <= days or minute > 59 or second > 59:
        return None
    fraction = Fraction(match["fraction"] or 0)
    if hour > 24 or (hour == 24 and (minute or second or fraction)):
        return None
    offset = 0
    zone = match["zone"]
    if zone is not None and zone != "Z":
        zone_hours, zone_minutes = int(zone[1:3]), int(zone[4:6])
        offset = zone_hours * 60 + zone_minutes
        if zone_minutes > 59 or offset > _MAX_ZONE_MINUTES:
            return None
        if zone[0] == "-":
            offset = -offset
    seconds = _days_from_civil(year, month, day) * _SECONDS_PER_DAY
    seconds += hour * 3600 + minute * 60 + second - offset * 60
    return seconds + fraction


def is_datetime(value: object) -> bool:
    """True if ``value`` is a valid ``xsd:dateTime`` string (signed years allowed)."""
    return parse_datetime(value) is not None
