"""BCP-47 language tags for Linked Art ``language`` references (Phase 1, Step 1.5).

A ``Name`` states its language as a reference to an AAT language concept, optionally with a
``notation`` such as ``"en"``. ``language_tag`` turns that into a BCP-47 tag: the notation if it is
a well-formed tag, else the tag of a common AAT language, else ``und`` (undetermined).

Sources for ``AAT_LANGUAGE_TAGS`` (data, not code): AAT ids and codes from
``linked-art/linked.art/scripts/language_key_map.md`` @ 3badf90, each id checked against
``linked.art/scripts/aat_labels.json`` @ f227e41 ("<Name> (language)") or
``linked-art/linkedart-parser/data/aat_dataset.csv`` @ 345ff92. The key map omits Spanish,
Japanese, Greek, Armenian, Basque, Albanian, Bokmål and Occitan; their ids come from the CSV and
their tags from ISO 639-1. Where the key map gives an ISO 639-2 code although a two-letter code
exists (Slovene ``slv``), the two-letter code is used, as BCP-47 requires.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

UNDETERMINED: Final = "und"
"""BCP-47 tag for a name whose language is not stated or not recognised."""

AAT_PREFIXES: Final = ("http://vocab.getty.edu/aat/", "https://vocab.getty.edu/aat/", "aat:")
"""Spellings of the AAT namespace accepted for concept URIs."""

AAT_LANGUAGE_TAGS: Final[Mapping[str, str]] = {
    "300387782": "af",  # Afrikaans
    "300387803": "sq",  # Albanian
    "300387843": "ar",  # Arabic
    "300387870": "hy",  # Armenian
    "300387955": "eu",  # Basque
    "300387967": "be",  # Belarusian
    "300387971": "bn",  # Bengali
    "300388034": "bg",  # Bulgarian
    "300388072": "ca",  # Catalan
    "300388113": "zh",  # Chinese
    "300388185": "hr",  # Croatian
    "300388191": "cs",  # Czech
    "300388204": "da",  # Danish
    "300388256": "nl",  # Dutch
    "300388277": "en",  # English
    "300388282": "eo",  # Esperanto
    "300388283": "et",  # Estonian
    "300388299": "fi",  # Finnish
    "300388306": "fr",  # French
    "300388327": "gl",  # Galician
    "300388343": "ka",  # Georgian
    "300388344": "de",  # German
    "300388401": "he",  # Hebrew
    "300388412": "hi",  # Hindi
    "300388449": "is",  # Icelandic
    "300388460": "id",  # Indonesian
    "300388474": "it",  # Italian
    "300388486": "ja",  # Japanese
    "300388633": "ko",  # Korean
    "300388693": "la",  # Latin
    "300388696": "lv",  # Latvian
    "300388723": "lt",  # Lithuanian
    "300388760": "mk",  # Macedonian
    "300388796": "mt",  # Maltese
    "300388992": "nn",  # Norwegian Nynorsk
    "300389109": "pl",  # Polish
    "300389115": "pt",  # Portuguese
    "300389157": "ro",  # Romanian
    "300389168": "ru",  # Russian
    "300389205": "sa",  # Sanskrit
    "300389247": "sr",  # Serbian
    "300389290": "sk",  # Slovak
    "300389291": "sl",  # Slovene
    "300389311": "es",  # Spanish
    "300389336": "sv",  # Swedish
    "300389365": "ta",  # Tamil
    "300389405": "th",  # Thai
    "300389410": "bo",  # Tibetan
    "300389470": "tr",  # Turkish
    "300389489": "uk",  # Ukrainian
    "300389502": "ur",  # Urdu
    "300389525": "vi",  # Vietnamese
    "300389555": "cy",  # Welsh
    "300389602": "yi",  # Yiddish
    "300389734": "el",  # Greek (AAT covers every period; BCP-47 "el" is the modern language)
    "300391418": "nb",  # Norwegian Bokmål
    "300391532": "oc",  # Occitan
}
"""AAT language concept id (the digits after ``aat/``) → BCP-47 tag."""

ISO_639_2_TO_1: Final[Mapping[str, str]] = {
    "afr": "af", "alb": "sq", "sqi": "sq", "ara": "ar", "arm": "hy", "hye": "hy", "baq": "eu",
    "eus": "eu", "bel": "be", "ben": "bn", "bul": "bg", "cat": "ca", "chi": "zh", "zho": "zh",
    "hrv": "hr", "cze": "cs", "ces": "cs", "dan": "da", "dut": "nl", "nld": "nl", "eng": "en",
    "epo": "eo", "est": "et", "fin": "fi", "fre": "fr", "fra": "fr", "glg": "gl", "geo": "ka",
    "kat": "ka", "ger": "de", "deu": "de", "heb": "he", "hin": "hi", "ice": "is", "isl": "is",
    "ind": "id", "ita": "it", "jpn": "ja", "kor": "ko", "lat": "la", "lav": "lv", "lit": "lt",
    "mac": "mk", "mkd": "mk", "mlt": "mt", "nno": "nn", "pol": "pl", "por": "pt", "rum": "ro",
    "ron": "ro", "rus": "ru", "san": "sa", "srp": "sr", "slo": "sk", "slk": "sk", "slv": "sl",
    "spa": "es", "swe": "sv", "tam": "ta", "tha": "th", "tib": "bo", "bod": "bo", "tur": "tr",
    "ukr": "uk", "urd": "ur", "vie": "vi", "wel": "cy", "cym": "cy", "yid": "yi", "gre": "el",
    "ell": "el", "nob": "nb", "oci": "oc",
}  # fmt: skip
"""ISO 639-2 (bibliographic and terminology) codes of the languages above → their ISO 639-1
code. BCP-47 uses the shortest code, and library data often carries the three-letter one."""

_TAG_RE: Final = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")
"""A well-formed tag in the RFC 4647 basic language-range syntax, without the ``*`` wildcard."""


def normalize_tag(tag: str) -> str | None:
    """Return ``tag`` in canonical BCP-47 form, or ``None`` if it is malformed.

    A three-letter ISO 639-2 code with a two-letter equivalent is shortened (``eng`` to ``en``).
    Case follows RFC 5646 §2.1.1: language lower case, four-letter script title case, two-letter
    region upper case; subtags after a singleton (extensions, ``x-`` private use) lower case.
    """
    tag = tag.strip()
    if not _TAG_RE.fullmatch(tag):
        return None
    language, *rest = tag.lower().split("-")
    subtags = [ISO_639_2_TO_1.get(language, language)]
    after_singleton = False
    for subtag in rest:
        if len(subtag) == 1:
            after_singleton = True
        elif not after_singleton and subtag.isalpha() and len(subtag) == 4:
            subtag = subtag.title()
        elif not after_singleton and subtag.isalpha() and len(subtag) == 2:
            subtag = subtag.upper()
        subtags.append(subtag)
    return "-".join(subtags)


def primary_language(tag: str) -> str:
    """Return the primary language subtag of ``tag``, lower case (``"en"`` for ``"en-GB"``)."""
    return tag.split("-", 1)[0].lower()


def aat_id(uri: str) -> str | None:
    """Return the numeric id of an AAT URI (``http``, ``https`` or ``aat:``); else ``None``."""
    for prefix in AAT_PREFIXES:
        if uri.startswith(prefix):
            local = uri[len(prefix) :]
            return local if local.isdigit() and local.isascii() else None
    return None


def aat_language_tag(uri: str) -> str | None:
    """Return the BCP-47 tag of a known AAT language concept URI; else ``None``."""
    concept = aat_id(uri)
    return None if concept is None else AAT_LANGUAGE_TAGS.get(concept)


def language_tag(language: Any) -> str:
    """Return the BCP-47 tag of one ``language`` entry: notation, else AAT id, else ``und``."""
    if not isinstance(language, Mapping):
        return UNDETERMINED
    notation = language.get("notation")
    if isinstance(notation, str) and (tag := normalize_tag(notation)) is not None:
        return tag
    uri = language.get("id")
    if isinstance(uri, str) and (tag := aat_language_tag(uri)) is not None:
        return tag
    return UNDETERMINED
