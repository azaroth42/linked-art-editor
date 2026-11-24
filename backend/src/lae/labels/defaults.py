"""The default label of an ``entity_labels`` row that has no record to take names from (Step 1.19b).

Rows of a sidecar source (the AAT seed of ``lae.bootstrap.aat`` in Phase 1; Phase 2 Step 2.14's
sidecar ingest) have labels by language but no record. Their ``default_label`` is the label in the
installation's label language (``Settings.label_language``), else the label in the source's own
language (``SOURCE_LANGUAGES``), else nothing: the caller keeps what it has. Contract
``docs/contracts/1.19b-relabel.md`` §2.1; ADR-0038.

The language is a required argument; this module reads no settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from lae.labels.transclude import match_label

SOURCE_LANGUAGES: Final[Mapping[str, str]] = {"aat": "en"}
"""``entity_labels.source`` → the language that source's own preferred labels are written in.

A fact about a dataset, not a preference of the installation: it is consulted only after the
label language found nothing.
"""


def default_label_from_labels(
    labels: Mapping[str, Any], label_language: str, source_language: str | None
) -> str | None:
    """Return the default label of a row with ``labels`` (tag → label) and no record.

    Only entries whose tag is a string and whose value is a string with a non-whitespace
    character count. In order: the label in ``label_language`` (``match_label``: the exact tag,
    else the first regional tag in tag order), else the label in ``source_language`` by the same
    matching, else ``None``. ``labels`` is not modified.
    """
    usable = {
        tag: value
        for tag, value in labels.items()
        if isinstance(tag, str) and isinstance(value, str) and value.strip()
    }
    found = match_label(usable, (label_language,))
    if found is None and source_language is not None:
        found = match_label(usable, (source_language,))
    return found
