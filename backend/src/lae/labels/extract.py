"""Labels of a record, by language, for ``entity_labels`` (Phase 1, Step 1.5).

The labels are the record's own ``identified_by`` ``Name`` entries. Within one language a Name
classified as Primary Name (``aat:300404670``) wins over the others; otherwise the first Name in
document order is used. A Name's language is its first ``language`` entry (``language_tag``); a
Name without one is ``und``. The default label is, in order: the first English primary name, the
first primary name, the first Name, the root ``_label``, the record ``id``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final, NamedTuple

from lae.labels.langtags import UNDETERMINED, aat_id, language_tag, primary_language

PRIMARY_NAME_AAT_ID: Final = "300404670"
"""AAT *preferred terms* (Linked Art's Primary Name), as the digits after ``aat/``."""

ENGLISH: Final = "en"


class NameLabel(NamedTuple):
    """One usable ``Name`` of a record."""

    language: str
    content: str
    primary: bool


def _as_list(value: Any) -> list[Any]:
    """JSON-LD allows a single object where an array is usual; treat it as a one-item array."""
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def is_primary_name(name: Mapping[str, Any]) -> bool:
    """True if ``name`` is classified as Primary Name (``aat:300404670`` in any AAT spelling)."""
    return any(
        isinstance(entry, Mapping)
        and isinstance(entry.get("id"), str)
        and aat_id(entry["id"]) == PRIMARY_NAME_AAT_ID
        for entry in _as_list(name.get("classified_as"))
    )


def iter_names(jsonld: Mapping[str, Any]) -> Iterator[NameLabel]:
    """Yield the record's ``Name`` entries that have non-blank string ``content``, in order."""
    for name in _as_list(jsonld.get("identified_by")):
        if not isinstance(name, Mapping) or name.get("type") != "Name":
            continue
        content = name.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        languages = _as_list(name.get("language"))
        tag = language_tag(languages[0]) if languages else UNDETERMINED
        yield NameLabel(tag, content, is_primary_name(name))


def labels_from_record(jsonld: Mapping[str, Any]) -> tuple[dict[str, str], str]:
    """Return ``(labels_by_lang, default_label)`` for a Linked Art record."""
    names = list(iter_names(jsonld))
    labels: dict[str, str] = {}
    primary_languages: set[str] = set()
    for name in names:
        if name.primary and name.language not in primary_languages:
            labels[name.language] = name.content
            primary_languages.add(name.language)
        elif name.language not in labels:
            labels[name.language] = name.content
    return labels, _default_label(jsonld, names)


def _default_label(jsonld: Mapping[str, Any], names: list[NameLabel]) -> str:
    """English primary name, else any primary name, else first Name, else ``_label``, else id."""
    primaries = [name for name in names if name.primary]
    for name in primaries:
        if primary_language(name.language) == ENGLISH:
            return name.content
    if primaries:
        return primaries[0].content
    if names:
        return names[0].content
    label = jsonld.get("_label")
    if isinstance(label, str) and label.strip():
        return label
    record_id = jsonld.get("id")
    return record_id if isinstance(record_id, str) else ""
