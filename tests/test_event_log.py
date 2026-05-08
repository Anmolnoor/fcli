"""Tests for v4 Stage 02 Pass A — persistent NDJSON event log."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from foundation.monitor import (
    EVENT_SCHEMA_VERSION,
    EventLogWriter,
    compose_event_sink,
)
from foundation.observability import (
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_CALL_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    EVENT_USER_REQUEST,
)


def _emit_session(
    writer: EventLogWriter,
    *,
    session_id: str,
    request_id: str = "req-1",
    request_text: str = "do the thing",
    extra_events: list[tuple[str, dict]] | None = None,
    end_status: str = "completed",
) -> None:
    writer.write_event(
        EVENT_USER_REQUEST,
        {"request_id": request_id, "request_text": request_text},
    )
    writer.write_event(
        EVENT_SESSION_START,
        {"request_id": request_id, "session_id": session_id},
    )
    for name, payload in extra_events or []:
        body = {"request_id": request_id, "session_id": session_id, **payload}
        writer.write_event(name, body)
    writer.write_event(
        EVENT_SESSION_END,
        {
            "request_id": request_id,
            "session_id": session_id,
            "status": end_status,
        },
    )


def _read_lines(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_writer_round_trip_writes_one_envelope_per_event(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    with EventLogWriter(events_dir=events_dir) as writer:
        _emit_session(
            writer,
            session_id="sess-1",
            extra_events=[
                (EVENT_TOOL_CALL_STARTED, {"action_id": "a1", "tool": "git"}),
                (EVENT_TOOL_CALL_FINISHED, {"action_id": "a1", "tool": "git"}),
            ],
        )

    log_path = events_dir / "sess-1.ndjson"
    assert log_path.exists()
    rows = _read_lines(log_path)
    names = [row["event"] for row in rows]
    assert names == [
        "user_request",
        "session_start",
        "tool_call_started",
        "tool_call_finished",
        "session_end",
    ]
    for row in rows:
        assert row["event_schema_version"] == EVENT_SCHEMA_VERSION
        assert row["session_id"] == "sess-1"
        assert "ts" in row


def test_writer_appends_index_row_with_summary_and_status(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    with EventLogWriter(events_dir=events_dir) as writer:
        _emit_session(
            writer,
            session_id="sess-1",
            request_text="hello world",
            end_status="completed",
        )

    index_rows = _read_lines(events_dir / "sessions.jsonl")
    assert len(index_rows) == 1
    row = index_rows[0]
    assert row["session_id"] == "sess-1"
    assert row["request_summary"] == "hello world"
    assert row["status"] == "completed"
    assert row["file_path"].endswith("sess-1.ndjson")
    assert row["started_at"] is not None
    assert row["ended_at"] is not None


def test_writer_redacts_secrets_before_disk(tmp_path: Path) -> None:
    """The writer never sees raw secrets — its sole input is the redacted payload."""
    events_dir = tmp_path / "events"
    canary = "CANARY-TOKEN-xyz789"
    from foundation.services.capabilities import CapabilityRegistry, CapabilityStore
    from foundation.services.observer import ObserverService
    from foundation.services.tools import LocalToolService

    tool_service = LocalToolService(workspace_root=tmp_path)
    registry = CapabilityRegistry(
        store=CapabilityStore(tmp_path / "capabilities"),
        tool_service=tool_service,
    )
    with EventLogWriter(events_dir=events_dir) as writer:
        observer = ObserverService(
            history_store=None,
            capability_registry=registry,
            event_sink=writer.write_event,
        )
        observer.emit(
            EVENT_USER_REQUEST,
            payload={"request_id": "r", "request_text": "go"},
            session_id=None,
        )
        observer.emit(
            EVENT_SESSION_START,
            payload={"request_id": "r", "session_id": "sess-1"},
            session_id="sess-1",
        )
        observer.emit(
            EVENT_TOOL_CALL_STARTED,
            payload={
                "request_id": "r",
                "session_id": "sess-1",
                "tool": "git",
                "api_token": canary,
            },
            session_id="sess-1",
        )
        observer.emit(
            EVENT_SESSION_END,
            payload={"request_id": "r", "session_id": "sess-1", "status": "completed"},
            session_id="sess-1",
        )

    log_path = events_dir / "sess-1.ndjson"
    on_disk = log_path.read_text(encoding="utf-8")
    assert canary not in on_disk
    assert "[redacted]" in on_disk


def test_writer_retention_prunes_oldest_by_count(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    with EventLogWriter(events_dir=events_dir, max_sessions=2) as writer:
        for idx in range(3):
            _emit_session(writer, session_id=f"sess-{idx}")

    surviving_files = sorted(p.name for p in events_dir.glob("*.ndjson"))
    assert surviving_files == ["sess-1.ndjson", "sess-2.ndjson"]
    index_rows = _read_lines(events_dir / "sessions.jsonl")
    surviving_ids = [row["session_id"] for row in index_rows]
    assert surviving_ids == ["sess-1", "sess-2"]


def test_writer_retention_prunes_by_bytes(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    # Each session produces a few events; cap bytes such that only the
    # newest fits.
    with EventLogWriter(events_dir=events_dir, max_bytes=200) as writer:
        for idx in range(4):
            _emit_session(writer, session_id=f"sess-{idx}")

    surviving = sorted(p.name for p in events_dir.glob("*.ndjson"))
    assert "sess-3.ndjson" in surviving
    # The oldest sessions should have been pruned.
    assert "sess-0.ndjson" not in surviving
    rows = _read_lines(events_dir / "sessions.jsonl")
    assert all(row["session_id"] != "sess-0" for row in rows)


def test_writer_creates_directory_with_owner_only_perms(tmp_path: Path) -> None:
    events_dir = tmp_path / "nested" / "events"
    EventLogWriter(events_dir=events_dir)
    assert events_dir.exists()
    mode = events_dir.stat().st_mode & 0o777
    assert mode == 0o700


def test_writer_session_files_have_owner_only_perms(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    with EventLogWriter(events_dir=events_dir) as writer:
        _emit_session(writer, session_id="sess-1")
    log_path = events_dir / "sess-1.ndjson"
    mode = log_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_writer_handles_session_without_session_end_via_close(tmp_path: Path) -> None:
    """Force-close marks an interrupted session in the index."""
    events_dir = tmp_path / "events"
    writer = EventLogWriter(events_dir=events_dir)
    writer.__enter__()
    try:
        writer.write_event(
            EVENT_USER_REQUEST,
            {"request_id": "r", "request_text": "go"},
        )
        writer.write_event(
            EVENT_SESSION_START,
            {"request_id": "r", "session_id": "sess-x"},
        )
    finally:
        writer.close(status="interrupted")

    rows = _read_lines(events_dir / "sessions.jsonl")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-x"
    assert rows[0]["status"] == "interrupted"
    assert rows[0]["ended_at"] is None


def test_compose_event_sink_continues_when_one_sink_throws() -> None:
    received_a: list[str] = []
    received_b: list[str] = []

    def sink_a(name: str, _payload):
        received_a.append(name)

    def boom(_name, _payload):
        raise ValueError("oops")

    def sink_b(name: str, _payload):
        received_b.append(name)

    fanout = compose_event_sink(sink_a, boom, sink_b)
    fanout("e1", {"x": 1})
    fanout("e2", {"x": 2})
    assert received_a == ["e1", "e2"]
    assert received_b == ["e1", "e2"]


def test_compose_event_sink_skips_none_sinks() -> None:
    received: list[str] = []

    fanout = compose_event_sink(
        None,
        lambda name, _p: received.append(name),
        None,
    )
    fanout("hello", {})
    assert received == ["hello"]


def test_writer_no_op_when_session_start_missing_session_id(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    with EventLogWriter(events_dir=events_dir) as writer:
        writer.write_event(EVENT_USER_REQUEST, {"request_id": "r", "request_text": "x"})
        # No session_id → no file is opened, no index row appended on end.
        writer.write_event(EVENT_SESSION_END, {"request_id": "r", "status": "completed"})
    assert not (events_dir / "sessions.jsonl").exists()
    assert list(events_dir.glob("*.ndjson")) == []


def test_writer_keeps_writing_when_stdout_is_not_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistence is on regardless of TTY; only the live UX gates on it."""
    import sys

    class _FakeNonTty:
        def isatty(self) -> bool:
            return False

        def __getattr__(self, name: str):
            return getattr(sys.__stdout__, name)

    monkeypatch.setattr(sys, "stdout", _FakeNonTty())

    events_dir = tmp_path / "events"
    with EventLogWriter(events_dir=events_dir, install_signal_handlers=False) as writer:
        _emit_session(writer, session_id="sess-pipe")

    log_path = events_dir / "sess-pipe.ndjson"
    assert log_path.exists()
    rows = _read_lines(log_path)
    names = [row["event"] for row in rows]
    assert "session_start" in names
    assert "session_end" in names


def test_writer_signal_handler_closes_session_with_interrupted_status(
    tmp_path: Path,
) -> None:
    """SIGTERM/SIGINT handler must flush the in-progress session."""
    import signal

    events_dir = tmp_path / "events"

    # Replace the previous SIGTERM handler with a sentinel we can detect
    # being invoked after our handler chains through.
    previous_called = {"count": 0}

    def previous_handler(_sig, _frame):
        previous_called["count"] += 1

    original = signal.signal(signal.SIGTERM, previous_handler)
    try:
        writer = EventLogWriter(events_dir=events_dir, install_signal_handlers=True)
        writer.__enter__()
        try:
            writer.write_event(
                EVENT_USER_REQUEST,
                {"request_id": "r", "request_text": "hi"},
            )
            writer.write_event(
                EVENT_SESSION_START,
                {"request_id": "r", "session_id": "sess-sig"},
            )
            writer._handle_signal(signal.SIGTERM, None)  # type: ignore[arg-type]
        finally:
            writer._restore_signal_handlers()
    finally:
        signal.signal(signal.SIGTERM, original)

    rows = _read_lines(events_dir / "sessions.jsonl")
    assert len(rows) == 1
    assert rows[0]["status"] == "interrupted"
    assert previous_called["count"] == 1


def test_writer_disk_full_marks_session_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_dir = tmp_path / "events"
    writer = EventLogWriter(events_dir=events_dir)
    real_open = os.open

    def fake_open(path, flags, mode=0o600):
        # Allow the first open (the dir creation already happened) for the
        # initial session file, but raise on the *write* via patched handle.
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fake_open)

    with writer:
        writer.write_event(EVENT_USER_REQUEST, {"request_id": "r", "request_text": "x"})
        writer.write_event(EVENT_SESSION_START, {"request_id": "r", "session_id": "sess-1"})

        class _BoomHandle:
            def write(self, _data):  # noqa: ANN001
                raise OSError("disk full")

            def flush(self):
                pass

            def close(self):
                pass

            def fileno(self):
                return 0

        # Replace the live handle with one that raises on write.
        writer._handle = _BoomHandle()  # type: ignore[attr-defined]
        writer.write_event(
            EVENT_TOOL_CALL_STARTED,
            {"request_id": "r", "session_id": "sess-1", "tool": "git"},
        )
        writer.write_event(
            EVENT_SESSION_END,
            {"request_id": "r", "session_id": "sess-1", "status": "completed"},
        )

    rows = _read_lines(events_dir / "sessions.jsonl")
    assert len(rows) == 1
    assert rows[0]["status"] == "write_truncated"
