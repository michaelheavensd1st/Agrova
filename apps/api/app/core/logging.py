"""Structured JSON logging.

Emits a single-line JSON record per log event, plus a ``request_id`` /
``user_id`` / ``organization_id`` context that middleware can populate.
The interface is stable so an OpenTelemetry log emitter can be plugged
in later without touching call-sites.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any, ClassVar, TextIO

from app.core.config import get_settings

# --------------------------------------------------------------------- #
# Context — populated by middleware, consumed by the JSON formatter.
# --------------------------------------------------------------------- #
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
organization_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "organization_id", default=None
)


def new_request_id() -> str:
    return uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    """Structured, single-line JSON formatter."""

    _RESERVED: ClassVar[set[str]] = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        req_id = request_id_var.get()
        if req_id:
            payload["request_id"] = req_id
        user_id = user_id_var.get()
        if user_id:
            payload["user_id"] = user_id
        org_id = organization_id_var.get()
        if org_id:
            payload["organization_id"] = org_id

        # Merge any extra=... fields passed to the log call.
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


class AgrovixLogHandler(logging.StreamHandler[TextIO]):
    """Application-owned handler, distinct from handlers installed by the host."""


def configure_logging() -> None:
    """Install the JSON handler on the root logger (idempotent)."""
    settings = get_settings()
    level = logging.DEBUG if settings.api_debug else logging.INFO

    root = logging.getLogger()
    handler = next(
        (item for item in root.handlers if isinstance(item, AgrovixLogHandler)),
        None,
    )
    if handler is None:
        handler = AgrovixLogHandler(sys.stdout)
        root.addHandler(handler)
    handler.setFormatter(JsonFormatter())
    root.setLevel(level)

    # Tame noisy libraries.
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
