"""Tests for ``lae.app.openapi_document`` and ``scripts/export_openapi.py`` (Phase 1, Step 1.16).

The frontend's generated API types (``frontend/src/api/schema.d.ts``) come from this document, so
it must match what a running server serves and must not depend on the caller's ``LAE_*``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from lae.app import create_app, openapi_document
from lae.settings import Settings

EXPORT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def test_document_lists_the_frontend_endpoints() -> None:
    document = openapi_document()
    assert document["openapi"].startswith("3.")
    for path in ("/health/live", "/auth/me", "/auth/logout", "/api/tokens"):
        assert path in document["paths"], path
    me_schema = document["components"]["schemas"]["Me"]
    assert set(me_schema["required"]) == {"id", "display_name", "roles", "permissions"}


def test_document_ignores_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = openapi_document()
    # Settings that ``create_app(Settings())`` refuses must not stop the export.
    monkeypatch.setenv("LAE_ENV", "production")
    monkeypatch.setenv("LAE_DEV_USER", "editor@example.org:Editor")
    assert openapi_document() == expected


async def test_document_equals_the_served_openapi_json() -> None:
    app = create_app(Settings(database_url="postgresql+asyncpg://lae@127.0.0.1:1/unused"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json() == openapi_document()


def test_export_script_writes_only_json_to_stdout() -> None:
    result = subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT)], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == openapi_document()


def test_export_script_writes_the_output_file(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    subprocess.run([sys.executable, str(EXPORT_SCRIPT), "-o", str(output)], check=True)
    assert json.loads(output.read_text(encoding="utf-8")) == openapi_document()
