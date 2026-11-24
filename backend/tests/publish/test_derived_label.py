"""The derived root ``_label`` through the real ``api`` and an in-process ``crdt-ws`` (Step 1.19a).

Contract ``docs/contracts/1.19a-derived-label.md`` §5, tests 14-18d. The tests that are not about
the label language use the ``served`` / ``network`` fixtures (default settings: ``en``); the
others build both applications from one ``Settings`` with ``label_language`` (``stack``). Only
one ``crdt-ws`` may serve a database at a time, so those tests never use the fixtures.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest
from pycrdt import Doc
from sqlalchemy.ext.asyncio import AsyncEngine

import lae.api.validation
from lae.api.records import LABEL_LANGUAGE_HEADER
from lae.crdt.server import create_crdt_app
from lae.labels.derive import with_derived_label
from lae.projection import ops
from lae.publish import errors, service
from tests.api.conftest import (
    AAT,
    BASE_URI,
    CRDT_WS_URL,
    ENGLISH,
    FRENCH,
    INTERNAL_TOKEN,
    PRIMARY_NAME,
    insert_record,
    insert_revision,
    titled_record,
)
from tests.auth.conftest import Served, make_settings, serve_app
from tests.publish.conftest import (
    Caller,
    Network,
    caller,
    edit,
    id_of,
    new_draft,
    new_uri,
    open_draft,
    publish,
    stored,
)

BILINGUAL = {
    "identified_by": [
        {
            "type": "Name",
            "content": "The Night Watch",
            "classified_as": [PRIMARY_NAME],
            "language": [ENGLISH],
        },
        {
            "type": "Name",
            "content": "La Ronde de nuit",
            "classified_as": [PRIMARY_NAME],
            "language": [FRENCH],
        },
    ],
}
"""A template with an English and a French primary name, English first, and no ``_label``."""


@asynccontextmanager
async def stack(db_url: str, label_language: str) -> AsyncIterator[tuple[Served, Network]]:
    """``api`` and ``crdt-ws`` built from settings with ``label_language``, on the test DB."""
    settings = {"base_uri": BASE_URI, "internal_token": INTERNAL_TOKEN}
    crdt_app = create_crdt_app(make_settings(db_url, label_language=label_language, **settings))
    async with crdt_app.router.lifespan_context(crdt_app):
        network = Network(crdt_app)
        async with serve_app(
            db_url,
            http_transport=network,
            crdt_ws_url=CRDT_WS_URL,
            label_language=label_language,
            **settings,
        ) as served:
            yield served, network


async def publish_accepting(app: Served, who: Caller, record_id: UUID) -> Any:
    """Publish, accepting whatever warnings the bundle has; return the final response."""
    response = await publish(app, who, record_id)
    if response.status_code == 409 and response.json().get("code") == errors.WARNINGS_NOT_ACCEPTED:
        shown = response.json()["params"]["records"]
        accepted = [
            {"code": w["code"], "path": w["path"], "record_id": rid}
            for rid, warnings in shown.items()
            for w in warnings
        ]
        response = await publish(app, who, record_id, *accepted)
    return response


def retitle(content: str, index: int = 0) -> Any:
    """An edit setting the content of the ``index``-th Name."""

    def change(doc: Doc[Any]) -> None:
        entry = ops.list_entries(doc, "identified_by")[index][1]
        ops.set_scalar(doc, "content", content, base=entry)

    return change


def root_label_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in changes if c["parent"] == [] and c["property"] == "_label"]


# --- 14-17: stubs, edits, missing names ----------------------------------------------------------


async def test_a_stub_created_with_a_name_has_its_label(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    # The template CreateStubDialog sends: one Name, no _label.
    template = {"identified_by": [{"type": "Name", "content": "  Ada\tLovelace "}]}
    record_id, _ = await new_draft(served, editor, "Person", template)

    record = (await stored(db_engine, record_id)).record
    assert record is not None and record.data["_label"] == "Ada Lovelace"
    snapshot = await network.rooms.snapshot(record_id)
    assert snapshot.jsonld["_label"] == "Ada Lovelace"
    validated = await served.client.post(
        f"/api/records/{record_id}/draft/validate", headers=editor.headers
    )
    assert validated.status_code == 200, validated.text
    assert not [i for i in validated.json()["items"] if i["code"] == "LAE-SCHEMA-required"]


async def test_a_parent_publishes_with_a_stub_created_by_the_dialog_template(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    stub_id, stub_uri = await new_draft(
        served, editor, "Person", {"identified_by": [{"type": "Name", "content": "Douris"}]}
    )
    parent_id, _ = await new_draft(
        served,
        editor,
        "HumanMadeObject",
        {
            "identified_by": [{"type": "Name", "content": "Kylix"}],
            "produced_by": {
                "type": "Production",
                "carried_out_by": [{"id": stub_uri, "type": "Person"}],
            },
        },
    )

    response = await publish_accepting(served, editor, parent_id)

    assert response.status_code == 200, response.text
    assert response.json()["published"] == [str(parent_id), str(stub_id)]
    for record_id, label in ((parent_id, "Kylix"), (stub_id, "Douris")):
        after = await stored(db_engine, record_id)
        assert after.record is not None and after.record.data["_label"] == label
        assert after.jsonld(1)["_label"] == label
        assert after.label is not None and after.label.default_label == label


async def test_publish_stores_the_label_of_the_edited_name(
    served: Served, network: Network, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", {**BILINGUAL, "_label": "Typed"})
    assert (await stored(db_engine, record_id)).record.data["_label"] == "The Night Watch"  # type: ignore[union-attr]
    assert (await publish_accepting(served, editor, record_id)).status_code == 200

    seen: list[tuple[str, Any]] = []
    validate, validate_document = lae.api.validation.validate, service.validate_document

    async def seen_by_validate(jsonld: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        seen.append(("validate", jsonld.get("_label")))
        return await validate(jsonld, *args, **kwargs)

    def seen_by_publish(jsonld: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        seen.append(("publish", jsonld.get("_label")))
        return validate_document(jsonld, *args, **kwargs)

    monkeypatch.setattr(lae.api.validation, "validate", seen_by_validate)
    monkeypatch.setattr(service, "validate_document", seen_by_publish)
    await open_draft(network, record_id)
    await edit(network, record_id, editor.id, retitle("The  Night Watch, 1642"))
    validated = await served.client.post(
        f"/api/records/{record_id}/draft/validate", headers=editor.headers
    )
    assert validated.status_code == 200
    response = await publish_accepting(served, editor, record_id)

    assert response.status_code == 200, response.text
    label = "The Night Watch, 1642"
    assert seen and all(found == label for _, found in seen)
    assert {stage for stage, _ in seen} == {"validate", "publish"}
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.record.data["_label"] == label
    assert after.jsonld(2)["_label"] == label
    assert after.jsonld(1)["_label"] == "The Night Watch"
    assert after.label is not None and after.label.default_label == "The  Night Watch, 1642"
    assert after.label.labels == {"en": "The  Night Watch, 1642", "fr": "La Ronde de nuit"}


async def test_a_record_with_no_name_and_no_label_is_refused_with_schema_required(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(
        served, editor, "Person", {"identified_by": [{"type": "Name", "content": "  "}]}
    )
    record = (await stored(db_engine, record_id)).record
    assert record is not None and "_label" not in record.data
    validated = await served.client.post(
        f"/api/records/{record_id}/draft/validate", headers=editor.headers
    )
    required = [
        item
        for item in validated.json()["items"]
        if item["code"] == "LAE-SCHEMA-required" and item["path"] == ""
    ]
    assert [item["params"]["missing"] for item in required] == [["_label"]]

    response = await publish(served, editor, record_id)

    assert response.status_code == 422
    assert response.json()["code"] == errors.VALIDATION_FAILED
    shown = response.json()["params"]["records"][str(record_id)]
    assert {"code": "LAE-SCHEMA-required", "path": ""} in [
        {"code": item["code"], "path": item["path"]} for item in shown
    ]
    after = await stored(db_engine, record_id)
    assert after.counts == (0, 0, 0, 1, 0)


async def test_a_bulk_loaded_record_without_names_keeps_its_label_and_publishes(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    uri = new_uri()
    loaded = {
        "@context": "https://linked.art/ns/v1/linked-art.json",
        "id": uri,
        "type": "HumanMadeObject",
        "_label": "A loaded label",
        "classified_as": [{"id": f"{AAT}300033618", "type": "Type"}],
    }
    await insert_record(db_engine, loaded, version=1)
    record_id = id_of(uri)
    await open_draft(network, record_id)
    await edit(
        network,
        record_id,
        editor.id,
        lambda doc: ops.add_reference(doc, "classified_as", f"{AAT}300015050", "Type"),
    )
    assert (await network.rooms.snapshot(record_id)).jsonld["_label"] == "A loaded label"

    response = await publish_accepting(served, editor, record_id)

    assert response.status_code == 200, response.text
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.record.data["_label"] == "A loaded label"
    assert after.jsonld(2)["_label"] == "A loaded label"
    assert after.label is not None and after.label.default_label == "A loaded label"


# --- 18: diff and revert -----------------------------------------------------------------------


async def test_draft_diff_reports_the_label_as_one_update_literal(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", BILINGUAL)
    assert (await publish_accepting(served, editor, record_id)).status_code == 200
    await open_draft(network, record_id)
    path = f"/api/records/{record_id}/draft/diff"
    assert (await served.client.post(path, headers=editor.headers)).json()["changes"] == []

    await edit(network, record_id, editor.id, retitle("The Night Watch (1642)"))
    changes = (await served.client.post(path, headers=editor.headers)).json()["changes"]

    (label,) = root_label_changes(changes)
    assert (label["kind"], label["path"], label["parent"], label["op"]) == (
        "update_literal",
        "_label",
        [],
        "change",
    )
    assert (label["old"], label["new"]) == ("The Night Watch", "The Night Watch (1642)")
    assert [c["path"] for c in changes if c is not label] == [
        "identified_by[Name:Primary Name].content"
    ]


async def test_revert_gives_the_revision_with_its_derived_label(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """Invariant 13 (amending contract 1.14 §3 item 17): a bulk-loaded version whose ``_label``
    is not the derived one comes back with the derived label; the two diffs differ by exactly
    that root ``_label`` change, and by nothing for a revision published after this step."""
    editor = await caller(db_engine)
    uri = new_uri()
    loaded = titled_record(uri, "Bulk title", "Titre", f"{AAT}300033618")
    loaded["_label"] = "Bulk-loaded label"
    await insert_record(db_engine, loaded, version=1)
    record_id = id_of(uri)

    async def revert_and_compare(version: int, current: int) -> list[dict[str, Any]]:
        reverted = await served.client.post(
            f"/api/records/{record_id}/revisions/{version}/revert", headers=editor.headers
        )
        assert reverted.status_code == 200, reverted.text
        draft = await served.client.post(
            f"/api/records/{record_id}/draft/diff", headers=editor.headers
        )
        stored_diff = await served.client.get(
            f"/api/records/{record_id}/revisions/{current}/diff/{version}",
            headers=editor.headers,
        )
        draft_changes = draft.json()["changes"]
        stored_changes = stored_diff.json()["changes"]
        others = [c for c in draft_changes if c not in root_label_changes(draft_changes)]
        assert others == [c for c in stored_changes if c not in root_label_changes(stored_changes)]
        return root_label_changes(draft_changes)

    # Version 1, bulk-loaded and current: the draft carries the derived label.
    (label,) = await revert_and_compare(1, 1)
    assert (label["old"], label["new"]) == ("Bulk-loaded label", "Bulk title")
    snapshot = await network.rooms.snapshot(record_id)
    assert snapshot.jsonld == with_derived_label(loaded, "en")
    assert snapshot.jsonld["_label"] == "Bulk title"

    # Publish (version 2 stores the derived label; version 1 is back-filled as loaded), edit
    # and publish again, then revert to 2: identical diffs, no _label change at all.
    assert (await publish_accepting(served, editor, record_id)).status_code == 200
    await open_draft(network, record_id)
    await edit(network, record_id, editor.id, retitle("Later title"))
    assert (await publish_accepting(served, editor, record_id)).json()["version"] == 3
    assert await revert_and_compare(2, 3) == [
        {**label, "op": "change", "old": "Later title", "new": "Bulk title"}
    ]
    after = await stored(db_engine, record_id)
    assert (await network.rooms.snapshot(record_id)).jsonld == after.jsonld(2)

    # Reverting to the bulk-loaded version 1 now: the stored diff names the loaded label.
    (label,) = await revert_and_compare(1, 3)
    assert label["new"] == "Bulk title"
    stored_diff = await served.client.get(
        f"/api/records/{record_id}/revisions/3/diff/1", headers=editor.headers
    )
    assert root_label_changes(stored_diff.json()["changes"])[0]["new"] == "Bulk-loaded label"


async def test_revert_to_a_revision_with_a_stale_label(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    uri = new_uri()
    v2 = titled_record(uri, "Second", "Deuxième", f"{AAT}300033618")
    v2["_label"] = "Stale"
    v3 = {**v2, "_label": "Second", "x_note": "three"}
    await insert_record(db_engine, v3, version=3)
    await insert_revision(db_engine, id_of(uri), 2, v2)
    response = await served.client.post(
        f"/api/records/{id_of(uri)}/revisions/2/revert", headers=editor.headers
    )
    assert response.status_code == 200, response.text
    snapshot = await network.rooms.snapshot(id_of(uri))
    assert snapshot.jsonld == with_derived_label(v2, "en") == {**v2, "_label": "Second"}


# --- 18a-18d: the label language ---------------------------------------------------------------


async def test_get_record_sends_the_label_language_header(
    served: Served, db_url: str, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    uri = new_uri()
    await insert_record(db_engine, titled_record(uri, "A", "B", f"{AAT}300033618"))
    response = await served.client.get(f"/api/records/{id_of(uri)}", headers=editor.headers)
    assert response.status_code == 200
    assert response.headers[LABEL_LANGUAGE_HEADER] == "en"
    assert response.headers["X-Draft-Epoch"] == "0"
    # The body is unchanged: the stored _label, transcluded as before.
    assert response.json()["_label"] == "A"

    async with serve_app(db_url, base_uri=BASE_URI, label_language="fra") as french:
        response = await french.client.get(f"/api/records/{id_of(uri)}", headers=editor.headers)
        assert response.status_code == 200
        assert response.headers[LABEL_LANGUAGE_HEADER] == "fr"
        anonymous = await french.client.get(f"/api/records/{id_of(uri)}")
        assert anonymous.status_code == 401
        assert LABEL_LANGUAGE_HEADER not in anonymous.headers

    declared = served.app.openapi()["paths"]["/api/records/{record_id}"]["get"]["responses"]
    assert declared["200"]["headers"][LABEL_LANGUAGE_HEADER]["schema"] == {"type": "string"}


async def test_a_new_record_is_labelled_in_the_configured_language(
    db_url: str, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    async with stack(db_url, "fr") as (served, network):
        record_id, _ = await new_draft(
            served, editor, "HumanMadeObject", {**BILINGUAL, "_label": "Typed"}
        )
        record = (await stored(db_engine, record_id)).record
        assert record is not None and record.data["_label"] == "La Ronde de nuit"
        snapshot = await network.rooms.snapshot(record_id)
        assert snapshot.jsonld["_label"] == "La Ronde de nuit"


async def test_publish_stores_label_and_default_label_in_the_configured_language(
    db_url: str, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    async with stack(db_url, "fr") as (served, _):
        record_id, _ = await new_draft(served, editor, "HumanMadeObject", BILINGUAL)
        response = await publish_accepting(served, editor, record_id)
        assert response.status_code == 200, response.text
    after = await stored(db_engine, record_id)
    assert after.record is not None and after.record.data["_label"] == "La Ronde de nuit"
    assert after.jsonld(1)["_label"] == "La Ronde de nuit"
    assert after.label is not None and after.label.default_label == "La Ronde de nuit"
    assert after.label.labels == {"en": "The Night Watch", "fr": "La Ronde de nuit"}


async def test_changing_the_label_language_rederives_drafts_and_keeps_published_labels(
    db_url: str, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    async with stack(db_url, "en") as (served, _):
        a_id, _ = await new_draft(served, editor, "HumanMadeObject", BILINGUAL)
        assert (await publish_accepting(served, editor, a_id)).status_code == 200
        b_id, _ = await new_draft(served, editor, "HumanMadeObject", BILINGUAL)
    a_before = await stored(db_engine, a_id)
    b_before = await stored(db_engine, b_id)
    assert a_before.record is not None and a_before.record.data["_label"] == "The Night Watch"
    assert a_before.label is not None and a_before.label.default_label == "The Night Watch"

    async with stack(db_url, "fr") as (served, network):
        # The open draft derives under the new language at its next read; nothing is written.
        snapshot = await network.rooms.snapshot(b_id)
        assert snapshot.jsonld["_label"] == "La Ronde de nuit"
        b_after = await stored(db_engine, b_id)
        assert b_after.updates == b_before.updates
        assert b_after.draft is not None and b_before.draft is not None
        assert b_after.draft.last_update_id == b_before.draft.last_update_id
        # The published record keeps what it was stored with.
        a_after = await stored(db_engine, a_id)
        assert a_after.record is not None
        assert a_after.record.data == a_before.record.data
        assert a_after.record.version == 1
        assert a_after.jsonld(1) == a_before.jsonld(1)
        assert a_after.label is not None and a_after.label.default_label == "The Night Watch"
        # A draft opened on it and not edited differs by exactly the root _label.
        await open_draft(network, a_id)
        diff = await served.client.post(f"/api/records/{a_id}/draft/diff", headers=editor.headers)
        assert diff.status_code == 200, diff.text
        (change,) = diff.json()["changes"]
        assert (change["kind"], change["path"], change["old"], change["new"]) == (
            "update_literal",
            "_label",
            "The Night Watch",
            "La Ronde de nuit",
        )


# --- invariant 14: the other readers of the setting (review of Step 1.19a) ---------------------

GERMAN = {"id": f"{AAT}300388344", "type": "Language"}
GERMAN_FIRST = {
    "identified_by": [
        {
            "type": "Name",
            "content": "Die Nachtwache",
            "classified_as": [PRIMARY_NAME],
            "language": [GERMAN],
        },
        {
            "type": "Name",
            "content": "La Ronde de nuit",
            "classified_as": [PRIMARY_NAME],
            "language": [FRENCH],
        },
    ],
}
"""A German and a French primary name, German first: a reader whose locale chain is ``it, en``
has no name of their own, so they see the default label, which follows the label language."""


@pytest.mark.parametrize(
    ("label_language", "expected"), [("en", "Die Nachtwache"), ("fr", "La Ronde de nuit")]
)
async def test_the_drafts_list_and_the_reader_title_follow_the_label_language(
    db_url: str, db_engine: AsyncEngine, label_language: str, expected: str
) -> None:
    """``GET /api/drafts``, ``draft/extend`` and the Public Reader title compute the default
    label of a record without an ``entity_labels`` row with ``Settings.label_language``."""
    editor = await caller(db_engine)
    italian = {**editor.headers, "Accept-Language": "it"}
    uri = new_uri()
    published = {**titled_record(uri, "A", "B", f"{AAT}300033618"), **GERMAN_FIRST}
    await insert_record(db_engine, published)
    async with stack(db_url, label_language) as (served, _):
        record_id, _ = await new_draft(served, editor, "HumanMadeObject", GERMAN_FIRST)
        listed = await served.client.get("/api/drafts", headers=italian)
        assert listed.status_code == 200, listed.text
        labels = {draft["record_id"]: draft["label"] for draft in listed.json()}
        assert labels[str(record_id)] == expected
        extended = await served.client.post(
            f"/api/records/{record_id}/draft/extend", headers=italian
        )
        assert extended.status_code == 200, extended.text
        assert extended.json()["label"] == expected
        page = await served.client.get(
            f"/id/{id_of(uri)}", headers={"Accept": "text/html", "Accept-Language": "it"}
        )
        assert page.status_code == 200, page.text
        assert re.findall(r"<h1[^>]*>(.*?)</h1>", page.text) == [expected]
