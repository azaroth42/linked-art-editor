"""The vocabulary bundle and term search under the label language (Step 1.19b, contract §5 tests
22-26).

A term with a ``label_override`` gets as its default label the label in the label language from
the override laid over its ``entity_labels`` labels, else the override's ``en``, else the row's
default label, else its URI; a term without one keeps the row's default label. The bundle's
``ETag`` and the in-process cache cover the label language.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import orjson
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.db.models import VocabularyTerm
from lae.vocab import service
from lae.vocab.loader import ImportSummary
from lae.vocab.service import (
    _resolved,
    build_bundle,
    bundle_etag,
    cached_bundle,
    clear_bundle_cache,
    registry_version,
)
from tests.api.conftest import auth_headers
from tests.auth.conftest import Served, serve_app
from tests.labels.relabel_support import run
from tests.vocab.conftest import AAT, Labels

BUNDLE = "/api/vocab/bundle"
GIVEN_NAME = f"{AAT}300404651"
MIDDLE_NAME = f"{AAT}300404654"
FAMILY_NAME = f"{AAT}300404652"
NAME_PARTS = "vocab:person_name_parts"
TERM_URI = "https://vocab.test/term"


def term(override: dict[str, str] | None) -> VocabularyTerm:
    return VocabularyTerm(
        vocabulary_id="vocab:test", uri=TERM_URI, position=0, label_override=override
    )


# --- 22: the four steps ------------------------------------------------------------------------

GIVEN = {"en": "Given Name"}
ROW = ({"en": "first names", "fr": "prénoms"}, "first names")


@pytest.mark.parametrize(
    ("override", "row", "language", "default_label"),
    [
        # 1. the label in the label language from the merged map: from the override ...
        ({"en": "Given Name", "fr": "Prénom"}, ROW, "fr", "Prénom"),
        # ... or from the row, which beats a curated override that is English only ...
        (GIVEN, ROW, "fr", "prénoms"),
        # ... with a regional tag, the exact tag first.
        (GIVEN, ({"fr-CA": "prénom (CA)"}, "x"), "fr", "prénom (CA)"),
        ({"en": "Given Name", "fr-CA": "prénom (CA)"}, ({"fr": "prénoms"}, "x"), "fr", "prénoms"),
        # Under en the override's en is the label in the label language: as before this step.
        (GIVEN, ROW, "en", "Given Name"),
        (GIVEN, None, "en", "Given Name"),
        # 2. else the override's en.
        (GIVEN, ({"en": "first names", "de": "Vornamen"}, "first names"), "fr", "Given Name"),
        (GIVEN, None, "fr", "Given Name"),
        # 3. else the row's default label (an override without en, written past the schema).
        ({"de": "Vorname"}, ({"en": "first names"}, "first names"), "fr", "first names"),
        # 4. else the URI.
        ({"de": "Vorname"}, None, "fr", TERM_URI),
        # A term without an override keeps the row's default label, whatever the language.
        (None, ROW, "fr", "first names"),
        ({}, ROW, "fr", "first names"),
        (None, None, "fr", TERM_URI),
    ],
)
def test_the_default_label_of_an_overridden_term(
    override: dict[str, str] | None,
    row: tuple[dict[str, str], str] | None,
    language: str,
    default_label: str,
) -> None:
    labels, chosen = _resolved(term(override), row, language)
    assert chosen == default_label
    assert labels == {**(row[0] if row else {}), **(override or {})}


# --- 23: under en nothing changes --------------------------------------------------------------


def resolved_before_this_step(
    term: VocabularyTerm, row: tuple[dict[str, str], str] | None
) -> tuple[dict[str, str], str]:
    """``lae.vocab.service._resolved`` as it was before Step 1.19b."""
    labels = dict(row[0]) if row is not None else {}
    override = term.label_override or {}
    labels.update(override)
    if "en" in override:
        return labels, override["en"]
    return labels, row[1] if row is not None else term.uri


async def seed_aat_rows(engine: AsyncEngine, labels: Labels) -> list[str]:
    """``entity_labels`` rows for the registry's terms, as the AAT seed (and a sidecar) write them:
    English for every other term, English and French for the rest; the others have no row."""
    async with AsyncSession(engine) as session:
        uris = list(
            (
                await session.execute(
                    text("SELECT DISTINCT uri FROM vocabulary_terms ORDER BY uri")
                )
            ).scalars()
        )
    for n, uri in enumerate(uris):
        if n % 3 == 0:
            await labels.put(uri, f"aat {n}", labels={"en": f"aat {n}"})
        elif n % 3 == 1:
            await labels.put(uri, f"aat {n}", labels={"en": f"aat {n}", "fr": f"aat fr {n}"})
    clear_bundle_cache()
    return uris


async def test_under_en_the_bundle_is_what_it_was(
    registry: ImportSummary,
    labels: Labels,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uris = await seed_aat_rows(db_engine, labels)
    assert len(uris) > 100
    async with AsyncSession(db_engine) as session:
        version = await registry_version(session)
        now = await build_bundle(session, version, "en")
        french = await build_bundle(session, version, "fr")
        monkeypatch.setattr(
            service,
            "_resolved",
            lambda term, row, label_language: resolved_before_this_step(term, row),
        )
        before = await build_bundle(session, version, "en")
    assert orjson.dumps(now.model_dump()) == orjson.dumps(before.model_dump())
    # The seed is not trivial: under fr the French AAT labels become defaults.
    assert now.model_dump() != french.model_dump()


# --- 24: ETag and cache --------------------------------------------------------------------------


def test_the_bundle_etag_covers_the_label_language() -> None:
    assert bundle_etag("v1", "en") != bundle_etag("v1", "fr")
    assert bundle_etag("v1", "fr") == bundle_etag("v1", "fr")
    assert bundle_etag("v2", "en") != bundle_etag("v1", "en")
    assert bundle_etag("v2", "fr") != bundle_etag("v1", "fr")
    assert bundle_etag("v1", "en").startswith('W/"vocab-') and bundle_etag("v1", "en")[-1] == '"'


async def test_the_bundle_cache_is_not_shared_between_label_languages(
    registry: ImportSummary, labels: Labels, db_engine: AsyncEngine
) -> None:
    await labels.put(GIVEN_NAME, "first names", labels={"en": "first names", "fr": "prénoms"})
    clear_bundle_cache()
    async with AsyncSession(db_engine) as session:
        english = await cached_bundle(session, "en")
        assert await cached_bundle(session, "en") is english  # reused
        french = await cached_bundle(session, "fr")
        assert french is not english
        assert (french.version, french.label_language) == (english.version, "fr")
        assert french.etag == bundle_etag(french.version, "fr") != english.etag
        assert await cached_bundle(session, "fr") is french
        back = await cached_bundle(session, "en")
    assert back.etag == english.etag and back.body == english.body
    assert default_of(english.bundle.model_dump(), GIVEN_NAME) == "Given Name"
    assert default_of(french.bundle.model_dump(), GIVEN_NAME) == "prénoms"


def default_of(bundle: dict[str, Any], uri: str) -> str:
    parts = next(v for v in bundle["vocabularies"] if v["id"] == NAME_PARTS)
    found: str = next(t["default_label"] for t in parts["terms"] if t["uri"] == uri)
    return found


# --- 25-26: through the API ---


@pytest.fixture
async def apps(db_url: str) -> AsyncIterator[dict[str, Served]]:
    """Two applications on one database, started with ``en`` and with ``fr``."""
    async with (
        serve_app(db_url, label_language="en") as english,
        serve_app(db_url, label_language="fr") as french,
    ):
        yield {"en": english, "fr": french}


@pytest.fixture
async def viewer(db_engine: AsyncEngine) -> dict[str, str]:
    return await auth_headers(db_engine, "Viewer")


async def test_a_bundle_cached_under_another_label_language_is_not_304(
    registry: ImportSummary, apps: dict[str, Served], viewer: dict[str, str]
) -> None:
    english = await apps["en"].client.get(BUNDLE, headers=viewer)
    assert english.status_code == 200
    etag = english.headers["etag"]
    same = await apps["en"].client.get(BUNDLE, headers={**viewer, "If-None-Match": etag})
    assert same.status_code == 304
    french = await apps["fr"].client.get(BUNDLE, headers={**viewer, "If-None-Match": etag})
    assert french.status_code == 200 and french.headers["etag"] != etag
    assert french.json()["version"] == english.json()["version"]
    again = await apps["fr"].client.get(
        BUNDLE, headers={**viewer, "If-None-Match": french.headers["etag"]}
    )
    assert again.status_code == 304


async def set_override(engine: AsyncEngine, uri: str, override: dict[str, str] | None) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE vocabulary_terms SET label_override = CAST(:o AS jsonb) WHERE uri = :u"),
            {"o": None if override is None else orjson.dumps(override).decode(), "u": uri},
        )
    clear_bundle_cache()


async def test_the_api_resolves_defaults_in_the_configured_language(
    registry: ImportSummary,
    apps: dict[str, Served],
    viewer: dict[str, str],
    labels: Labels,
    db_engine: AsyncEngine,
) -> None:
    await labels.put(GIVEN_NAME, "first names", labels={"en": "first names", "fr": "prénoms"})
    # An override without en (past the schema) lets the default show through the en fallback
    # of the locale chain in term search.
    await labels.put(MIDDLE_NAME, "middle names", labels={"fr": "deuxièmes prénoms"})
    await set_override(db_engine, MIDDLE_NAME, {"de": "Zweitname"})
    defaults = {}
    for language, app in apps.items():
        bundle = await app.client.get(BUNDLE, headers=viewer)
        assert bundle.status_code == 200
        terms = await app.client.get(
            f"/api/vocab/{NAME_PARTS}/terms", headers={**viewer, "Accept-Language": "ja"}
        )
        assert terms.status_code == 200
        by_uri = {option["uri"]: option["label"] for option in terms.json()}
        defaults[language] = (
            default_of(bundle.json(), GIVEN_NAME),
            default_of(bundle.json(), MIDDLE_NAME),
            by_uri[MIDDLE_NAME],
        )
    assert defaults == {
        "en": ("Given Name", "middle names", "middle names"),
        "fr": ("prénoms", "deuxièmes prénoms", "deuxièmes prénoms"),
    }


async def test_after_a_relabel_a_running_api_serves_the_new_defaults(
    registry: ImportSummary,
    apps: dict[str, Served],
    viewer: dict[str, str],
    labels: Labels,
    db_engine: AsyncEngine,
    db_url: str,
) -> None:
    await labels.put(FAMILY_NAME, "family names", labels={"en": "family names", "fr": "noms"})
    await set_override(db_engine, FAMILY_NAME, None)
    french = apps["fr"]
    first = await french.client.get(BUNDLE, headers=viewer)
    assert default_of(first.json(), FAMILY_NAME) == "family names"
    etag = first.headers["etag"]
    assert (
        await french.client.get(BUNDLE, headers={**viewer, "If-None-Match": etag})
    ).status_code == 304

    summary = await run(db_url, "fr")
    assert summary["registry_touched"] is True
    assert (await labels.get(FAMILY_NAME)).default_label == "noms"  # type: ignore[union-attr]

    after = await french.client.get(BUNDLE, headers={**viewer, "If-None-Match": etag})
    assert after.status_code == 200 and after.headers["etag"] != etag
    assert default_of(after.json(), FAMILY_NAME) == "noms"
