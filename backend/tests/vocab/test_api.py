"""``GET /api/vocab/bundle`` (ETag, 304) and ``GET /api/vocab/{id}/terms`` (Step 2.1)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.api.vocab import VOCAB_CODES
from lae.vocab.loader import ImportSummary, load_from_yaml
from lae.vocab.membership import refresh_membership
from lae.vocab.service import clear_bundle_cache
from tests.api.conftest import auth_headers
from tests.auth.conftest import Served, serve_app
from tests.vocab.conftest import AAT, NATIONALITY, Labels, fresh_uri
from tests.vocab.test_loader import copy_seeds

BUNDLE = "/api/vocab/bundle"
ERRORS_JSON = Path(__file__).resolve().parents[3] / "frontend/public/locales/en/errors.json"


@pytest.fixture
async def served(db_url: str) -> AsyncIterator[Served]:
    async with serve_app(db_url) as running:
        yield running


@pytest.fixture
async def viewer(db_engine: AsyncEngine) -> dict[str, str]:
    return await auth_headers(db_engine, "Viewer")


def terms_path(vocabulary_id: str) -> str:
    return f"/api/vocab/{vocabulary_id}/terms"


async def test_the_bundle_needs_a_signed_in_user(served: Served, registry: ImportSummary) -> None:
    response = await served.client.get(BUNDLE)
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"
    response = await served.client.get(terms_path("vocab:gender"))
    assert response.status_code == 401


async def test_the_bundle_has_every_value_set_and_binding(
    served: Served, registry: ImportSummary, viewer: dict[str, str], labels: Labels
) -> None:
    # The bundle resolves labels from entity_labels, overridden by the curated label.
    await labels.put(
        f"{AAT}300404651", "first names", labels={"en": "first names", "fr": "prénoms"}
    )
    clear_bundle_cache()
    response = await served.client.get(BUNDLE, headers=viewer)
    assert response.status_code == 200, response.text
    assert response.headers["etag"].startswith('W/"vocab-')
    assert response.headers["cache-control"] == "private, no-cache"
    body = response.json()
    by_id = {v["id"]: v for v in body["vocabularies"]}
    assert len(by_id) == 12
    parts = by_id["vocab:person_name_parts"]
    assert parts["mode"] == "enumerated" and parts["enforcement"] == "extensible"
    given = parts["terms"][0]
    assert given["uri"] == f"{AAT}300404651"
    assert given["label"] == {"en": "Given Name", "fr": "prénoms"}
    assert given["default_label"] == "Given Name"
    accession = by_id["vocab:identifier_types"]["terms"][0]
    assert accession["flags"] == {"unique": True} and accession["is_default"]
    nationality = by_id["vocab:nationality"]
    assert nationality["mode"] == "rule" and nationality["rule"]["metatype_uris"] == [NATIONALITY]
    assert len(body["bindings"]) == 18
    person = [b for b in body["bindings"] if b["entity_class"] == "Person"]
    assert {b["vocabulary_id"] for b in person if b["property_path"] == "classified_as"} == {
        "vocab:gender",
        "vocab:nationality",
        "vocab:occupation",
    }
    assert all(b["enforcement"] is None for b in body["bindings"])
    statement = next(b for b in body["bindings"] if b["vocabulary_id"] == "vocab:statement_types")
    assert statement["effective_enforcement"] == "advisory"


async def test_if_none_match_gets_304_until_the_registry_changes(
    served: Served,
    registry: ImportSummary,
    viewer: dict[str, str],
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    first = await served.client.get(BUNDLE, headers=viewer)
    etag = first.headers["etag"]
    cached = await served.client.get(BUNDLE, headers={**viewer, "If-None-Match": etag})
    assert cached.status_code == 304 and cached.content == b""
    assert cached.headers["etag"] == etag
    stale = await served.client.get(BUNDLE, headers={**viewer, "If-None-Match": 'W/"other"'})
    assert stale.status_code == 200

    directory = copy_seeds(tmp_path, "gender")
    path = directory / "gender.yaml"
    path.write_text(path.read_text().replace("enforcement: extensible", "enforcement: strict"))
    async with AsyncSession(db_engine) as session:
        summary = await load_from_yaml(session, directory)
        await session.commit()
    assert summary.updated == ["vocab:gender"]
    changed = await served.client.get(BUNDLE, headers={**viewer, "If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    gender = next(v for v in changed.json()["vocabularies"] if v["id"] == "vocab:gender")
    assert gender["enforcement"] == "strict"


async def test_rule_terms_are_searched_by_prefix_most_used_first(
    served: Served,
    registry: ImportSummary,
    viewer: dict[str, str],
    db_engine: AsyncEngine,
    labels: Labels,
) -> None:
    word = "zq" + fresh_uri()[-8:]
    rare = await labels.put(fresh_uri(), f"{word} Rare", metatypes=[NATIONALITY], usage_count=1)
    common = await labels.put(
        fresh_uri(), f"{word} Common", metatypes=[NATIONALITY], usage_count=50
    )
    inner = await labels.put(
        fresh_uri(),
        f"Old {word}",
        labels={"en": f"Old {word}", "fr": f"Ancien {word}"},
        metatypes=[NATIONALITY],
        usage_count=1,
    )
    other_word = await labels.put(fresh_uri(), "Unrelated", metatypes=[NATIONALITY])
    french_only = await labels.put(
        fresh_uri(), "Plain", labels={"en": "Plain", "fr": f"{word}ois"}, metatypes=[NATIONALITY]
    )
    dutch = await labels.put(f"{AAT}300111175", "Dutch (culture or style)", usage_count=1000)
    async with AsyncSession(db_engine) as session:
        await refresh_membership(session, "vocab:nationality")
        await session.commit()

    response = await served.client.get(
        terms_path("vocab:nationality"), params={"q": word.upper()}, headers=viewer
    )
    assert response.status_code == 200, response.text
    found = response.json()
    # Most used first; ties by default label in code-point order ("Old ..." before "zq...").
    assert [t["uri"] for t in found] == [common, inner, rare, french_only]
    assert other_word not in [t["uri"] for t in found]
    assert found[0] == {
        "uri": common,
        "label": f"{word} Common",
        "scope_note": None,
        "deprecated": False,
        "pinned": False,
        "usage_count": 50,
        "source": "aat",
    }
    french = await served.client.get(
        terms_path("vocab:nationality"),
        params={"q": word, "limit": 3},
        headers={**viewer, "Accept-Language": "fr"},
    )
    assert [t["label"] for t in french.json()] == [
        f"{word} Common",
        f"Ancien {word}",
        f"{word} Rare",
    ]

    # Empty q: the pinned shortlist (with curated labels) first, then the most used members.
    shortlist = await served.client.get(
        terms_path("vocab:nationality"), params={"limit": 40}, headers=viewer
    )
    listed = shortlist.json()
    assert listed[0]["label"] == "American" and all(t["pinned"] for t in listed[:29])
    dutch_term = next(t for t in listed if t["uri"] == dutch)
    assert dutch_term["label"] == "Dutch" and dutch_term["usage_count"] == 1000
    assert [t["uri"] for t in listed[29:32]] == [common, inner, rare]
    assert not any(t["pinned"] for t in listed[29:])
    limited = await served.client.get(
        terms_path("vocab:nationality"), params={"limit": 3}, headers=viewer
    )
    assert [t["label"] for t in limited.json()] == ["American", "Austrian", "Australian"]


async def test_enumerated_terms_are_filtered_in_their_order(
    served: Served, registry: ImportSummary, viewer: dict[str, str]
) -> None:
    response = await served.client.get(
        terms_path("vocab:dimension_types"), params={"q": "th"}, headers=viewer
    )
    assert [t["label"] for t in response.json()] == ["Thickness"]
    everything = await served.client.get(terms_path("vocab:person_name_parts"), headers=viewer)
    assert [t["label"] for t in everything.json()] == [
        "Given Name",
        "Middle Name",
        "Family Name",
        "Prefix",
        "Suffix",
    ]
    assert all(t["pinned"] for t in everything.json())


async def test_an_unknown_value_set_is_404(
    served: Served, registry: ImportSummary, viewer: dict[str, str]
) -> None:
    response = await served.client.get(terms_path("vocab:nowhere"), headers=viewer)
    assert response.status_code == 404
    assert response.json() == {
        "code": "LAE-VOCAB-001",
        "path": "/api/vocab/vocab:nowhere/terms",
        "params": {"vocabulary_id": "vocab:nowhere"},
    }


def test_every_vocab_code_has_an_english_message() -> None:
    messages = json.loads(ERRORS_JSON.read_text(encoding="utf-8"))
    assert {key for key in messages if key.startswith("LAE-VOCAB-")} == set(VOCAB_CODES)
