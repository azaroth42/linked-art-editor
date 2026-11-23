"""``GET /api/profiles/{class}`` and the startup check of ``profiles/`` (Phase 1 Step 1.18)."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.api.profiles import (
    PROFILE_CODES,
    PROFILES_DIR,
    ProfileConfigError,
    load_profiles,
)
from lae.profiles.validation import load_yaml
from tests.api.conftest import auth_headers
from tests.auth.conftest import Served, serve_app

ERRORS_JSON = Path(__file__).resolve().parents[3] / "frontend/public/locales/en/errors.json"


@pytest.fixture
async def served(db_url: str) -> AsyncIterator[Served]:
    async with serve_app(db_url) as running:
        yield running


@pytest.fixture
async def viewer(db_engine: AsyncEngine) -> dict[str, str]:
    return await auth_headers(db_engine, "Viewer")


async def test_a_profile_needs_a_signed_in_user(served: Served) -> None:
    response = await served.client.get("/api/profiles/HumanMadeObject")
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


@pytest.mark.parametrize("record_class", ["HumanMadeObject", "Person", "Place", "Type"])
async def test_a_profile_is_its_yaml_as_json(
    served: Served, viewer: dict[str, str], record_class: str
) -> None:
    response = await served.client.get(f"/api/profiles/{record_class}", headers=viewer)
    assert response.status_code == 200, response.text
    expected = load_yaml(PROFILES_DIR / "ui" / f"{record_class}.yaml")
    for section in expected["sections"]:
        section.setdefault("advanced", False)
    assert response.json() == expected


async def test_an_unknown_class_is_404(served: Served, viewer: dict[str, str]) -> None:
    response = await served.client.get("/api/profiles/Currency", headers=viewer)
    assert response.status_code == 404
    assert response.json() == {
        "code": "LAE-PROF-001",
        "path": "/api/profiles/Currency",
        "params": {"class": "Currency"},
    }


def test_an_invalid_profile_stops_the_start(tmp_path: Path) -> None:
    copy = tmp_path / "profiles"
    shutil.copytree(PROFILES_DIR, copy, ignore=shutil.ignore_patterns("schema", "shapes"))
    broken = copy / "ui" / "Place.yaml"
    broken.write_text(broken.read_text(encoding="utf-8").replace("widget: wkt", "widget: map"))
    with pytest.raises(ProfileConfigError, match="Place.yaml"):
        load_profiles(copy)


def test_every_profile_code_has_an_english_message() -> None:
    messages = json.loads(ERRORS_JSON.read_text(encoding="utf-8"))
    assert {key for key in messages if key.startswith("LAE-PROF-")} == set(PROFILE_CODES)
