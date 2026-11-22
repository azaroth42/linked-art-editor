"""Placeholder tests for the ``lae`` CLI skeleton."""

import pytest

import lae
from lae.cli import COMMANDS, main


def test_package_has_version() -> None:
    assert lae.__version__


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_placeholder_command_prints_name_and_exits_zero(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command]) == 0
    assert capsys.readouterr().out.strip() == command


def test_expected_commands_exist() -> None:
    assert set(COMMANDS) == {"api", "crdt-ws", "sync-worker", "scheduler", "migrate", "bootstrap"}


def test_missing_command_is_an_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
