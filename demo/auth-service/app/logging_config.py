"""Structured JSON logging: one JSON object per line on stdout.

Fields are stable so logs can be parsed by machines (Docker/K8s log tools, Azure Monitor KQL)
and still read well by an LLM. Tracebacks go into `exc` instead of spanning many lines.
"""

import json
import logging
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime

from app.config import config

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": config.service_name,
            "env": config.environment,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if rid := request_id_var.get():
            entry["request_id"] = rid
        for key in ("method", "path", "status", "duration_ms", "rss_mb", "threads"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            exc_type, exc, _ = record.exc_info
            entry["exc_type"] = f"{exc_type.__module__}.{exc_type.__qualname__}"
            entry["exc_message"] = str(exc)
            entry["exc"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        return json.dumps(entry, default=str)


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": JsonFormatter}},
    "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "json", "stream": "ext://sys.stdout"}},
    "root": {"level": "INFO", "handlers": ["stdout"]},
    "loggers": {
        "uvicorn": {"level": "INFO", "handlers": [], "propagate": True},
        "uvicorn.error": {"level": "INFO", "handlers": [], "propagate": True},
        "uvicorn.access": {"level": "WARNING", "handlers": [], "propagate": False},
    },
}
