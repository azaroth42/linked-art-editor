"""Writing a published local record's labels to ``entity_labels`` (Phase 1, Step 1.5).

Called inside the publish transaction (Step 1.10). Only the columns this step owns are written;
``metatype_uris``, ``vocab_ids`` and ``usage_count`` keep their values on update (``lae.vocab``
maintains them). Afterwards the metatypes the record states inline on the concepts it references
(and, for a concept record, its own) are added to those concepts' rows
(``lae.vocab.metatypes.harvest_record_metatypes``, Phase 2 Step 2.1, ADR-0029).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lae.db.models import EntityLabel, EntityLabelStatus, Record
from lae.labels.extract import labels_from_record
from lae.vocab.metatypes import harvest_record_metatypes

LOCAL_SOURCE: Final = "local"
"""``entity_labels.source`` of rows for this installation's own records."""


def primary_classification(jsonld: Mapping[str, Any]) -> str | None:
    """Return the ``id`` of the first ``classified_as`` entry that has one, else ``None``."""
    classified_as = jsonld.get("classified_as")
    entries = classified_as if isinstance(classified_as, list) else [classified_as]
    for entry in entries:
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
            classification: str = entry["id"]
            return classification
    return None


async def upsert_entity_labels_for_record(session: AsyncSession, record: Record) -> None:
    """Insert or update the ``entity_labels`` row of ``record`` from its published JSON-LD, then
    harvest the metatypes it states inline."""
    labels, default_label = labels_from_record(record.data)
    values: dict[str, Any] = {
        "type": record.type,
        "labels": labels,
        "default_label": default_label,
        "primary_classification": primary_classification(record.data),
        "source": LOCAL_SOURCE,
        "status": EntityLabelStatus.PUBLISHED,
    }
    statement = insert(EntityLabel).values(uri=record.uri, **values)
    statement = statement.on_conflict_do_update(
        index_elements=[EntityLabel.uri], set_={**values, "updated_at": func.now()}
    )
    await session.execute(statement)
    await harvest_record_metatypes(session, record.data)
