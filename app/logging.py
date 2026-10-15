"""Structured (JSON) logging for both our code and third-party/stdlib loggers."""

from __future__ import annotations

import logging
import sys

import structlog

_SENSITIVE = {
    "password",
    "new_password",
    "current_password",
    "token",
    "access_token",
    "authorization",
    "cookie",
    "secret",
    "secret_key",
}


def _redact(_logger, _method, event_dict):
    event_dict.pop("color_message", None)  # uvicorn's ANSI-coloured duplicate of the message
    for key in list(event_dict):
        if key.lower() in _SENSITIVE:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level: str = "INFO", json_logs: bool = True, **static: str) -> None:
    def add_static(_logger, _method, event_dict):
        for k, v in static.items():
            event_dict.setdefault(k, v)
        return event_dict

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.ExtraAdder(),
        add_static,
        _redact,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Route uvicorn's own logs through the same JSON pipeline (access log is
    # replaced by our middleware, which also records request ids and timings).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
