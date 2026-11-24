"""A record's root ``_label``, derived from its primary name (Step 1.19a, ADR-0036).

The root ``_label`` of a draft is not edited by anyone: it is a pure function of the record's
names and the installation's label language (``Settings.label_language``), applied wherever a
draft becomes JSON-LD. On the server that is ``lae.crdt.rooms.Room.snapshot`` (draft
validation, publish and ``draft/diff`` all read it) and ``lae.api.records.new_record_data``;
in the browser ``frontend/src/crdt/derivedLabel.ts`` mirrors this module exactly. Nothing writes
the label into the CRDT, so it converges by construction, creates no undo entry and needs no
migration (contract ``docs/contracts/1.19a-derived-label.md``).

- A **usable name** is what ``lae.labels.extract.iter_names`` yields: an item of the root
  ``identified_by`` with ``type == "Name"`` and a string ``content`` holding a non-whitespace
  character. Names of embedded nodes never count.
- The **selection** is ``lae.labels.extract.primary_name``: the first primary name in the label
  language, else the first primary name, else the first name.
- The label is that name's ``content`` normalized by ``normalize_label``.
- Without a usable name the document is left as it is: its own ``_label`` (if any) is kept, and
  an absent one stays absent. The root ``_label`` is never an input.

Every function here is pure and takes the label language as a required argument; none reads the
settings. ``DEFAULT_LABEL_LANGUAGE`` is only the default of ``Settings.label_language`` and of
the ``label_language`` keyword of ``Room`` and ``InMemoryRoomManager``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from lae.labels.extract import iter_names, primary_name

LABEL_KEY: Final = "_label"
"""The root key the derived label is stored under."""

DEFAULT_LABEL_LANGUAGE: Final = "en"
"""Default of ``Settings.label_language`` and of the two constructors that take the language."""


def normalize_label(text: str) -> str:
    """Strip ``text`` and collapse every run of whitespace into one U+0020.

    Whitespace is exactly what ``str.isspace`` accepts (29 code points). Nothing else changes:
    no Unicode normalization, no case folding, no truncation.
    """
    return " ".join(text.split())


def derive_label(jsonld: Mapping[str, Any], label_language: str) -> str | None:
    """The label of ``jsonld``: its selected usable name, normalized; ``None`` without one."""
    name = primary_name(iter_names(jsonld), label_language)
    return None if name is None else normalize_label(name.content)


def with_derived_label(jsonld: dict[str, Any], label_language: str) -> dict[str, Any]:
    """``jsonld`` with its root ``_label`` set to the derived label.

    Returns ``jsonld`` itself when there is no usable name or the label is already that string;
    otherwise a shallow copy. The argument is never modified.
    """
    label = derive_label(jsonld, label_language)
    if label is None or jsonld.get(LABEL_KEY) == label:
        return jsonld
    return {**jsonld, LABEL_KEY: label}
