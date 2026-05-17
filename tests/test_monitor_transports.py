"""Tests for v4 Stage 02 Pass B live transports (Unix socket + HTTP/SSE)."""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from foundation.monitor import (
    LocalHttpSseTransport,
    MonitorServer,
    TransportStartError,
    UnixSocketTransport,
)


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """Yield a short-path tmpdir suitable for AF_UNIX sockets on macOS (<=104)."""
    with tempfile.TemporaryDirectory(prefix="fclis", dir="/tmp") as tmp:
        yield Path(tmp)


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _read_lines(sock: socket.socket, *, count: int, timeout: float = 2.0) -> list[bytes]:
    sock.settimeout(timeout)
    buf = b""
    lines: list[bytes] = []
    deadline = time.monotonic() + timeout
    while len(lines) < count and time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf and len(lines) < count:
            line, _, buf = buf.partition(b"\n")
            lines.append(line)
    return lines


# --------------------------------------------------------------------------- #
# Unix socket
# --------------------------------------------------------------------------- #


def test_unix_socket_transport_delivers_to_connected_subscriber(short_socket_dir: Path) -> None:
    socket_path = short_socket_dir / "m.sock"
    server = MonitorServer()
    transport = UnixSocketTransport(path=socket_path, server=server)
    transport.start()
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        # Wait for the subscriber to be registered before publishing.
        assert _wait_until(lambda: server.subscriber_count == 1)
        server.publish("session_start", {"request_id": "r", "session_id": "s"})
        server.publish("session_end", {"request_id": "r", "session_id": "s", "status": "ok"})
        lines = _read_lines(client, count=2)
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["event"] == "session_start"
        assert first["session_id"] == "s"
    finally:
        client.close()
        transport.close()
        server.close()


def test_unix_socket_path_owner_only_perms_and_cleanup(short_socket_dir: Path) -> None:
    socket_path = short_socket_dir / "m.sock"
    server = MonitorServer()
    transport = UnixSocketTransport(path=socket_path, server=server)
    transport.start()
    try:
        assert socket_path.exists()
        mode = socket_path.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        transport.close()
        server.close()
    assert not socket_path.exists()


def test_unix_socket_transport_unlinks_stale_path(short_socket_dir: Path) -> None:
    socket_path = short_socket_dir / "m.sock"
    socket_path.write_text("not actually a socket")
    server = MonitorServer()
    transport = UnixSocketTransport(path=socket_path, server=server)
    try:
        transport.start()
        assert socket_path.exists()
        # The file is now a real socket replacing the stale plain file.
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.close()
    finally:
        transport.close()
        server.close()


def test_unix_socket_transport_refuses_when_peer_already_listening(
    short_socket_dir: Path,
) -> None:
    socket_path = short_socket_dir / "m.sock"
    server_a = MonitorServer()
    transport_a = UnixSocketTransport(path=socket_path, server=server_a)
    transport_a.start()
    try:
        server_b = MonitorServer()
        transport_b = UnixSocketTransport(path=socket_path, server=server_b)
        with pytest.raises(TransportStartError):
            transport_b.start()
        server_b.close()
    finally:
        transport_a.close()
        server_a.close()


# --------------------------------------------------------------------------- #
# HTTP / SSE
# --------------------------------------------------------------------------- #


def _http_request(
    *, port: int, path: str, headers: dict[str, str] | None = None, method: str = "GET"
) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    conn.request(method, path, headers=headers or {})
    return conn.getresponse()


def test_http_transport_rejects_missing_token() -> None:
    server = MonitorServer()
    transport = LocalHttpSseTransport(port=0, token="hunter2", server=server)
    transport.start()
    try:
        resp = _http_request(port=transport.port, path="/events")
        assert resp.status == 401
    finally:
        transport.close()
        server.close()


def test_http_transport_rejects_wrong_token() -> None:
    server = MonitorServer()
    transport = LocalHttpSseTransport(port=0, token="hunter2", server=server)
    transport.start()
    try:
        resp = _http_request(
            port=transport.port,
            path="/events",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status == 401
    finally:
        transport.close()
        server.close()


def test_http_transport_returns_404_on_unknown_path() -> None:
    server = MonitorServer()
    transport = LocalHttpSseTransport(port=0, token="t", server=server)
    transport.start()
    try:
        resp = _http_request(
            port=transport.port,
            path="/nope",
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status == 404
    finally:
        transport.close()
        server.close()


def test_http_transport_options_returns_schema() -> None:
    server = MonitorServer()
    transport = LocalHttpSseTransport(port=0, token="t", server=server)
    transport.start()
    try:
        resp = _http_request(port=transport.port, path="/events", method="OPTIONS")
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
        assert body["event_schema_version"] == "1"
        assert body["endpoint"] == "/events"
    finally:
        transport.close()
        server.close()


def test_http_transport_streams_sse_with_valid_token() -> None:
    server = MonitorServer()
    transport = LocalHttpSseTransport(port=0, token="hunter2", server=server)
    transport.start()
    received: list[bytes] = []
    stop = threading.Event()

    def reader() -> None:
        sock = socket.create_connection(("127.0.0.1", transport.port), timeout=2.0)
        try:
            sock.sendall(
                b"GET /events HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer hunter2\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            sock.settimeout(2.0)
            buf = b""
            deadline = time.monotonic() + 2.5
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    break
                if not chunk:
                    break
                buf += chunk
                received.append(chunk)
        finally:
            sock.close()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        # Wait for the subscriber to register before publishing.
        assert _wait_until(lambda: server.subscriber_count == 1, timeout=2.0)
        server.publish("session_start", {"request_id": "r", "session_id": "s"})
        # Wait briefly for the SSE frame to arrive.
        assert _wait_until(lambda: any(b"data:" in c for c in received), timeout=2.0)
    finally:
        stop.set()
        transport.close()
        server.close()
        t.join(timeout=1.0)

    flat = b"".join(received)
    assert b"data: " in flat
    assert b"session_start" in flat


def test_http_transport_refuses_remote_bind() -> None:
    with pytest.raises(ValueError):
        LocalHttpSseTransport(port=0, token="t", server=MonitorServer(), host="0.0.0.0")
