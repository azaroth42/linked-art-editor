"""``lae bootstrap <stage>``: argument parsing, dispatch, summaries and exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import lae.bootstrap.aat
import lae.bootstrap.export
import lae.bootstrap.load
from lae.bootstrap.cli import EXIT_FAILED
from lae.cli import HANDLERS, NOT_IMPLEMENTED, main
from lae.settings import get_settings


@pytest.fixture(autouse=True)
def fresh_settings() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_bootstrap_is_implemented() -> None:
    assert "bootstrap" in HANDLERS
    assert "bootstrap" not in NOT_IMPLEMENTED


@pytest.mark.parametrize(
    "argv",
    [
        ["bootstrap"],
        ["bootstrap", "nope"],
        ["bootstrap", "load"],
        ["bootstrap", "export-nt"],
        ["bootstrap", "build-index", "--nt", "x.nt"],
        ["bootstrap", "aat-labels"],
    ],
)
def test_missing_or_unknown_arguments_are_usage_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 2


def test_each_stage_runs_and_prints_its_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, tuple[Any, dict[str, Any]]] = {}

    def fake(stage: str) -> Any:
        async def run(path: Path, **kwargs: Any) -> dict[str, Any]:
            calls[stage] = (path, kwargs)
            return {"stage": stage}

        return run

    monkeypatch.setattr(lae.bootstrap.load, "load", fake("load"))
    monkeypatch.setattr(lae.bootstrap.export, "export_nt", fake("export"))
    monkeypatch.setattr(lae.bootstrap.aat, "load_aat_labels", fake("aat-labels"))

    argv = ["bootstrap", "load", "--input", "in", "--batch", "5", "--streams", "2"]
    assert main([*argv, "--workers", "3", "--report", "r.jsonl"]) == 0
    assert json.loads(capsys.readouterr().out) == {"stage": "load"}
    path, kwargs = calls["load"]
    assert path == Path("in")
    assert (kwargs["batch"], kwargs["streams"], kwargs["workers"]) == (5, 2, 3)
    assert kwargs["report_path"] == Path("r.jsonl")

    assert main(["bootstrap", "export-nt", "--out", "x.nt.gz", "--workers", "4"]) == 0
    assert calls["export"][0] == Path("x.nt.gz")
    assert calls["export"][1]["workers"] == 4

    assert main(["bootstrap", "aat-labels", "--csv", "aat.csv"]) == 0
    assert calls["aat-labels"][0] == Path("aat.csv")


def test_a_failed_index_build_exits_non_zero(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    nt = tmp_path / "x.nt"
    nt.write_text("")
    argv = ["bootstrap", "build-index", "--nt", str(nt), "--dir", str(tmp_path / "i")]
    assert main(argv) == EXIT_FAILED
    assert "lae bootstrap build-index:" in capsys.readouterr().err
