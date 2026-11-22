"""Tests for ``lae.logging``: JSON log lines, bound context, and the request-id middleware."""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI

from lae.logging import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    bind_log_context,
    configure_logging,
    get_logger,
)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Configure JSON logging into a buffer; restore the previous logging setup afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    stream = io.StringIO()
    configure_logging("debug", stream=stream)
    try:
        yield stream
    finally:
        root.handlers, root.level = saved_handlers, saved_level
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse every line written so far (each must be a JSON object), minus asyncio's debug."""
    parsed = [json.loads(line) for line in stream.getvalue().splitlines()]
    return [line for line in parsed if line["logger"] != "asyncio"]


def test_structlog_events_are_json_lines(log_stream: io.StringIO) -> None:
    get_logger("lae.test").info("something_happened", count=3)
    [line] = lines(log_stream)
    assert line["event"] == "something_happened"
    assert line["count"] == 3
    assert line["level"] == "info"
    assert line["logger"] == "lae.test"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z", line["timestamp"])


def test_standard_library_records_are_json_lines(log_stream: io.StringIO) -> None:
    structlog.contextvars.bind_contextvars(request_id="abc")
    logging.getLogger("uvicorn.error").warning("Started %s", "server")
    [line] = lines(log_stream)
    assert line["event"] == "Started server"
    assert line["level"] == "warning"
    assert line["logger"] == "uvicorn.error"
    assert line["request_id"] == "abc"


def test_exceptions_are_rendered_inside_the_json_line(log_stream: io.StringIO) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("lae.test").exception("failed")
    [line] = lines(log_stream)
    assert "ValueError: boom" in line["exception"]


def test_bind_log_context_adds_user_and_record(log_stream: io.StringIO) -> None:
    bind_log_context(user_id=7, record_id="0193b2c4-0000-7000-8000-000000000001")
    bind_log_context(record_id=None)  # None leaves an existing binding alone
    get_logger("lae.test").info("edited")
    [line] = lines(log_stream)
    assert line["user_id"] == "7"
    assert line["record_id"] == "0193b2c4-0000-7000-8000-000000000001"


def test_level_filters_lower_records() -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("warning", stream=stream)
        get_logger("lae.test").info("hidden")
        get_logger("lae.test").warning("shown")
    finally:
        root.handlers, root.level = saved_handlers, saved_level
        structlog.reset_defaults()
    assert [line["event"] for line in lines(stream)] == ["shown"]


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    logger = get_logger("lae.test")

    @app.get("/things/{thing_id}")
    async def read_thing(thing_id: str) -> dict[str, str]:
        bind_log_context(record_id=thing_id)
        logger.info("reading_thing")
        return {"id": thing_id}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def get(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        return await client.get(path, headers=headers)


async def test_middleware_binds_request_id_and_logs_the_request(log_stream: io.StringIO) -> None:
    response = await get("/things/t1")
    request_id = response.headers[REQUEST_ID_HEADER]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    inner, request_line = lines(log_stream)
    assert inner["event"] == "reading_thing"
    assert inner["request_id"] == request_id
    assert inner["record_id"] == "t1"
    assert request_line["event"] == "http_request"
    assert request_line["level"] == "info"
    assert request_line["request_id"] == request_id
    assert request_line["record_id"] == "t1"
    assert request_line["method"] == "GET"
    assert request_line["path"] == "/things/t1"
    assert request_line["status"] == 200
    assert request_line["duration_ms"] >= 0


async def test_middleware_reuses_a_well_formed_incoming_request_id(
    log_stream: io.StringIO,
) -> None:
    response = await get("/things/t1", headers={REQUEST_ID_HEADER: "proxy-42.a:b"})
    assert response.headers[REQUEST_ID_HEADER] == "proxy-42.a:b"
    assert {line["request_id"] for line in lines(log_stream)} == {"proxy-42.a:b"}


@pytest.mark.parametrize("bad", ["has space", "x" * 129, "semi;colon", ""])
async def test_middleware_replaces_a_malformed_incoming_request_id(
    log_stream: io.StringIO, bad: str
) -> None:
    response = await get("/things/t1", headers={REQUEST_ID_HEADER: bad})
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers[REQUEST_ID_HEADER])


async def test_requests_do_not_share_context(log_stream: io.StringIO) -> None:
    first = await get("/things/t1")
    second = await get("/health/live")
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]
    last = lines(log_stream)[-1]
    assert last["path"] == "/health/live"
    assert "record_id" not in last


async def test_health_requests_are_logged_at_debug(log_stream: io.StringIO) -> None:
    await get("/health/live")
    [line] = lines(log_stream)
    assert line["event"] == "http_request"
    assert line["level"] == "debug"
