"""``/id/{uuid}`` content negotiation and ``/id/{uuid}/node/{nid}`` (Phase 1, Step 1.6).

The invariant checked throughout: no public body (JSON-LD, Turtle, HTML, current or
``?version=N``) contains an internal node IRI or a derived (skolemized) node IRI. Both start with
``<record_uri>/node/``; the stored test records have such ids on every embedded node. The HTML
is the Public Reader (Step 1.21); ``tests/reader`` tests its parts.
"""

from __future__ import annotations

import json
import re
from email.utils import format_datetime
from typing import Any

import httpx
import pyoxigraph as ox
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid4, uuid7

from lae.api.public import Representation, entity_tag, if_none_match, negotiate
from lae.corpus import CORPUS_DIR, corpus_files
from lae.db.models import RecordStatus
from lae.ids import NODE_PATH, derived_node_iri
from tests.api.conftest import (
    BASE_URI,
    PROFILE_CONTENT_TYPE,
    a_while_ago,
    corpus_record,
    id_of,
    insert_labels,
    insert_record,
    insert_revision,
    new_uri,
    titled_record,
)
from tests.auth.conftest import (
    FakeOIDCProvider,
    Served,
    bearer,
    login,
    serve_app,
    unique_subject,
)

HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
"""What a browser sends."""

READER_SCRIPT = '<script type="module" src="/reader/assets/reader.js"></script>'

SPA_ASSET = re.compile(r"(?<!/reader)/assets/|/src/|\.tsx|index-[\w-]+\.js|/locales/|/@vite/")
"""What the SPA's build (``/assets/index-*.js``) or dev server (``/src/main.tsx``) serves."""

CRM = "http://www.cidoc-crm.org/cidoc-crm/"


LOCAL_NODE_IRI = re.compile(re.escape(BASE_URI) + r"[^/\s\"<>]+" + re.escape(NODE_PATH))
"""The start of any internal or derived node IRI of any local record."""


def assert_public(response: httpx.Response, uri: str) -> None:
    """The body is non-empty and contains no node IRI of the record or any other local record."""
    assert response.status_code == 200, response.text
    assert response.content
    assert node_path(uri) not in response.text
    assert not LOCAL_NODE_IRI.search(response.text)


def node_path(uri: str) -> str:
    return uri + NODE_PATH


async def published(engine: AsyncEngine, **kwargs: Any) -> tuple[str, str, dict[str, Any]]:
    """Insert a published titled record and a label row for its classification.

    Returns ``(record URI, classification URI, stored JSON-LD)``.
    """
    uri, type_uri = new_uri(), f"https://vocab.test/{uuid7()}"
    data = titled_record(uri, "Night Scene", "Scène de nuit", type_uri)
    assert data["identified_by"][0]["id"].startswith(node_path(uri))
    await insert_record(engine, data, **kwargs)
    await insert_labels(engine, type_uri, {"en": "paintings", "fr": "peintures"}, "paintings")
    return uri, type_uri, data


def path_of(uri: str) -> str:
    return f"/id/{id_of(uri)}"


def turtle_graph(text: str) -> list[ox.Triple]:
    return [quad.triple for quad in ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)]


# --- negotiation (unit) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        (None, Representation.HTML),
        ("", Representation.HTML),
        ("*/*", Representation.HTML),
        (HTML_ACCEPT, Representation.HTML),
        ("text/html", Representation.HTML),
        ("application/ld+json", Representation.JSONLD),
        (PROFILE_CONTENT_TYPE, Representation.JSONLD),
        ('application/ld+json; profile="https://linked.art/ns/v1/linked-art.json"', "jsonld"),
        ("application/json", Representation.JSONLD),
        ("APPLICATION/LD+JSON", Representation.JSONLD),
        ("application/*", Representation.JSONLD),
        ("text/turtle", Representation.TURTLE),
        ("text/turtle;q=0.9, application/ld+json;q=0.5", Representation.TURTLE),
        ("application/ld+json, text/turtle", Representation.JSONLD),
        ("text/html;q=0.1, text/turtle", Representation.TURTLE),
        ("application/ld+json, */*;q=0.1", Representation.JSONLD),
        ("text/turtle, */*", Representation.TURTLE),
        ("text/*", Representation.HTML),
        ("image/png", Representation.HTML),
        ("text/html;q=0, text/turtle;q=0.1", Representation.TURTLE),
        ("application/ld+json;q=0, */*", Representation.HTML),
        ("text/turtle;q=bad, application/ld+json", Representation.JSONLD),
        ("garbage", Representation.HTML),
    ],
)
def test_negotiate(accept: str | None, expected: str) -> None:
    assert negotiate(accept) == expected


def test_entity_tag_and_if_none_match() -> None:
    etag = entity_tag("abc123", Representation.TURTLE)
    assert etag == 'W/"abc123-ttl"'
    assert if_none_match('W/"abc123-ttl"', etag)
    assert if_none_match('"abc123-ttl"', etag)
    assert if_none_match('"other", W/"abc123-ttl"', etag)
    assert if_none_match("*", etag)
    assert not if_none_match('W/"abc123-jsonld"', etag)
    assert not if_none_match(None, etag)
    # An etag value that may not stand between quotes is hashed.
    assert entity_tag('a "quoted" tag', Representation.HTML).startswith('W/"')
    assert entity_tag('a "quoted" tag', Representation.HTML).count('"') == 2


# --- media types ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "accept",
    [
        PROFILE_CONTENT_TYPE,
        "application/ld+json",
        "application/json",
        'application/ld+json;profile="https://example.org/other-profile"',
    ],
)
async def test_jsonld(served: Served, db_engine: AsyncEngine, accept: str) -> None:
    uri, type_uri, _ = await published(db_engine)
    response = await served.client.get(
        path_of(uri), headers={"Accept": accept, "Accept-Language": "fr"}
    )
    assert_public(response, uri)
    assert response.headers["content-type"] == PROFILE_CONTENT_TYPE
    body = response.json()
    assert list(body)[:4] == ["@context", "id", "type", "_label"]
    assert body["id"] == uri
    names = body["identified_by"]
    assert [name["content"] for name in names] == ["Night Scene", "Scène de nuit"]
    assert all("id" not in name for name in names)
    assert "id" not in body["produced_by"] and "id" not in body["produced_by"]["timespan"]
    # References keep their ids; labels are transcluded in the caller's language.
    assert body["classified_as"] == [{"id": type_uri, "type": "Type", "_label": "peintures"}]


async def test_turtle(served: Served, db_engine: AsyncEngine) -> None:
    uri, type_uri, _ = await published(db_engine)
    response = await served.client.get(path_of(uri), headers={"Accept": "text/turtle"})
    assert_public(response, uri)
    assert response.headers["content-type"] == "text/turtle; charset=utf-8"
    triples = turtle_graph(response.text)
    record = ox.NamedNode(uri)
    assert (
        ox.Triple(
            record,
            ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
            ox.NamedNode(f"{CRM}E22_Human-Made_Object"),
        )
        in triples
    )
    assert ox.Triple(record, ox.NamedNode(f"{CRM}P2_has_type"), ox.NamedNode(type_uri)) in triples
    assert (
        ox.Triple(
            ox.NamedNode(type_uri),
            ox.NamedNode("http://www.w3.org/2000/01/rdf-schema#label"),
            ox.Literal("paintings"),
        )
        in triples
    )
    # Embedded nodes are blank nodes.
    names = [t.object for t in triples if t.predicate == ox.NamedNode(f"{CRM}P1_is_identified_by")]
    assert len(names) == 2
    assert all(isinstance(name, ox.BlankNode) for name in names)


@pytest.mark.parametrize("accept", [None, "*/*", "text/html", HTML_ACCEPT, "image/png"])
async def test_html(served: Served, db_engine: AsyncEngine, accept: str | None) -> None:
    uri, type_uri, _ = await published(db_engine)
    headers = {"Accept-Language": "fr"}
    if accept is not None:
        headers["Accept"] = accept
    request = served.client.build_request("GET", path_of(uri), headers=headers)
    if accept is None:
        del request.headers["Accept"]
    response = await served.client.send(request)
    assert_public(response, uri)
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["vary"] == "Accept, Accept-Language, Cookie"
    page = response.text
    assert page.startswith("<!DOCTYPE html>")
    # The page is in the first language of the chain that has reader strings (only en so far);
    # the title is in the caller's language and says so.
    assert '<html lang="en" dir="ltr">' in page
    assert "<title>Scène de nuit – Laertes</title>" in page
    assert '<h1 lang="fr" dir="auto">Scène de nuit</h1>' in page
    assert 'href="?format=jsonld"' in page
    assert 'href="?format=ttl"' in page
    assert '<link rel="alternate" type="text/turtle" href="?format=ttl">' in page
    # Sections, with the classification label transcluded in French and marked as such.
    assert '<h2 id="section-names">Names</h2>' in page
    assert '<span lang="fr" dir="auto">Scène de nuit</span>' in page
    assert f'<a href="{type_uri}" lang="fr" dir="auto">peintures</a>' in page
    assert '<h2 id="section-produced_by">Production</h2>' in page
    assert '<span dir="auto">1880</span>' in page
    # No script at all without a IIIF resource, and no edit link without a session.
    assert "<script" not in page
    assert "/edit/" not in page


async def test_html_escapes_the_label(served: Served, db_engine: AsyncEngine) -> None:
    uri = new_uri()
    data = titled_record(uri, "<script>x</script> & co", "B", f"https://vocab.test/{uuid7()}")
    await insert_record(db_engine, data)
    response = await served.client.get(path_of(uri), headers={"Accept": "text/html"})
    assert "<script>" not in response.text
    assert "&lt;script&gt;x&lt;/script&gt; &amp; co" in response.text


def iiif_record(uri: str) -> dict[str, Any]:
    """A titled record whose image is a IIIF Image API URL with a region."""
    data = titled_record(uri, "Kylix", "Coupe", f"https://vocab.test/{uuid7()}")
    data["representation"] = [
        {
            "type": "VisualItem",
            "digitally_shown_by": [
                {
                    "type": "DigitalObject",
                    "access_point": [
                        {
                            "id": "https://iiif.test/image/kylix/10.4,20,300,200/max/0/default.jpg",
                            "type": "DigitalObject",
                        }
                    ],
                }
            ],
        }
    ]
    return data


async def test_html_shows_canvas_panel_for_a_iiif_image(
    served: Served, db_engine: AsyncEngine
) -> None:
    uri = new_uri()
    await insert_record(db_engine, iiif_record(uri))
    response = await served.client.get(path_of(uri), headers={"Accept": "text/html"})
    assert_public(response, uri)
    page = response.text
    assert READER_SCRIPT in page
    assert page.count("<script") == 1
    assert (
        '<canvas-panel data-image-service="https://iiif.test/image/kylix" '
        'data-region="10,20,300,200" height="480" interactive="true" tabindex="0" '
        'role="application" aria-label="Image viewer" aria-describedby="viewer-help">'
        "</canvas-panel>"
    ) in page
    assert (
        '<img class="fallback" src="https://iiif.test/image/kylix/full/!800,800/0/default.jpg" '
        'alt="Image of Kylix"'
    ) in page


@pytest.mark.parametrize("record", ["plain", "iiif"])
async def test_html_references_no_spa_asset(
    served: Served, db_engine: AsyncEngine, record: str
) -> None:
    """The reader loads nothing from the SPA: at most its own script, from /reader/assets/."""
    uri = new_uri()
    data = iiif_record(uri) if record == "iiif" else titled_record(uri, "A", "B", "https://x.test")
    await insert_record(db_engine, data)
    page = (await served.client.get(path_of(uri), headers={"Accept": "text/html"})).text
    assert not SPA_ASSET.search(page), SPA_ASSET.search(page)
    sources = re.findall(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]*)"', page)
    assert all(source.startswith(("?format=", "/reader/assets/")) for source in sources), sources
    assert 'rel="stylesheet"' not in page
    assert ("/reader/assets/reader.js" in page) is (record == "iiif")


async def test_html_edit_link_only_with_a_session(db_url: str, db_engine: AsyncEngine) -> None:
    uri, _, _ = await published(db_engine)
    edit = f'<a class="edit" href="/edit/{id_of(uri)}">Edit this record</a>'
    idp = FakeOIDCProvider()
    async with serve_app(db_url, idp, base_uri=BASE_URI) as served:
        anonymous = await served.client.get(path_of(uri))
        assert anonymous.status_code == 200
        assert edit not in anonymous.text
        await login(served.client, idp, unique_subject())
        signed_in = await served.client.get(path_of(uri))
        assert signed_in.status_code == 200
        assert edit in signed_in.text
        assert signed_in.headers["vary"] == "Accept, Accept-Language, Cookie"
        # The two pages have different tags, so a cached anonymous page is not reused.
        assert signed_in.headers["etag"] == anonymous.headers["etag"][:-1] + '-edit"'
        revalidated = await served.client.get(
            path_of(uri), headers={"If-None-Match": anonymous.headers["etag"]}
        )
        assert revalidated.status_code == 200
        assert edit in revalidated.text
        # JSON-LD and Turtle do not depend on the session.
        jsonld = await served.client.get(path_of(uri), params={"format": "jsonld"})
        assert jsonld.headers["vary"] == "Accept, Accept-Language"
        assert not jsonld.headers["etag"].endswith('-edit"')
        await served.client.post("/auth/logout")
        assert edit not in (await served.client.get(path_of(uri))).text


async def test_html_ignores_a_forged_session_cookie(served: Served, db_engine: AsyncEngine) -> None:
    uri, _, _ = await published(db_engine)
    served.client.cookies.set("lae_session", "forged")
    response = await served.client.get(path_of(uri))
    assert response.status_code == 200
    assert "/edit/" not in response.text


async def test_reader_script_is_served(db_url: str, tmp_path: Any) -> None:
    (tmp_path / "reader.js").write_text("export {};\n")
    async with serve_app(db_url, base_uri=BASE_URI, reader_assets_dir=tmp_path) as served:
        script = await served.client.get("/reader/assets/reader.js")
        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
        assert script.text == "export {};\n"
        assert (await served.client.get("/reader/assets/missing.js")).status_code == 404
        assert (await served.client.get("/reader/assets/../pyproject.toml")).status_code == 404
    async with serve_app(
        db_url, base_uri=BASE_URI, reader_assets_dir=tmp_path / "not-built"
    ) as served:
        assert (await served.client.get("/reader/assets/reader.js")).status_code == 404


@pytest.mark.parametrize(
    ("value", "content_type"),
    [
        ("jsonld", PROFILE_CONTENT_TYPE),
        ("ttl", "text/turtle; charset=utf-8"),
        ("html", "text/html; charset=utf-8"),
    ],
)
async def test_format_overrides_accept(
    served: Served, db_engine: AsyncEngine, value: str, content_type: str
) -> None:
    uri, _, _ = await published(db_engine)
    response = await served.client.get(
        path_of(uri), params={"format": value}, headers={"Accept": HTML_ACCEPT}
    )
    assert_public(response, uri)
    assert response.headers["content-type"] == content_type


async def test_unknown_format(served: Served, db_engine: AsyncEngine) -> None:
    uri, _, _ = await published(db_engine)
    response = await served.client.get(path_of(uri), params={"format": "rdfxml"})
    assert response.status_code == 422


async def test_never_requires_authentication(served: Served, db_engine: AsyncEngine) -> None:
    uri, _, _ = await published(db_engine)
    anonymous = await served.client.get(path_of(uri), headers={"Accept": "text/turtle"})
    assert_public(anonymous, uri)
    # Credentials are not even looked at: a rejected token changes nothing.
    with_bad_token = await served.client.get(
        path_of(uri), headers={"Accept": "text/turtle", **bearer("lae_pat_not-a-token")}
    )
    assert with_bad_token.status_code == 200
    assert with_bad_token.text == anonymous.text


# --- statuses ---------------------------------------------------------------------------------


@pytest.mark.parametrize("query", [{}, {"version": "1"}, {"format": "jsonld"}])
async def test_deleted_is_gone(
    served: Served, db_engine: AsyncEngine, query: dict[str, str]
) -> None:
    uri, _, _ = await published(db_engine, status=RecordStatus.DELETED)
    response = await served.client.get(path_of(uri), params=query)
    assert response.status_code == 410
    assert response.json()["code"] == "LAE-REC-002"
    assert not LOCAL_NODE_IRI.search(response.text)


@pytest.mark.parametrize(
    ("query", "suffix"),
    [({}, ""), ({"version": "1"}, ""), ({"format": "ttl"}, "?format=ttl")],
)
async def test_merged_moves_permanently(
    served: Served, db_engine: AsyncEngine, query: dict[str, str], suffix: str
) -> None:
    target, _, _ = await published(db_engine)
    uri, _, _ = await published(db_engine, status=RecordStatus.MERGED, merged_into=id_of(target))
    response = await served.client.get(path_of(uri), params=query)
    assert response.status_code == 301
    assert response.headers["location"] == target + suffix
    assert response.json()["code"] == "LAE-REC-003"
    assert response.json()["params"] == {"merged_into": target + suffix}


async def test_merged_without_target_is_gone(served: Served, db_engine: AsyncEngine) -> None:
    uri, _, _ = await published(db_engine, status=RecordStatus.MERGED)
    response = await served.client.get(path_of(uri))
    assert response.status_code == 410


async def test_never_published_is_not_found(served: Served, db_engine: AsyncEngine) -> None:
    uri, _, _ = await published(db_engine, version=0)
    for query in ({}, {"version": "0"}, {"format": "jsonld"}):
        response = await served.client.get(path_of(uri), params=query)
        assert response.status_code == 404
        assert response.json()["code"] == "LAE-REC-001"


@pytest.mark.parametrize(
    "segment",
    [
        lambda: str(uuid7()),  # no such record
        lambda: str(uuid7()).upper(),  # not the canonical spelling of a URI
        lambda: str(uuid7()).replace("-", ""),
        lambda: "not-a-uuid",
    ],
)
async def test_unknown_or_malformed_id_is_not_found(served: Served, segment: Any) -> None:
    response = await served.client.get(f"/id/{segment()}")
    assert response.status_code == 404
    assert response.json()["code"] == "LAE-REC-001"


async def test_non_canonical_spelling_of_an_existing_record(
    served: Served, db_engine: AsyncEngine
) -> None:
    uri, _, _ = await published(db_engine)
    response = await served.client.get(f"/id/{str(id_of(uri)).upper()}")
    assert response.status_code == 404


# --- versions ---------------------------------------------------------------------------------


async def edited_record(engine: AsyncEngine) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """A record at version 3 with revision rows for 2 and 3 (1 was bulk-loaded)."""
    uri, type_uri = new_uri(), f"https://vocab.test/{uuid7()}"
    v2 = titled_record(uri, "Second Title", "Deuxième titre", type_uri)
    v3 = titled_record(uri, "Third Title", "Troisième titre", type_uri)
    await insert_record(engine, v3, version=3)
    await insert_revision(engine, id_of(uri), 2, v2, created_at=a_while_ago())
    await insert_revision(engine, id_of(uri), 3, v3)
    return uri, v2, v3


@pytest.mark.parametrize("value", ["jsonld", "ttl", "html"])
async def test_versioned_fetch(served: Served, db_engine: AsyncEngine, value: str) -> None:
    uri, _, _ = await edited_record(db_engine)
    second = await served.client.get(path_of(uri), params={"version": "2", "format": value})
    assert_public(second, uri)
    assert "Second Title" in second.text
    assert "Third Title" not in second.text
    assert second.headers["last-modified"] == format_datetime(a_while_ago(), usegmt=True)
    third = await served.client.get(path_of(uri), params={"version": "3", "format": value})
    assert_public(third, uri)
    assert "Third Title" in third.text
    if value == "html":
        assert 'href="?version=2&amp;format=jsonld"' in second.text
        assert "You are viewing version 2 of this record, published Jan 2, 2026." in second.text
        assert f'<a href="/id/{id_of(uri)}">View the current version</a>' in second.text
        assert "You are viewing version 3 of this record" in third.text
        assert "This is the current version." in third.text
        current = await served.client.get(path_of(uri), params={"format": value})
        assert "You are viewing version" not in current.text


async def test_versioned_jsonld_is_the_stored_snapshot(
    served: Served, db_engine: AsyncEngine
) -> None:
    uri, v2, _ = await edited_record(db_engine)
    response = await served.client.get(
        path_of(uri), params={"version": "2"}, headers={"Accept": "application/ld+json"}
    )
    assert_public(response, uri)
    assert response.headers["content-type"] == PROFILE_CONTENT_TYPE
    body = response.json()
    assert body["_label"] == v2["_label"]
    assert [name["content"] for name in body["identified_by"]] == [
        name["content"] for name in v2["identified_by"]
    ]


@pytest.mark.parametrize("version", ["0", "1", "4", "-1"])
async def test_unknown_version(served: Served, db_engine: AsyncEngine, version: str) -> None:
    uri, _, _ = await edited_record(db_engine)
    response = await served.client.get(path_of(uri), params={"version": version})
    assert response.status_code == 404
    assert response.json()["code"] == "LAE-REC-004"
    assert response.json()["params"] == {"version": int(version)}


async def test_malformed_version(served: Served, db_engine: AsyncEngine) -> None:
    uri, _, _ = await edited_record(db_engine)
    response = await served.client.get(path_of(uri), params={"version": "two"})
    assert response.status_code == 422


@pytest.mark.parametrize("value", ["jsonld", "ttl", "html"])
async def test_bulk_loaded_version_one(served: Served, db_engine: AsyncEngine, value: str) -> None:
    uri, _, data = await published(db_engine, updated_at=a_while_ago())
    response = await served.client.get(path_of(uri), params={"version": "1", "format": value})
    assert_public(response, uri)
    assert "Night Scene" in response.text
    assert response.headers["last-modified"] == format_datetime(a_while_ago(), usegmt=True)
    missing = await served.client.get(path_of(uri), params={"version": "2", "format": value})
    assert missing.status_code == 404


# --- caching ----------------------------------------------------------------------------------


async def test_etag_and_if_none_match(served: Served, db_engine: AsyncEngine) -> None:
    uri, _, data = await published(db_engine, updated_at=a_while_ago())
    from lae.api.records import record_etag

    jsonld = {"Accept": "application/ld+json"}
    first = await served.client.get(path_of(uri), headers=jsonld)
    etag = first.headers["etag"]
    assert etag == f'W/"{record_etag(data)}-jsonld"'
    assert first.headers["last-modified"] == format_datetime(a_while_ago(), usegmt=True)
    assert first.headers["vary"] == "Accept, Accept-Language"

    for condition in (etag, etag.removeprefix("W/"), f'"stale", {etag}', "*"):
        cached = await served.client.get(
            path_of(uri), headers={**jsonld, "If-None-Match": condition}
        )
        assert cached.status_code == 304, condition
        assert cached.content == b""
        assert cached.headers["etag"] == etag
        assert cached.headers["last-modified"] == first.headers["last-modified"]

    stale = await served.client.get(path_of(uri), headers={**jsonld, "If-None-Match": '"stale"'})
    assert stale.status_code == 200
    assert stale.content == first.content

    # Another representation has another entity tag, so the JSON-LD tag does not match it.
    turtle = await served.client.get(
        path_of(uri), headers={"Accept": "text/turtle", "If-None-Match": etag}
    )
    assert turtle.status_code == 200
    assert turtle.headers["etag"] == f'W/"{record_etag(data)}-ttl"'


async def test_versioned_etag(served: Served, db_engine: AsyncEngine) -> None:
    uri, _, _ = await edited_record(db_engine)
    current = await served.client.get(path_of(uri), params={"format": "jsonld"})
    second = await served.client.get(path_of(uri), params={"version": "2", "format": "jsonld"})
    third = await served.client.get(path_of(uri), params={"version": "3", "format": "jsonld"})
    assert len({current.headers["etag"], second.headers["etag"], third.headers["etag"]}) == 3
    cached = await served.client.get(
        path_of(uri),
        params={"version": "2", "format": "jsonld"},
        headers={"If-None-Match": second.headers["etag"]},
    )
    assert cached.status_code == 304


# --- node IRIs --------------------------------------------------------------------------------


async def test_internal_node_iri_redirects_to_the_record(served: Served) -> None:
    uri = new_uri()
    response = await served.client.get(f"{path_of(uri)}/node/{uuid7()}")
    assert response.status_code == 303
    assert response.headers["location"] == uri


async def test_derived_node_iri_redirects_to_the_record(served: Served) -> None:
    uri = new_uri()
    derived = derived_node_iri(uri, "https://linked.art/example/Name/0")
    node_id = derived.rpartition("/")[2]
    response = await served.client.get(f"{path_of(uri)}/node/{node_id}")
    assert response.status_code == 303
    assert response.headers["location"] == uri


@pytest.mark.parametrize(
    "node_id",
    [
        lambda: str(uuid4()),  # neither UUIDv7 nor UUIDv5
        lambda: str(uuid7()).upper(),
        lambda: "not-a-uuid",
    ],
)
async def test_other_node_ids_are_not_found(served: Served, node_id: Any) -> None:
    response = await served.client.get(f"{path_of(new_uri())}/node/{node_id()}")
    assert response.status_code == 404
    assert response.json()["code"] == "LAE-REC-001"


async def test_node_of_a_malformed_record_id_is_not_found(served: Served) -> None:
    response = await served.client.get(f"/id/not-a-uuid/node/{uuid7()}")
    assert response.status_code == 404


# --- the corpus -------------------------------------------------------------------------------


CORPUS = sorted(str(path.relative_to(CORPUS_DIR)) for path in corpus_files())


async def test_no_node_iri_in_any_public_body_of_the_corpus(
    served: Served, db_engine: AsyncEngine
) -> None:
    """Every corpus record, every representation, from ``records`` and from a revision row."""
    assert len(CORPUS) >= 244
    with_nodes = 0
    for name in CORPUS:
        uri = new_uri()
        data = corpus_record(name, uri)
        await insert_record(db_engine, data, version=2)
        await insert_revision(db_engine, id_of(uri), 2, data)
        with_nodes += node_path(uri) in json.dumps(data)
        for value in ("jsonld", "ttl", "html"):
            for query in ({"format": value}, {"format": value, "version": "2"}):
                response = await served.client.get(path_of(uri), params=query)
                assert response.status_code == 200, (name, query, response.text)
                # Some source URLs contain "/node/" (Drupal pages); local node IRIs never appear.
                assert not LOCAL_NODE_IRI.search(response.text), (name, query)
    # Most stored records have internal node IRIs to strip (193 of 244 when written); the others
    # have no embedded node without an id of its own.
    assert with_nodes > 0.75 * len(CORPUS)
