"""Tests for ``lae.settings``."""

import pydantic
import pytest

from lae.settings import Settings, get_settings

FIELDS = (
    "env",
    "database_url",
    "qlever_url",
    "qlever_update_url",
    "qlever_access_token",
    "base_uri",
    "context_version",
    "draft_ttl_days",
    "api_host",
    "api_port",
    "log_level",
    "crdt_ws_url",
    "internal_token",
    "oidc_issuer",
    "oidc_client_id",
    "oidc_client_secret",
    "session_secret",
    "dev_user",
)

SECRETS = ("qlever_access_token", "internal_token", "oidc_client_secret", "session_secret")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every ``LAE_*`` variable that maps to a settings field."""
    for field in FIELDS:
        monkeypatch.delenv(f"LAE_{field.upper()}", raising=False)
    return monkeypatch


def test_fields_are_exactly_the_specified_ones() -> None:
    assert set(Settings.model_fields) == set(FIELDS)


def test_defaults(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings()
    assert settings.env == "development"
    assert settings.database_url == "postgresql+asyncpg://localhost:5432/lae"
    assert settings.qlever_url is None
    assert settings.qlever_update_url is None
    assert settings.base_uri == "http://localhost:8000/id/"
    assert settings.context_version == 1
    assert settings.draft_ttl_days == 5
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.log_level == "info"
    assert settings.crdt_ws_url == "http://crdt-ws:8001"
    assert settings.oidc_issuer is None
    assert settings.oidc_client_id is None
    assert settings.dev_user is None
    for secret in SECRETS:
        assert getattr(settings, secret) is None


def test_reads_lae_prefixed_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LAE_ENV", "production")
    clean_env.setenv("LAE_DATABASE_URL", "postgresql+asyncpg://localhost:5432/lae_i3")
    clean_env.setenv("LAE_QLEVER_URL", "http://localhost:7003")
    clean_env.setenv("LAE_QLEVER_UPDATE_URL", "http://localhost:7003")
    clean_env.setenv("LAE_BASE_URI", "https://example.org/id/")
    clean_env.setenv("LAE_CONTEXT_VERSION", "2")
    clean_env.setenv("LAE_DRAFT_TTL_DAYS", "7")
    clean_env.setenv("LAE_API_HOST", "0.0.0.0")
    clean_env.setenv("LAE_API_PORT", "9003")
    clean_env.setenv("LAE_LOG_LEVEL", "debug")
    clean_env.setenv("LAE_CRDT_WS_URL", "http://localhost:9503")
    clean_env.setenv("LAE_OIDC_ISSUER", "https://idp.example.org/realms/lae")
    clean_env.setenv("LAE_OIDC_CLIENT_ID", "lae")
    clean_env.setenv("LAE_DEV_USER", "editor@example.org:Editor")
    settings = Settings()
    assert settings.env == "production"
    assert settings.database_url == "postgresql+asyncpg://localhost:5432/lae_i3"
    assert settings.qlever_url == "http://localhost:7003"
    assert settings.qlever_update_url == "http://localhost:7003"
    assert settings.base_uri == "https://example.org/id/"
    assert settings.context_version == 2
    assert settings.draft_ttl_days == 7
    assert settings.api_host == "0.0.0.0"
    assert settings.api_port == 9003
    assert settings.log_level == "debug"
    assert settings.crdt_ws_url == "http://localhost:9503"
    assert settings.oidc_issuer == "https://idp.example.org/realms/lae"
    assert settings.oidc_client_id == "lae"
    assert settings.dev_user == "editor@example.org:Editor"


@pytest.mark.parametrize("field", SECRETS)
def test_secrets_are_read_but_never_shown(clean_env: pytest.MonkeyPatch, field: str) -> None:
    clean_env.setenv(f"LAE_{field.upper()}", "s3cr3t-value")
    settings = Settings()
    secret = getattr(settings, field)
    assert secret is not None
    assert secret.get_secret_value() == "s3cr3t-value"
    assert "s3cr3t-value" not in repr(settings)
    assert "s3cr3t-value" not in str(settings.model_dump())


@pytest.mark.parametrize(("variable", "value"), [("LAE_ENV", "prod"), ("LAE_LOG_LEVEL", "loud")])
def test_unknown_choices_are_rejected(
    clean_env: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    # A typo such as LAE_ENV=prod must not silently count as a non-production environment.
    clean_env.setenv(variable, value)
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_unprefixed_variables_are_ignored(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", "postgresql://elsewhere/other")
    assert Settings().database_url == "postgresql+asyncpg://localhost:5432/lae"


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
