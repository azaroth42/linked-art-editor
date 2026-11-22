"""Process settings, loaded from ``LAE_*`` environment variables.

This is the only module that reads the environment (conventions §2). Defaults match the native
development setup started by ``make up`` (see the root README and ``deploy/README.md``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings shared by every ``lae`` process; each field reads ``LAE_<FIELD>``."""

    model_config = SettingsConfigDict(env_prefix="LAE_")

    database_url: str = "postgresql+asyncpg://localhost:5432/lae"
    """SQLAlchemy async URL of the system-of-record database."""

    qlever_url: str | None = None
    """QLever SPARQL query endpoint, e.g. ``http://localhost:7010``; unset means no QLever."""

    qlever_update_url: str | None = None
    """QLever SPARQL Update endpoint (internal only, conventions §9); often equal to the above."""

    base_uri: str = "http://localhost:8000/id/"
    """Prefix of public record URIs: ``<base_uri><uuid>``."""

    context_version: int = 1
    """Version of the pinned Linked Art ``@context`` (``profiles/context/v<N>.json``)."""

    draft_ttl_days: int = 5
    """Days without activity after which a draft is discarded (decision B7)."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    return Settings()
