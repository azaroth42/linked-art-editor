"""Labels of a record, by language, for ``entity_labels`` (Phase 1, Step 1.5).

The labels are the record's own ``identified_by`` ``Name`` entries. Within one language a Name
classified as Primary Name (``aat:300404670``) wins over the others; otherwise the first Name in
document order is used. A Name's language is its first ``language`` entry (``language_tag``); a
Name without one is ``und``.

The default label is, in order: the content of ``primary_name`` (the first primary name in the
installation's label language, ``Settings.label_language``; else the first primary name; else the
first Name), the root ``_label``, the record ``id``. The language is a required argument: these
functions never read the settings (Step 1.19a; ``lae.labels.derive`` uses the same selection
for a record's derived root ``_label``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Final, NamedTuple

from lae.labels.langtags import UNDETERMINED, aat_id, language_tag, primary_language

PRIMARY_NAME_AAT_ID: Final = "300404670"
"""AAT *preferred terms* (Linked Art's Primary Name), as the digits after ``aat/``."""


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


def primary_name(names: Iterable[NameLabel], label_language: str) -> NameLabel | None:
    """The name a record is labelled by: the first primary name whose primary language is
    ``label_language``, else the first primary name, else the first name, else ``None``.

    ``label_language`` is compared as given (a lower-case primary language subtag such as
    ``en``, as ``Settings.label_language`` holds it); another string matches no name.
    """
    found = list(names)
    primaries = [name for name in found if name.primary]
    for name in primaries:
        if primary_language(name.language) == label_language:
            return name
    if primaries:
        return primaries[0]
    return found[0] if found else None


def labels_from_record(
    jsonld: Mapping[str, Any], label_language: str
) -> tuple[dict[str, str], str]:
    """Return ``(labels_by_lang, default_label)`` for a Linked Art record.

    ``labels_by_lang`` does not depend on ``label_language``; ``default_label`` prefers a primary
    name in it (``primary_name``).
    """
    names = list(iter_names(jsonld))
    labels: dict[str, str] = {}
    primary_languages: set[str] = set()
    for name in names:
        if name.primary and name.language not in primary_languages:
            labels[name.language] = name.content
            primary_languages.add(name.language)
        elif name.language not in labels:
            labels[name.language] = name.content
    return labels, _default_label(jsonld, names, label_language)


def _default_label(jsonld: Mapping[str, Any], names: list[NameLabel], label_language: str) -> str:
    """``primary_name``'s content, else the root ``_label``, else the record ``id``."""
    name = primary_name(names, label_language)
    if name is not None:
        return name.content
    label = jsonld.get("_label")
    if isinstance(label, str) and label.strip():
        return label
    record_id = jsonld.get("id")
    return record_id if isinstance(record_id, str) else ""
