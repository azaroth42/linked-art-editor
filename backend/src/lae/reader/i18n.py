"""The Public Reader's string catalogue (namespace ``reader``) and its page language.

The strings are the SPA's own: ``frontend/public/locales/<language>/reader.json``, where the
frontend's i18n tooling keeps every namespace (conventions §5, decision D10). The server reads
those files, as ``lae.validation.diagnostics`` reads ``errors.json``, so there is one copy of each
string. Keys are nested JSON objects addressed with dots (``section.names``). A message may use
plain ICU arguments only (``{name}``); ``tests/reader/test_i18n.py`` rejects any other ICU syntax,
so the server and ``i18next-icu`` format every message the same way.

The page language is the first range of the caller's locale chain (``lae.api.deps.locale_chain``)
that has a catalogue, by RFC 4647 lookup (``fr-CA`` falls back to ``fr``), else ``en``. Its
writing direction comes from CLDR (``babel``). A message missing from that language falls back
to English.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from babel import Locale, UnknownLocaleError

LOCALES_DIR: Final = Path(__file__).resolve().parents[4] / "frontend" / "public" / "locales"
"""``frontend/public/locales``: one directory per language, one JSON file per namespace."""

NAMESPACE: Final = "reader"
DEFAULT_LANGUAGE: Final = "en"

Direction = Literal["ltr", "rtl"]

ARGUMENT: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
"""A plain ICU argument, the only placeholder reader messages may use."""


class CatalogueError(RuntimeError):
    """The English reader catalogue is missing or malformed (a broken installation)."""


def catalogue_path(language: str) -> Path:
    """Return the path of the reader catalogue of ``language``."""
    return LOCALES_DIR / language / f"{NAMESPACE}.json"


def _flatten(tree: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    """Return the messages of a nested catalogue keyed by dotted path."""
    flat: dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(_flatten(value, path + "."))
        elif isinstance(value, str):
            flat[path] = value
        else:
            raise CatalogueError(f"{path}: a message must be a string")
    return flat


@lru_cache(maxsize=64)
def load_messages(language: str) -> Mapping[str, str]:
    """Return the reader messages of ``language`` keyed by dotted path ({} if it has none)."""
    try:
        tree = json.loads(catalogue_path(language).read_bytes())
    except FileNotFoundError:
        if language == DEFAULT_LANGUAGE:
            raise CatalogueError(f"{catalogue_path(language)} is missing") from None
        return {}
    if not isinstance(tree, dict):
        raise CatalogueError(f"{catalogue_path(language)}: not a JSON object")
    return _flatten(tree)


@lru_cache(maxsize=1)
def available_languages() -> tuple[str, ...]:
    """Return the languages with a reader catalogue, ``en`` always among them."""
    found = {path.parent.name for path in LOCALES_DIR.glob(f"*/{NAMESPACE}.json")}
    return tuple(sorted(found | {DEFAULT_LANGUAGE}))


def negotiate_language(chain: Sequence[str], available: Sequence[str]) -> str:
    """Return the first range of ``chain`` that ``available`` answers (RFC 4647 lookup).

    Each range is tried as it is and then with its last subtag removed, repeatedly
    (``zh-Hant-TW``, ``zh-Hant``, ``zh``); matching ignores case. Without a match, ``en``.
    """
    by_lower = {language.lower(): language for language in available}
    for language_range in chain:
        subtags = language_range.lower().split("-")
        while subtags:
            found = by_lower.get("-".join(subtags))
            if found is not None:
                return found
            subtags.pop()
            if subtags and len(subtags[-1]) == 1:  # never end on a singleton (RFC 4647 §3.4)
                subtags.pop()
    return DEFAULT_LANGUAGE


def babel_locale(language: str) -> Locale:
    """Return the CLDR locale of a BCP-47 tag, falling back to its language, then English."""
    for candidate in (language, language.split("-", 1)[0], DEFAULT_LANGUAGE):
        try:
            return Locale.parse(candidate, sep="-")
        except (ValueError, UnknownLocaleError):
            continue
    raise CatalogueError("CLDR data for English is missing")  # pragma: no cover


def direction(language: str) -> Direction:
    """Return the writing direction of ``language`` from CLDR."""
    return "rtl" if babel_locale(language).character_order == "right-to-left" else "ltr"


@dataclass(frozen=True)
class Catalogue:
    """The reader's messages in one language, with English behind them."""

    language: str
    messages: Mapping[str, str]
    fallback: Mapping[str, str]

    @property
    def direction(self) -> Direction:
        """Writing direction of the page language."""
        return direction(self.language)

    @property
    def locale(self) -> Locale:
        """CLDR locale for dates and numbers on the page."""
        return babel_locale(self.language)

    def has(self, key: str) -> bool:
        """True if ``key`` names a message."""
        return key in self.messages or key in self.fallback

    def t(self, key: str, /, **params: object) -> str:
        """Return message ``key`` with its ``{name}`` arguments filled from ``params``.

        Raises ``KeyError`` for an unknown key or a missing argument: both are bugs.
        """
        template = self.messages.get(key)
        if template is None:
            template = self.fallback[key]
        return ARGUMENT.sub(lambda match: str(params[match[1]]), template)


def catalogue_for(chain: Sequence[str]) -> Catalogue:
    """Return the catalogue of the page language negotiated from ``chain``."""
    language = negotiate_language(chain, available_languages())
    return Catalogue(language, load_messages(language), load_messages(DEFAULT_LANGUAGE))
