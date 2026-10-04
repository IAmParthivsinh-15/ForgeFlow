"""Structured JSON logging with workflow correlation fields (spec section 69)."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_correlation: ContextVar[dict[str, str] | None] = ContextVar("forgeflow_correlation", default=None)


def bind_context(**fields: str | None) -> None:
    """Attach correlation identifiers to every log line emitted in this context."""
    current = dict(_correlation.get() or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    _correlation.set(current)


def clear_context() -> None:
    _correlation.set(None)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "service": self.service,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(_correlation.get() or {})
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            entry.update(extra)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Third-party clients are noisy at INFO.
    for noisy in ("aiokafka", "httpx", "openai", "pymongo"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger, message: str, level: int = logging.INFO, **fields: Any
) -> None:
    logger.log(level, message, extra={"fields": fields})
