"""Persistent NDJSON event-log writer for v4 Stage 02 (Pass A).

Every fcli session, when persistence is enabled, writes a redacted NDJSON
event file under ``<events_dir>/<session_id>.ndjson`` plus a row in
``<events_dir>/sessions.jsonl`` so a future GUI or analyzer can enumerate
past sessions without scanning every file.

The writer is designed to never block the agent: I/O errors are swallowed
and surfaced as a session-level ``status=write_truncated`` row in the index.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import threading
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from foundation.monitor.protocol import build_envelope, encode_envelope
from foundation.observability import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_USER_REQUEST,
)

logger = logging.getLogger("foundation.monitor.event_log")

_INDEX_FILENAME = "sessions.jsonl"
_INDEX_TMP_SUFFIX = ".tmp"
_DEFAULT_RETENTION_SESSIONS = 200
_DEFAULT_RETENTION_BYTES = 500 * 1024 * 1024
# Signals we handle to flush an in-progress session on hard kill.
_INTERRUPT_SIGNALS: tuple[int, ...] = tuple(
    sig
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None))
    if sig is not None
)


class EventLogWriter:
    """One-event-at-a-time writer; one logical instance per fcli process.

    Multiple sessions may flow through the same writer (interactive REPL),
    each producing its own NDJSON file. The writer maintains the current
    file handle and rotates on ``session_start``; flushes and closes on
    ``session_end``. ``__exit__`` and an ``atexit`` hook guarantee that an
    interrupted session is closed with ``status=interrupted``.
    """

    def __init__(
        self,
        *,
        events_dir: Path,
        max_sessions: int = _DEFAULT_RETENTION_SESSIONS,
        max_bytes: int = _DEFAULT_RETENTION_BYTES,
        install_signal_handlers: bool = True,
    ) -> None:
        self._events_dir = Path(events_dir).expanduser()
        self._max_sessions = max(1, int(max_sessions))
        self._max_bytes = max(1, int(max_bytes))
        self._lock = threading.Lock()
        self._handle: BinaryIO | None = None
        self._file_path: Path | None = None
        self._session_id: str | None = None
        self._request_id: str | None = None
        self._request_summary: str = ""
        self._started_at: str | None = None
        self._truncated = False
        self._closed = False
        self._pending_envelopes: list[Mapping[str, Any]] = []
        self._index_path = self._events_dir / _INDEX_FILENAME
        self._atexit_registered = False
        self._install_signal_handlers = install_signal_handlers
        self._previous_signal_handlers: dict[int, Any] = {}
        self._ensure_dir()

    def __enter__(self) -> EventLogWriter:
        if not self._atexit_registered:
            atexit.register(self._atexit_close)
            self._atexit_registered = True
        if self._install_signal_handlers:
            self._install_handlers()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._restore_signal_handlers()
        self.close(status="interrupted" if exc is not None else None)

    @property
    def events_dir(self) -> Path:
        return self._events_dir

    def write_event(self, event_name: str, payload: Mapping[str, Any]) -> None:
        """Sink callback wired into ``ObserverService.event_sink``."""
        with self._lock:
            if event_name == EVENT_USER_REQUEST and self._request_summary == "":
                summary = payload.get("request_text") or ""
                self._request_summary = str(summary)[:200]
            if event_name == EVENT_SESSION_START:
                session_id = self._coerce_session_id(payload)
                if session_id is not None:
                    self._open_session(
                        session_id=session_id,
                        request_id=str(payload.get("request_id") or ""),
                        timestamp_payload=payload,
                    )
            envelope = build_envelope(event_name, payload)
            self._write_envelope(envelope)
            if event_name == EVENT_SESSION_END:
                ended_at = envelope["ts"]
                status = str(payload.get("status") or "completed")
                self._close_session(ended_at=ended_at, status=status)

    def close(self, *, status: str | None = None) -> None:
        """Force-close any open session (e.g. on KeyboardInterrupt)."""
        with self._lock:
            if self._handle is None:
                return
            self._close_session(
                ended_at=None,
                status=status or "interrupted",
            )

    # --- internals --------------------------------------------------------

    def _ensure_dir(self) -> None:
        try:
            self._events_dir.mkdir(parents=True, exist_ok=True)
            with suppress(OSError):
                os.chmod(self._events_dir, 0o700)
        except OSError as exc:
            logger.warning(
                "event_log_dir_unavailable path=%s error=%s",
                self._events_dir,
                exc,
            )

    def _open_session(
        self,
        *,
        session_id: str,
        request_id: str,
        timestamp_payload: Mapping[str, Any],
    ) -> None:
        if self._handle is not None:
            # Defensive: a prior session never received session_end.
            self._close_session(ended_at=None, status="interrupted")
        self._session_id = session_id
        self._request_id = request_id
        self._truncated = False
        self._closed = False
        self._file_path = self._events_dir / f"{session_id}.ndjson"
        try:
            fd = os.open(
                self._file_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            self._handle = os.fdopen(fd, "ab", buffering=0)
        except OSError as exc:
            logger.warning(
                "event_log_open_failed session_id=%s path=%s error=%s",
                session_id,
                self._file_path,
                exc,
            )
            self._handle = None
            self._truncated = True
        # Flush pre-session-start events (e.g. EVENT_USER_REQUEST) that
        # arrived before we knew the session id; back-fill session_id /
        # request_id so consumers can index the file consistently.
        pending = self._pending_envelopes
        self._pending_envelopes = []
        for envelope in pending:
            patched = dict(envelope)
            if patched.get("session_id") is None:
                patched["session_id"] = session_id
            if patched.get("request_id") is None and request_id:
                patched["request_id"] = request_id
            self._write_envelope(patched)

    def _write_envelope(self, envelope: Mapping[str, Any]) -> None:
        if self._session_id is None:
            # No session is open yet; buffer the envelope so we can flush it
            # once the upcoming session_start tells us where to write.
            self._pending_envelopes.append(envelope)
            return
        if self._handle is None or self._truncated:
            return
        if self._started_at is None:
            self._started_at = str(envelope.get("ts") or "")
        try:
            self._handle.write(encode_envelope(envelope))
        except OSError as exc:
            logger.warning(
                "event_log_write_failed session_id=%s error=%s",
                self._session_id,
                exc,
            )
            self._truncated = True
            with suppress(OSError):
                self._handle.close()
            self._handle = None

    def _close_session(self, *, ended_at: str | None, status: str) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle is not None:
            try:
                self._handle.flush()
            except OSError:
                pass
            with suppress(OSError):
                os.fsync(self._handle.fileno())
            with suppress(OSError):
                self._handle.close()
            self._handle = None
        if self._session_id is None:
            return
        index_status = "write_truncated" if self._truncated else status
        self._append_index_row(
            ended_at=ended_at,
            status=index_status,
        )
        self._run_retention()
        self._reset_session_state()

    def _reset_session_state(self) -> None:
        self._session_id = None
        self._request_id = None
        self._request_summary = ""
        self._started_at = None
        self._file_path = None
        self._truncated = False

    def _append_index_row(self, *, ended_at: str | None, status: str) -> None:
        if self._session_id is None or self._file_path is None:
            return
        row = {
            "session_id": self._session_id,
            "request_id": self._request_id,
            "started_at": self._started_at,
            "ended_at": ended_at,
            "request_summary": self._request_summary,
            "status": status,
            "file_path": str(self._file_path),
            "schema_version": "1",
        }
        try:
            with open(self._index_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")
            with suppress(OSError):
                os.chmod(self._index_path, 0o600)
        except OSError as exc:
            logger.warning(
                "event_log_index_append_failed path=%s error=%s",
                self._index_path,
                exc,
            )

    def _run_retention(self) -> None:
        try:
            rows = _read_index_rows(self._index_path)
        except OSError as exc:
            logger.warning(
                "event_log_index_read_failed path=%s error=%s",
                self._index_path,
                exc,
            )
            return
        keep, drop = _select_for_retention(
            rows,
            max_sessions=self._max_sessions,
            max_bytes=self._max_bytes,
        )
        if not drop:
            return
        for row in drop:
            file_path = row.get("file_path")
            if isinstance(file_path, str):
                with suppress(OSError):
                    Path(file_path).unlink()
        try:
            tmp_path = self._index_path.with_suffix(self._index_path.suffix + _INDEX_TMP_SUFFIX)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                for row in keep:
                    handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")
            os.replace(tmp_path, self._index_path)
            with suppress(OSError):
                os.chmod(self._index_path, 0o600)
        except OSError as exc:
            logger.warning(
                "event_log_index_rewrite_failed path=%s error=%s",
                self._index_path,
                exc,
            )

    def _atexit_close(self) -> None:
        try:
            self.close(status="interrupted")
        except Exception:  # pragma: no cover - exit must not raise
            pass

    def _install_handlers(self) -> None:
        # signal.signal must be called from the main thread; silently skip
        # otherwise. atexit still covers the common shutdown paths.
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in _INTERRUPT_SIGNALS:
            try:
                previous = signal.signal(sig, self._handle_signal)
            except (OSError, ValueError):
                continue
            self._previous_signal_handlers[sig] = previous

    def _restore_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self._previous_signal_handlers.clear()
            return
        previous = self._previous_signal_handlers
        self._previous_signal_handlers = {}
        for sig, handler in previous.items():
            with suppress(OSError, ValueError):
                signal.signal(sig, handler)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        try:
            self.close(status="interrupted")
        except Exception:  # pragma: no cover - signal path must not raise
            logger.exception("event_log_signal_close_failed signal=%s", signum)
        previous = self._previous_signal_handlers.get(signum)
        # Re-raise the signal through the previously-installed handler so
        # the user's expected behavior (e.g., default termination) takes over.
        if previous in (None, signal.SIG_DFL):
            with suppress(OSError, ValueError):
                signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        elif previous is signal.SIG_IGN:
            return
        elif callable(previous):
            previous(signum, frame)

    @staticmethod
    def _coerce_session_id(payload: Mapping[str, Any]) -> str | None:
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
        return None


def _read_index_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _select_for_retention(
    rows: list[dict[str, Any]],
    *,
    max_sessions: int,
    max_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (keep_rows_oldest_first, drop_rows). Newest rows are preserved."""
    annotated: list[tuple[dict[str, Any], int]] = []
    for row in rows:
        size = 0
        file_path = row.get("file_path")
        if isinstance(file_path, str):
            try:
                size = os.path.getsize(file_path)
            except OSError:
                size = 0
        annotated.append((row, size))

    keep_reverse: list[tuple[dict[str, Any], int]] = []
    running_bytes = 0
    for row, size in reversed(annotated):
        if len(keep_reverse) >= max_sessions:
            break
        if keep_reverse and running_bytes + size > max_bytes:
            # Always keep the newest session even if it exceeds max_bytes;
            # only later sessions are subject to the byte cap.
            break
        keep_reverse.append((row, size))
        running_bytes += size
    keep_set = {id(row) for row, _ in keep_reverse}
    keep_rows = [row for row, _ in annotated if id(row) in keep_set]
    drop_rows = [row for row, _ in annotated if id(row) not in keep_set]
    return keep_rows, drop_rows
