"""Reading bootstrap input: a directory of JSON files, or JSON Lines, one record per item."""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Final, NamedTuple

JSON_SUFFIXES: Final = (".json",)
"""A file with one of these suffixes holds one record."""

JSONL_SUFFIXES: Final = (".jsonl", ".ndjson", ".jsonl.gz", ".ndjson.gz")
"""A file with one of these suffixes holds one record per line (gzip if it ends in ``.gz``)."""


class SourceItem(NamedTuple):
    """One input record, still as bytes, and where it came from (for the report)."""

    source: str
    text: bytes


def _suffix_in(path: Path, suffixes: tuple[str, ...]) -> bool:
    return path.name.endswith(suffixes)


def input_files(path: Path) -> list[Path]:
    """Return the input files under ``path`` (itself, if it is a file), in a stable order.

    In a directory, every ``*.json`` and JSON Lines file at any depth counts; others are ignored.
    """
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"no such file or directory: {path}")
    return sorted(
        file
        for file in path.rglob("*")
        if file.is_file() and _suffix_in(file, JSON_SUFFIXES + JSONL_SUFFIXES)
    )


def _open_lines(path: Path) -> IO[bytes]:
    if path.name.endswith(".gz"):
        return gzip.GzipFile(path, "rb")  # type: ignore[return-value]
    return path.open("rb")


def iter_source(path: Path) -> Iterator[SourceItem]:
    """Yield every record of the input at ``path`` (see ``input_files``), in order.

    A JSON Lines file yields one item per non-blank line (``source`` is ``<file>:<line>``); any
    other file yields its whole content as one item (``source`` is the file name).
    """
    root = path if path.is_dir() else path.parent
    for file in input_files(path):
        name = file.relative_to(root).as_posix() if file.is_relative_to(root) else str(file)
        if _suffix_in(file, JSONL_SUFFIXES):
            with _open_lines(file) as lines:
                for number, line in enumerate(lines, start=1):
                    line = line.strip()
                    if line:
                        yield SourceItem(f"{name}:{number}", line)
        else:
            yield SourceItem(name, file.read_bytes())
