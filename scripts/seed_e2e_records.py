"""Store published records for the e2e suite straight into ``records`` and ``entity_labels``.

``frontend/e2e/globalSetup.ts`` runs it before the Playwright tests, against the database of the
e2e ``lae api`` (``LAE_DATABASE_URL``):

    uv run --project backend --locked python scripts/seed_e2e_records.py --input records.json

``records.json`` is a JSON array of Linked Art records whose ids are record URIs of this
installation (``<LAE_BASE_URI><uuidv7>``). Publishing arrives with Step 1.10; until then the
Public Reader tests (Step 1.21) need published records, so each one is stored as published
version 1, with internal node IRIs and its ``entity_labels`` row, as the bulk loader stores it.
A record that is already there is replaced, so the command can run again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

try:
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from lae.api.records import record_etag
    from lae.db.models import Record, RecordStatus
    from lae.ids import record_id_from_uri, record_uri
    from lae.labels.upsert import upsert_entity_labels_for_record
    from lae.projection import assign_node_iris
    from lae.settings import Settings, get_settings
except ModuleNotFoundError as exc:  # pragma: no cover - wrong interpreter
    raise SystemExit(
        f"{exc}. Run with the backend environment: "
        "uv run --project backend python scripts/seed_e2e_records.py"
    ) from exc

PUBLISHED_VERSION = 1


def prepared(data: dict[str, Any], settings: Settings) -> Record:
    """The ``records`` row of one input record; raise ``ValueError`` if its id is not local."""
    uri = data.get("id")
    record_id = record_id_from_uri(uri, base_uri=settings.base_uri) if isinstance(uri, str) else None
    if record_id is None or record_uri(record_id, base_uri=settings.base_uri) != uri:
        raise ValueError(f"{uri!r} is not a record URI under {settings.base_uri}")
    stored = assign_node_iris(data, uri)
    return Record(
        id=record_id,
        uri=uri,
        type=stored["type"],
        version=PUBLISHED_VERSION,
        status=RecordStatus.ACTIVE,
        etag=record_etag(stored),
        data=stored,
        context_version=settings.context_version,
    )


async def seed(records: list[dict[str, Any]], settings: Settings) -> int:
    """Insert or replace every record and its labels in one transaction; return the count."""
    rows = [prepared(data, settings) for data in records]
    engine = create_async_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            for row in rows:
                values = {
                    "uri": row.uri,
                    "type": row.type,
                    "version": row.version,
                    "status": row.status,
                    "etag": row.etag,
                    "data": row.data,
                    "context_version": row.context_version,
                    "merged_into": None,
                }
                statement = insert(Record).values(id=row.id, **values)
                await session.execute(
                    statement.on_conflict_do_update(index_elements=[Record.id], set_=values)
                )
                await upsert_entity_labels_for_record(session, row, settings.label_language)
    finally:
        await engine.dispose()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments, seed the records, and report how many were written."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, required=True, help="JSON array of records")
    args = parser.parse_args(argv)
    records = json.loads(args.input.read_text())
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        parser.error("--input must hold a JSON array of objects")
    count = asyncio.run(seed(records, get_settings()))
    print(f"seeded {count} records", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
