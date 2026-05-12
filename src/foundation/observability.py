"""Structured observability helpers for Foundation CLI."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

try:  # Optional dependency for richer structured logging.
    import structlog  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency.
    structlog = None

STRUCTURED_LOG_SCHEMA_VERSION = "1.0.0"

EVENT_SESSION_START = "session_start"
EVENT_SESSION_END = "session_end"
EVENT_USER_REQUEST = "user_request"
EVENT_PLAN_STARTED = "plan_generation_started"
EVENT_PLAN_FINISHED = "plan_generation_finished"
EVENT_PLAN_FAILED = "plan_generation_failed"
EVENT_PLAN_PROVIDER_CALL_STARTED = "plan_provider_call_started"
EVENT_PLAN_PROVIDER_CALL_FINISHED = "plan_provider_call_finished"
EVENT_PLAN_VALIDATION_STARTED = "plan_validation_started"
EVENT_PLAN_REPAIR_ATTEMPT = "plan_repair_attempt"
EVENT_PROVIDER_CALL_STARTED = "provider_call_started"
EVENT_PROVIDER_CALL_FINISHED = "provider_call_finished"
EVENT_PROVIDER_CALL_RETRY = "provider_call_retry"
EVENT_PROVIDER_CALL_FAILED = "provider_call_failed"
EVENT_TOOL_CALL_STARTED = "tool_call_started"
EVENT_TOOL_CALL_FINISHED = "tool_call_finished"
EVENT_TOOL_CALL_FAILED = "tool_call_failed"
EVENT_TOOL_EXECUTION_STARTED = "tool_execution_started"
EVENT_TOOL_EXECUTION_FINISHED = "tool_execution_finished"
EVENT_TOOL_EXECUTION_FAILED = "tool_execution_failed"
EVENT_SHELL_EXECUTION_STARTED = "shell_execution_started"
EVENT_SHELL_EXECUTION_FINISHED = "shell_execution_finished"
EVENT_SHELL_EXECUTION_FAILED = "shell_execution_failed"
EVENT_APPROVAL_REQUESTED = "approval_requested"
EVENT_APPROVAL_RESOLVED = "approval_resolved"
EVENT_EXCEPTION = "exception"
EVENT_RETRY = "retry"
EVENT_ITERATION_STARTED = "iteration_started"
EVENT_ITERATION_COMPLETED = "iteration_completed"

_SENSITIVE_KEY_HINTS = {
    "api_key",
    "authorization",
    "bearer",
    "credential",
    "cookie",
    "password",
    "private",
    "secret",
    "token",
}


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    normalized = level.upper()
    return getattr(logging, normalized, logging.INFO)


def _to_safe_scalar(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if BaseModel is not None and isinstance(value, BaseModel):
        return _to_safe_value(value.model_dump(mode="json"))
    if isinstance(value, set):
        return sorted(str(item) for item in value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _to_safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _redact_payload(value)
    if isinstance(value, list):
        return [_to_safe_value(item) for item in value]
    return _to_safe_scalar(value)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if "secret" in lowered or "password" in lowered or "token" in lowered:
        return True
    return any(hint in lowered for hint in _SENSITIVE_KEY_HINTS)


def _redact_payload(
    payload: Mapping[str, Any] | None,
    *,
    allow_text_values: set[str] | None = None,
) -> dict[str, Any]:
    if payload is None:
        return {}

    redacted: dict[str, Any] = {}
    allowed = {item.lower() for item in allow_text_values or set()}
    for key, value in payload.items():
        if key in allowed:
            redacted[key] = _to_safe_value(value)
            continue
        if isinstance(key, str) and _is_sensitive_key(key):
            redacted[key] = "[redacted]"
            continue
        if isinstance(value, Mapping):
            redacted[key] = _redact_payload(value)
        else:
            redacted[key] = _to_safe_value(value)
    return redacted


def redact_payload(
    payload: Mapping[str, Any] | None,
    *,
    allow_text_values: set[str] | None = None,
) -> dict[str, Any]:
    """Return a redacted copy of one event payload."""
    return _redact_payload(payload, allow_text_values=allow_text_values)


def _event_payload(
    *,
    event_name: str,
    payload: Mapping[str, Any] | None,
    level: str = "info",
) -> dict[str, Any]:
    return {
        "event_name": event_name,
        "event_schema_version": STRUCTURED_LOG_SCHEMA_VERSION,
        "event_time": _utcnow(),
        "level": level,
        "payload": _redact_payload(payload),
    }


def emit_event(
    event_name: str,
    *,
    payload: Mapping[str, Any] | None = None,
    logger_name: str = "foundation.events",
    level: int | str = logging.INFO,
) -> None:
    """Emit one stable-structured event to the configured logging back-end."""
    normalized_level = _normalize_level(level)
    logger = logging.getLogger(logger_name)
    event_payload = _event_payload(
        event_name=event_name,
        payload=payload,
        level=logging.getLevelName(normalized_level),
    )
    message_payload = json.dumps(event_payload["payload"], ensure_ascii=True, sort_keys=True)
    logger.log(
        normalized_level,
        "event_name=%s",
        event_name,
        extra={
            "foundation_event": event_name,
            "foundation_event_payload": event_payload["payload"],
            "foundation_event_schema_version": STRUCTURED_LOG_SCHEMA_VERSION,
            "foundation_event_time": event_payload["event_time"],
        },
    )
    logger.debug(
        "event_payload=%s",
        message_payload,
    )


def emit_exception(
    event_name: str,
    exc: BaseException,
    *,
    payload: Mapping[str, Any] | None = None,
    logger_name: str = "foundation.events",
    level: int | str = logging.ERROR,
    include_trace: bool = False,
) -> None:
    """Emit a failure event; include traceback only when explicitly requested."""
    details: dict[str, Any] = {
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }
    if payload:
        details.update(payload)
    if include_trace or _normalize_level(level) <= logging.DEBUG:
        details["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    emit_event(
        event_name,
        payload=details,
        logger_name=logger_name,
        level=level,
    )


def configure_structlog_if_available() -> bool:
    """Configure a lightweight, optional structlog stack."""
    if structlog is None:
        return False

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return True
