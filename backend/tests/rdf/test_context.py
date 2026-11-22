"""The pinned-context loader serves only profiles/context and never touches the network."""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest
from pyld import jsonld
from pyld.context_resolver import ContextResolver

from lae.corpus import iter_corpus
from lae.rdf.context import (
    CONTEXT_URLS,
    UnpinnedContextError,
    context_path,
    document_loader,
    load_context,
)

PINNED_URL = "https://linked.art/ns/v1/linked-art.json"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything opens a socket connection."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"network access attempted: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def fresh_options(loader: Any) -> dict[str, Any]:
    """pyld options with a private context cache, so the loader is really called."""
    return {"documentLoader": loader, "contextResolver": ContextResolver({}, loader)}


def test_load_context_reads_the_pinned_file() -> None:
    assert load_context(1) == json.loads(context_path(1).read_bytes())


def test_load_context_returns_a_fresh_copy() -> None:
    first = load_context(1)
    first["@context"].clear()
    assert load_context(1)["@context"]


def test_load_context_unknown_version() -> None:
    with pytest.raises(UnpinnedContextError):
        load_context(999)


def test_document_loader_serves_the_pinned_context() -> None:
    remote = document_loader(PINNED_URL)
    assert remote["documentUrl"] == PINNED_URL
    assert remote["contentType"] == "application/ld+json"
    assert remote["contextUrl"] is None
    assert remote["document"] == load_context(1)


@pytest.mark.parametrize(
    "url",
    [
        "http://linked.art/ns/v1/linked-art.json",
        "https://linked.art/ns/v2/linked-art.json",
        "https://example.org/context.jsonld",
        "https://www.w3.org/ns/activitystreams",
        "file:///etc/passwd",
        context_path(1).as_uri(),
        str(context_path(1)),
        "v1.json",
        "../schema/core.json",
    ],
)
def test_document_loader_refuses_everything_else(url: str, no_network: None) -> None:
    with pytest.raises(UnpinnedContextError):
        document_loader(url)


def test_pinned_context_has_no_remote_references() -> None:
    """Nothing beyond profiles/context needs vendoring: no @import, no remote scoped context."""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assert "@import" not in node
            scoped = node.get("@context")
            assert scoped is None or isinstance(scoped, dict)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(load_context(1)["@context"])


def test_every_corpus_context_is_pinned() -> None:
    assert {doc["@context"] for _, doc in iter_corpus()} <= set(CONTEXT_URLS)


def test_expansion_uses_only_the_loader(no_network: None) -> None:
    calls: list[str] = []

    def spy(url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append(url)
        return document_loader(url, options)

    record = next(doc for _, doc in iter_corpus() if doc["type"] == "HumanMadeObject")
    expanded = jsonld.expand(record, fresh_options(spy))
    assert calls == [PINNED_URL]
    assert expanded[0]["@type"] == [f"{CRM}E22_Human-Made_Object"]


def test_expansion_of_an_unpinned_context_fails(no_network: None) -> None:
    doc = {"@context": "https://example.org/context.jsonld", "id": "urn:x", "type": "Thing"}
    with pytest.raises(jsonld.JsonLdError) as info:
        jsonld.expand(doc, fresh_options(document_loader))
    causes = []
    error: BaseException | None = info.value
    while error is not None:
        causes.append(error)
        error = error.__cause__
    assert any(isinstance(e, UnpinnedContextError) for e in causes)
