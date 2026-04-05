"""Logging helpers for Foundation CLI."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from foundation.observability import configure_structlog_if_available


class FoundationJSONFormatter(logging.Formatter):
    """Simple JSON formatter that preserves structured event metadata."""

    _IGNORE_ATTRS = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "exc_info",
        "exc_text",
        "stack_info",
    }

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "event": getattr(record, "foundation_event", None),
            "event_schema_version": getattr(
                record,
                "foundation_event_schema_version",
                None,
            ),
            "event_payload": getattr(record, "foundation_event_payload", None),
            "event_time": getattr(record, "foundation_event_time", None),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in self._IGNORE_ATTRS or key.startswith("_"):
                continue
            if key in {
                "foundation_event",
                "foundation_event_payload",
                "foundation_event_schema_version",
                "foundation_event_time",
            }:
                continue
            payload[f"extra:{key}"] = _safe_json_value(value)
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _safe_json_value(v) for k, v in value.items()}
    if isinstance(value, (set, tuple, list)):
        return [_safe_json_value(item) for item in list(value)]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _normalize_level(level: int | str) -> int:
    if isinstance(level, str):
        return getattr(logging, level.upper(), logging.INFO)
    return level


def _install_handlers(
    *,
    level: int,
    log_path: Path | None,
    structured: bool,
) -> None:
    formatter: logging.Formatter
    if structured:
        formatter = FoundationJSONFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_path is not None:
        try:
            path = Path(log_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.FileHandler(
                    path,
                    mode="a",
                    encoding="utf-8",
                )
            )
        except OSError:
            # Keep logging alive even if the configured path is not writable.
            handlers = [logging.StreamHandler()]

    for handler in handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    for handler in handlers:
        root_logger.addHandler(handler)
    root_logger.propagate = False


def configure_logging(
    level: int | str = logging.INFO,
    *,
    log_path: Path | None = None,
    structured: bool = False,
) -> logging.Logger:
    """Configure a process-wide structured/unstructured logging baseline."""
    normalized_level = _normalize_level(level)
    structured = bool(structured)
    _install_handlers(
        level=normalized_level,
        log_path=log_path,
        structured=structured,
    )
    if structured:
        configure_structlog_if_available()
    return logging.getLogger("foundation")
