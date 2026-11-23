"""``/api/profiles``: the UI Profiles of ``profiles/ui/`` as JSON (Phase 1 Step 1.18; spec §6.1).

- ``GET /api/profiles/{class}``: the profile of one record class as its YAML says, with a
  section's optional ``advanced`` filled in (``false``); other optional keys the file leaves out
  are left out here too. 404 ``LAE-PROF-001`` for a class without a profile.
- ``GET /api/profiles/schema/{class}`` (Step 1.19): the vendored Linked Art JSON Schema of
  ``profiles/schema/`` that validates records of the class (``lae.validation.schema_map``), so
  that the editor can run the same schema check in the browser on every change. The answer is
  ``{class, entry, schemas}``: ``entry`` is the ``$id`` of the entry-point schema and ``schemas``
  holds it and every file it refers to, byte-for-byte as vendored, keyed by ``$id``. The files
  declare 2020-12 but are read as Draft 7 (``profiles/schema/SOURCE.md``); so must the client
  be. ``Activity`` is answered with ``event.json``: only the whole record can tell a provenance
  activity, which the server check alone handles. 404 ``LAE-PROF-002`` for a class that no
  schema validates.

The whole ``profiles/`` directory is checked with ``lae.profiles.validation`` when the API starts
(``warm_up``, called from the lifespan), so a broken profile stops the start instead of failing the
first request. Profiles are read once per process; a changed file needs a restart. Any signed-in
user may read them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, status
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from lae.api.errors import ApiError
from lae.auth import CurrentUser, current_user
from lae.logging import bind_log_context
from lae.profiles.validation import PROFILE_SUFFIX, load_yaml, validate_profiles_dir
from lae.validation.schema_map import SCHEMA_BASE_URI, SCHEMA_BY_TYPE, SCHEMA_DIR

PROFILES_DIR: Final = Path(__file__).resolve().parents[4] / "profiles"
UI_DIR: Final = PROFILES_DIR / "ui"

UNKNOWN_PROFILE: Final = "LAE-PROF-001"
"""404: no UI Profile exists for this class; params ``class``."""

UNKNOWN_SCHEMA: Final = "LAE-PROF-002"
"""404: no vendored JSON Schema validates records of this class; params ``class``."""

PROFILE_CODES: Final = (UNKNOWN_PROFILE, UNKNOWN_SCHEMA)
"""Every code this router emits (English text in ``frontend/public/locales/en/errors.json``)."""

LangMap = dict[str, str]
Widget = Literal[
    "text",
    "langtext",
    "edtf",
    "entity_ref",
    "classification",
    "dimension",
    "identifier",
    "statement",
    "wkt",
    "production",
]


class ProfileSlot(BaseModel):
    """One editable field bound to a path (``profiles/README.md``, "UI Profiles")."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: LangMap
    help_text: LangMap | None = None
    path: str
    widget: Widget
    value_set: str | None = None
    allowed_classes: list[str] | None = None
    multiple: bool
    required: bool
    advanced: bool


class ProfileSection(BaseModel):
    """An ordered group of slots; ``advanced`` sections sit behind "Add details"."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: LangMap
    advanced: bool = False
    slots: list[ProfileSlot]


class GridColumn(BaseModel):
    """A column of the grid view (Phase 2)."""

    model_config = ConfigDict(extra="forbid")

    slot: str
    label: LangMap | None = None


class UiProfile(BaseModel):
    """The editing form of one record class."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    record_class: str = Field(alias="class")
    version: int
    sections: list[ProfileSection]
    grid_columns: list[GridColumn] | None = None


class RecordSchema(BaseModel):
    """The JSON Schema of one record class, with every schema file it refers to."""

    model_config = ConfigDict(populate_by_name=True)

    record_class: str = Field(alias="class")
    entry: str
    """``$id`` of the entry-point schema (``https://linked.art/api/1.0/schema/object.json``)."""
    schemas: dict[str, dict[str, Any]]
    """The entry point and every schema it refers to, transitively, keyed by ``$id``."""


class ProfileConfigError(RuntimeError):
    """``profiles/`` does not pass ``lae.profiles.validation``; the message lists every problem."""


@cache
def load_profiles(profiles_dir: Path = PROFILES_DIR) -> dict[str, UiProfile]:
    """Validate ``profiles_dir``, return its UI Profiles by class; else ``ProfileConfigError``."""
    report = validate_profiles_dir(profiles_dir)
    if not report.ok:
        raise ProfileConfigError("\n".join(report.errors))
    profiles: dict[str, UiProfile] = {}
    for path in sorted((profiles_dir / "ui").glob(f"*{PROFILE_SUFFIX}")):
        profiles[path.stem] = UiProfile.model_validate(load_yaml(path))
    return profiles


def _refs(value: Any) -> Iterator[str]:
    """Yield every ``$ref`` string anywhere in a schema."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                yield item
            else:
                yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


@cache
def record_schema(schema_name: str, schema_dir: Path = SCHEMA_DIR) -> dict[str, dict[str, Any]]:
    """Return the schema file ``schema_name`` and every file it refers to, keyed by ``$id``.

    A ``$ref`` is ``<file>#<pointer>`` relative to ``SCHEMA_BASE_URI`` or ``#<pointer>`` within
    the same file; the files are read as they are vendored.
    """
    found: dict[str, dict[str, Any]] = {}
    pending = [schema_name]
    while pending:
        name = pending.pop()
        uri = SCHEMA_BASE_URI + name
        if uri in found:
            continue
        found[uri] = json.loads((schema_dir / name).read_bytes())
        for ref in _refs(found[uri]):
            target = ref.split("#", 1)[0]
            if target.startswith(SCHEMA_BASE_URI):
                target = target.removeprefix(SCHEMA_BASE_URI)
            if target and SCHEMA_BASE_URI + target not in found:
                pending.append(target)
    return dict(sorted(found.items()))


def warm_up() -> None:
    """Load and validate the profiles now (the API lifespan calls it)."""
    load_profiles()


router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get(
    "/schema/{record_class}",
    responses={status.HTTP_404_NOT_FOUND: {"description": "LAE-PROF-002"}},
)
async def get_record_schema(
    user: Annotated[CurrentUser, Depends(current_user)],
    record_class: Annotated[str, PathParam(description="Record class, e.g. HumanMadeObject")],
) -> RecordSchema:
    """The Linked Art JSON Schema that validates records of ``record_class``."""
    bind_log_context(user_id=user.id)
    schema_name = SCHEMA_BY_TYPE.get(record_class)
    if schema_name is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, UNKNOWN_SCHEMA, {"class": record_class})
    return RecordSchema(
        record_class=record_class,
        entry=SCHEMA_BASE_URI + schema_name,
        schemas=record_schema(schema_name),
    )


@router.get(
    "/{record_class}",
    response_model_exclude_none=True,
    responses={status.HTTP_404_NOT_FOUND: {"description": "LAE-PROF-001"}},
)
async def get_profile(
    user: Annotated[CurrentUser, Depends(current_user)],
    record_class: Annotated[str, PathParam(description="Record class, e.g. HumanMadeObject")],
) -> UiProfile:
    """The UI Profile of ``record_class``."""
    bind_log_context(user_id=user.id)
    profile = load_profiles().get(record_class)
    if profile is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, UNKNOWN_PROFILE, {"class": record_class})
    return profile
