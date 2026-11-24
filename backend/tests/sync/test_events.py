"""Translation of outbox events into SPARQL Updates (Step 1.11 contract §3 items 1-9; tests 1-10).

Events come from the real publish path; ``process_batch`` runs with a recording client.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any
from uuid import UUID

import asyncpg
import pyoxigraph
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from lae.corpus import CORPUS_DIR, corpus_files, load_record
from lae.db.models import OutboxEventKind
from lae.ids import NODE_PATH, derived_node_iri
from lae.rdf import EXPANDER_VERSION, compress, diff_triples, owned_subjects
from lae.sparql.updates import build_delete_where_subjects, build_update
from lae.sync.errors import REVISION_MISSING, UNREADABLE, UNTRANSLATABLE, EventError
from lae.sync.events import Event, Mode, build_event_update, subjects_of, translate_event
from lae.sync.worker import process_batch
from tests.api.conftest import AAT, insert_record, titled_record
from tests.publish.conftest import Caller, publish
from tests.publish.test_publish import set_bootstrap_versions
from tests.sync.conftest import (
    Network,
    RecordingClient,
    Served,
    all_events,
    current_data,
    event,
    expected_store,
    id_of,
    insert_event,
    new_uri,
    operations,
    pending_events,
    person,
    publish_content,
    publish_edit,
    publish_new,
    publish_ok,
    rename,
    revision_jsonld,
    revision_lines,
    triples_of,
)

RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"

pytestmark = pytest.mark.usefixtures("queue")

DELETE_WHERE = re.compile(r"DELETE WHERE \{ <([^>]*)> \?p \?o \}")
ELSEWHERE = "https://elsewhere.test/statement/1"
"""An embedded node's own id: its triples are under a derived node IRI (ADR-0021)."""


async def only_event(conn: asyncpg.Connection) -> Event:
    (pending,) = await pending_events(conn)
    return pending


def delete_where_subjects(update: str) -> list[str]:
    return DELETE_WHERE.findall(update)


async def all_subjects(
    conn: asyncpg.Connection, record_id: UUID, uri: str, *versions: int
) -> set[str]:
    subjects: set[str] = set()
    for version in versions:
        subjects |= owned_subjects(await revision_jsonld(conn, record_id, version), uri)
        subjects |= subjects_of(await revision_lines(conn, record_id, version))
    return subjects


# -- 1-4: insert and diff -----------------------------------------------------------------------


async def test_a_first_publish_inserts_every_triple_and_deletes_nothing(
    served: Served, editor: Caller, conn: asyncpg.Connection
) -> None:
    record_id, _ = await publish_new(served, editor, person("Ada Lovelace"))
    # A revision 0 that cannot be read: looking it up would fail the translation.
    await conn.execute(
        "INSERT INTO record_revisions (record_id, version, ntriples_zstd, jsonld_zstd, "
        "context_version) VALUES ($1, 0, $2, $2, 1)",
        record_id,
        b"not zstd",
    )
    lines = await revision_lines(conn, record_id, 1)
    client = RecordingClient()

    result = await process_batch(conn, client)

    assert result.outcome == "done" and result.updates_sent == 1
    assert client.sent == [build_update([], lines)]
    assert client.sent[0].startswith("INSERT DATA {") and "DELETE" not in client.sent[0]
    assert len(operations(client.sent[0])) == 1


async def test_a_changed_title_is_one_delete_and_one_insert(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    record_id, _ = await publish_new(served, editor, person("Night Watch"))
    client = RecordingClient()
    assert (await process_batch(conn, client)).outcome == "done"
    await publish_edit(served, network, editor, db_engine, record_id, rename("The Night Watch"))

    result = await process_batch(conn, client)

    assert result.outcome == "done"
    update = client.sent[-1]
    delete, insert = operations(update)
    assert delete.startswith("DELETE DATA {") and insert.startswith("INSERT DATA {")
    deleted, inserted = delete.splitlines()[1:-1], insert.splitlines()[1:-1]
    # The name's content and the root _label derived from it (Step 1.19a): nothing else.
    assert len(deleted) == 2 and all('"Night Watch"' in line for line in deleted)
    assert len(inserted) == 2 and all('"The Night Watch"' in line for line in inserted)
    assert sum(RDFS_LABEL in line for line in deleted) == 1
    assert sum(RDFS_LABEL in line for line in inserted) == 1


async def test_the_diff_is_between_the_two_revisions_not_the_current_data(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    record_id, _ = await publish_new(served, editor, person("One"))
    client = RecordingClient()
    assert (await process_batch(conn, client)).outcome == "done"
    await publish_edit(served, network, editor, db_engine, record_id, rename("Two"))
    await publish_edit(served, network, editor, db_engine, record_id, rename("Three"))
    assert (await current_data(db_engine, record_id))["identified_by"][0]["content"] == "Three"
    v1, v2, v3 = [await revision_lines(conn, record_id, v) for v in (1, 2, 3)]
    first, second = await pending_events(conn)

    assert await translate_event(conn, first) == build_update(*diff_triples(v1, v2))
    assert await translate_event(conn, second) == build_update(*diff_triples(v2, v3))
    result = await process_batch(conn, client)
    assert result.done == (first.id, second.id)
    assert client.sent[-1] == " ;\n".join(
        [build_update(*diff_triples(v1, v2)), build_update(*diff_triples(v2, v3))]
    )


async def test_a_publish_that_changes_no_triple_sends_nothing(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    record_id, _ = await publish_new(served, editor, person("Unchanged"))
    client = RecordingClient()
    assert (await process_batch(conn, client)).outcome == "done"
    await publish_edit(served, network, editor, db_engine, record_id, lambda data: None)
    pending = await only_event(conn)
    assert (pending.from_version, pending.to_version) == (1, 2)

    assert await translate_event(conn, pending) == ""
    result = await process_batch(conn, client)

    assert (result.outcome, result.done, result.updates_sent) == ("done", (pending.id,), 0)
    assert len(client.sent) == 1  # only the first publish
    assert [row["status"] for row in await all_events(conn)] == ["done", "done"]


# -- 5-8: replace and retract -------------------------------------------------------------------


def loaded_record(uri: str) -> dict[str, Any]:
    """A bulk-loaded record: two names with internal node IRIs and a statement with its own id."""
    data = titled_record(uri, "A title", "Un titre", f"{AAT}300033618")
    data["referred_to_by"] = [
        {"id": ELSEWHERE, "type": "LinguisticObject", "content": "A statement from elsewhere"}
    ]
    return data


async def test_drift_check_retracts_the_owned_subjects_of_the_previous_json_ld(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    await set_bootstrap_versions(db_engine, None, None)  # nobody knows what built the index
    uri = new_uri()
    loaded = loaded_record(uri)
    await insert_record(db_engine, loaded, version=1)
    record_id = id_of(uri)
    removed = loaded["identified_by"][1]["id"]
    edited = json.loads(json.dumps(loaded))
    del edited["identified_by"][1]  # the edit removes an embedded node
    await publish_content(served, network, editor, record_id, edited)
    pending = await only_event(conn)
    assert pending.drift_check and (pending.from_version, pending.to_version) == (1, 2)
    owned = owned_subjects(await revision_jsonld(conn, record_id, 1), uri)
    v2 = await revision_lines(conn, record_id, 2)
    assert removed in owned and derived_node_iri(uri, ELSEWHERE) in owned
    assert removed not in subjects_of(v2)

    update = await translate_event(conn, pending)

    assert update == build_delete_where_subjects(owned) + " ;\n" + build_update([], v2)
    assert sorted(delete_where_subjects(update)) == sorted(owned)
    assert "DELETE DATA" not in update
    assert operations(update)[-1].startswith("INSERT DATA {")
    assert len(operations(update)) == len(owned) + 1


async def test_a_back_filled_revision_without_triples_is_replaced_not_diffed(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    await set_bootstrap_versions(db_engine, 1, EXPANDER_VERSION)
    uri = new_uri()
    good = titled_record(uri, "A title", "Un titre", f"{AAT}300033618")
    loaded = json.loads(json.dumps(good))
    loaded["produced_by"]["timespan"]["begin_of_the_begin"] = "1880"  # cannot be expanded
    await insert_record(db_engine, loaded, version=1)
    record_id = id_of(uri)
    await publish_content(served, network, editor, record_id, good)
    back_filled = await conn.fetchrow(
        "SELECT published_by FROM record_revisions WHERE record_id = $1 AND version = 1",
        record_id,
    )
    assert back_filled is not None and back_filled["published_by"] is None
    assert await revision_lines(conn, record_id, 1) == frozenset()
    owned = owned_subjects(loaded, uri)
    v2 = await revision_lines(conn, record_id, 2)
    expected = build_delete_where_subjects(owned) + " ;\n" + build_update([], v2)

    pending = await only_event(conn)
    assert pending.drift_check
    assert await translate_event(conn, pending) == expected
    await conn.execute(
        "UPDATE outbox_events SET payload = jsonb_set(payload, '{drift_check}', 'false') "
        "WHERE id = $1",
        pending.id,
    )
    pending = await event(conn, pending.id)
    assert not pending.drift_check
    assert await translate_event(conn, pending) == expected


async def test_a_delete_event_retracts_every_owned_subject(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    record_id, uri = await publish_new(served, editor, person("To be deleted", "Second name"))
    await publish_edit(served, network, editor, db_engine, record_id, rename("Renamed"))
    client = RecordingClient()
    assert (await process_batch(conn, client)).outcome == "done"
    event_id = await insert_event(conn, record_id, "delete", 2, None)
    subjects = await all_subjects(conn, record_id, uri, 2)

    update = await translate_event(conn, await event(conn, event_id))
    result = await process_batch(conn, client)

    assert update == build_delete_where_subjects(subjects)
    assert all(DELETE_WHERE.fullmatch(op) for op in operations(update))
    assert sorted(delete_where_subjects(update)) == sorted(subjects)
    assert result.done == (event_id,) and client.sent[-1] == update
    assert len(client.store) == 0


async def test_an_event_after_a_dead_letter_replaces_from_the_last_applied_version(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    record_id, uri = await publish_new(served, editor, person("Version one", "Dropped later"))
    client = RecordingClient()
    assert (await process_batch(conn, client)).outcome == "done"

    def second(data: dict[str, Any]) -> None:
        data["identified_by"][0]["content"] = "Version two"
        del data["identified_by"][1]  # a node only v1 has

    await publish_edit(served, network, editor, db_engine, record_id, second)
    (failed,) = await pending_events(conn)
    await conn.execute(
        "UPDATE outbox_events SET status = 'failed', attempts = 5, processed_at = now(), "
        "last_error = 'LAE-SYNC-004 {}' WHERE id = $1",
        failed.id,
    )
    await publish_edit(served, network, editor, db_engine, record_id, rename("Version three"))
    heal = await only_event(conn)
    subjects = await all_subjects(conn, record_id, uri, 1, 2)
    v3 = await revision_lines(conn, record_id, 3)

    update = await translate_event(conn, heal)

    assert update == build_delete_where_subjects(subjects) + " ;\n" + build_update([], v3)
    v1_only = subjects_of(await revision_lines(conn, record_id, 1)) - subjects_of(
        await revision_lines(conn, record_id, 2)
    )
    assert v1_only and v1_only <= set(delete_where_subjects(update))
    assert (await process_batch(conn, client)).done == (heal.id,)
    await publish_edit(served, network, editor, db_engine, record_id, rename("Version four"))
    after = await only_event(conn)
    v4 = await revision_lines(conn, record_id, 4)
    assert await translate_event(conn, after) == build_update(*diff_triples(v3, v4))


async def test_an_event_after_two_dead_letters_replaces_from_the_first(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    """Review of PR #148 (item 4): ``lo`` is the smallest version of the whole failed run."""
    record_id, uri = await publish_new(served, editor, person("Version one", "Dropped later"))
    client = RecordingClient()
    assert (await process_batch(conn, client)).outcome == "done"

    def second(data: dict[str, Any]) -> None:
        data["identified_by"][0]["content"] = "Version two"
        del data["identified_by"][1]  # a node only v1 has

    await publish_edit(served, network, editor, db_engine, record_id, second)
    await publish_edit(served, network, editor, db_engine, record_id, rename("Version three"))
    await conn.execute(
        "UPDATE outbox_events SET status = 'failed', attempts = 5, processed_at = now(), "
        "last_error = 'LAE-SYNC-004 {}' WHERE status = 'pending'"
    )
    await publish_edit(served, network, editor, db_engine, record_id, rename("Version four"))
    heal = await only_event(conn)
    assert (heal.from_version, heal.to_version) == (3, 4)
    subjects = await all_subjects(conn, record_id, uri, 1, 2, 3)
    v4 = await revision_lines(conn, record_id, 4)

    update = await translate_event(conn, heal)

    assert update == build_delete_where_subjects(subjects) + " ;\n" + build_update([], v4)
    v1_only = subjects_of(await revision_lines(conn, record_id, 1)) - subjects_of(
        await revision_lines(conn, record_id, 3)
    )
    assert v1_only and v1_only <= set(delete_where_subjects(update))
    assert (await process_batch(conn, client)).done == (heal.id,)
    assert triples_of(client.store) == expected_store(v4)


# -- 9-10: errors and the shape of every update -------------------------------------------------


async def test_translation_errors_carry_their_code(
    served: Served, editor: Caller, conn: asyncpg.Connection
) -> None:
    record_id, uri = await publish_new(served, editor, person("Errors"))

    async def error_of(kind: str, from_version: int | None, to_version: int | None) -> EventError:
        event_id = await insert_event(conn, record_id, kind, from_version, to_version)
        with pytest.raises(EventError) as raised:
            await translate_event(conn, await event(conn, event_id))
        return raised.value

    missing = await error_of("publish", 1, 2)
    assert missing.code == REVISION_MISSING
    assert missing.params == {
        "event": missing.params["event"],
        "record_id": record_id,
        "version": 2,
    }
    assert str(missing).startswith("LAE-SYNC-001 {")

    for blob, detail in (
        (b"not zstd at all", "ZstdError"),
        (compress(b"<https://x.test/a> <https://x.test/b> <https://x.test/c>.\n"), "canonical"),
    ):
        await conn.execute(
            "INSERT INTO record_revisions (record_id, version, ntriples_zstd, jsonld_zstd, "
            "context_version) VALUES ($1, 2, $2, $3, 1) ON CONFLICT (record_id, version) "
            "DO UPDATE SET ntriples_zstd = EXCLUDED.ntriples_zstd",
            record_id,
            blob,
            compress(b"{}"),
        )
        unreadable = await error_of("publish", 1, 2)
        assert unreadable.code == UNREADABLE and unreadable.params["version"] == 2
        assert detail in unreadable.params["detail"]

    for kind, from_version, to_version in (
        ("merge", 1, 2),
        ("split", 1, 2),
        ("publish", 1, None),
        ("delete", None, None),
    ):
        untranslatable = await error_of(kind, from_version, to_version)
        assert untranslatable.code == UNTRANSLATABLE
        assert untranslatable.params == {"event": untranslatable.params["event"], "kind": kind}

    gone = Event(
        id=10**12,
        kind=OutboxEventKind.PUBLISH,
        record_id=UUID(str(uuid7())),
        from_version=None,
        to_version=1,
        drift_check=False,
        attempts=0,
        created_at=(await event(conn, missing.params["event"])).created_at,
    )
    with pytest.raises(EventError) as raised:
        await translate_event(conn, gone)
    assert raised.value.code == REVISION_MISSING and raised.value.params["version"] is None
    assert uri  # the record itself is untouched


def subjects_in(update: str) -> set[str]:
    """Every subject an update names: each ``DELETE WHERE`` subject and each data line's."""
    found = set(delete_where_subjects(update))
    for line in update.splitlines():
        if line.startswith("<") and line.endswith(" ."):
            found.add(line[1 : line.index(">")])
    return found


async def test_updates_parse_as_sparql_and_name_only_owned_subjects(
    served: Served,
    network: Network,
    editor: Caller,
    db_engine: AsyncEngine,
    conn: asyncpg.Connection,
) -> None:
    """At least every sixth corpus record: first publish, an edit, a replace, a retract."""
    checked = 0
    for path in corpus_files()[::6]:
        template = load_record(path)
        created = await served.client.post(
            "/api/records",
            json={"type": template["type"], "template": template},
            headers=editor.headers,
        )
        if created.status_code == 422:
            continue
        record_id, uri = UUID(created.json()["id"]), created.json()["uri"]
        response = await publish(served, editor, record_id)
        if response.status_code == 422:  # refused by validation, as in Step 1.10's test
            continue
        if response.status_code != 200:
            await publish_ok(served, editor, record_id)  # accepts the warnings

        def edit(data: dict[str, Any]) -> None:
            data["_label"] = f"{data.get('_label', '')} (edited)"

        await publish_edit(served, network, editor, db_engine, record_id, edit)
        first, second = [e for e in await pending_events(conn) if e.record_id == record_id]
        replace = dataclasses.replace(second, id=second.id + 10**9, drift_check=True)
        retract = dataclasses.replace(
            second,
            id=second.id + 2 * 10**9,
            kind=OutboxEventKind.DELETE,
            from_version=2,
            to_version=None,
        )
        updates = {
            Mode.INSERT: await translate_event(conn, first),
            Mode.DIFF: await translate_event(conn, second),
            Mode.REPLACE: await translate_event(conn, replace),
            Mode.RETRACT: await translate_event(conn, retract),
        }
        assert updates[Mode.INSERT].startswith("INSERT DATA"), path
        assert updates[Mode.REPLACE].startswith("DELETE WHERE"), path
        assert updates[Mode.RETRACT].startswith("DELETE WHERE"), path
        for mode, update in updates.items():
            store = pyoxigraph.Store()
            store.update(update)  # raises if it is not SPARQL Update
            prefix = uri + NODE_PATH
            assert all(s == uri or s.startswith(prefix) for s in subjects_in(update)), (path, mode)
        await conn.execute("DELETE FROM outbox_events WHERE record_id = $1", record_id)
        checked += 1
    assert checked >= 25, checked
    assert CORPUS_DIR.is_dir()


def test_build_event_update_orders_replace_and_omits_empty_parts() -> None:
    uri = "https://data.test/id/r"
    line = f'<{uri}> <https://x.test/p> "v" .'
    replace = build_event_update(
        Mode.REPLACE, old_lines=frozenset(), new_lines=frozenset({line}), retract_subjects={uri}
    )
    assert replace == f"DELETE WHERE {{ <{uri}> ?p ?o }} ;\nINSERT DATA {{\n{line}\n}}"
    retract = build_event_update(
        Mode.RETRACT, old_lines=frozenset({line}), new_lines=frozenset(), retract_subjects={uri}
    )
    assert retract == f"DELETE WHERE {{ <{uri}> ?p ?o }}"
    same = frozenset({line})
    assert build_event_update(Mode.DIFF, old_lines=same, new_lines=same, retract_subjects=()) == ""
    assert subjects_of([line, f"<{uri}/node/1> <https://x.test/p> <{uri}> ."]) == {
        uri,
        f"{uri}/node/1",
    }
