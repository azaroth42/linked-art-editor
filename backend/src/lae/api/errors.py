"""Structured HTTP errors: ``{code, path, params}`` bodies instead of English prose.

Conventions §4 ("structured output only") and decision C3: the backend returns a diagnostic code
and its parameters; the client looks the code up in ``frontend/public/locales/<lang>/errors.json``.
Raise :class:`ApiError` anywhere in a request (endpoint or dependency); the handler that
``lae.app.create_app`` registers turns it into

```json
{"code": "LAE-AUTH-001", "path": "/api/records", "params": {"missing": ["edit_draft"]}}
```

``path`` is the request path the error refers to (validation diagnostics, Step 1.9, use a JSON
Pointer into the document instead). ``params`` holds only JSON values the message interpolates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """An HTTP error answered with a structured ``{code, path, params}`` body."""

    def __init__(
        self,
        status_code: int,
        code: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.params: dict[str, Any] = dict(params or {})
        self.headers: dict[str, str] = dict(headers or {})


def error_body(code: str, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the structured error body ``{code, path, params}``."""
    return {"code": code, "path": path, "params": dict(params or {})}


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Exception handler: answer an :class:`ApiError` with its status, headers, and body."""
    assert isinstance(exc, ApiError)
    return JSONResponse(
        error_body(exc.code, request.url.path, exc.params),
        status_code=exc.status_code,
        headers=exc.headers,
    )
