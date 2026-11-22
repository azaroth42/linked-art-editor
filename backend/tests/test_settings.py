"""Tests for ``lae.settings``."""

import pytest

from lae.settings import Settings, get_settings

FIELDS = (
    "database_url",
    "qlever_url",
    "qlever_update_url",
    "base_uri",
    "context_version",
    "draft_ttl_days",
)


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
    assert settings.database_url == "postgresql+asyncpg://localhost:5432/lae"
    assert settings.qlever_url is None
    assert settings.qlever_update_url is None
    assert settings.base_uri == "http://localhost:8000/id/"
    assert settings.context_version == 1
    assert settings.draft_ttl_days == 5


def test_reads_lae_prefixed_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LAE_DATABASE_URL", "postgresql+asyncpg://localhost:5432/lae_i3")
    clean_env.setenv("LAE_QLEVER_URL", "http://localhost:7003")
    clean_env.setenv("LAE_QLEVER_UPDATE_URL", "http://localhost:7003")
    clean_env.setenv("LAE_BASE_URI", "https://example.org/id/")
    clean_env.setenv("LAE_CONTEXT_VERSION", "2")
    clean_env.setenv("LAE_DRAFT_TTL_DAYS", "7")
    settings = Settings()
    assert settings.database_url == "postgresql+asyncpg://localhost:5432/lae_i3"
    assert settings.qlever_url == "http://localhost:7003"
    assert settings.qlever_update_url == "http://localhost:7003"
    assert settings.base_uri == "https://example.org/id/"
    assert settings.context_version == 2
    assert settings.draft_ttl_days == 7


def test_unprefixed_variables_are_ignored(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", "postgresql://elsewhere/other")
    assert Settings().database_url == "postgresql+asyncpg://localhost:5432/lae"


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
