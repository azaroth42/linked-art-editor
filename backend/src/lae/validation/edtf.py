"""Syntax check for Extended Date/Time Format strings (EDTF, ISO 8601-2:2019), levels 0 to 2.

Used by the rule ``LAE-RULE-002``. It answers one question, "is this string EDTF?", and builds no
date values.

**The reference is edtf.js.** The editor parses what a cataloguer types with the ``edtf`` npm
package (Phase 1 Step 1.18), so a string that one side accepts and the other rejects would show
as a client/server disagreement. This module therefore accepts exactly the strings that
``parse(text)`` of edtf.js 4.12.0 accepts with its default options (``level: 2``), and is written
as a transcription of that package's grammar (``src/edtf.ne``), rule by rule, with the rule names
kept. ``tests/validation/data/edtf_js_cases.json`` holds the verdict of edtf.js for some
thousands of strings, valid and not, and ``test_edtf.py`` compares every one.

Where edtf.js and the Library of Congress specification differ, edtf.js wins. What that means:

- Accepted beyond the specification: a two-digit century (``19``) and a three-digit decade
  (``198``, ``198~``) from ISO 8601-2; ``02-29`` in any year; a time without seconds
  (``T23:20``), with fractional seconds, and ``24:00``; spaces around the commas of a set; an
  interval with both ends unknown or open (``/``, ``../..``).
- Rejected although the specification can be read to allow them: a qualified or unspecified
  season (``2001-21~``, level 3 in edtf.js) and an interval of seasons; a qualifier on a date
  with unspecified digits (``201X?``); an interval that mixes a level 1 end (``1984?``) with a
  level 2 end (``2004-~06``); a range inside a set whose two ends differ in precision; long
  years, significant digits and date-times inside a set.

Not checked here, as in ``parse``: that an interval's start is before its end. The edtf.js
*constructor* ``edtf(text)`` does refuse that, and it also reads a string of five or more
digits as a Unix time rather than as EDTF; callers on the client should use ``parse`` for the
syntax question.
"""

from __future__ import annotations

import re
from typing import Final

# --- Level 0 --------------------------------------------------------------------------------
# year: four digits, optionally negative; "-0000" is not a year.
_YEAR: Final = r"(?:-(?!0000)[0-9]{4}|[0-9]{4})"
_M31: Final = r"(?:01|03|05|07|08|10|12)"
_M30: Final = r"(?:04|06|09|11)"
_MONTH: Final = r"(?:0[1-9]|1[0-2])"
_D29: Final = r"(?:0[1-9]|[12][0-9])"
_D30: Final = rf"(?:{_D29}|30)"
_D31: Final = rf"(?:{_D30}|31)"
_MONTH_DAY: Final = rf"(?:{_M31}-{_D31}|{_M30}-{_D30}|02-{_D29})"
_YEAR_MONTH: Final = rf"{_YEAR}-{_MONTH}"
_YEAR_MONTH_DAY: Final = rf"{_YEAR}-{_MONTH_DAY}"
_DATE: Final = rf"(?:{_YEAR_MONTH_DAY}|{_YEAR_MONTH}|{_YEAR})"

_MINUTES: Final = r"[0-5][0-9]"
_TIME: Final = rf"(?:(?:[01][0-9]|2[0-3]):{_MINUTES}(?::{_MINUTES}(?:\.[0-9]+)?)?|24:00(?::00)?)"
# A negative offset is 00:01 to 12:00 (U+2212 MINUS SIGN is accepted for it); a positive one is
# 00:00 to 14:00. The colon is optional, and so are the minutes.
_OFFSET: Final = rf"(?:(?:0[1-9]|1[01]):?{_MINUTES}|00:?(?:0[1-9]|[1-5][0-9])|12:?00|0[1-9]|1[0-2])"
_POSITIVE_OFFSET: Final = rf"(?:{_OFFSET}|00:?00|1[23]:?{_MINUTES}|14:?00|00|13|14)"
_TIMEZONE: Final = rf"(?:Z|[-\u2212]{_OFFSET}|\+{_POSITIVE_OFFSET})"
_DATETIME: Final = rf"{_YEAR_MONTH_DAY}T{_TIME}{_TIMEZONE}?"
_DATE_TIME: Final = rf"(?:{_DATE}|{_DATETIME})"
_CENTURY: Final = r"(?:-(?!00)[0-9]{2}|[0-9]{2})"

# --- Level 1 --------------------------------------------------------------------------------
_UA: Final = r"[?~%]"
_DATE_UA: Final = rf"{_DATE}{_UA}"
# Unspecified digits, level 1 forms: whole components from the right, or the last one or two
# digits of the year.
_L1X: Final = (
    rf"(?:-?[0-9]{{4}}-{_MONTH}-XX|-?[0-9]{{4}}-XX-XX|XXXX-XX-XX|-?[0-9]{{4}}-XX|XXXX-XX"
    r"|-?[0-9]{2}XX|-?[0-9]{3}X|XXXX)"
)
_L1Y: Final = r"Y-?[1-9][0-9]{4,}"
_L1S: Final = rf"{_YEAR}-2[1-4]"

# --- Level 2 --------------------------------------------------------------------------------
_Q: Final = rf"{_UA}?"
_UA_YEAR: Final = rf"{_UA}{_YEAR}"
_UA_YEAR_MONTH: Final = rf"{_Q}{_YEAR}{_Q}-{_Q}{_MONTH}{_Q}"
_UA_YEAR_MONTH_DAY: Final = (
    rf"{_Q}{_YEAR}{_Q}-(?:{_Q}{_M31}{_Q}-{_Q}{_D31}{_Q}"
    rf"|{_Q}{_M30}{_Q}-{_Q}{_D30}{_Q}|{_Q}02{_Q}-{_Q}{_D29}{_Q})"
)
# A qualified date needs a qualifier somewhere: the plain date is level 0, not this.
_UA_DATE: Final = rf"(?=.*{_UA})(?:{_UA_YEAR_MONTH_DAY}|{_UA_YEAR_MONTH}|{_UA_YEAR})"
_DX4: Final = r"-?[0-9X]{4}"
_MX: Final = r"(?:0[1-9X]|[1X][012X])"
_M31X: Final = r"(?:[0X][13578X]|[1X][02]|1X)"
_M30X: Final = r"(?:[0X][469]|11)"
_D29X: Final = r"(?:0[1-9X]|[12X][0-9X])"
_D30X: Final = rf"(?:{_D29X}|30)"
_D31X: Final = rf"(?:{_D30X}|3[1X])"
_MDX: Final = rf"(?:{_M31X}-{_D31X}|{_M30X}-{_D30X}|02-{_D29X})"
# An unspecified date needs an X somewhere, for the same reason.
_L2X: Final = rf"(?=.*X)(?:{_DX4}-{_MDX}|{_DX4}-{_MX}|{_DX4})"
_DECADE: Final = r"(?:-(?!000)[0-9]{3}|[0-9]{3})"
_SIGNIFICANT: Final = r"S[1-9]"
_L2Y: Final = rf"(?:Y-?[0-9]+E[0-9]+(?:{_SIGNIFICANT})?|{_L1Y}{_SIGNIFICANT}|{_YEAR}{_SIGNIFICANT})"
_L2S: Final = rf"{_YEAR}-(?:2[5-9]|3[0-9]|4[01])"

_SINGLE: Final = re.compile(
    rf"{_DATE_TIME}|{_CENTURY}"  # level 0
    rf"|{_DATE_UA}|{_L1X}|{_L1Y}|{_L1S}"  # level 1
    rf"|{_UA_DATE}|{_L2X}|{_L2Y}|{_L2S}|{_DECADE}{_Q}|{_CENTURY}{_Q}"  # level 2
)

# An interval's ends are both of level 0 or 1, or both of level 0 or 2: a level 1 qualified date
# (``1984?``) does not combine with a level 2 one (``2004-~06``). ``..`` is an open end and the
# empty string an unknown one; either may stand on both sides.
_OPEN_OR_UNKNOWN: Final = r"(?:\.\.)?"
_L1_END: Final = re.compile(rf"{_OPEN_OR_UNKNOWN}|{_DATE_UA}|{_DATE_TIME}")
_L2_END: Final = re.compile(rf"{_OPEN_OR_UNKNOWN}|{_UA_DATE}|{_L2X}|{_DATE_TIME}")

# A member of a set: a date (plain, qualified or unspecified), or a range between two plain
# dates of the same precision.
_MEMBER: Final = re.compile(
    rf"{_DATE}|{_UA_DATE}|{_L2X}"
    rf"|{_YEAR_MONTH_DAY}\.\.{_YEAR_MONTH_DAY}|{_YEAR_MONTH}\.\.{_YEAR_MONTH}|{_YEAR}\.\.{_YEAR}"
)
_BRACKETS: Final = {"[": "]", "{": "}"}
_OPEN: Final = ".."
_SEPARATOR: Final = re.compile(r" *, *")


def _is_interval(text: str) -> bool:
    start, _, end = text.partition("/")
    return any(
        side.fullmatch(start) is not None and side.fullmatch(end) is not None
        for side in (_L1_END, _L2_END)
    )


def _is_set(text: str) -> bool:
    """``[...]`` (one of) or ``{...}`` (all of), open at either end with ``..``."""
    if len(text) < 2 or _BRACKETS[text[0]] != text[-1]:
        return False
    body = text[1:-1]
    # The ``..`` of an open set belongs to the bracket, so a member is tried without it first;
    # nothing else begins or ends with two full stops.
    for prefix in (_OPEN, ""):
        for suffix in (_OPEN, ""):
            if not body.startswith(prefix) or not body.endswith(suffix):
                continue
            inner = body[len(prefix) : len(body) - len(suffix)]
            if len(prefix) + len(suffix) > len(body):
                continue
            if all(_MEMBER.fullmatch(member) for member in _SEPARATOR.split(inner)):
                return True
    return False


def is_edtf(value: object) -> bool:
    """True if ``value`` is a syntactically valid EDTF string (levels 0, 1 or 2).

    Exactly the strings edtf.js 4.12.0 ``parse`` accepts by default (module docstring).
    """
    if not isinstance(value, str) or not value:
        return False
    if value[0] in _BRACKETS:
        return _is_set(value)
    if "/" in value:
        return _is_interval(value)
    return _SINGLE.fullmatch(value) is not None
