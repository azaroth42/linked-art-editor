"""Structured JSON logging shared by every ``lae`` process (structlog over the standard library).

``configure_logging`` sends structlog events and standard-library records (uvicorn, SQLAlchemy,
httpx) through one JSON renderer, one object per line. Context lives in ``contextvars``:
``RequestContextMiddleware`` binds ``request_id`` for each HTTP request, and code that learns who
or what a request is about binds it with ``bind_log_context(user_id=..., record_id=...)``. Every
later line from the same request (or worker task) then carries those keys.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from typing import IO, Any

import orjson
import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
"""Response header carrying the request id; an incoming value is reused if it is well formed."""

_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

_QUIET_LOGGERS = ("httpx", "httpcore")
"""Libraries that log every outgoing request at INFO; raised to WARNING unless level is DEBUG."""

_SHARED_PROCESSORS: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]


def _dumps(value: Any, **_: Any) -> str:
    """Serialize one log event; values JSON does not know are rendered with ``str``."""
    return orjson.dumps(value, default=str).decode()


def configure_logging(level: str = "info", *, stream: IO[str] | None = None) -> None:
    """Route structlog and standard-library logging to JSON lines on ``stream`` (stdout)."""
    numeric_level = logging.getLevelNamesMapping()[level.upper()]
    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(serializer=_dumps),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        # Let uvicorn's records reach the root handler instead of its own formatters.
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger; call ``configure_logging`` once at process start."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def bind_log_context(*, user_id: object = None, record_id: object = None) -> None:
    """Add ``user_id`` and/or ``record_id`` to every later log line of the current context."""
    values = {"user_id": user_id, "record_id": record_id}
    structlog.contextvars.bind_contextvars(
        **{key: str(value) for key, value in values.items() if value is not None}
    )


def request_id_from_headers(headers: Headers) -> str:
    """Return the caller's ``X-Request-ID`` when it is well formed, else a new random id."""
    incoming = headers.get(REQUEST_ID_HEADER)
    if incoming is not None and _VALID_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """ASGI middleware: bind ``request_id``, echo it as ``X-Request-ID``, log each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = get_logger("lae.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection; only ``http`` scopes are logged."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = request_id_from_headers(Headers(scope=scope))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        status_code = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            path: str = scope["path"]
            # Readiness probes arrive every few seconds; keep them out of INFO logs.
            log = self.logger.debug if path.startswith("/health/") else self.logger.info
            log(
                "http_request",
                method=scope["method"],
                path=path,
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
