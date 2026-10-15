"""Structured logging with request/job correlation identifiers.

Correlation ids are stored in ``structlog.contextvars`` so every log line emitted while
handling a request or job carries them without explicit plumbing.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from repolens.config import LogFormat

_REDACTED_KEYS = frozenset({"authorization", "token", "api_token", "password", "secret"})


def _redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in event_dict.keys() & _REDACTED_KEYS:
        event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level: str = "INFO", fmt: LogFormat = LogFormat.JSON) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if fmt is LogFormat.JSON
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


bind_context = structlog.contextvars.bind_contextvars
clear_context = structlog.contextvars.clear_contextvars
