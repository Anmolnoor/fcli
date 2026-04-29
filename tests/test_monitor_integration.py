"""Integration: file writer + Unix-socket subscriber receive the same turn."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from foundation.monitor import (
    EventLogWriter,
    MonitorServer,
    UnixSocketTransport,
    compose_event_sink,
)


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="fclis", dir="/tmp") as tmp:
        yield Path(tmp)


def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_file_writer_and_unix_subscriber_receive_same_turn(
    short_socket_dir: Path, tmp_path: Path
) -> None:
    socket_path = short_socket_dir / "m.sock"
    events_dir = tmp_path / "events"

    server = MonitorServer()
    transport = UnixSocketTransport(path=socket_path, server=server)
    transport.start()
    writer = EventLogWriter(events_dir=events_dir)
    writer.__enter__()

    sink = compose_event_sink(writer.write_event, server.publish)

    received_lines: list[bytes] = []
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)

    def _drain_socket() -> None:
        buf = b""
        while True:
            try:
                chunk = client.recv(4096)
            except TimeoutError:
                return
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                received_lines.append(line)

    drain_thread = threading.Thread(target=_drain_socket, daemon=True)
    drain_thread.start()

    try:
        assert _wait_until(lambda: server.subscriber_count == 1)
        sink("user_request", {"request_id": "r-1", "request_text": "hello"})
        sink(
            "session_start",
            {"request_id": "r-1", "session_id": "sess-int"},
        )
        sink(
            "iteration_started",
            {"request_id": "r-1", "session_id": "sess-int", "iteration": 1},
        )
        sink(
            "session_end",
            {
                "request_id": "r-1",
                "session_id": "sess-int",
                "status": "completed",
            },
        )
        assert _wait_until(lambda: len(received_lines) >= 4)
    finally:
        writer.__exit__(None, None, None)
        client.close()
        transport.close()
        server.close()
        drain_thread.join(timeout=1.0)

    file_lines = [
        line
        for line in (events_dir / "sess-int.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    socket_lines = [line.decode("utf-8") for line in received_lines if line]

    assert len(file_lines) == 4
    assert len(socket_lines) >= 4

    file_events = [json.loads(line)["event"] for line in file_lines]
    socket_events = [json.loads(line)["event"] for line in socket_lines[:4]]
    assert file_events == [
        "user_request",
        "session_start",
        "iteration_started",
        "session_end",
    ]
    assert socket_events == file_events


def test_file_and_unix_subscriber_byte_parity(
    short_socket_dir: Path, tmp_path: Path
) -> None:
    """The on-disk NDJSON bytes match what the live socket subscriber sees.

    The plan calls for byte-level identity between the persistent log and
    the live transport for every event in a session. This test attaches
    both surfaces, plays a deterministic event sequence through the same
    composed sink, and asserts the captured bytes match line-for-line.
    """
    import re

    socket_path = short_socket_dir / "p.sock"
    events_dir = tmp_path / "events"

    server = MonitorServer()
    transport = UnixSocketTransport(path=socket_path, server=server)
    transport.start()
    writer = EventLogWriter(events_dir=events_dir, install_signal_handlers=False)
    writer.__enter__()
    sink = compose_event_sink(writer.write_event, server.publish)

    received: bytearray = bytearray()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.settimeout(2.0)
    stop_drain = threading.Event()

    def drain() -> None:
        while not stop_drain.is_set():
            try:
                chunk = client.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not chunk:
                return
            received.extend(chunk)

    drain_thread = threading.Thread(target=drain, daemon=True)
    drain_thread.start()

    try:
        assert _wait_until(lambda: server.subscriber_count == 1)
        events = [
            ("user_request", {"request_id": "r-2", "request_text": "fix bug"}),
            ("session_start", {"request_id": "r-2", "session_id": "sess-byte"}),
            ("iteration_started",
             {"request_id": "r-2", "session_id": "sess-byte", "iteration": 1}),
            ("tool_call_started",
             {"request_id": "r-2", "session_id": "sess-byte",
              "action_id": "a1", "tool": "foundation.file.read"}),
            ("tool_call_finished",
             {"request_id": "r-2", "session_id": "sess-byte",
              "action_id": "a1", "tool": "foundation.file.read"}),
            ("session_end",
             {"request_id": "r-2", "session_id": "sess-byte", "status": "completed"}),
        ]
        for name, payload in events:
            sink(name, payload)
        # Wait until the socket has at least as many lines as events.
        assert _wait_until(lambda: received.count(b"\n") >= len(events))
    finally:
        stop_drain.set()
        writer.__exit__(None, None, None)
        client.close()
        transport.close()
        server.close()
        drain_thread.join(timeout=1.0)

    file_lines = (events_dir / "sess-byte.ndjson").read_bytes().splitlines()
    socket_lines = bytes(received).splitlines()

    ts_re = re.compile(rb'"ts":"[^"]*"')
    norm_file = [ts_re.sub(b'"ts":""', line) for line in file_lines]
    norm_socket = [ts_re.sub(b'"ts":""', line) for line in socket_lines]

    # The first event (user_request) precedes session_start; the writer
    # back-fills session_id on flush so its bytes legitimately differ from
    # what the live publish saw. Compare in-session events only.
    file_in_session = [
        line for line in norm_file if b'"event":"user_request"' not in line
    ]
    socket_in_session = [
        line for line in norm_socket if b'"event":"user_request"' not in line
    ]
    assert file_in_session == socket_in_session
    # Sanity: the in-session bytes are non-trivial.
    assert len(file_in_session) >= 5


def test_compose_event_sink_isolates_failing_sink(
    short_socket_dir: Path, tmp_path: Path
) -> None:
    """A throwing sink must not break the file writer or the live subscriber."""
    socket_path = short_socket_dir / "m.sock"
    events_dir = tmp_path / "events"

    def boom(_name, _payload):
        raise RuntimeError("nope")

    server = MonitorServer()
    transport = UnixSocketTransport(path=socket_path, server=server)
    transport.start()
    writer = EventLogWriter(events_dir=events_dir)
    writer.__enter__()
    try:
        sink = compose_event_sink(boom, writer.write_event, server.publish)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.settimeout(2.0)
        try:
            assert _wait_until(lambda: server.subscriber_count == 1)
            sink("session_start", {"request_id": "r", "session_id": "sess-iso"})
            sink(
                "session_end",
                {"request_id": "r", "session_id": "sess-iso", "status": "completed"},
            )
            buf = b""
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and buf.count(b"\n") < 2:
                try:
                    chunk = client.recv(4096)
                except TimeoutError:
                    break
                if not chunk:
                    break
                buf += chunk
            assert buf.count(b"\n") >= 2
        finally:
            client.close()
    finally:
        writer.__exit__(None, None, None)
        transport.close()
        server.close()

    file_path = events_dir / "sess-iso.ndjson"
    assert file_path.exists()
    rows = [
        json.loads(line)
        for line in file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["event"] for r in rows] == ["session_start", "session_end"]
