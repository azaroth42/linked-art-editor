"""Alembic entry points shared by ``lae migrate`` and the test fixtures."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[3]
"""The ``backend/`` directory, which holds ``alembic.ini`` and ``migrations/``."""

ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

DATABASE_URL_ATTRIBUTE = "lae_database_url"
"""``Config.attributes`` key that overrides ``LAE_DATABASE_URL`` in ``migrations/env.py``."""


def alembic_config(database_url: str | None = None) -> Config:
    """Return the Alembic config for ``backend/alembic.ini``, optionally for another database."""
    if not ALEMBIC_INI.is_file():
        raise FileNotFoundError(
            f"{ALEMBIC_INI} not found: lae migrate runs from a repository checkout "
            "(an editable install of backend/)"
        )
    config = Config(str(ALEMBIC_INI))
    if database_url is not None:
        config.attributes[DATABASE_URL_ATTRIBUTE] = database_url
    return config


def upgrade(revision: str = "head", database_url: str | None = None) -> None:
    """Run ``alembic upgrade <revision>`` on ``database_url``, else ``LAE_DATABASE_URL``."""
    command.upgrade(alembic_config(database_url), revision)


def downgrade(revision: str, database_url: str | None = None) -> None:
    """Run ``alembic downgrade <revision>`` on ``database_url``, else ``LAE_DATABASE_URL``."""
    command.downgrade(alembic_config(database_url), revision)
