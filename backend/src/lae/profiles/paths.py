"""Parser for UI Profile paths; the grammar is specified in ``profiles/README.md``."""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Same pattern as ``$defs.Path`` in ``profiles/{ui,vocab}/_schema.json``.
PATH_PATTERN = (
    r"^[a-z][a-z0-9_]*(?:\[[A-Z][A-Za-z]*\])?(?:\.[a-z][a-z0-9_]*(?:\[[A-Z][A-Za-z]*\])?)*$"
)

_PATH_RE = re.compile(PATH_PATTERN)
_SEGMENT_RE = re.compile(r"([a-z][a-z0-9_]*)(?:\[([A-Z][A-Za-z]*)\])?")


class PathSyntaxError(ValueError):
    """Raised when a string is not a valid UI Profile path."""


@dataclass(frozen=True, slots=True)
class PathSegment:
    """One step of a profile path: a JSON-LD key and an optional ``type`` filter."""

    key: str
    type_filter: str | None = None

    def __str__(self) -> str:
        """Render the segment in path syntax, e.g. ``identified_by[Name]``."""
        return self.key if self.type_filter is None else f"{self.key}[{self.type_filter}]"


def parse_path(path: str) -> tuple[PathSegment, ...]:
    """Split a profile path into its segments, raising ``PathSyntaxError`` if malformed."""
    if _PATH_RE.fullmatch(path) is None:
        raise PathSyntaxError(f"not a valid profile path: {path!r}")
    segments: list[PathSegment] = []
    for part in path.split("."):
        match = _SEGMENT_RE.fullmatch(part)
        if match is None:  # pragma: no cover - unreachable once PATH_PATTERN matched
            raise PathSyntaxError(f"not a valid profile path segment: {part!r}")
        segments.append(PathSegment(match.group(1), match.group(2)))
    return tuple(segments)


def format_path(segments: tuple[PathSegment, ...] | list[PathSegment]) -> str:
    """Join segments back into path syntax (inverse of ``parse_path``)."""
    return ".".join(str(segment) for segment in segments)
