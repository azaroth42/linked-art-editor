"""Tests for the UI Profile path parser."""

import json
from pathlib import Path

import pytest

from lae.profiles.paths import PATH_PATTERN, PathSegment, PathSyntaxError, format_path, parse_path

PROFILES = Path(__file__).resolve().parents[3] / "profiles"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("classified_as", (PathSegment("classified_as"),)),
        ("identified_by[Name]", (PathSegment("identified_by", "Name"),)),
        (
            "identified_by[Name].content",
            (PathSegment("identified_by", "Name"), PathSegment("content")),
        ),
        (
            "produced_by.timespan.begin_of_the_begin",
            (
                PathSegment("produced_by"),
                PathSegment("timespan"),
                PathSegment("begin_of_the_begin"),
            ),
        ),
        (
            "identified_by[Name].part[Name].classified_as",
            (
                PathSegment("identified_by", "Name"),
                PathSegment("part", "Name"),
                PathSegment("classified_as"),
            ),
        ),
    ],
)
def test_parse_and_format_round_trip(path: str, expected: tuple[PathSegment, ...]) -> None:
    assert parse_path(path) == expected
    assert format_path(expected) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".content",
        "content.",
        "identified_by..content",
        "identified_by[0].content",
        "identified_by[name]",
        "identified_by[Name",
        "identified_by[Name][Identifier]",
        "Identified_by",
        "_label",
        "produced_by.timespan.{begin_of_the_begin,end_of_the_end}",
        "identified_by[Name].content ",
        "@context",
    ],
)
def test_rejects_malformed_paths(path: str) -> None:
    with pytest.raises(PathSyntaxError):
        parse_path(path)


@pytest.mark.parametrize("sub", ["ui", "vocab"])
def test_pattern_matches_the_schema(sub: str) -> None:
    schema = json.loads((PROFILES / sub / "_schema.json").read_text(encoding="utf-8"))
    assert schema["$defs"]["Path"]["pattern"] == PATH_PATTERN
