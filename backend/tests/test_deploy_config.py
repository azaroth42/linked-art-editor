"""Consistency checks for the native deployment files under ``deploy/``."""

import configparser
import re
from pathlib import Path

from lae.cli import COMMANDS

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

# CLI sub-commands that are long-running processes (the others are one-shot tools).
PROCESSES = {"api", "crdt-ws", "sync-worker", "scheduler"}


def _procfile_entries() -> dict[str, str]:
    entries = {}
    for line in (DEPLOY / "Procfile").read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.+)$", line)
        if match:
            entries[match.group(1)] = match.group(2)
    return entries


def test_procfile_runs_each_process_by_its_cli_name() -> None:
    entries = _procfile_entries()
    assert set(entries) == PROCESSES
    assert PROCESSES <= set(COMMANDS)
    for name, command in entries.items():
        assert command == f"uv run --project backend lae {name}"


def test_every_process_has_a_systemd_unit() -> None:
    for name in PROCESSES:
        unit = (DEPLOY / "systemd" / f"lae-{name}.service").read_text()
        assert re.search(rf"^ExecStart=\S*/lae {name}$", unit, re.MULTILINE), name
    qlever_unit = (DEPLOY / "systemd" / "qlever.service").read_text()
    assert "deploy/Qleverfile start --run-in-foreground" in qlever_unit


def test_qleverfile_fixed_settings() -> None:
    # Raw parsing: the $$(...) values are evaluated by qlever-control, not here.
    qleverfile = configparser.RawConfigParser()
    qleverfile.read(DEPLOY / "Qleverfile")
    assert qleverfile.get("data", "name") == "lae"
    assert qleverfile.get("index", "text_index") == "from_literals"  # decision A15
    assert qleverfile.get("index", "add_has_word_triples") == "true"  # decision A15
    assert qleverfile.get("server", "persist_updates") == "true"  # decision A14
    assert qleverfile.get("runtime", "system") == "native"
    assert "QLEVER_PORT:-7010" in qleverfile.get("server", "port")
    assert "LAE_QLEVER_ACCESS_TOKEN" in qleverfile.get("server", "access_token")


def test_postgres_init_creates_required_extensions() -> None:
    sql = (DEPLOY / "postgres" / "init.sql").read_text()
    for extension in ("vector", "pg_trgm", "btree_gin"):
        assert f"CREATE EXTENSION IF NOT EXISTS {extension};" in sql
