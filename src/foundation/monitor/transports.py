"""Live transports for the v4 Stage 02 monitor surface.

Two implementations:

- :class:`UnixSocketTransport`: ``AF_UNIX`` listener with owner-only
  permissions. Each accepted connection is registered as a subscriber and
  receives raw NDJSON lines (one per line, ``\\n`` terminated).

- :class:`LocalHttpSseTransport`: stdlib ``ThreadingHTTPServer`` bound to
  ``127.0.0.1`` only. The single endpoint ``GET /events`` validates a Bearer
  token and emits Server-Sent Events. ``OPTIONS /events`` returns the schema
  metadata. Anything else returns 404 / 405 / 401.

Both transports treat the connection as read-only: subscribers receive
events; nothing they say steers the agent.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from foundation.monitor.protocol import EVENT_SCHEMA_VERSION
from foundation.monitor.server import MonitorServer

logger = logging.getLogger("foundation.monitor.transports")


# --------------------------------------------------------------------------- #
# Unix socket
# --------------------------------------------------------------------------- #


class TransportStartError(RuntimeError):
    """Raised when a live transport cannot bind / start."""


class UnixSocketTransport:
    """``AF_UNIX`` SOCK_STREAM listener that registers each connection."""

    def __init__(
        self,
        *,
        path: Path,
        server: MonitorServer,
        backlog: int = 8,
    ) -> None:
        self._path = Path(path).expanduser()
        self._server = server
        self._backlog = backlog
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._connections: list[socket.socket] = []
        self._connections_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def __enter__(self) -> UnixSocketTransport:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(self._path.parent, 0o700)
        self._handle_stale_path()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self._path))
        except OSError as exc:
            sock.close()
            raise TransportStartError(
                f"Could not bind monitor unix socket at {self._path}: {exc}"
            ) from exc
        with suppress(OSError):
            os.chmod(self._path, 0o600)
        sock.listen(self._backlog)
        sock.settimeout(0.5)
        self._socket = sock
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="fcli-monitor-unix-accept",
            daemon=True,
        )
        self._accept_thread.start()
        logger.info("monitor_unix_listening path=%s", self._path)

    def close(self) -> None:
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            with suppress(OSError):
                sock.close()
        thread = self._accept_thread
        self._accept_thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            with suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                conn.close()
        with suppress(OSError):
            self._path.unlink()

    # --- internals ----------------------------------------------------------

    def _handle_stale_path(self) -> None:
        if not self._path.exists():
            return
        # Try to connect; if a peer is alive, fail loudly. Otherwise unlink
        # the stale path and continue.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self._path))
        except OSError:
            probe.close()
            with suppress(OSError):
                self._path.unlink()
            return
        probe.close()
        raise TransportStartError(
            f"Another fcli appears to be serving the monitor socket at "
            f"{self._path}. Refusing to overwrite."
        )

    def _accept_loop(self) -> None:
        sock = self._socket
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    return
                if exc.errno in {errno.EBADF, errno.EINVAL}:
                    return
                logger.warning("monitor_unix_accept_failed error=%s", exc)
                continue
            self._register_connection(conn)

    def _register_connection(self, conn: socket.socket) -> None:
        with self._connections_lock:
            self._connections.append(conn)

        def _write(line: bytes) -> bool:
            try:
                conn.sendall(line)
                return True
            except OSError:
                return False

        sub_id = self._server.register(_write, label=f"unix:{conn.fileno()}")
        if sub_id < 0:  # server already closed
            with suppress(OSError):
                conn.close()
            return

        def _watch_close() -> None:
            try:
                # Block on a tiny read so we notice peer-close promptly.
                while not self._stop.is_set():
                    try:
                        conn.settimeout(0.5)
                        data = conn.recv(64)
                    except TimeoutError:
                        continue
                    except OSError:
                        return
                    if not data:
                        return
            finally:
                self._server.unregister(sub_id)
                with suppress(OSError):
                    conn.close()
                with self._connections_lock:
                    if conn in self._connections:
                        self._connections.remove(conn)

        threading.Thread(
            target=_watch_close,
            name=f"fcli-monitor-unix-{sub_id}",
            daemon=True,
        ).start()


# --------------------------------------------------------------------------- #
# Local HTTP / SSE
# --------------------------------------------------------------------------- #


class LocalHttpSseTransport:
    """Localhost-only HTTP/SSE transport with bearer-token auth."""

    def __init__(
        self,
        *,
        port: int,
        token: str,
        server: MonitorServer,
        host: str = "127.0.0.1",
    ) -> None:
        self._port = int(port)
        self._token = str(token)
        self._monitor = server
        self._host = host
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError(
                "LocalHttpSseTransport refuses to bind outside localhost"
            )
        self._http: ThreadingHTTPServer | None = None
        self._serve_thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._port

    def __enter__(self) -> LocalHttpSseTransport:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        handler_factory = _make_sse_handler(self._monitor, self._token)
        try:
            self._http = ThreadingHTTPServer(
                (self._host, self._port), handler_factory
            )
        except OSError as exc:
            raise TransportStartError(
                f"Could not bind monitor HTTP transport on {self._host}:"
                f"{self._port}: {exc}"
            ) from exc
        # If port=0 the OS picks one; surface the bound port.
        self._port = self._http.server_address[1]
        self._serve_thread = threading.Thread(
            target=self._http.serve_forever,
            name="fcli-monitor-http",
            daemon=True,
            kwargs={"poll_interval": 0.2},
        )
        self._serve_thread.start()
        logger.info(
            "monitor_http_listening host=%s port=%s",
            self._host,
            self._port,
        )

    def close(self) -> None:
        http = self._http
        self._http = None
        if http is not None:
            with suppress(Exception):
                http.shutdown()
            with suppress(Exception):
                http.server_close()
        thread = self._serve_thread
        self._serve_thread = None
        if thread is not None:
            thread.join(timeout=1.0)


def _make_sse_handler(server: MonitorServer, token: str):
    class _Handler(BaseHTTPRequestHandler):
        # Suppress the noisy default logging.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002
            return

        def do_OPTIONS(self) -> None:  # noqa: N802 - http verb naming
            if self.path != "/events":
                self.send_error(404)
                return
            body = json.dumps(
                {
                    "event_schema_version": EVENT_SCHEMA_VERSION,
                    "endpoint": "/events",
                    "method": "GET",
                    "auth": "Bearer",
                    "content_type": "text/event-stream",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http verb naming
            if self.path != "/events":
                self.send_error(404)
                return
            auth = self.headers.get("Authorization", "")
            if not _check_bearer(auth, token):
                self.send_error(401, "Unauthorized")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            stop = threading.Event()

            def _write(line: bytes) -> bool:
                try:
                    self.wfile.write(b"data: ")
                    self.wfile.write(line.rstrip(b"\n"))
                    self.wfile.write(b"\n\n")
                    self.wfile.flush()
                    return True
                except (OSError, ConnectionError):
                    stop.set()
                    return False

            sub_id = server.register(_write, label=f"http:{self.client_address[1]}")
            if sub_id < 0:
                return
            try:
                stop.wait()
            finally:
                server.unregister(sub_id)

        def do_POST(self) -> None:  # noqa: N802
            self.send_error(405)

    return _Handler


def _check_bearer(header_value: str, expected: str) -> bool:
    parts = header_value.split(None, 1)
    if len(parts) != 2:
        return False
    scheme, value = parts
    if scheme.lower() != "bearer":
        return False
    return _constant_time_eq(value.strip(), expected)


def _constant_time_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b, strict=True):
        result |= ord(x) ^ ord(y)
    return result == 0
