"""Process settings, loaded from ``LAE_*`` environment variables.

This is the only module that reads the environment (conventions §2). Defaults match the native
development setup started by ``make up`` (see the root README and ``deploy/README.md``). Every
field is optional; secrets are ``SecretStr`` so they never appear in ``repr`` or logs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from lae.labels.langtags import AAT_LANGUAGE_TAGS, normalize_tag

Environment = Literal["development", "test", "staging", "production"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class Settings(BaseSettings):
    """Settings shared by every ``lae`` process; each field reads ``LAE_<FIELD>``."""

    model_config = SettingsConfigDict(env_prefix="LAE_")

    env: Environment = "development"
    """Deployment environment; ``production`` refuses ``dev_user`` (decision E2, Step 1.7)."""

    database_url: str = "postgresql+asyncpg://localhost:5432/lae"
    """SQLAlchemy async URL of the system-of-record database."""

    qlever_url: str | None = None
    """QLever SPARQL query endpoint, e.g. ``http://localhost:7010``; unset means no QLever."""

    qlever_update_url: str | None = None
    """QLever SPARQL Update endpoint (internal only, conventions §9); often equal to the above."""

    qlever_access_token: SecretStr | None = None
    """QLever access token, required by its update endpoint (``deploy/Qleverfile``)."""

    sync_metrics_host: str = "127.0.0.1"
    """Interface the sync worker's ``GET /metrics`` listener binds to (Step 1.11)."""

    sync_metrics_port: int | None = None
    """Port of the sync worker's ``GET /metrics`` listener; unset means no listener."""

    base_uri: str = "http://localhost:8000/id/"
    """Prefix of public record URIs: ``<base_uri><uuid>``."""

    context_version: int = 1
    """Version of the pinned Linked Art ``@context`` (``profiles/context/v<N>.json``)."""

    label_language: str = "en"
    """Language preferred for a record's derived ``_label`` and for ``entity_labels.default_label``
    (Step 1.19a).

    One of the 57 tags of ``lae.labels.langtags.AAT_LANGUAGE_TAGS`` (``en``, ``fr``, ``de``, ...);
    case, surrounding whitespace and ISO 639-2 codes are normalized (``fra`` gives ``fr``), and
    any other value stops the process when it starts. Every process of an installation must have
    the same value; changing it means restarting ``api`` and ``crdt-ws`` together.
    """

    draft_ttl_days: Annotated[int, Field(ge=1)] = 5
    """Days without activity after which a draft is discarded (decision B7; at least 1)."""

    api_host: str = "127.0.0.1"
    """Interface ``lae api`` binds to."""

    api_port: int = 8000
    """Port ``lae api`` listens on."""

    log_level: LogLevel = "info"
    """Minimum level of the structured JSON logs every process writes to stdout."""

    crdt_ws_url: str = "http://crdt-ws:8001"
    """Base URL at which ``api`` reaches the ``crdt-ws`` process (internal RPC, Step 1.8)."""

    crdt_ws_host: str = "127.0.0.1"
    """Interface ``lae crdt-ws`` binds to (WebSocket and internal RPC share the listener)."""

    crdt_ws_port: int = 8001
    """Port ``lae crdt-ws`` listens on."""

    crdt_room_idle_seconds: int = 600
    """Seconds without a connection or a write after which a room leaves memory (decision B5)."""

    allowed_origins: Annotated[list[str], NoDecode] = []
    """Browser origins (``scheme://host[:port]``, comma-separated) that may open a draft room.

    The origin of ``base_uri`` is always allowed, and any loopback origin when ``env`` is
    ``development``. Set this when the editor is served from another origin than the record
    URIs. ``crdt-ws`` refuses a WebSocket handshake whose ``Origin`` is none of these.
    """

    internal_token: SecretStr | None = None
    """Shared secret for ``/internal/`` endpoints, sent as ``X-Internal-Token`` (conventions §2)."""

    oidc_issuer: str | None = None
    """OpenID Connect issuer URL for human login (Step 1.7)."""

    oidc_client_id: str | None = None
    """OpenID Connect client id registered with the issuer."""

    oidc_client_secret: SecretStr | None = None
    """OpenID Connect client secret."""

    github_client_id: str | None = None
    """Client id of the GitHub OAuth App; GitHub login is enabled when it and the secret are set."""

    github_client_secret: SecretStr | None = None
    """Client secret of the GitHub OAuth App."""

    github_allowed_orgs: Annotated[list[str], NoDecode] = []
    """GitHub organizations whose active members may sign in (comma-separated; ADR-0026)."""

    github_allowed_users: Annotated[list[str], NoDecode] = []
    """GitHub logins that may sign in (comma-separated); with no allowlist, anyone may."""

    session_secret: SecretStr | None = None
    """Key that signs the session cookie (Step 1.7)."""

    dev_user: str | None = None
    """Development login bypass, ``<email>:<Role>``; refused when ``env`` is ``production``."""

    reader_assets_dir: Path | None = None
    """Built Public Reader script (``corepack pnpm build:reader``); default
    ``frontend/dist-reader`` in the repository (``lae.reader.assets``, Step 1.21)."""

    @field_validator(
        "github_allowed_orgs", "github_allowed_users", "allowed_origins", mode="before"
    )
    @classmethod
    def _split_list(cls, value: object) -> object:
        """Read a comma- or whitespace-separated string (the environment's form) as a list."""
        if isinstance(value, str):
            return [item for item in value.replace(",", " ").split() if item]
        return value

    @field_validator("label_language", mode="before")
    @classmethod
    def _check_label_language(cls, value: object) -> str:
        """Normalize the tag (``normalize_tag``); accept only a language of the AAT table."""
        tag = normalize_tag(value) if isinstance(value, str) else None
        if tag is None or tag not in LABEL_LANGUAGES:
            raise ValueError(f"{value!r} is not one of the tags of AAT_LANGUAGE_TAGS")
        return tag


LABEL_LANGUAGES: frozenset[str] = frozenset(AAT_LANGUAGE_TAGS.values())
"""The values ``Settings.label_language`` accepts: the tags of the AAT language table."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    return Settings()
