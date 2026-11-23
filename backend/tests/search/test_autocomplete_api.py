"""``GET /api/search/autocomplete`` against Postgres, with a scripted QLever (Step 1.13).

The real QLever runs in ``test_autocomplete_qlever.py``. Here ``FakeQLever`` plays the index, so
the tests can say exactly what it knows: what the endpoint asks it, how its answers merge with the
``entity_labels`` overlay, and how labels, context and errors come out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lae.db.models import EntityLabelStatus
from lae.search.autocomplete import MAX_QUERY_LENGTH, NOT_AT_START
from tests.api.conftest import auth_headers
from tests.search.conftest import (
    AAT,
    CRM,
    FakeQLever,
    date_cell,
    iri_cell,
    put_label,
    search_row,
    serve,
    set_watermark_time,
    word,
)

PATH = "/api/search/autocomplete"
BUILT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
"""When the (pretend) live index was built."""


@pytest.fixture
async def headers(db_engine: AsyncEngine) -> dict[str, str]:
    """A signed-in Viewer: autocomplete needs no permission beyond signing in."""
    return await auth_headers(db_engine, "Viewer")


@pytest.fixture
async def built_index(db_engine: AsyncEngine) -> AsyncIterator[datetime]:
    """``sync_state`` says the live index was built at ``BUILT``; removed afterwards."""
    await set_watermark_time(db_engine, BUILT.isoformat())
    yield BUILT
    await set_watermark_time(db_engine, ...)


async def test_anonymous_is_refused(db_url: str) -> None:
    async with serve(db_url, FakeQLever()) as client:
        response = await client.get(PATH, params={"q": "rem"})
    assert response.status_code == 401
    assert response.json()["code"] == "LAE-AUTH-002"


@pytest.mark.parametrize("q", [None, "", "r", "  r  ", "x" * (MAX_QUERY_LENGTH + 1)])
async def test_a_query_of_the_wrong_length_is_refused(
    db_url: str, headers: dict[str, str], q: str | None
) -> None:
    qlever = FakeQLever()
    async with serve(db_url, qlever) as client:
        response = await client.get(PATH, params={} if q is None else {"q": q}, headers=headers)
    assert response.status_code == 422
    assert response.json() == {
        "code": "LAE-SEARCH-001",
        "path": PATH,
        "params": {"min_length": 2, "max_length": MAX_QUERY_LENGTH},
    }
    assert qlever.queries == []


async def test_an_unknown_type_is_refused(db_url: str, headers: dict[str, str]) -> None:
    async with serve(db_url, FakeQLever()) as client:
        response = await client.get(
            PATH, params={"q": "rem", "types": "Person,Name"}, headers=headers
        )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "LAE-SEARCH-002"
    assert body["params"]["type"] == "Name"
    assert "Person" in body["params"]["allowed"]


async def test_results_have_labels_in_the_locale_chain_context_and_source(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str], built_index: datetime
) -> None:
    key = word()
    person = f"https://data.test/id/{key}-person"
    place = f"https://data.test/id/{key}-place"
    country = f"https://data.test/id/{key}-country"
    concept = f"{AAT}{key}"
    before = built_index - timedelta(days=1)
    await put_label(
        db_engine,
        person,
        "Person",
        f"{key} Rembrandt",
        labels={"en": f"{key} Rembrandt", "fr": f"{key} Rembrandt (fr)"},
        updated_at=before,
    )
    await put_label(db_engine, place, "Place", f"{key} Leiden", updated_at=before)
    await put_label(
        db_engine,
        country,
        "Place",
        "Netherlands",
        labels={"en": "Netherlands", "fr": "Pays-Bas"},
        updated_at=before,
    )
    await put_label(db_engine, concept, "Type", f"{key} paintings", source="aat", updated_at=before)
    qlever = FakeQLever(
        search_rows=[
            search_row(person, "E21_Person", 20, f"{key} Rembrandt"),
            search_row(place, "E53_Place", 18, f"{key} Leiden"),
            search_row(concept, "E55_Type", 21, f"{key} paintings"),
            # Known to QLever but without an entity_labels row: keeps the literal that matched.
            search_row(f"https://data.test/id/{key}-orphan", "E21_Person", 30, f"{key} Orphan"),
        ],
        context_rows=[
            [iri_cell(person), date_cell("1606-07-15T00:00:00Z"), None, None, None],
            [iri_cell(person), None, date_cell("1669-10-04T23:59:59Z"), None, None],
            [iri_cell(place), None, None, iri_cell(country), '"Nederland"@nl'],
            [iri_cell(concept), None, None, None, None],
        ],
    )
    async with serve(db_url, qlever) as client:
        response = await client.get(
            PATH,
            params={"q": key, "types": "Person,Place,Type"},
            headers={**headers, "Accept-Language": "fr-CH, fr;q=0.9"},
        )
    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "id": place,
            "type": "Place",
            "label": f"{key} Leiden",
            "context": "Pays-Bas",
            "source": "local",
        },
        {
            "id": person,
            "type": "Person",
            "label": f"{key} Rembrandt (fr)",
            "context": "1606–1669",
            "source": "local",
        },
        {
            "id": concept,
            "type": "Type",
            "label": f"{key} paintings",
            "context": None,
            "source": "aat",
        },
        {
            "id": f"https://data.test/id/{key}-orphan",
            "type": "Person",
            "label": f"{key} Orphan",
            "context": None,
            "source": "local",
        },
    ]
    [search, context] = qlever.queries
    assert f'ql:contains-word "{key}*"' in search
    assert f"<{CRM}E21_Person>" in search and f"<{CRM}E53_Place>" in search
    assert f"<{CRM}E74_Group>" not in search
    assert "LIMIT 20" in search
    assert f"<{person}>" in context and f"<{place}>" in context and f"<{concept}>" in context


async def test_the_locale_parameter_comes_before_accept_language(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str]
) -> None:
    key = word()
    uri = f"https://data.test/id/{key}"
    await put_label(db_engine, uri, "Group", f"{key} Guild", labels={"en": "Guild", "nl": "Gilde"})
    async with serve(db_url, FakeQLever()) as client:
        response = await client.get(
            PATH,
            params={"q": key, "locale": "nl"},
            headers={**headers, "Accept-Language": "en"},
        )
    assert [result["label"] for result in response.json()] == ["Gilde"]


async def test_the_overlay_adds_what_was_published_after_the_index_was_built(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str], built_index: datetime
) -> None:
    """Spike S1: QLever's prefix search never sees updated literals, so Postgres supplies them."""
    key = word()
    fresh = f"https://data.test/id/{key}-fresh"
    renamed = f"https://data.test/id/{key}-renamed"
    indexed = f"https://data.test/id/{key}-indexed"
    after = built_index + timedelta(minutes=5)
    await put_label(db_engine, fresh, "Person", f"{key} Newperson", updated_at=after)
    # A word of the label (not its start) matches too, ranked after labels that start with q.
    await put_label(db_engine, renamed, "Person", f"Saskia {key}", updated_at=after)
    # Already in the index: QLever finds it if it matches, so Postgres does not repeat it.
    await put_label(
        db_engine, indexed, "Person", f"{key} Old", updated_at=built_index - timedelta(days=1)
    )
    # Drafts and other classes stay out.
    await put_label(
        db_engine,
        f"https://data.test/id/{key}-draft",
        "Person",
        f"{key} Draft",
        status=EntityLabelStatus.DRAFT,
        updated_at=after,
    )
    await put_label(
        db_engine, f"https://data.test/id/{key}-group", "Group", f"{key} Group", updated_at=after
    )
    qlever = FakeQLever(
        context_rows=[[iri_cell(fresh), date_cell("1901-01-01T00:00:00Z"), None, None, None]]
    )
    async with serve(db_url, qlever) as client:
        response = await client.get(
            PATH, params={"q": key.upper(), "types": "Person"}, headers=headers
        )
    assert response.status_code == 200, response.text
    assert [(r["id"], r["context"]) for r in response.json()] == [
        (fresh, "1901–"),
        (renamed, None),
    ]
    # The context of an overlay hit comes from QLever: its triples are there, only text is not.
    assert f"<{fresh}>" in qlever.queries[-1]


async def test_a_hit_from_both_sources_is_listed_once_in_rank_order(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str], built_index: datetime
) -> None:
    key = word()
    both = f"https://data.test/id/{key}-both"
    other = f"https://data.test/id/{key}-other"
    await put_label(
        db_engine, both, "Person", f"{key}", updated_at=built_index + timedelta(seconds=1)
    )
    await put_label(db_engine, other, "Person", f"{key} Other", updated_at=built_index)
    qlever = FakeQLever(
        search_rows=[
            search_row(other, "E21_Person", NOT_AT_START + 40, f"about {key}"),
            search_row(both, "E21_Person", len(key), key),
        ]
    )
    async with serve(db_url, qlever) as client:
        response = await client.get(PATH, params={"q": key, "limit": 5}, headers=headers)
    assert [r["id"] for r in response.json()] == [both, other]


async def test_limit(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str], built_index: datetime
) -> None:
    key = word()
    after = built_index + timedelta(minutes=1)
    for n in range(5):
        await put_label(
            db_engine,
            f"https://data.test/id/{key}-{n}",
            "Person",
            f"{key}{'x' * n}",
            updated_at=after,
        )
    async with serve(db_url, FakeQLever()) as client:
        response = await client.get(PATH, params={"q": key, "limit": 3}, headers=headers)
        too_many = await client.get(PATH, params={"q": key, "limit": 101}, headers=headers)
    assert [r["label"] for r in response.json()] == [key, f"{key}x", f"{key}xx"]
    assert too_many.status_code == 422


async def test_like_wildcards_in_q_are_literal(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str]
) -> None:
    key = word()
    await put_label(db_engine, f"https://data.test/id/{key}-pct", "Type", f"{key}%off")
    await put_label(db_engine, f"https://data.test/id/{key}-x", "Type", f"{key}xoff")
    async with serve(db_url, FakeQLever()) as client:
        percent = await client.get(PATH, params={"q": f"{key}%"}, headers=headers)
        underscore = await client.get(PATH, params={"q": f"{key}_off"}, headers=headers)
    assert [r["label"] for r in percent.json()] == [f"{key}%off"]
    assert underscore.json() == []


async def test_without_qlever_every_published_label_is_searched(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str], built_index: datetime
) -> None:
    """No index configured: nothing is known to be indexed, so the watermark time is ignored."""
    key = word()
    uri = f"https://data.test/id/{key}"
    await put_label(
        db_engine, uri, "Place", f"{key} Amsterdam", updated_at=built_index - timedelta(days=9)
    )
    async with serve(db_url) as client:
        response = await client.get(PATH, params={"q": key}, headers=headers)
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": uri,
            "type": "Place",
            "label": f"{key} Amsterdam",
            "context": None,
            "source": "local",
        }
    ]


async def test_without_a_recorded_watermark_time_every_published_label_is_searched(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str]
) -> None:
    key = word()
    uri = f"https://data.test/id/{key}"
    await put_label(db_engine, uri, "Place", f"{key} Haarlem", updated_at=BUILT - timedelta(days=9))
    for unknown in (None, ...):
        await set_watermark_time(db_engine, unknown)
        async with serve(db_url, FakeQLever()) as client:
            response = await client.get(PATH, params={"q": key}, headers=headers)
        assert [r["id"] for r in response.json()] == [uri]


async def test_a_query_without_words_skips_qlever(
    db_url: str, db_engine: AsyncEngine, headers: dict[str, str]
) -> None:
    qlever = FakeQLever()
    async with serve(db_url, qlever) as client:
        response = await client.get(PATH, params={"q": "--"}, headers=headers)
    assert response.status_code == 200
    assert qlever.searches == []


async def test_a_failing_index_is_503(db_url: str, headers: dict[str, str]) -> None:
    async with serve(db_url, FakeQLever(fail=True)) as client:
        response = await client.get(PATH, params={"q": "rem"}, headers=headers)
    assert response.status_code == 503
    assert response.json() == {"code": "LAE-SEARCH-003", "path": PATH, "params": {}}
