"""FastAPI dependencies shared by the API routers.

``locale_chain`` turns ``Accept-Language`` into the ordered list of language ranges that label
transclusion (``lae.labels.transclude``) matches with RFC 4647 basic filtering. The chain always
ends with ``en``, the language every configured string and most labels have (decision C6).
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import Request

from lae.labels.langtags import normalize_tag

FALLBACK_LOCALE: Final = "en"
"""The last entry of every locale chain."""

_QVALUE_RE: Final = re.compile(r"0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?")
"""RFC 9110 §12.4.2 ``qvalue``."""


def _quality(parameters: list[str]) -> float | None:
    """Return the ``q`` weight among an entry's parameters (1 if absent); ``None`` if malformed."""
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "q":
            value = value.strip()
            return float(value) if _QVALUE_RE.fullmatch(value) else None
    return 1.0


def parse_accept_language(header: str | None) -> list[str]:
    """Return the language ranges of an ``Accept-Language`` value, best first, without repeats.

    Entries are ordered by quality, ties in header order. Entries with ``q=0`` (not acceptable),
    the ``*`` wildcard (``default_label`` already covers "any language"), and malformed entries
    are dropped; ranges are put in canonical case (``en-us`` → ``en-US``).
    """
    if not header:
        return []
    weighted: list[tuple[float, int, str]] = []
    for position, entry in enumerate(header.split(",")):
        language_range, *parameters = entry.split(";")
        quality = _quality(parameters)
        tag = normalize_tag(language_range)
        if quality is None or quality <= 0 or tag is None:
            continue
        weighted.append((-quality, position, tag))
    chain: list[str] = []
    for _, _, tag in sorted(weighted):
        if tag not in chain:
            chain.append(tag)
    return chain


def locale_chain(request: Request) -> list[str]:
    """FastAPI dependency: the ``Accept-Language`` ranges, best first, ending with ``en``."""
    chain = parse_accept_language(request.headers.get("accept-language"))
    if not chain or chain[-1] != FALLBACK_LOCALE:
        chain.append(FALLBACK_LOCALE)
    return chain
