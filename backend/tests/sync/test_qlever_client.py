"""``lae.sync.qlever_client``: result parsing, requests sent, and errors (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest

from lae.sync.qlever_client import (
    QLEVER_RESULTS_JSON,
    QLeverClient,
    QLeverError,
    parse_results,
    parse_value,
)

XSD = "http://www.w3.org/2001/XMLSchema#"
QUERY_URL = "http://qlever.test:7000"
UPDATE_URL = "http://qlever.test:7000/update"


@pytest.mark.parametrize(
    ("cell", "value"),
    [
        (None, None),
        ("<https://example.org/a>", "https://example.org/a"),
        ('"Rembrandt"', "Rembrandt"),
        ('"Rembrandt"@en', "Rembrandt"),
        ('"Night Watch"@nl-NL', "Night Watch"),
        # QLever writes the lexical form unescaped: quotes and newlines stay as they are.
        ('"{"type": "Point"}"', '{"type": "Point"}'),
        ('"say "hi""@en', 'say "hi"'),
        ('"two\nlines"', "two\nlines"),
        ('"ends with "@en""', 'ends with "@en"'),
        (f'"7"^^<{XSD}int>', 7),
        (f'"-12"^^<{XSD}integer>', -12),
        (f'"2.5"^^<{XSD}decimal>', 2.5),
        (f'"1606-07-15T00:00:00Z"^^<{XSD}dateTime>', "1606-07-15T00:00:00Z"),
        (f'"x"^^<{XSD}int>', "x"),
        ("_:b0", "_:b0"),
    ],
)
def test_parse_value(cell: str | None, value: object) -> None:
    assert parse_value(cell) == value


def test_parse_results_keys_rows_by_variable_name() -> None:
    body = {
        "status": "OK",
        "selected": ["?entity", "?n"],
        "res": [["<urn:a>", f'"1"^^<{XSD}int>'], ["<urn:b>", None]],
    }
    assert parse_results(body) == [{"entity": "urn:a", "n": 1}, {"entity": "urn:b", "n": None}]
    assert parse_results({"selected": ["?x"], "res": []}) == []


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_query_posts_the_form_and_asks_for_qlever_json() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"selected": ["?s"], "res": [["<urn:s>"]]})

    async with client_for(handle) as http:
        client = QLeverClient(QUERY_URL, UPDATE_URL, "secret", http_client=http)
        assert await client.query("SELECT ?s WHERE { ?s ?p ?o }") == [{"s": "urn:s"}]
    [request] = seen
    assert request.method == "POST" and str(request.url) == QUERY_URL
    assert request.headers["accept"] == QLEVER_RESULTS_JSON
    assert "authorization" not in request.headers
    assert parse_qs(request.content.decode()) == {"query": ["SELECT ?s WHERE { ?s ?p ?o }"]}


async def test_update_posts_to_the_update_url_with_the_token() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "OK"})

    async with client_for(handle) as http:
        client = QLeverClient(QUERY_URL, UPDATE_URL, "secret", http_client=http)
        await client.update("INSERT DATA { <urn:s> <urn:p> <urn:o> }")
    [request] = seen
    assert str(request.url) == UPDATE_URL
    assert request.headers["authorization"] == "Bearer secret"
    assert parse_qs(request.content.decode()) == {
        "update": ["INSERT DATA { <urn:s> <urn:p> <urn:o> }"]
    }


async def test_the_update_url_defaults_to_the_query_url() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    async with client_for(handle) as http:
        await QLeverClient(QUERY_URL, http_client=http).update("CLEAR DEFAULT")
    assert seen == [QUERY_URL]


@pytest.mark.parametrize("method", ["query", "update"])
async def test_a_refusal_raises_with_the_status_and_body(method: str) -> None:
    error_body = {"status": "ERROR", "exception": "Invalid SPARQL query"}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=error_body)

    async with client_for(handle) as http:
        client = QLeverClient(QUERY_URL, UPDATE_URL, "secret", http_client=http)
        with pytest.raises(QLeverError) as info:
            await getattr(client, method)("nonsense")
    assert info.value.status == 400
    assert json.loads(info.value.body) == error_body
    assert "Invalid SPARQL query" in str(info.value)


async def test_an_error_status_in_a_200_answer_raises() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ERROR", "exception": "boom"})

    async with client_for(handle) as http:
        with pytest.raises(QLeverError, match="boom"):
            await QLeverClient(QUERY_URL, http_client=http).query("ASK {}")


async def test_an_unreachable_server_raises_without_a_status() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with client_for(handle) as http:
        with pytest.raises(QLeverError) as info:
            await QLeverClient(QUERY_URL, http_client=http).query("ASK {}")
    assert info.value.status is None
    assert "connection refused" in info.value.body


async def test_no_result_cache() -> None:
    """Every call reaches QLever: the editor reads straight after it writes."""
    answers = iter(["<urn:old>", "<urn:new>"])

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"selected": ["?s"], "res": [[next(answers)]]})

    async with client_for(handle) as http:
        client = QLeverClient(QUERY_URL, http_client=http)
        assert await client.query("SELECT ?s {}") == [{"s": "urn:old"}]
        assert await client.query("SELECT ?s {}") == [{"s": "urn:new"}]


async def test_a_shared_http_client_stays_open() -> None:
    async with client_for(lambda request: httpx.Response(200, json={})) as http:
        async with QLeverClient(QUERY_URL, http_client=http):
            pass
        assert not http.is_closed
    async with QLeverClient(QUERY_URL) as own:
        pass
    assert own._client.is_closed
