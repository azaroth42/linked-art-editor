"""Tests for ``lae.ids``: UUIDv7 ids, record URIs, internal node IRIs."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest

from lae.ids import (
    is_internal_node_iri,
    new_uuid7,
    node_iri,
    owner_of,
    record_id_from_uri,
    record_uri,
)
from lae.settings import get_settings

BASE = "https://collection.example.org/id/"
OTHER_BASE = "https://elsewhere.example.net/id/"
RECORD_ID = UUID("0190f5a2-7b3c-7d4e-8f00-112233445566")
NODE_ID = UUID("0190f5a2-7b3c-7d4e-9abc-def012345678")
RECORD = f"{BASE}{RECORD_ID}"
NODE = f"{RECORD}/node/{NODE_ID}"
NODE_IRI_RE = re.compile(
    re.escape(RECORD) + r"/node/[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)

NOT_LOCAL = [
    pytest.param("", id="empty"),
    pytest.param(BASE, id="base-only"),
    pytest.param(f"{OTHER_BASE}{RECORD_ID}", id="other-base"),
    pytest.param(f"{OTHER_BASE}{RECORD_ID}/node/{NODE_ID}", id="node-of-other-base"),
    pytest.param(f"http://collection.example.org/id/{RECORD_ID}", id="other-scheme"),
    pytest.param("http://vocab.getty.edu/aat/300404670", id="aat"),
    pytest.param("https://linked.art/example/object/1", id="linked-art-example"),
    pytest.param(
        "https://linked-art.library.yale.edu/node/14a3551f-6e93-4501-9b18-1b107f0274aa",
        id="yale-node-record",
    ),
    pytest.param(f"{BASE}{str(RECORD_ID).upper()}", id="upper-case-uuid"),
    pytest.param(f"{BASE}{RECORD_ID.hex}", id="unhyphenated-uuid"),
    pytest.param(f"{BASE}{{{RECORD_ID}}}", id="braced-uuid"),
    pytest.param(f"{BASE}urn:uuid:{RECORD_ID}", id="urn-uuid"),
    pytest.param(f"{BASE}not-a-uuid", id="not-a-uuid"),
    pytest.param(f"{BASE}{RECORD_ID}/", id="trailing-slash"),
    pytest.param(f"{BASE}{RECORD_ID}#this", id="fragment"),
    pytest.param(f"{BASE}{RECORD_ID}?version=2", id="query"),
    pytest.param(f"{BASE}people/{RECORD_ID}", id="class-typed-path"),
    pytest.param(f"{BASE}{RECORD_ID}/node/", id="node-without-id"),
    pytest.param(f"{BASE}{RECORD_ID}/node/{uuid4()}", id="node-id-not-v7"),
    pytest.param(f"{BASE}{RECORD_ID}/node/{str(NODE_ID).upper()}", id="node-id-upper-case"),
    pytest.param(f"{NODE}/node/{NODE_ID}", id="nested-node"),
    pytest.param(f"{NODE}/", id="node-trailing-slash"),
]


@pytest.fixture
def configured_base(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Configure ``LAE_BASE_URI`` for functions called without ``base_uri``."""
    monkeypatch.setenv("LAE_BASE_URI", BASE)
    get_settings.cache_clear()
    try:
        yield BASE
    finally:
        get_settings.cache_clear()


def test_new_uuid7_is_a_stdlib_version_7_uuid() -> None:
    value = new_uuid7()
    assert type(value) is UUID
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"


def test_new_uuid7_is_unique_and_time_ordered() -> None:
    first = [new_uuid7() for _ in range(10_000)]
    time.sleep(0.002)
    later = new_uuid7()
    assert len(set(first)) == len(first)
    assert first == sorted(first)
    assert later > max(first)
    millis = later.int >> 80
    assert abs(millis - time.time_ns() // 1_000_000) < 60_000


def test_record_uri_appends_the_id_to_the_base() -> None:
    assert record_uri(RECORD_ID, base_uri=BASE) == f"{BASE}0190f5a2-7b3c-7d4e-8f00-112233445566"


def test_record_uri_defaults_to_the_configured_base(configured_base: str) -> None:
    assert record_uri(RECORD_ID) == RECORD


def test_record_uri_default_setting_is_the_development_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LAE_BASE_URI", raising=False)
    get_settings.cache_clear()
    try:
        assert record_uri(RECORD_ID) == f"http://localhost:8000/id/{RECORD_ID}"
    finally:
        get_settings.cache_clear()


def test_record_id_from_uri_round_trips() -> None:
    minted = new_uuid7()
    assert record_id_from_uri(record_uri(minted, base_uri=BASE), base_uri=BASE) == minted
    assert record_id_from_uri(RECORD, base_uri=BASE) == RECORD_ID


def test_record_id_from_uri_accepts_any_uuid_version_for_records() -> None:
    legacy = uuid4()
    assert record_id_from_uri(f"{BASE}{legacy}", base_uri=BASE) == legacy


def test_record_id_from_uri_of_a_node_iri_is_its_record() -> None:
    assert record_id_from_uri(NODE, base_uri=BASE) == RECORD_ID


def test_record_id_from_uri_uses_the_configured_base(configured_base: str) -> None:
    assert record_id_from_uri(RECORD) == RECORD_ID
    assert record_id_from_uri(f"{OTHER_BASE}{RECORD_ID}") is None


@pytest.mark.parametrize("uri", NOT_LOCAL)
def test_record_id_from_uri_is_none_for_non_local_uris(uri: str) -> None:
    assert record_id_from_uri(uri, base_uri=BASE) is None


def test_node_iri_mints_a_fresh_v7_node_under_the_record() -> None:
    first, second = node_iri(RECORD), node_iri(RECORD)
    assert NODE_IRI_RE.fullmatch(first)
    assert first != second
    assert UUID(first.rsplit("/", 1)[1]).version == 7


def test_node_iri_works_for_any_record_uri() -> None:
    corpus_uri = "https://linked.art/example/object/1"
    minted = node_iri(corpus_uri)
    assert minted.startswith(f"{corpus_uri}/node/")
    assert is_internal_node_iri(minted, corpus_uri)


def test_is_internal_node_iri_for_local_nodes() -> None:
    assert is_internal_node_iri(NODE, base_uri=BASE)
    assert is_internal_node_iri(node_iri(RECORD), base_uri=BASE)


def test_is_internal_node_iri_uses_the_configured_base(configured_base: str) -> None:
    assert is_internal_node_iri(NODE)
    assert not is_internal_node_iri(f"{OTHER_BASE}{RECORD_ID}/node/{NODE_ID}")


def test_a_record_uri_is_not_a_node_iri() -> None:
    assert not is_internal_node_iri(RECORD, base_uri=BASE)
    assert not is_internal_node_iri(RECORD, RECORD)


@pytest.mark.parametrize("iri", NOT_LOCAL)
def test_is_internal_node_iri_is_false_for_non_local_iris(iri: str) -> None:
    assert not is_internal_node_iri(iri, base_uri=BASE)


@pytest.mark.parametrize("value", [None, 1, ["x"], {"id": NODE}])
def test_is_internal_node_iri_is_false_for_non_strings(value: object) -> None:
    assert not is_internal_node_iri(value, base_uri=BASE)


def test_is_internal_node_iri_scoped_to_one_record() -> None:
    other_record = f"{BASE}{new_uuid7()}"
    assert is_internal_node_iri(NODE, RECORD)
    assert not is_internal_node_iri(NODE, other_record)
    assert not is_internal_node_iri(f"{RECORD}/node/{uuid4()}", RECORD)
    assert not is_internal_node_iri(f"{NODE}/node/{NODE_ID}", RECORD)


def test_is_internal_node_iri_scoped_to_a_non_local_record() -> None:
    yale = "https://linked-art.library.yale.edu/node/14a3551f-6e93-4501-9b18-1b107f0274aa"
    minted = node_iri(yale)
    assert is_internal_node_iri(minted, yale)
    assert not is_internal_node_iri(minted, base_uri=BASE)
    assert not is_internal_node_iri(yale, "https://linked-art.library.yale.edu")


def test_owner_of_a_node_is_its_record() -> None:
    assert owner_of(NODE, base_uri=BASE) == RECORD
    assert owner_of(node_iri(RECORD), base_uri=BASE) == RECORD


def test_owner_of_a_record_is_itself() -> None:
    assert owner_of(RECORD, base_uri=BASE) == RECORD


def test_owner_of_uses_the_configured_base(configured_base: str) -> None:
    assert owner_of(NODE) == RECORD


@pytest.mark.parametrize("iri", NOT_LOCAL)
def test_owner_of_rejects_non_local_iris(iri: str) -> None:
    with pytest.raises(ValueError, match="not a local record URI"):
        owner_of(iri, base_uri=BASE)
