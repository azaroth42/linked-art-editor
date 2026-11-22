"""Structured validation diagnostics (spec §5; decisions C2, C3).

A ``Diagnostic`` is ``{code, path, severity, params, message_en}``:

- ``code``: ``LAE-<AREA>-<NNN>`` (``LAE-SCHEMA-<validator>`` for JSON Schema), which is also the
  i18n key under the ``errors`` namespace (conventions §2).
- ``path``: a JSON Pointer (RFC 6901) into the validated document; ``""`` is the record itself.
  A pointer may name a property that is not there, when that absence is the problem.
- ``severity``: ``violation`` blocks publishing, ``warning`` needs acknowledgement, ``info`` is
  guidance.
- ``params``: the JSON values the message interpolates.
- ``message_en``: the English message, for RDF and MCP consumers (spec §5). The text comes from
  ``frontend/public/locales/en/errors.json``, the only place English messages live (decision C3),
  with ``{name}`` placeholders filled from ``params``. Clients that localize ignore it and look
  the code up themselves.

Every diagnostic is built with ``diagnostic()``, so a code without an ``errors.json`` entry is
visible in one place: ``message_en`` then falls back to the validator's own text (or the code),
and ``tests/validation`` fails because it records every code the tests emit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, computed_field

Severity = Literal["violation", "warning", "info"]

ERRORS_JSON: Final = (
    Path(__file__).resolve().parents[4] / "frontend" / "public" / "locales" / "en" / "errors.json"
)
"""The English message catalogue, keyed by diagnostic code."""

_PLACEHOLDER: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SEVERITY_ORDER: Final = {"violation": 0, "warning": 1, "info": 2}


class Diagnostic(BaseModel):
    """One validation finding; see the module docstring for the fields."""

    model_config = ConfigDict(frozen=True)

    code: str
    path: str
    severity: Severity
    params: dict[str, Any]
    message_en: str


class Diagnostics(BaseModel):
    """The findings for one document; ``ok`` means that none of them is a violation."""

    model_config = ConfigDict(frozen=True)

    items: list[Diagnostic]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        """True when no item has severity ``violation`` (warnings do not count)."""
        return has_no_violation(self.items)


def has_no_violation(items: Iterable[Diagnostic]) -> bool:
    """True when no diagnostic in ``items`` is a violation."""
    return all(item.severity != "violation" for item in items)


@lru_cache(maxsize=1)
def messages() -> Mapping[str, str]:
    """Return the English catalogue; empty when the frontend files are not installed."""
    try:
        loaded = json.loads(ERRORS_JSON.read_bytes())
    except FileNotFoundError:
        return {}
    return {key: value for key, value in loaded.items() if isinstance(value, str)}


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return ", ".join(_render(item) for item in value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def message_en(code: str, params: Mapping[str, Any], fallback: str | None = None) -> str:
    """Return the English message of ``code`` with ``{name}`` placeholders filled from ``params``.

    A placeholder without a parameter is left as it is. Without a catalogue entry the result is
    ``fallback`` (the validator's own text), or the code.
    """
    template = messages().get(code)
    if template is None:
        return fallback if fallback else code
    return _PLACEHOLDER.sub(
        lambda match: _render(params[match[1]]) if match[1] in params else match[0], template
    )


def diagnostic(
    code: str,
    path: str,
    severity: Severity,
    params: Mapping[str, Any] | None = None,
    *,
    fallback: str | None = None,
) -> Diagnostic:
    """Build a ``Diagnostic``, taking ``message_en`` from the catalogue (else ``fallback``)."""
    values = dict(params or {})
    return Diagnostic(
        code=code,
        path=path,
        severity=severity,
        params=values,
        message_en=message_en(code, values, fallback),
    )


def pointer_token(part: str | int) -> str:
    """Escape one reference token of a JSON Pointer (RFC 6901 §3)."""
    return str(part).replace("~", "~0").replace("/", "~1")


def pointer(parts: Iterable[str | int]) -> str:
    """Return the JSON Pointer for a sequence of keys and indexes; ``""`` for the root."""
    return "".join("/" + pointer_token(part) for part in parts)


def child(path: str, part: str | int) -> str:
    """Return the JSON Pointer of ``part`` below ``path``."""
    return f"{path}/{pointer_token(part)}"


def sort_key(item: Diagnostic) -> tuple[int, str, str, str]:
    """Order diagnostics by severity, then path, code, and parameters (deterministic output)."""
    return (
        _SEVERITY_ORDER[item.severity],
        item.path,
        item.code,
        json.dumps(item.params, sort_keys=True, default=str),
    )


def ordered(items: Iterable[Diagnostic]) -> list[Diagnostic]:
    """Return ``items`` without exact duplicates, in ``sort_key`` order."""
    unique = {sort_key(item): item for item in items}
    return [unique[key] for key in sorted(unique)]
