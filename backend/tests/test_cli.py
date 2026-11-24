"""Tests for the ``lae`` CLI: ``api``, the lazily imported processes, ``migrate``."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

import lae
import lae.cli
from lae.cli import (
    COMMANDS,
    EXIT_NOT_IMPLEMENTED,
    HANDLERS,
    NOT_IMPLEMENTED,
    PROCESS_MODULES,
    main,
)
from lae.db import migrate
from lae.settings import Settings, get_settings

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://lae@127.0.0.1:1/lae_unreachable"


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Clear the cached settings around a test that sets ``LAE_*`` variables."""
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def logging_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record ``configure_logging`` calls instead of reconfiguring the test process."""
    calls: list[str] = []
    monkeypatch.setattr(lae.cli, "configure_logging", calls.append)
    return calls


def test_package_has_version() -> None:
    assert lae.__version__


def test_expected_commands_exist() -> None:
    assert set(COMMANDS) == {
        "api",
        "crdt-ws",
        "sync-worker",
        "scheduler",
        "migrate",
        "bootstrap",
        "vocab",
        "relabel",
    }


def test_relabel_is_a_one_shot_command_not_a_process() -> None:
    assert COMMANDS["relabel"] == (
        "Recompute entity_labels.default_label under LAE_LABEL_LANGUAGE."
    )
    assert HANDLERS["relabel"] is lae.cli.run_relabel
    assert "relabel" not in PROCESS_MODULES
    deploy = Path(__file__).resolve().parents[2] / "deploy"
    assert "relabel" not in (deploy / "Procfile").read_text()
    assert not list((deploy / "systemd").glob("*relabel*"))


def test_every_command_has_exactly_one_implementation() -> None:
    groups = [set(HANDLERS), set(PROCESS_MODULES), set(NOT_IMPLEMENTED)]
    assert set().union(*groups) == set(COMMANDS)
    assert sum(len(group) for group in groups) == len(COMMANDS)


def test_process_modules_are_the_ones_the_guide_names() -> None:
    assert PROCESS_MODULES == {
        "crdt-ws": ("lae.crdt.server", "1.8"),
        "sync-worker": ("lae.sync.worker", "1.11"),
        "scheduler": ("lae.scheduler.main", "1.15"),
    }


def test_api_runs_uvicorn_on_create_app(
    settings_env: pytest.MonkeyPatch, logging_calls: list[str]
) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    settings_env.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    settings_env.setenv("LAE_API_HOST", "0.0.0.0")
    settings_env.setenv("LAE_API_PORT", "9014")
    settings_env.setenv("LAE_LOG_LEVEL", "warning")
    assert main(["api"]) == 0
    [(app, kwargs)] = calls
    assert isinstance(app, FastAPI)
    assert isinstance(app.state.settings, Settings)
    assert {"/health/live", "/health/ready"} <= set(app.openapi()["paths"])
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9014
    assert kwargs["log_config"] is None
    assert kwargs["access_log"] is False
    assert logging_calls == ["warning"]


@pytest.mark.parametrize("command", sorted(PROCESS_MODULES))
def test_process_whose_module_is_missing_reports_its_step(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module_name, step = PROCESS_MODULES[command]
    monkeypatch.setitem(PROCESS_MODULES, command, (f"lae.no_such_package.{command}", step))
    assert main([command]) == EXIT_NOT_IMPLEMENTED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"lae {command}: not implemented yet (Phase 1, Step {step};" in captured.err


@pytest.mark.parametrize("command", sorted(NOT_IMPLEMENTED))
def test_not_implemented_command_reports_its_step(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command]) == EXIT_NOT_IMPLEMENTED
    assert f"Step {NOT_IMPLEMENTED[command]}" in capsys.readouterr().err


def test_missing_submodule_of_existing_package_is_not_implemented(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(PROCESS_MODULES, "crdt-ws", ("lae.crdt.no_such_module", "1.8"))
    assert main(["crdt-ws"]) == EXIT_NOT_IMPLEMENTED
    assert "Step 1.8" in capsys.readouterr().err


def test_missing_dependency_inside_a_process_module_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "lae_fake_process.py").write_text("import lae_dependency_that_is_missing\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(PROCESS_MODULES, "scheduler", ("lae_fake_process", "1.15"))
    with pytest.raises(ModuleNotFoundError) as excinfo:
        main(["scheduler"])
    assert excinfo.value.name == "lae_dependency_that_is_missing"


def install_process_module(monkeypatch: pytest.MonkeyPatch, command: str, main_fn: Any) -> None:
    module = ModuleType(f"lae_fake_{command.replace('-', '_')}")
    module.main = main_fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(PROCESS_MODULES, command, (module.__name__, "1.x"))


@pytest.mark.parametrize("command", sorted(PROCESS_MODULES))
def test_process_runs_module_main_with_settings(
    command: str, settings_env: pytest.MonkeyPatch, logging_calls: list[str]
) -> None:
    received: list[Settings] = []

    def fake_main(settings: Settings, argv: Sequence[str] = ()) -> int:
        received.append(settings)
        return 0

    install_process_module(settings_env, command, fake_main)
    settings_env.setenv("LAE_DATABASE_URL", "postgresql+asyncpg://localhost:5432/lae_i14")
    assert main([command]) == 0
    [settings] = received
    assert settings.database_url == "postgresql+asyncpg://localhost:5432/lae_i14"
    assert logging_calls == ["info"]


@pytest.mark.parametrize(("returned", "exit_code"), [(None, 0), (0, 0), (4, 4)])
def test_process_main_may_be_async_and_its_result_is_the_exit_code(
    returned: int | None,
    exit_code: int,
    settings_env: pytest.MonkeyPatch,
    logging_calls: list[str],
) -> None:
    async def fake_main(settings: Settings, argv: Sequence[str] = ()) -> int | None:
        return returned

    install_process_module(settings_env, "sync-worker", fake_main)
    assert main(["sync-worker"]) == exit_code


def test_sync_worker_gets_its_arguments(
    settings_env: pytest.MonkeyPatch, logging_calls: list[str]
) -> None:
    received: list[list[str]] = []

    def fake_main(settings: Settings, argv: Sequence[str] = ()) -> int:
        received.append(list(argv))
        return 0

    install_process_module(settings_env, "sync-worker", fake_main)
    assert main(["sync-worker", "--once"]) == 0
    assert main(["sync-worker", "--replay-after", "7", "--target-url", "http://q:7324"]) == 0
    assert received == [["--once"], ["--replay-after", "7", "--target-url", "http://q:7324"]]
    with pytest.raises(SystemExit):
        main(["sync-worker", "--no-such-option"])


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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def test_lae_api_serves_health_over_http() -> None:
    """Start the real ``lae api`` process (Postgres unreachable) and probe it over HTTP."""
    port = free_port()
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAE_")}
    env |= {
        "LAE_API_PORT": str(port),
        "LAE_DATABASE_URL": UNREACHABLE_DATABASE_URL,
        "LAE_LOG_LEVEL": "info",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "lae.cli", "api"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, process.communicate()[0]
            try:
                live = httpx.get(f"{base}/health/live", timeout=1)
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline, "lae api did not start within 30 s"
                time.sleep(0.1)
        ready = httpx.get(f"{base}/health/ready", timeout=10)
    finally:
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert "x-request-id" in live.headers
    assert ready.status_code == 503
    assert ready.json()["failing"] == ["postgres"]
    # uvicorn shuts down gracefully, then re-raises SIGTERM so the exit status reports it.
    assert process.returncode in (0, -signal.SIGTERM), output
    events = [json.loads(line) for line in output.splitlines()]  # every line is JSON
    assert events[-1]["event"].startswith("Finished server process")
    assert any(event["logger"] == "uvicorn.error" for event in events)
    assert any(event["event"] == "readiness_check_failed" for event in events)
