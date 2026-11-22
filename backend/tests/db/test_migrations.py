"""Migration checks: single head, models match the migrations, lz4, NOTIFY, round trip."""

import asyncio

import asyncpg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from lae.cli import main
from lae.db.migrate import alembic_config, downgrade
from lae.db.models import Base
from lae.settings import get_settings

CORE_TABLES = {
    "records",
    "record_revisions",
    "entity_labels",
    "crdt_drafts",
    "crdt_updates",
    "outbox_events",
    "sync_state",
    "users",
    "roles",
    "user_roles",
    "api_tokens",
}
CORE_ENUMS = {
    "records_status",
    "entity_labels_status",
    "outbox_events_kind",
    "outbox_events_status",
    "users_kind",
}


def asyncpg_dsn(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(False)


def test_migrations_have_a_single_head() -> None:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, f"several Alembic heads {heads}: rebase (docs/agent-workflow.md §4)"


async def test_models_match_migrations(db_engine: AsyncEngine) -> None:
    def diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    async with db_engine.connect() as connection:
        assert await connection.run_sync(diff) == []


async def test_records_data_uses_lz4(db_session: AsyncSession) -> None:
    compression = await db_session.scalar(
        text(
            "SELECT attcompression::text FROM pg_attribute "
            "WHERE attrelid = 'records'::regclass AND attname = 'data'"
        )
    )
    assert compression == "l"


async def test_outbox_insert_notifies_with_the_event_id(db_url: str) -> None:
    dsn = asyncpg_dsn(db_url)
    received: asyncio.Queue[str] = asyncio.Queue()
    listener = await asyncpg.connect(dsn)
    writer = await asyncpg.connect(dsn)
    try:
        await listener.add_listener("outbox", lambda *args: received.put_nowait(args[3]))
        async with writer.transaction():
            event_id = await writer.fetchval(
                "INSERT INTO outbox_events (kind, record_id) VALUES ('publish', $1) RETURNING id",
                uuid7(),
            )
        assert await asyncio.wait_for(received.get(), timeout=5) == str(event_id)
    finally:
        await writer.execute("DELETE FROM outbox_events")
        await writer.close()
        await listener.close()


async def _schema_objects(database_url: str) -> tuple[set[str], set[str]]:
    connection = await asyncpg.connect(asyncpg_dsn(database_url))
    try:
        tables = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        enums = await connection.fetch(
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' AND t.typtype = 'e'"
        )
    finally:
        await connection.close()
    return {row[0] for row in tables}, {row[0] for row in enums}


def test_downgrade_base_then_lae_migrate(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    downgrade("base", database_url=db_url)
    tables, enums = asyncio.run(_schema_objects(db_url))
    assert tables == {"alembic_version"}
    assert enums == set()

    # `lae migrate` reads LAE_DATABASE_URL through the settings.
    monkeypatch.setenv("LAE_DATABASE_URL", db_url)
    get_settings.cache_clear()
    try:
        assert main(["migrate"]) == 0
    finally:
        get_settings.cache_clear()
    tables, enums = asyncio.run(_schema_objects(db_url))
    assert tables >= CORE_TABLES | {"alembic_version"}
    assert enums >= CORE_ENUMS
