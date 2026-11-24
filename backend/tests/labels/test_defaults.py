"""``lae.labels.defaults.default_label_from_labels`` (Step 1.19b, contract §5 test 1)."""

from __future__ import annotations

import copy
from typing import Any

from lae.labels.defaults import SOURCE_LANGUAGES, default_label_from_labels


def test_the_label_in_the_label_language_wins() -> None:
    labels = {"en": "paintings", "fr": "peintures"}
    assert default_label_from_labels(labels, "fr", "en") == "peintures"
    assert default_label_from_labels(labels, "en", "en") == "paintings"
    # The source language is consulted only after the label language found nothing.
    assert default_label_from_labels(labels, "fr", None) == "peintures"
    assert default_label_from_labels({"de": "Gemälde", "en": "paintings"}, "de", "en") == "Gemälde"


def test_a_regional_tag_matches_and_the_exact_tag_wins() -> None:
    assert default_label_from_labels({"fr-CA": "québécois"}, "fr", None) == "québécois"
    assert default_label_from_labels({"fr-CA": "québécois", "fr": "français"}, "fr", None) == (
        "français"
    )
    assert default_label_from_labels({"fr-CH": "suisse", "fr-CA": "québécois"}, "fr", None) == (
        "québécois"
    )
    # A regional tag of the source language too.
    assert default_label_from_labels({"en-GB": "colour"}, "fr", "en") == "colour"
    # Case does not matter; a tag that only shares a prefix does not match.
    assert default_label_from_labels({"FR": "majuscule"}, "fr", None) == "majuscule"
    assert default_label_from_labels({"fra": "trois lettres"}, "fr", None) is None


def test_the_source_language_is_the_fallback() -> None:
    assert default_label_from_labels({"en": "paintings"}, "fr", "en") == "paintings"
    assert default_label_from_labels({"en": "paintings"}, "fr", None) is None
    assert default_label_from_labels({"en": "paintings"}, "fr", SOURCE_LANGUAGES["aat"]) == (
        "paintings"
    )
    assert SOURCE_LANGUAGES == {"aat": "en"}


def test_no_usable_label_gives_none() -> None:
    assert default_label_from_labels({}, "fr", "en") is None
    assert default_label_from_labels({"fr": "  ", "en": "\t\n"}, "fr", "en") is None
    odd: dict[Any, Any] = {"fr": None, "en": 3, "de": ["x"], 7: "seven", "it": {"x": "y"}}
    assert default_label_from_labels(odd, "fr", "en") is None
    assert default_label_from_labels(odd, "it", None) is None
    assert default_label_from_labels({"de": "Gemälde", "nl": "schilderijen"}, "fr", None) is None
    # A blank label in the label language does not hide the source language's.
    assert default_label_from_labels({"fr": " ", "en": "paintings"}, "fr", "en") == "paintings"
    # Nor does a blank exact tag hide a usable regional one.
    assert default_label_from_labels({"fr": "", "fr-CA": "québécois"}, "fr", None) == "québécois"


def test_the_argument_is_not_modified() -> None:
    labels: dict[Any, Any] = {"fr": " ", "fr-CA": "québécois", "en": "paintings", "x": None}
    before = copy.deepcopy(labels)
    default_label_from_labels(labels, "fr", "en")
    default_label_from_labels(labels, "de", "en")
    default_label_from_labels(labels, "de", None)
    assert labels == before
    assert list(labels) == list(before)
