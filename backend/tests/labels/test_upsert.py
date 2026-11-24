"""Tests for ``lae.labels.upsert.upsert_entity_labels_for_record``."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import EntityLabel, EntityLabelStatus
from lae.labels.upsert import primary_classification, upsert_entity_labels_for_record
from tests.labels.conftest import MONA_LISA, NIGHT_WATCH, as_record, corpus

PAINTING = "http://vocab.getty.edu/aat/300033618"
TYPE_OF_WORK = "http://vocab.getty.edu/aat/300435443"


async def fetch(session: AsyncSession, uri: str) -> EntityLabel:
    session.expire_all()
    row = (await session.execute(select(EntityLabel).where(EntityLabel.uri == uri))).scalar_one()
    return row


async def test_insert_writes_every_column(db_session: AsyncSession) -> None:
    data = corpus(MONA_LISA)
    await upsert_entity_labels_for_record(db_session, as_record(data), "en")
    row = await fetch(db_session, data["id"])
    assert row.type == "HumanMadeObject"
    assert row.labels == {"en": "Mona Lisa", "fr": "La Joconde", "it": "La Gioconda"}
    assert row.default_label == "Mona Lisa"
    assert row.primary_classification == PAINTING
    assert row.source == "local"
    assert row.status is EntityLabelStatus.PUBLISHED
    assert row.metatype_uris == [] and row.vocab_ids == []


async def test_update_replaces_labels_and_keeps_phase_2_columns(db_session: AsyncSession) -> None:
    data = corpus(NIGHT_WATCH)
    uri = data["id"]
    # Other tests leave rows behind in the shared database: compare with what was there.
    others = set((await db_session.execute(select(EntityLabel.uri))).scalars()) - {uri}
    await upsert_entity_labels_for_record(db_session, as_record(data), "en")
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await db_session.execute(
        update(EntityLabel)
        .where(EntityLabel.uri == uri)
        .values(
            metatype_uris=[TYPE_OF_WORK],
            vocab_ids=["object_types"],
            status=EntityLabelStatus.DRAFT,
            updated_at=old,
        )
    )
    renamed = corpus(NIGHT_WATCH)
    renamed["identified_by"][0]["content"] = "The Militia Company of Captain Frans Banninck Cocq"
    del renamed["identified_by"][1]
    await upsert_entity_labels_for_record(db_session, as_record(renamed), "en")
    row = await fetch(db_session, uri)
    assert row.labels == {"en": "The Militia Company of Captain Frans Banninck Cocq"}
    assert row.default_label == "The Militia Company of Captain Frans Banninck Cocq"
    assert row.status is EntityLabelStatus.PUBLISHED
    assert row.metatype_uris == [TYPE_OF_WORK] and row.vocab_ids == ["object_types"]
    assert row.updated_at > old
    assert set((await db_session.execute(select(EntityLabel.uri))).scalars()) == others | {uri}


async def test_record_without_names(db_session: AsyncSession) -> None:
    data = {"id": "https://example.org/id/0190f5a2", "type": "Person"}
    await upsert_entity_labels_for_record(db_session, as_record(data), "en")
    row = await fetch(db_session, data["id"])
    assert (row.labels, row.default_label, row.primary_classification) == ({}, data["id"], None)


def test_primary_classification() -> None:
    assert primary_classification(corpus(MONA_LISA)) == PAINTING
    assert primary_classification({"classified_as": [{"type": "Type"}, {"id": "urn:b"}]}) == "urn:b"
    assert primary_classification({"classified_as": {"id": "urn:single"}}) == "urn:single"
    assert primary_classification({"classified_as": []}) is None
    assert primary_classification({}) is None
