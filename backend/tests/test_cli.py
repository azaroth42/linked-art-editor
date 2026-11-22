"""Tests for the ``lae`` CLI: placeholder commands and ``lae migrate``."""

import pytest

import lae
from lae.cli import COMMANDS, HANDLERS, main
from lae.db import migrate


def test_package_has_version() -> None:
    assert lae.__version__


@pytest.mark.parametrize("command", sorted(set(COMMANDS) - set(HANDLERS)))
def test_placeholder_command_prints_name_and_exits_zero(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command]) == 0
    assert capsys.readouterr().out.strip() == command


def test_expected_commands_exist() -> None:
    assert set(COMMANDS) == {"api", "crdt-ws", "sync-worker", "scheduler", "migrate", "bootstrap"}


def test_handlers_are_registered_commands() -> None:
    assert set(HANDLERS) <= set(COMMANDS)


def test_migrate_upgrades_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_upgrade(revision: str = "head", database_url: str | None = None) -> None:
        calls.append((revision, database_url))

    monkeypatch.setattr(migrate, "upgrade", fake_upgrade)
    assert main(["migrate"]) == 0
    assert calls == [("head", None)]


def test_missing_command_is_an_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
