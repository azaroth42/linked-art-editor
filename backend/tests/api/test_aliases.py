"""Record aliases: lookups (``lae.aliases``) and the redirects of ``lae.api.public`` (ADR-0030).

The application's ``base_uri`` is ``https://data.test/id/`` while the test client calls
``https://api.test``: an alias is looked up on the host of ``base_uri``, never on the request's.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.aliases import (
    BOOTSTRAP_SOURCE,
    local_alias_candidates,
    resolve_alias,
    resolve_aliases,
    resolve_local_alias,
    resolve_record_id,
)
from lae.db.models import RecordAlias
from lae.ids import node_iri
from tests.api.conftest import AAT, BASE_URI, id_of, insert_record, new_uri, titled_record
from tests.auth.conftest import Served

HOST = "data.test"


async def insert_alias(engine: AsyncEngine, alias_uri: str, record_id: UUID) -> None:
    async with AsyncSession(engine) as session:
        session.add(RecordAlias(alias_uri=alias_uri, record_id=record_id, source=BOOTSTRAP_SOURCE))
        await session.commit()


async def record_with_alias(engine: AsyncEngine, alias_uri: str) -> str:
    """Insert a published record and ``alias_uri`` for it; return the record URI."""
    uri = new_uri()
    await insert_record(engine, titled_record(uri, "Aliased", "Avec alias", f"{AAT}300033618"))
    await insert_alias(engine, alias_uri, id_of(uri))
    return uri


def test_candidates_use_the_host_of_base_uri_in_both_schemes() -> None:
    assert local_alias_candidates("/object/1", "", BASE_URI) == [
        f"https://{HOST}/object/1",
        f"http://{HOST}/object/1",
    ]
    assert local_alias_candidates("/o", "a=1", "http://museum.example:8080/id/") == [
        "http://museum.example:8080/o?a=1",
        "https://museum.example:8080/o?a=1",
        "http://museum.example:8080/o",
        "https://museum.example:8080/o",
    ]
    assert local_alias_candidates("no-slash", "", BASE_URI) == []
    assert local_alias_candidates("/x", "", "urn:not-a-host") == []


async def test_lookups(db_engine: AsyncEngine) -> None:
    alias = f"https://elsewhere.example/object/{uuid7()}"
    uri = await record_with_alias(db_engine, alias)
    record_id = id_of(uri)
    async with AsyncSession(db_engine) as session:
        assert await resolve_alias(session, alias) == record_id
        assert await resolve_alias(session, alias + "x") is None
        assert await resolve_aliases(session, [alias, "urn:x:none", alias]) == {alias: record_id}
        assert await resolve_aliases(session, []) == {}
        # A record answers to its URI, its node IRIs and its aliases.
        assert await resolve_record_id(session, uri, base_uri=BASE_URI) == record_id
        assert await resolve_record_id(session, node_iri(uri), base_uri=BASE_URI) == record_id
        assert await resolve_record_id(session, alias, base_uri=BASE_URI) == record_id
        assert await resolve_record_id(session, "urn:x:none", base_uri=BASE_URI) is None
        assert await resolve_local_alias(session, "/nothing", "", BASE_URI) is None


async def test_an_alias_is_deleted_with_its_record(db_engine: AsyncEngine) -> None:
    alias = f"https://elsewhere.example/object/{uuid7()}"
    uri = await record_with_alias(db_engine, alias)
    async with AsyncSession(db_engine) as session:
        await session.execute(text("DELETE FROM records WHERE id = :id"), {"id": id_of(uri)})
        await session.commit()
        assert await resolve_alias(session, alias) is None


@pytest.mark.parametrize("scheme", ["https", "http"])
async def test_an_alias_under_id_redirects_to_the_record(
    served: Served, db_engine: AsyncEngine, scheme: str
) -> None:
    slug = f"old-{uuid7()}"
    uri = await record_with_alias(db_engine, f"{scheme}://{HOST}/id/{slug}")
    response = await served.client.get(f"/id/{slug}")
    assert response.status_code == 301
    assert response.headers["location"] == uri
    assert response.content == b""


async def test_an_alias_anywhere_on_the_host_redirects(
    served: Served, db_engine: AsyncEngine
) -> None:
    path = f"/collection/objects/{uuid7()}/view.json"
    uri = await record_with_alias(db_engine, f"https://{HOST}{path}")
    response = await served.client.get(path, headers={"Accept": "application/ld+json"})
    assert response.status_code == 301
    assert response.headers["location"] == uri
    assert (await served.client.head(path)).headers["location"] == uri
    # A deeper path under /id/ is no route either.
    nested = f"/id/object/{uuid7()}"
    other = await record_with_alias(db_engine, f"https://{HOST}{nested}")
    assert (await served.client.get(nested)).headers["location"] == other


async def test_format_is_passed_on_and_an_alias_with_a_query_matches_exactly(
    served: Served, db_engine: AsyncEngine
) -> None:
    slug = f"old-{uuid7()}"
    uri = await record_with_alias(db_engine, f"https://{HOST}/id/{slug}")
    response = await served.client.get(f"/id/{slug}?format=ttl")
    assert response.headers["location"] == f"{uri}?format=ttl"
    # The route validates ?format= before anything is looked up, as it does for a record.
    assert (await served.client.get(f"/id/{slug}?format=bogus")).status_code == 422

    path = f"/lookup/{uuid7()}"
    queried = await record_with_alias(db_engine, f"https://{HOST}{path}?object=7&format=ttl")
    response = await served.client.get(f"{path}?object=7&format=ttl")
    assert (response.status_code, response.headers["location"]) == (301, queried)
    assert (await served.client.get(f"{path}?object=8")).status_code == 404


async def test_percent_encoding_is_kept_as_sent(served: Served, db_engine: AsyncEngine) -> None:
    path = f"/objects/{uuid7()}/a%20b"
    uri = await record_with_alias(db_engine, f"https://{HOST}{path}")
    response = await served.client.get(path)
    assert (response.status_code, response.headers["location"]) == (301, uri)


async def test_an_alias_on_another_host_is_not_served(
    served: Served, db_engine: AsyncEngine
) -> None:
    """The client's own host (api.test) is not the editor's: only ``base_uri``'s host counts."""
    path = f"/object/{uuid7()}"
    await record_with_alias(db_engine, f"https://api.test{path}")
    await record_with_alias(db_engine, f"https://elsewhere.example{path}x")
    assert (await served.client.get(path)).status_code == 404
    assert (await served.client.get(f"{path}x")).status_code == 404


async def test_what_is_not_an_alias_stays_not_found(served: Served, db_engine: AsyncEngine) -> None:
    response = await served.client.get(f"/id/{uuid7()}")
    assert response.status_code == 404
    assert response.json()["code"] == "LAE-REC-001"
    response = await served.client.get(f"/no/such/{uuid7()}")
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
    # Only GET and HEAD are redirected.
    path = f"/object/{uuid7()}"
    await record_with_alias(db_engine, f"https://{HOST}{path}")
    assert (await served.client.post(path)).status_code == 404
    # A route that exists keeps its own errors.
    assert (await served.client.post("/health/live")).status_code == 405


async def test_a_real_record_is_served_before_any_alias(
    served: Served, db_engine: AsyncEngine
) -> None:
    """An alias can never shadow a record's own URI."""
    uri = new_uri()
    await insert_record(db_engine, titled_record(uri, "Real", "Vrai", f"{AAT}300033618"))
    other = new_uri()
    await insert_record(db_engine, titled_record(other, "Other", "Autre", f"{AAT}300033618"))
    await insert_alias(db_engine, uri.replace(BASE_URI, f"https://{HOST}/id/"), id_of(other))
    response = await served.client.get(
        f"/id/{id_of(uri)}", headers={"Accept": "application/ld+json"}
    )
    assert response.status_code == 200
    assert response.json()["id"] == uri
