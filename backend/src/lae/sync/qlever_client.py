"""The QLever SPARQL client: queries for every process, updates for the sync worker.

Follows the pattern of ``lux/qlever-middletier/qleverlux/clients/qlever.py`` @ 937ea8b (one
``httpx.AsyncClient``, ``POST`` with a form body, ``Accept: application/qlever-results+json``,
IRI brackets stripped and numeric literals coerced) **without** its result cache: the editor reads
straight after it writes, so a cached answer would be stale (Phase 1 guide, Common Mistakes). It
does not shed load either; callers bound their own concurrency.

``QLeverClient(base_url, update_url, access_token)`` is the interface Phase 1 Step 1.11 names:

- ``await client.query(sparql) -> list[dict]``: one dict per result row, keyed by variable name
  without the ``?``. An IRI is its string without brackets, a literal its lexical form (an
  ``xsd`` integer type as ``int``, ``decimal``/``double``/``float`` as ``float``), an unbound
  variable ``None``.
- ``await client.update(sparql) -> None``: ``POST`` to the update endpoint with the access token
  as ``Authorization: Bearer``.

Both raise :class:`QLeverError` when QLever cannot be reached, times out (30 s by default), or
answers with a non-2xx status; the exception carries the status and the response body.

Pass the process's shared ``httpx.AsyncClient`` (``lae.app.get_http_client``) as ``http_client``
to reuse its connection pool; without one the client opens its own, closed by ``aclose()`` or at
the end of ``async with``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, Self

import httpx

QLEVER_RESULTS_JSON: Final = "application/qlever-results+json"
"""The media type ``query`` asks for: QLever's own result format (``selected`` and ``res``)."""

DEFAULT_TIMEOUT_S: Final = 30.0
"""Seconds a query or update may take, including the connection."""

_XSD: Final = "http://www.w3.org/2001/XMLSchema#"
_INTEGER_TYPES: Final = frozenset(
    _XSD + name
    for name in (
        "integer",
        "int",
        "long",
        "short",
        "byte",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "negativeInteger",
        "positiveInteger",
        "unsignedLong",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
    )
)
_FLOAT_TYPES: Final = frozenset(_XSD + name for name in ("decimal", "double", "float"))

# QLever writes a literal as its lexical form between quotes, unescaped, then an optional language
# tag or datatype. The greedy body takes everything up to the last quote that such a suffix (or
# the end) can follow, so quotes inside the value are kept.
_LITERAL: Final = re.compile(r'"(.*)"(?:@([A-Za-z0-9-]+)|\^\^<([^<>]*)>)?', re.DOTALL)


class QLeverError(RuntimeError):
    """QLever could not be reached or did not answer 2xx; ``status`` is ``None`` without a reply."""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(f"{message} (HTTP {status}): {body}" if status else f"{message}: {body}")
        self.status = status
        self.body = body


def parse_value(value: str | None) -> Any:
    """Turn one ``qlever-results+json`` cell into a Python value (see the module docstring)."""
    if value is None:
        return None
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1]
    match = _LITERAL.fullmatch(value)
    if match is None:
        return value
    lexical, _, datatype = match.groups()
    try:
        if datatype in _INTEGER_TYPES:
            return int(lexical)
        if datatype in _FLOAT_TYPES:
            return float(lexical)
    except ValueError:
        return lexical
    return lexical


def parse_results(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Turn a ``qlever-results+json`` body into one dict per row, keyed by variable name."""
    names = [str(variable).removeprefix("?") for variable in body.get("selected", [])]
    return [
        {name: parse_value(cell) for name, cell in zip(names, row, strict=True)}
        for row in body.get("res", [])
    ]


class QLeverClient:
    """Asynchronous SPARQL client for one QLever server; no result cache."""

    def __init__(
        self,
        base_url: str,
        update_url: str | None = None,
        access_token: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url
        self.update_url = update_url or base_url
        self.access_token = access_token
        self.timeout = timeout
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP client if this object opened it (a shared one is left open)."""
        if self._owns_client:
            await self._client.aclose()

    async def _post(
        self, url: str, data: dict[str, str], headers: dict[str, str]
    ) -> httpx.Response:
        try:
            response = await self._client.post(
                url, data=data, headers=headers, timeout=self.timeout
            )
        except httpx.HTTPError as error:
            raise QLeverError(f"QLever at {url} failed", body=repr(error)) from error
        if not response.is_success:
            raise QLeverError(
                f"QLever at {url} refused", status=response.status_code, body=response.text
            )
        return response

    async def query(self, sparql: str) -> list[dict[str, Any]]:
        """Run a SPARQL query; return its rows (see the module docstring)."""
        response = await self._post(
            self.base_url, {"query": sparql}, {"Accept": QLEVER_RESULTS_JSON}
        )
        try:
            body = response.json()
        except ValueError as error:
            raise QLeverError(
                "QLever answered without JSON", status=response.status_code, body=response.text
            ) from error
        if not isinstance(body, dict) or body.get("status") == "ERROR":
            raise QLeverError(
                "QLever query failed", status=response.status_code, body=response.text
            )
        return parse_results(body)

    async def update(self, sparql: str) -> None:
        """Send a SPARQL Update with the access token; raise ``QLeverError`` unless it is 2xx."""
        headers = {"Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        await self._post(self.update_url, {"update": sparql}, headers)
