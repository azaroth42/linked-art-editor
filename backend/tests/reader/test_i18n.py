"""The reader catalogue (``frontend/public/locales/<lang>/reader.json``) and page language."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from lae.reader import i18n, view
from lae.reader.i18n import (
    ARGUMENT,
    Catalogue,
    CatalogueError,
    catalogue_for,
    direction,
    load_messages,
    negotiate_language,
)
from lae.reader.render import TEMPLATES_DIR

TEMPLATE = (TEMPLATES_DIR / "record.html").read_text()
VIEW_SOURCE = Path(view.__file__).read_text()
LITERAL_KEY = re.compile(r"""\bt\(\s*["']([a-z_]+\.[a-z_.]+)["']""")
"""``t("key")`` in the template or the view."""

KEY_ARGUMENT = re.compile(r"""["']([a-z_]+\.[a-z_]+)["']""")
"""A key passed on in the view (``first_label(node, "name.untyped")``)."""


def used_keys() -> set[str]:
    """Every key the template or the view names literally."""
    found = set(LITERAL_KEY.findall(TEMPLATE)) | set(LITERAL_KEY.findall(VIEW_SOURCE))
    return found | {key for key in KEY_ARGUMENT.findall(VIEW_SOURCE) if key in english()}


DYNAMIC_FAMILIES = {
    "event": set(view.EVENTS),  # t(f"event.{key}") for every key of EVENTS
    "class": None,  # t(f"class.{type}") when the catalogue has it, else the type
    "property": None,  # t(f"property.{key}") when the catalogue has it, else the key
    "date": {"from", "until"},  # t(f"date.{period.kind}") for the open-ended kinds
}


def english() -> dict[str, str]:
    return dict(load_messages("en"))


def test_english_catalogue_is_the_frontend_file() -> None:
    assert i18n.catalogue_path("en") == (
        Path(__file__).resolve().parents[3]
        / "frontend"
        / "public"
        / "locales"
        / "en"
        / "reader.json"
    )
    messages = english()
    assert messages["site.name"] == "Laertes"
    assert messages["site.page_title"] == "{title} – Laertes"


@pytest.mark.parametrize("key", sorted(load_messages("en")))
def test_messages_use_plain_arguments_only(key: str) -> None:
    """Plain ``{name}`` arguments are all the server formats; ICU would treat anything else
    (plural, select, quoting apostrophes) differently, so the catalogue must not use it."""
    message = english()[key]
    rest = ARGUMENT.sub("", message)
    assert "{" not in rest and "}" not in rest, message
    assert "'" not in message, f"{message!r}: use ’ (U+2019); ' quotes in ICU"
    assert message.strip() == message and message


def test_every_literal_key_exists() -> None:
    used = set(LITERAL_KEY.findall(TEMPLATE)) | set(LITERAL_KEY.findall(VIEW_SOURCE))
    assert len(used) > 10, "t() calls not found"
    missing = used - set(english())
    assert not missing


def test_every_message_is_used() -> None:
    used = used_keys()
    for key in english():
        family, _, name = key.partition(".")
        allowed = DYNAMIC_FAMILIES.get(family, set())
        if key in used or allowed is None or name in (allowed or set()):
            continue
        pytest.fail(f"{key} is not used by the reader")
    for event in view.EVENTS:
        assert f"event.{event}" in english()


def test_t_fills_arguments_and_rejects_missing_ones() -> None:
    catalogue = catalogue_for(["en"])
    assert catalogue.t("site.page_title", title="A {b}") == "A {b} – Laertes"
    with pytest.raises(KeyError):
        catalogue.t("site.page_title")
    with pytest.raises(KeyError):
        catalogue.t("no.such.key")


@pytest.mark.parametrize(
    ("chain", "available", "expected"),
    [
        (["en"], ["en"], "en"),
        (["fr", "en"], ["en"], "en"),
        (["fr", "en"], ["en", "fr"], "fr"),
        (["fr-CA", "en"], ["en", "fr"], "fr"),
        (["FR-ca"], ["en", "fr-CA"], "fr-CA"),
        (["zh-Hant-TW", "en"], ["en", "zh-Hant"], "zh-Hant"),
        (["de-x-private", "en"], ["en", "de"], "de"),
        (["ar", "fr", "en"], ["en", "fr"], "fr"),
        ([], ["en", "fr"], "en"),
    ],
)
def test_negotiate_language(chain: list[str], available: list[str], expected: str) -> None:
    assert negotiate_language(chain, available) == expected


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "ltr"),
        ("fr-CA", "ltr"),
        ("ar", "rtl"),
        ("he", "rtl"),
        ("fa-IR", "rtl"),
        ("xx", "ltr"),
    ],
)
def test_direction(language: str, expected: str) -> None:
    assert direction(language) == expected


@pytest.fixture
def locales(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A locales directory with the real ``en`` reader strings, for adding languages."""
    (tmp_path / "en").mkdir()
    shutil.copy(i18n.catalogue_path("en"), tmp_path / "en" / "reader.json")
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.load_messages.cache_clear()
    i18n.available_languages.cache_clear()
    yield tmp_path
    i18n.load_messages.cache_clear()
    i18n.available_languages.cache_clear()


def test_another_language_falls_back_to_english_per_message(locales: Path) -> None:
    (locales / "ar").mkdir()
    (locales / "ar" / "reader.json").write_text(json.dumps({"section": {"names": "الأسماء"}}))
    catalogue = catalogue_for(["ar-EG", "en"])
    assert isinstance(catalogue, Catalogue)
    assert (catalogue.language, catalogue.direction) == ("ar", "rtl")
    assert catalogue.t("section.names") == "الأسماء"
    assert catalogue.t("site.name") == "Laertes"
    assert catalogue.locale.language == "ar"


def test_missing_english_catalogue_is_an_error(locales: Path) -> None:
    (locales / "en" / "reader.json").unlink()
    with pytest.raises(CatalogueError):
        load_messages("en")


def test_non_string_message_is_an_error(locales: Path) -> None:
    (locales / "en" / "reader.json").write_text(json.dumps({"site": {"name": 3}}))
    with pytest.raises(CatalogueError):
        load_messages("en")
