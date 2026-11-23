"""``GET /api/profiles/{class}``, the startup check of ``profiles/`` (Phase 1 Step 1.18), and
``GET /api/profiles/schema/{class}`` (Step 1.19)."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator
from referencing import Registry
from referencing.jsonschema import DRAFT7
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.api.profiles import (
    PROFILE_CODES,
    PROFILES_DIR,
    ProfileConfigError,
    load_profiles,
    record_schema,
)
from lae.corpus import CORPUS_DIR
from lae.profiles.validation import load_yaml
from lae.validation.schema import schema_validator
from lae.validation.schema_map import SCHEMA_BASE_URI, SCHEMA_DIR
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


async def test_a_record_schema_needs_a_signed_in_user(served: Served) -> None:
    response = await served.client.get("/api/profiles/schema/HumanMadeObject")
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


@pytest.mark.parametrize(
    ("record_class", "schema_name"),
    [("HumanMadeObject", "object.json"), ("Person", "person.json"), ("Material", "concept.json")],
)
async def test_a_record_schema_is_the_vendored_entry_point_and_what_it_refers_to(
    served: Served, viewer: dict[str, str], record_class: str, schema_name: str
) -> None:
    response = await served.client.get(f"/api/profiles/schema/{record_class}", headers=viewer)
    assert response.status_code == 200, response.text
    body = response.json()
    entry = SCHEMA_BASE_URI + schema_name
    assert body["class"] == record_class
    assert body["entry"] == entry
    # The vendored files as they are: the entry point and core.json, which it refers to.
    assert set(body["schemas"]) == {entry, SCHEMA_BASE_URI + "core.json"}
    for uri, schema in body["schemas"].items():
        assert schema == json.loads((SCHEMA_DIR / uri.removeprefix(SCHEMA_BASE_URI)).read_bytes())


def test_the_served_files_resolve_every_reference_of_the_entry_point() -> None:
    """In a registry by ``$id`` alone, the served files check a record as the server does."""
    schemas = record_schema("object.json")
    registry: Registry[Any] = Registry().with_resources(
        (uri, DRAFT7.create_resource(contents)) for uri, contents in schemas.items()
    )
    validator = Draft7Validator(schemas[SCHEMA_BASE_URI + "object.json"], registry=registry)
    record = json.loads((CORPUS_DIR / "linkedart" / "object-0.json").read_bytes())
    del record["_label"]
    record["identified_by"][0]["content"] = 1
    found = sorted((e.validator, list(e.absolute_path)) for e in validator.iter_errors(record))
    server = schema_validator("object.json").iter_errors(record)
    assert found == sorted((e.validator, list(e.absolute_path)) for e in server)
    assert found  # the two errors are found, not skipped for an unresolved reference


async def test_a_class_without_a_schema_is_404(served: Served, viewer: dict[str, str]) -> None:
    response = await served.client.get("/api/profiles/schema/Name", headers=viewer)
    assert response.status_code == 404
    assert response.json() == {
        "code": "LAE-PROF-002",
        "path": "/api/profiles/schema/Name",
        "params": {"class": "Name"},
    }
