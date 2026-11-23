"""``/api/profiles``: the UI Profiles of ``profiles/ui/`` as JSON (Phase 1 Step 1.18; spec §6.1).

- ``GET /api/profiles/{class}``: the profile of one record class as its YAML says, with a
  section's optional ``advanced`` filled in (``false``); other optional keys the file leaves out
  are left out here too. 404 ``LAE-PROF-001`` for a class without a profile.

The whole ``profiles/`` directory is checked with ``lae.profiles.validation`` when the API starts
(``warm_up``, called from the lifespan), so a broken profile stops the start instead of failing the
first request. Profiles are read once per process; a changed file needs a restart. Any signed-in
user may read them.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, status
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from lae.api.errors import ApiError
from lae.auth import CurrentUser, current_user
from lae.logging import bind_log_context
from lae.profiles.validation import PROFILE_SUFFIX, load_yaml, validate_profiles_dir

PROFILES_DIR: Final = Path(__file__).resolve().parents[4] / "profiles"
UI_DIR: Final = PROFILES_DIR / "ui"

UNKNOWN_PROFILE: Final = "LAE-PROF-001"
"""404: no UI Profile exists for this class; params ``class``."""

PROFILE_CODES: Final = (UNKNOWN_PROFILE,)
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


def warm_up() -> None:
    """Load and validate the profiles now (the API lifespan calls it)."""
    load_profiles()


router = APIRouter(prefix="/api/profiles", tags=["profiles"])


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
