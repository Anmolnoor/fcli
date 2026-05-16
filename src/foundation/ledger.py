"""Append-only JSONL ledger of completed agent actions.

This is a thin, user-facing artifact intended for ``grep``/``tail`` workflows,
sibling to but smaller than the SQLite ``history`` store. One process writes
one file in append mode; each record is a single JSON object on its own line.
Records are immutable once written; this module never reads, rewrites, or
truncates existing entries.

Concurrency
-----------
On POSIX, single ``write()`` calls shorter than ``PIPE_BUF`` (4 KB on Linux
and macOS) are atomic across processes when the file was opened with
``O_APPEND``. The ledger enforces a per-record cap (2 KB by default) so that
concurrent writers from different ``foundation`` processes interleave cleanly
at line boundaries. Longer summaries are truncated with an ellipsis.

Secret redaction
----------------
Two passes are applied to every entry's free-form text fields:

1. Dict-level: ``redact_payload`` from :mod:`foundation.observability` masks
   any value under a known-sensitive key (``api_key``, ``token``, …).
2. Text-level: a small regex set replaces obvious credential patterns in
   the surfaces summaries (Bearer tokens, OpenAI ``sk-…`` keys, AWS access
   key ids, JWTs) with ``[redacted]``.

The redactor is intentionally conservative. It is meant to catch the
obvious case where a model echoes back a secret it was just handed — it is
not a full DLP layer.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from foundation.models import ExecutionResult, ExecutionStatus, PlannedAction
from foundation.observability import redact_payload

logger = logging.getLogger("foundation.ledger")

_LEDGER_SCHEMA_VERSION = "1.0.0"
_DEFAULT_MAX_SUMMARY_CHARS = 200
_DEFAULT_MAX_RECORD_BYTES = 2 * 1024  # stay safely under POSIX PIPE_BUF.

# Text-level secret patterns. Each must match a *whole token* in isolation,
# so we use word-boundary anchors where possible.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI-style.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id.
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),  # JWT.
)


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _scrub_text(text: str) -> str:
    """Replace obvious secret-shaped substrings with ``[redacted]``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _summarize(value: Any, *, limit: int) -> str:
    """Turn an arbitrary value into a short scrubbed string suitable for ledger I/O."""
    if value is None:
        return ""
    if isinstance(value, Mapping):
        rendered = json.dumps(redact_payload(value), default=str, ensure_ascii=False)
    elif isinstance(value, BaseModel):
        rendered = json.dumps(
            redact_payload(value.model_dump(mode="json")),
            default=str,
            ensure_ascii=False,
        )
    else:
        rendered = str(value)
    scrubbed = _scrub_text(rendered)
    if len(scrubbed) > limit:
        return scrubbed[: limit - 1] + "…"
    return scrubbed


class LedgerEntry(BaseModel):
    """One append-only record describing a completed agent action.

    The shape is intentionally narrow so that downstream tools (``jq``,
    ``grep``, dashboards) can rely on a stable set of fields. Add new
    optional fields here freely; do not rename or remove existing ones
    without bumping the schema version.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = _LEDGER_SCHEMA_VERSION
    timestamp: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    capability_id: str | None = None
    action_id: str = Field(min_length=1)
    input_summary: str = ""
    output_summary: str = ""
    status: str = Field(min_length=1)
    error: str | None = None


def build_entry(
    action: PlannedAction,
    result: ExecutionResult,
    *,
    actor: str = "agent",
    max_summary_chars: int = _DEFAULT_MAX_SUMMARY_CHARS,
) -> LedgerEntry:
    """Construct a redacted, length-bounded LedgerEntry for one completed action."""
    capability_id: str | None = None
    input_value: Any
    if action.tool_call is not None:
        capability_id = action.tool_call.capability_id
        input_value = action.tool_call.arguments
    elif action.shell is not None:
        input_value = {
            "command": action.shell.command,
            "args": list(action.shell.args),
            "cwd": action.shell.cwd,
        }
    elif action.explanation is not None:
        input_value = action.explanation
    else:
        input_value = action.summary

    output_value: Any = result.summary
    if result.artifact is not None:
        output_value = {"summary": result.summary, "artifact": result.artifact}

    return LedgerEntry(
        timestamp=_utcnow(),
        actor=actor,
        action_type=action.kind.value,
        capability_id=capability_id,
        action_id=action.id,
        input_summary=_summarize(input_value, limit=max_summary_chars),
        output_summary=_summarize(output_value, limit=max_summary_chars),
        status=result.status.value
        if isinstance(result.status, ExecutionStatus)
        else str(result.status),
        error=result.error,
    )


class Ledger:
    """Append-only JSONL writer for :class:`LedgerEntry` records.

    Threading: a single instance is safe for multi-threaded use within one
    process. Cross-process safety is handled by ``O_APPEND`` atomic writes
    on POSIX as long as each line stays under the per-record byte cap.
    """

    def __init__(
        self,
        *,
        path: Path,
        max_record_bytes: int = _DEFAULT_MAX_RECORD_BYTES,
    ) -> None:
        self._path = Path(path).expanduser()
        self._max_record_bytes = max(256, int(max_record_bytes))
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """Filesystem path the ledger appends to."""
        return self._path

    def record(self, entry: LedgerEntry) -> None:
        """Append one entry. Truncates the line if it would exceed the byte cap."""
        line = entry.model_dump_json() + "\n"
        encoded = line.encode("utf-8")
        if len(encoded) > self._max_record_bytes:
            # Truncate input/output summaries proportionally to fit the cap.
            overhead = len(encoded) - self._max_record_bytes
            shrunk = entry.model_copy(
                update={
                    "input_summary": _truncate_text(entry.input_summary, overhead // 2 + 1),
                    "output_summary": _truncate_text(
                        entry.output_summary, overhead - overhead // 2 + 1
                    ),
                }
            )
            line = shrunk.model_dump_json() + "\n"
            encoded = line.encode("utf-8")
        with self._lock:
            try:
                with self._path.open("ab") as handle:
                    handle.write(encoded)
            except OSError as exc:  # pragma: no cover - filesystem failure path.
                logger.warning("ledger_write_failed path=%s error=%s", self._path, exc)


def _truncate_text(text: str, drop: int) -> str:
    drop = max(drop, 0)
    if drop >= len(text):
        return "…"
    return text[: len(text) - drop - 1] + "…"


__all__ = [
    "Ledger",
    "LedgerEntry",
    "build_entry",
]
