"""``POST /api/records/{id}/publish``: what one publish writes, and what stops it (Step 1.10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.api.records import new_record_data, record_etag
from lae.auth import CurrentUser
from lae.bootstrap.common import BOOTSTRAP_CONTEXT_VERSION, BOOTSTRAP_EXPANDER_VERSION
from lae.corpus import CORPUS_DIR, corpus_files, load_record
from lae.crdt import store
from lae.crdt.client import CrdtWsClient
from lae.db.models import (
    EntityLabel,
    EntityLabelStatus,
    OutboxEventKind,
    OutboxEventStatus,
    QaFindingStatus,
    RecordStatus,
    SyncState,
    UserKind,
)
from lae.labels.normalize import normalize_refs
from lae.labels.refs import reference_ids
from lae.projection import ops, strip_internal_iris
from lae.publish import errors
from lae.publish.service import publish as publish_service
from lae.rdf import EXPANDER_VERSION, diff_triples, expand_to_ntriples
from lae.validation import validate_document
from tests.api.conftest import insert_record, titled_record
from tests.publish.conftest import (
    AAT,
    NO_PRIMARY_NAME,
    REPAIR_WARNING,
    WARNING_PATH,
    Network,
    Served,
    add_alias,
    add_finding,
    caller,
    edit,
    id_of,
    new_draft,
    new_uri,
    open_draft,
    painting,
    person,
    person_with_violation,
    person_with_warning,
    publish,
    stored,
)

ERRORS_JSON = Path(__file__).resolve().parents[3] / "frontend/public/locales/en/errors.json"

UNTOUCHED = (0, 0, 0, 1, 0)
"""Row counts of a never-published record with a fresh draft: only the ``crdt_drafts`` row."""


def add_name(content: str) -> Any:
    return lambda doc: ops.add_node(doc, "identified_by", {"type": "Name", "content": content})


async def set_bootstrap_versions(
    engine: AsyncEngine, context_version: int | None, expander_version: int | None
) -> None:
    """Set (or, with ``None``, remove) what ``sync_state`` says the QLever index was built with."""
    values = {
        BOOTSTRAP_CONTEXT_VERSION: context_version,
        BOOTSTRAP_EXPANDER_VERSION: expander_version,
    }
    async with AsyncSession(engine) as session:
        await session.execute(delete(SyncState).where(SyncState.key.in_(list(values))))
        for key, value in values.items():
            if value is not None:
                await session.execute(insert(SyncState).values(key=key, value=value))
        await session.commit()


def test_every_code_has_an_english_message() -> None:
    messages: dict[str, str] = json.loads(ERRORS_JSON.read_text())
    assert {key for key in messages if key.startswith("LAE-PUB-")} == set(errors.PUBLISH_CODES)


# -- what a publish writes ----------------------------------------------------------------------


async def test_first_publish_writes_record_revision_label_and_outbox_and_clears_the_draft(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    collaborator = await caller(db_engine, "Collaborator")
    record_id, uri = await new_draft(served, collaborator, "Person", person("Ada Lovelace"))
    await edit(network, record_id, collaborator.id, add_name("Countess of Lovelace"))
    draft = (await network.rooms.snapshot(record_id)).jsonld
    assert (await stored(db_engine, record_id)).counts == (0, 0, 0, 1, 1)

    response = await publish(served, editor, record_id, summary="First version")

    assert response.status_code == 200, response.text
    assert response.json() == {"version": 1, "published": [str(record_id)], "draft_epoch": 1}
    after = await stored(db_engine, record_id)
    assert after.counts == (1, 1, 1, 0, 0)
    record = after.record
    assert record is not None
    assert (record.version, record.draft_epoch, record.context_version) == (1, 1, 1)
    assert record.data == draft and record.etag == record_etag(draft)
    assert [name["content"] for name in record.data["identified_by"]] == [
        "Ada Lovelace",
        "Countess of Lovelace",
    ]
    revision = after.revision(1)
    assert revision.authors == sorted([collaborator.id, editor.id])
    assert (revision.published_by, revision.summary) == (editor.id, "First version")
    assert revision.accepted_warnings == [] and revision.context_version == 1
    assert after.jsonld(1) == record.data
    assert after.triples(1) == expand_to_ntriples(record.data, 1, record_uri=uri)
    assert len(after.triples(1)) > 4
    (event,) = after.events
    assert (event.kind, event.status) == (OutboxEventKind.PUBLISH, OutboxEventStatus.PENDING)
    assert (event.from_version, event.to_version) == (None, 1)
    assert event.payload == {"context_version": 1, "drift_check": False}
    assert after.label is not None
    assert (after.label.type, after.label.default_label) == ("Person", "Ada Lovelace")
    assert (after.label.source, after.label.status) == ("local", EntityLabelStatus.PUBLISHED)
    assert await network.rooms.get(record_id) is None

    shown = await served.client.get(f"/api/records/{record_id}", headers=editor.headers)
    assert shown.status_code == 200 and shown.json()["_label"] == "Ada Lovelace"


async def test_second_publish_adds_a_version_whose_triples_differ_by_the_edit(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person("Ada"))
    assert (await publish(served, editor, record_id)).status_code == 200

    assert await open_draft(network, record_id) == 1
    await edit(network, record_id, editor.id, add_name("Ada, Countess of Lovelace"))
    response = await publish(served, editor, record_id, summary="Second")

    assert response.json() == {"version": 2, "published": [str(record_id)], "draft_epoch": 2}
    after = await stored(db_engine, record_id)
    assert after.counts == (2, 2, 1, 0, 0)
    assert [revision.version for revision in after.revisions] == [1, 2]
    assert [(event.from_version, event.to_version) for event in after.events] == [(None, 1), (1, 2)]
    assert all(event.payload["drift_check"] is False for event in after.events)
    removed, added = diff_triples(after.triples(1), after.triples(2))
    assert not removed
    assert sum('"Ada, Countess of Lovelace"' in line for line in added) == 1
    assert after.record is not None and after.jsonld(2) == after.record.data
    assert after.jsonld(1) != after.jsonld(2)
    first = await served.client.get(f"/api/records/{record_id}/revisions/1", headers=editor.headers)
    assert [name["content"] for name in first.json()["identified_by"]] == ["Ada"]


def _without_reference_labels(document: dict[str, Any]) -> dict[str, Any]:
    return normalize_refs(document, reference_ids(document))


async def test_corpus_records_publish_end_to_end(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """Every sixth corpus record goes through create, validate, accept, publish.

    A record validation refuses must be refused by publish with the same diagnostics; every
    other one must be stored exactly as its draft, with the triples of the stored JSON-LD.
    """
    editor = await caller(db_engine)
    published = refused = 0
    for path in corpus_files()[::6]:
        template = load_record(path)
        name = path.relative_to(CORPUS_DIR).as_posix()
        created = await served.client.post(
            "/api/records",
            json={"type": template["type"], "template": template},
            headers=editor.headers,
        )
        if created.status_code == 422:  # not a record class, or not hydratable (LAE-REC-005/6)
            continue
        record_id, uri = UUID(created.json()["id"]), created.json()["uri"]
        draft = (await network.rooms.snapshot(record_id)).jsonld
        expected = validate_document(draft, uri, context_version=1)

        response = await publish(served, editor, record_id)
        if not expected.ok:
            assert response.status_code == 422, name
            assert response.json()["code"] == errors.VALIDATION_FAILED, name
            assert (await stored(db_engine, record_id)).counts == UNTOUCHED, name
            refused += 1
            continue
        warnings = [item for item in expected.items if item.severity == "warning"]
        if warnings:
            assert response.status_code == 409, (name, response.text)
            body = response.json()
            assert body["code"] == errors.WARNINGS_NOT_ACCEPTED, name
            shown = body["params"]["records"][str(record_id)]
            assert [(w["code"], w["path"]) for w in shown] == [(w.code, w.path) for w in warnings]
            assert (await stored(db_engine, record_id)).counts == UNTOUCHED, name
            accepted = [{"code": w["code"], "path": w["path"]} for w in shown]
            response = await publish(served, editor, record_id, *accepted)
        assert response.status_code == 200, (name, response.text)

        after = await stored(db_engine, record_id)
        assert after.counts == (1, 1, 1, 0, 0), name
        assert after.record is not None
        data = after.record.data
        assert _without_reference_labels(data) == _without_reference_labels(draft), name
        assert after.jsonld(1) == data, name
        assert after.triples(1) == expand_to_ntriples(data, 1, record_uri=uri), name
        assert len(after.revision(1).accepted_warnings) == len(
            {(w.code, w.path) for w in warnings}
        ), name
        # Nothing of the source record is lost: the public form is the template again.
        original = {**template, "id": uri}
        assert _without_reference_labels(strip_internal_iris(data, uri)) == (
            _without_reference_labels(strip_internal_iris(original, template["id"]))
        ), name
        published += 1
    assert published >= 25 and refused >= 1, (published, refused)


# -- dependent stubs ----------------------------------------------------------------------------


async def test_unpublished_records_the_draft_refers_to_are_published_with_it(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    other = await caller(db_engine, "Collaborator")
    group_id, group_uri = await new_draft(served, other, "Group", person("Analytical Society"))
    maker_id, maker_uri = await new_draft(
        served,
        other,
        "Person",
        {**person("Ada"), "member_of": [{"id": group_uri, "type": "Group", "_label": "stale"}]},
    )
    await edit(network, maker_id, other.id, add_name("A. A. Lovelace"))
    # A published record with an open draft is referred to as well; it is not part of the bundle.
    known_id, known_uri = await new_draft(served, editor, "Person", person("Charles"))
    assert (await publish(served, editor, known_id)).status_code == 200
    await open_draft(network, known_id)
    await edit(network, known_id, other.id, add_name("Charles Babbage"))
    object_id, object_uri = await new_draft(
        served, editor, "HumanMadeObject", painting("Engine", maker_uri, known_uri)
    )

    response = await publish(served, editor, object_id, NO_PRIMARY_NAME, summary="Bundle")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 1 and body["published"][0] == str(object_id)
    assert body["published"][1:] == sorted([str(group_id), str(maker_id)])
    for record_id in (object_id, maker_id, group_id):
        after = await stored(db_engine, record_id)
        assert after.counts == (1, 1, 1, 0, 0)
        assert after.record is not None
        assert (after.record.version, after.record.draft_epoch) == (1, 1)
        assert (after.events[0].from_version, after.events[0].to_version) == (None, 1)
        assert after.revision(1).summary == "Bundle"
        assert after.revision(1).published_by == editor.id
        assert await network.rooms.get(record_id) is None
    assert (await stored(db_engine, maker_id)).revision(1).authors == sorted([other.id, editor.id])
    assert (await stored(db_engine, object_id)).revision(1).authors == [editor.id]
    # References inside the bundle are normalized: the targets have labels once it commits.
    object_data = (await stored(db_engine, object_id)).record.data  # type: ignore[union-attr]
    assert object_data["produced_by"]["carried_out_by"] == [
        {"id": maker_uri, "type": "Person"},
        {"id": known_uri, "type": "Person"},
    ]
    maker_data = (await stored(db_engine, maker_id)).record.data  # type: ignore[union-attr]
    assert maker_data["member_of"] == [{"id": group_uri, "type": "Group"}]
    shown = await served.client.get(f"/api/records/{object_id}", headers=editor.headers)
    assert [ref["_label"] for ref in shown.json()["produced_by"]["carried_out_by"]] == [
        "Ada",
        "Charles",
    ]
    # The published record that was only referred to keeps its version and its draft.
    known = await stored(db_engine, known_id)
    assert known.record is not None and known.record.version == 1
    assert known.counts == (1, 1, 1, 1, 1)


async def test_references_inside_the_bundle_lose_their_labels_whatever_the_order(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """Each record refers to the other, so one is written before the other has a label row."""
    editor = await caller(db_engine)
    first_id, first_uri = await new_draft(served, editor, "Group", person("First workshop"))
    second_id, second_uri = await new_draft(
        served,
        editor,
        "Group",
        {
            **person("Second workshop"),
            "member_of": [{"id": first_uri, "type": "Group", "_label": "stale"}],
        },
    )
    await network.rooms.close(first_id, discard=True)
    await network.rooms.open(
        first_id,
        content=new_record_data(
            "Group",
            first_uri,
            1,
            {
                **person("First workshop"),
                "member_of": [{"id": second_uri, "type": "Group", "_label": "stale"}],
                "classified_as": [
                    {"id": "https://nowhere.example/unknown", "type": "Type", "_label": "Kept"}
                ],
            },
        ),
    )

    response = await publish(served, editor, first_id)

    assert response.json()["published"] == [str(first_id), str(second_id)], response.text
    first = (await stored(db_engine, first_id)).record
    second = (await stored(db_engine, second_id)).record
    assert first is not None and second is not None
    assert first.data["member_of"] == [{"id": second_uri, "type": "Group"}]
    assert second.data["member_of"] == [{"id": first_uri, "type": "Group"}]
    # A reference nothing is known about keeps the only label there is (ADR-0006).
    assert first.data["classified_as"][0]["_label"] == "Kept"
    assert (first.draft_epoch, second.draft_epoch) == (2, 1)  # one discard, one publish


async def test_a_stub_without_a_draft_blocks_the_publish(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    maker_id, maker_uri = await new_draft(served, editor, "Person", person())
    object_id, _ = await new_draft(served, editor, "HumanMadeObject", painting("Engine", maker_uri))
    await network.rooms.close(maker_id, discard=True)

    response = await publish(served, editor, object_id, NO_PRIMARY_NAME)

    assert response.status_code == 409
    assert response.json()["code"] == errors.NO_DRAFT
    assert response.json()["params"] == {"id": str(maker_id)}
    assert (await stored(db_engine, object_id)).counts == UNTOUCHED
    assert (await stored(db_engine, maker_id)).counts == (0, 0, 0, 0, 0)


async def test_a_violation_in_a_stub_blocks_the_whole_bundle(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    maker_id, maker_uri = await new_draft(served, editor, "Person", person_with_violation())
    object_id, _ = await new_draft(served, editor, "HumanMadeObject", painting("Engine", maker_uri))

    response = await publish(served, editor, object_id, NO_PRIMARY_NAME)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == errors.VALIDATION_FAILED
    assert list(body["params"]["records"]) == [str(maker_id)]
    (diagnostic,) = body["params"]["records"][str(maker_id)]
    assert (diagnostic["code"], diagnostic["path"], diagnostic["severity"]) == (
        "LAE-RULE-003",
        "/identified_by/0/content",
        "violation",
    )
    for record_id in (object_id, maker_id):
        after = await stored(db_engine, record_id)
        assert after.counts == UNTOUCHED
        assert after.record is not None and after.record.version == 0
        assert await network.rooms.get(record_id) is not None


async def test_a_reference_must_agree_with_the_class_of_the_stub_it_names(
    served: Served, db_engine: AsyncEngine
) -> None:
    """Once published, the stub's class is known; the bundle is validated as if it already were.

    On its own the draft passes: a reference to a record without a label row is taken at its
    word (ADR-0025, item 5). Published like that, the object would be invalid from then on.
    """
    editor = await caller(db_engine)
    place_id, place_uri = await new_draft(served, editor, "Place", person("Somewhere"))
    record_id, uri = await new_draft(
        served, editor, "HumanMadeObject", painting("Engine", place_uri)
    )
    alone = await served.client.post(
        f"/api/records/{record_id}/draft/validate", headers=editor.headers
    )
    assert alone.json()["ok"] is True

    response = await publish(served, editor, record_id, NO_PRIMARY_NAME)

    assert response.status_code == 422, response.text
    found = response.json()["params"]["records"][str(record_id)]
    violations = [item for item in found if item["severity"] == "violation"]
    assert [item["path"] for item in violations] == ["/produced_by/carried_out_by/0"]
    for bundled in (record_id, place_id):
        assert (await stored(db_engine, bundled)).counts == UNTOUCHED


# -- validation ---------------------------------------------------------------------------------


async def test_a_violation_blocks_and_nothing_is_written(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person_with_violation())
    before = (await network.rooms.snapshot(record_id)).jsonld

    response = await publish(served, editor, record_id)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == errors.VALIDATION_FAILED
    assert body["path"] == f"/api/records/{record_id}/publish"
    assert [item["code"] for item in body["params"]["records"][str(record_id)]] == ["LAE-RULE-003"]
    after = await stored(db_engine, record_id)
    assert after.counts == UNTOUCHED
    assert after.record is not None
    assert (after.record.version, after.record.draft_epoch) == (0, 0)
    assert (await network.rooms.snapshot(record_id)).jsonld == before


async def test_a_warning_must_be_accepted_by_code_and_exact_path(
    served: Served, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person_with_warning())
    warning = {"code": REPAIR_WARNING, "path": WARNING_PATH}

    for accepted in (
        [],
        [{"code": REPAIR_WARNING, "path": "/classified_as/1/id"}],  # where it was before an edit
        [{"code": "LAE-SHACL-002", "path": WARNING_PATH}],
        [{**warning, "record_id": str(uuid7())}],  # accepted, but for another record
    ):
        response = await publish(served, editor, record_id, *accepted)
        assert response.status_code == 409, accepted
        body = response.json()
        assert body["code"] == errors.WARNINGS_NOT_ACCEPTED
        assert body["params"]["unaccepted"] == [{"record_id": str(record_id), **warning}]
        (shown,) = body["params"]["records"][str(record_id)]
        assert (shown["code"], shown["path"], shown["severity"]) == (
            REPAIR_WARNING,
            WARNING_PATH,
            "warning",
        )
        assert shown["params"]["encoded"].endswith("Self%20Portrait.jpg")
        assert (await stored(db_engine, record_id)).counts == UNTOUCHED

    # Accepting more than there is does no harm; what is recorded is what was there.
    response = await publish(served, editor, record_id, warning, NO_PRIMARY_NAME)
    assert response.status_code == 200, response.text
    after = await stored(db_engine, record_id)
    assert after.revision(1).accepted_warnings == [warning]
    # The id is stored as it was written; only the RDF has the encoded form (ADR-0028).
    assert after.record is not None
    assert after.record.data["classified_as"][0]["id"].endswith("Self Portrait.jpg")
    assert any("Self%20Portrait.jpg" in line for line in after.triples(1))


async def test_a_warning_in_a_stub_is_accepted_for_that_stub(
    served: Served, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    maker_id, maker_uri = await new_draft(served, editor, "Person", person_with_warning())
    object_id, _ = await new_draft(served, editor, "HumanMadeObject", painting("Engine", maker_uri))
    warning = {"code": REPAIR_WARNING, "path": WARNING_PATH}

    response = await publish(served, editor, object_id, NO_PRIMARY_NAME, warning)
    assert response.status_code == 409
    body = response.json()
    assert body["params"]["unaccepted"] == [{"record_id": str(maker_id), **warning}]
    assert set(body["params"]["records"]) == {str(object_id), str(maker_id)}

    response = await publish(
        served, editor, object_id, NO_PRIMARY_NAME, {**warning, "record_id": str(maker_id)}
    )
    assert response.status_code == 200, response.text
    assert (await stored(db_engine, object_id)).revision(1).accepted_warnings == [NO_PRIMARY_NAME]
    assert (await stored(db_engine, maker_id)).revision(1).accepted_warnings == [warning]


# -- bulk-loaded records ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("built_with", "drift_check"),
    [
        ((1, EXPANDER_VERSION), False),
        ((None, None), True),  # nobody recorded what the index was built with
        ((1, EXPANDER_VERSION - 1), True),
        ((2, EXPANDER_VERSION), True),
        ((1, None), True),
    ],
)
async def test_first_edit_of_a_bulk_loaded_record_stores_the_loaded_version_too(
    served: Served,
    network: Network,
    db_engine: AsyncEngine,
    built_with: tuple[int | None, int | None],
    drift_check: bool,
) -> None:
    await set_bootstrap_versions(db_engine, *built_with)
    editor = await caller(db_engine)
    uri = new_uri()
    loaded = titled_record(uri, "A title", "Un titre", f"{AAT}300033618")
    row = await insert_record(db_engine, loaded, version=1)
    record_id = id_of(uri)
    assert (await stored(db_engine, record_id)).counts == (0, 0, 0, 0, 0)
    await open_draft(network, record_id)
    await edit(network, record_id, editor.id, add_name("Another title"))

    response = await publish(served, editor, record_id)

    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    after = await stored(db_engine, record_id)
    assert after.counts == (2, 1, 1, 0, 0)
    first, second = after.revisions
    assert (first.version, second.version) == (1, 2)
    assert (first.published_by, first.authors, first.summary) == (None, [], "")
    assert first.accepted_warnings == [] and first.context_version == 1
    assert first.created_at == row.updated_at
    assert after.jsonld(1) == loaded
    assert after.triples(1) == expand_to_ntriples(loaded, 1, record_uri=uri)
    assert second.published_by == editor.id
    assert after.triples(2) == expand_to_ntriples(after.jsonld(2), 1, record_uri=uri)
    (event,) = after.events
    assert (event.from_version, event.to_version) == (1, 2)
    assert event.payload == {"context_version": 1, "drift_check": drift_check}

    # Version 1 stays readable, with the entity tag it had while it was current.
    old = await served.client.get(f"/api/records/{record_id}/revisions/1", headers=editor.headers)
    assert old.status_code == 200
    assert [name["content"] for name in old.json()["identified_by"]] == ["A title", "Un titre"]
    listed = await served.client.get(f"/api/records/{record_id}/revisions", headers=editor.headers)
    assert [(entry["version"], entry["published_by"]) for entry in listed.json()] == [
        (1, None),
        (2, str(editor.id)),
    ]

    # The next edit is an ordinary one: both revisions exist, so the diff is exact.
    await open_draft(network, record_id)
    await edit(network, record_id, editor.id, add_name("A third title"))
    assert (await publish(served, editor, record_id)).json()["version"] == 3
    final = await stored(db_engine, record_id)
    assert [revision.version for revision in final.revisions] == [1, 2, 3]
    assert final.events[-1].payload == {"context_version": 1, "drift_check": False}


async def test_a_loaded_version_that_cannot_be_expanded_is_kept_and_flagged(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """Its triples are unknown, so the worker must not diff against them (ADR-0007)."""
    await set_bootstrap_versions(db_engine, 1, EXPANDER_VERSION)
    editor = await caller(db_engine)
    uri = new_uri()
    good = titled_record(uri, "A title", "Un titre", f"{AAT}300033618")
    loaded = json.loads(json.dumps(good))
    loaded["produced_by"]["timespan"]["begin_of_the_begin"] = "1880"  # not an xsd:dateTime
    await insert_record(db_engine, loaded, version=1)
    record_id = id_of(uri)
    await network.rooms.open(record_id, content=good)

    response = await publish(served, editor, record_id)

    assert response.status_code == 200, response.text
    after = await stored(db_engine, record_id)
    assert after.jsonld(1) == loaded and after.triples(1) == frozenset()
    assert after.events[0].payload == {"context_version": 1, "drift_check": True}
    assert after.triples(2) == expand_to_ntriples(good, 1, record_uri=uri)


# -- what else a publish settles ----------------------------------------------------------------


async def test_a_reference_to_an_alias_is_stored_as_the_record_uri(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """A draft can name a record by a URI it had before a bulk load re-minted it (ADR-0030).

    The alias is resolved first: the resolved document is the one validated (its class checks
    use the record's facts), searched for stubs, and stored, in ``data``, the JSON-LD snapshot
    and the triples.
    """
    editor = await caller(db_engine)
    maker_id, maker_uri = await new_draft(served, editor, "Person", person("Ada"))
    assert (await publish(served, editor, maker_id)).status_code == 200
    maker_alias = f"https://old.example.org/person/{maker_id}"
    await add_alias(db_engine, maker_alias, maker_id)
    # An alias of a never-published record: resolving it first makes that record a stub.
    group_id, group_uri = await new_draft(served, editor, "Group", person("Analytical Society"))
    group_alias = f"https://old.example.org/group/{group_id}"
    await add_alias(db_engine, group_alias, group_id)
    template = painting("Engine", maker_alias, group_alias)
    template["produced_by"]["carried_out_by"][1]["type"] = "Group"
    object_id, object_uri = await new_draft(served, editor, "HumanMadeObject", template)
    draft = (await network.rooms.snapshot(object_id)).jsonld
    assert [ref["id"] for ref in draft["produced_by"]["carried_out_by"]] == [
        maker_alias,
        group_alias,
    ]

    response = await publish(served, editor, object_id, NO_PRIMARY_NAME)

    assert response.status_code == 200, response.text
    assert response.json()["published"] == [str(object_id), str(group_id)]
    after = await stored(db_engine, object_id)
    assert after.record is not None
    data = after.record.data
    assert data["id"] == object_uri
    assert data["produced_by"]["carried_out_by"] == [
        {"id": maker_uri, "type": "Person"},
        {"id": group_uri, "type": "Group"},
    ]
    assert after.jsonld(1) == data
    triples = after.triples(1)
    assert after.triples(1) == expand_to_ntriples(data, 1, record_uri=object_uri)
    assert any(f"<{maker_uri}>" in line for line in triples)
    assert any(f"<{group_uri}>" in line for line in triples)
    assert not any("old.example.org" in line for line in triples)
    assert "old.example.org" not in json.dumps(after.jsonld(1))
    assert (await stored(db_engine, maker_id)).record.version == 1  # type: ignore[union-attr]

    # Validation sees the record the alias names: a Person cited as a Place is a violation.
    place_alias = f"https://old.example.org/place/{maker_id}"
    await add_alias(db_engine, place_alias, maker_id)
    wrong = painting("Mill", place_alias)
    wrong["produced_by"]["carried_out_by"][0]["type"] = "Place"
    wrong_id, _ = await new_draft(served, editor, "HumanMadeObject", wrong)
    refused = await publish(served, editor, wrong_id, NO_PRIMARY_NAME)
    assert refused.status_code == 422, refused.text
    found = refused.json()["params"]["records"][str(wrong_id)]
    assert [item["path"] for item in found if item["severity"] == "violation"] == [
        "/produced_by/carried_out_by/0"
    ]
    assert (await stored(db_engine, wrong_id)).counts == UNTOUCHED


async def test_open_schema_findings_are_resolved_by_the_publish(
    served: Served, network: Network, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bulk load records the schema failures of what it loads; a publish settles them."""
    editor = await caller(db_engine)
    uri, other_uri = new_uri(), new_uri()
    for loaded_uri in (uri, other_uri):
        loaded = titled_record(loaded_uri, "A title", "Un titre", f"{AAT}300033618")
        await insert_record(db_engine, loaded, version=1)
    record_id, other_id = id_of(uri), id_of(other_uri)
    await add_finding(db_engine, record_id, "LAE-SCHEMA-required")
    await add_finding(db_engine, record_id, "LAE-SCHEMA-type", QaFindingStatus.CLAIMED)
    await add_finding(db_engine, record_id, "LAE-SCHEMA-enum", QaFindingStatus.DISMISSED)
    await add_finding(db_engine, record_id, "LAE-QA-001")
    await add_finding(db_engine, other_id, "LAE-SCHEMA-required")
    await open_draft(network, record_id)
    await edit(network, record_id, editor.id, add_name("Another title"))

    async def findings(of: UUID) -> list[tuple[str, QaFindingStatus, UUID | None]]:
        return [
            (finding.code, finding.status, finding.resolved_by)
            for finding in (await stored(db_engine, of)).findings
        ]

    untouched = [
        ("LAE-QA-001", QaFindingStatus.OPEN, None),
        ("LAE-SCHEMA-enum", QaFindingStatus.DISMISSED, None),
        ("LAE-SCHEMA-required", QaFindingStatus.OPEN, None),
        ("LAE-SCHEMA-type", QaFindingStatus.CLAIMED, None),
    ]
    assert await findings(record_id) == untouched

    # A publish that fails inside its transaction resolves nothing.
    class Failed(Exception):
        pass

    async def failing(*args: Any, **kwargs: Any) -> int:
        raise Failed

    with monkeypatch.context() as patched:
        patched.setattr(store, "delete_draft", failing)
        with pytest.raises(Failed):
            await publish(served, editor, record_id)
    assert await findings(record_id) == untouched

    response = await publish(served, editor, record_id)

    assert response.status_code == 200, response.text
    assert await findings(record_id) == [
        ("LAE-QA-001", QaFindingStatus.OPEN, None),
        ("LAE-SCHEMA-enum", QaFindingStatus.DISMISSED, None),
        ("LAE-SCHEMA-required", QaFindingStatus.RESOLVED, editor.id),
        ("LAE-SCHEMA-type", QaFindingStatus.RESOLVED, editor.id),
    ]
    assert await findings(other_id) == [("LAE-SCHEMA-required", QaFindingStatus.OPEN, None)]


async def test_inline_metatypes_are_harvested_at_publish(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    """A concept's metatype stated where a record cites it reaches its label row (ADR-0032)."""
    editor = await caller(db_engine)
    concept, unknown = (f"https://vocab.example.org/concept/{uuid7()}" for _ in range(2))
    nationality = f"https://vocab.example.org/metatype/{uuid7()}"
    async with AsyncSession(db_engine) as session:
        session.add(EntityLabel(uri=concept, type="Type", default_label="Dutch", source="aat"))
        await session.commit()
    metatype = [{"id": nationality, "type": "Type", "_label": "Nationality"}]
    template = {
        **person("Rembrandt"),
        "classified_as": [
            {"id": concept, "type": "Type", "_label": "Dutch", "classified_as": metatype},
            {"id": unknown, "type": "Type", "_label": "Painter", "classified_as": metatype},
        ],
    }
    record_id, _ = await new_draft(served, editor, "Person", template)

    response = await publish(served, editor, record_id)

    assert response.status_code == 200, response.text
    async with AsyncSession(db_engine) as session:
        row = await session.get(EntityLabel, concept)
        assert row is not None and row.metatype_uris == [nationality]
        assert row.default_label == "Dutch" and row.source == "aat"
        assert await session.get(EntityLabel, unknown) is None
        assert await session.get(EntityLabel, nationality) is None


async def test_a_draft_of_another_class_changes_the_record_type(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, uri = await new_draft(served, editor, "Person", person("The Workshop"))
    assert (await publish(served, editor, record_id)).status_code == 200
    published = (await stored(db_engine, record_id)).record
    assert published is not None and published.type == "Person"
    await network.rooms.open(record_id, content={**published.data, "type": "Group"})

    assert (await publish(served, editor, record_id)).status_code == 200

    after = await stored(db_engine, record_id)
    assert after.record is not None and after.label is not None
    assert (after.record.type, after.record.data["type"], after.label.type) == (
        "Group",
        "Group",
        "Group",
    )


async def test_the_publish_stands_when_the_room_cannot_be_closed_afterwards(
    served: Served, network: Network, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person())

    async def unreachable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("crdt-ws went away")

    with monkeypatch.context() as patched:
        patched.setattr(CrdtWsClient, "close", unreachable)
        response = await publish(served, editor, record_id)

    assert response.status_code == 200
    assert (await stored(db_engine, record_id)).counts == (1, 1, 1, 0, 0)
    # The room that outlived its draft is retired at the next open, and the next draft is new.
    stale = await network.rooms.get(record_id)
    assert stale is not None and stale.draft_epoch == 0
    assert await open_draft(network, record_id) == 1
    assert (await network.rooms.snapshot(record_id)).base_version == 1


# -- who may publish, and what can be published ------------------------------------------------


async def test_publish_needs_the_publish_permission(
    served: Served, db_engine: AsyncEngine, network: Network
) -> None:
    collaborator = await caller(db_engine, "Collaborator")
    record_id, _ = await new_draft(served, collaborator, "Person", person())

    refused = await publish(served, collaborator, record_id)
    assert refused.status_code == 403
    assert refused.json()["code"] == "LAE-AUTH-001"
    assert refused.json()["params"]["missing"] == ["publish"]
    anonymous = await served.client.post(f"/api/records/{record_id}/publish", json={})
    assert anonymous.status_code == 401
    assert (await stored(db_engine, record_id)).counts == UNTOUCHED

    # The service checks for itself: other callers (agents, Phase 3) do not come through HTTP.
    principal = CurrentUser(
        id=collaborator.id,
        display_name="C",
        kind=UserKind.HUMAN,
        roles=frozenset({"Collaborator"}),
        permissions=frozenset({"edit_draft"}),
        via="token",
    )
    with pytest.raises(Exception) as raised:
        await publish_service(
            record_id,
            principal,
            "",
            [],
            sessions=served.app.state.sessionmaker,
            crdt=CrdtWsClient.from_settings(
                served.app.state.settings, served.app.state.http_client
            ),
            settings=served.app.state.settings,
        )
    assert getattr(raised.value, "code", None) == "LAE-AUTH-001"
    assert (await stored(db_engine, record_id)).counts == UNTOUCHED


async def test_what_cannot_be_published(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)

    unknown = await publish(served, editor, UUID(str(uuid7())))
    assert (unknown.status_code, unknown.json()["code"]) == (404, "LAE-REC-001")

    record_id, _ = await new_draft(served, editor, "Person", person())
    assert (await publish(served, editor, record_id)).status_code == 200
    again = await publish(served, editor, record_id)
    assert (again.status_code, again.json()["code"]) == (409, errors.NO_DRAFT)
    assert again.json()["params"] == {"id": str(record_id)}
    assert (await stored(db_engine, record_id)).counts == (1, 1, 1, 0, 0)

    for status in (RecordStatus.DELETED, RecordStatus.MERGED):
        uri = new_uri()
        data = titled_record(uri, "A title", "Un titre", f"{AAT}300033618")
        await insert_record(db_engine, data, version=2, status=status)
        gone = await publish(served, editor, id_of(uri))
        assert (gone.status_code, gone.json()["code"]) == (409, errors.NOT_PUBLISHABLE)
        assert gone.json()["params"] == {"id": str(id_of(uri)), "status": status.value}

    too_long = await served.client.post(
        f"/api/records/{record_id}/publish", json={"summary": "x" * 2001}, headers=editor.headers
    )
    assert too_long.status_code == 422


async def test_nothing_is_published_while_crdt_ws_is_unreachable(
    served: Served, network: Network, db_engine: AsyncEngine
) -> None:
    editor = await caller(db_engine)
    record_id, _ = await new_draft(served, editor, "Person", person())
    network.crdt_down = True

    response = await publish(served, editor, record_id)

    assert (response.status_code, response.json()["code"]) == (503, "LAE-CRDT-009")
    assert (await stored(db_engine, record_id)).counts == UNTOUCHED
