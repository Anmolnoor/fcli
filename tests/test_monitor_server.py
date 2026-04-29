"""Tests for the in-process MonitorServer fan-out."""

from __future__ import annotations

import threading
import time

from foundation.monitor import MonitorServer


def _wait_until(predicate, *, timeout: float = 1.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_publish_delivers_to_all_subscribers() -> None:
    received_a: list[bytes] = []
    received_b: list[bytes] = []
    with MonitorServer() as server:
        server.register(lambda line: bool(received_a.append(line)) or True, label="a")
        server.register(lambda line: bool(received_b.append(line)) or True, label="b")
        server.publish("hello", {"x": 1})
        server.publish("world", {"y": 2})
        assert _wait_until(lambda: len(received_a) == 2 and len(received_b) == 2)

    for line in received_a:
        assert line.endswith(b"\n")
    assert received_a == received_b


def test_unregister_removes_subscriber() -> None:
    received: list[bytes] = []
    with MonitorServer() as server:
        sub_id = server.register(
            lambda line: bool(received.append(line)) or True, label="x"
        )
        server.publish("a", {})
        assert _wait_until(lambda: len(received) == 1)
        server.unregister(sub_id)
        # Give the subscriber thread time to wind down.
        assert _wait_until(lambda: server.subscriber_count == 0)
        server.publish("b", {})
        time.sleep(0.05)
        assert len(received) == 1


def test_overflow_drops_slow_subscriber_only() -> None:
    fast: list[bytes] = []
    block_release = threading.Event()
    slow_calls = {"count": 0}

    def fast_writer(line: bytes) -> bool:
        fast.append(line)
        return True

    def slow_writer(_line: bytes) -> bool:
        slow_calls["count"] += 1
        # Block forever until released so the queue fills up.
        block_release.wait(timeout=2.0)
        return True

    with MonitorServer(queue_size=4) as server:
        server.register(fast_writer, label="fast")
        slow_id = server.register(slow_writer, label="slow")

        # Publish more than queue_size to force overflow on the slow one.
        for i in range(20):
            server.publish("evt", {"i": i})

        # Wait for the slow subscriber to be evicted.
        assert _wait_until(
            lambda: server.subscriber_count == 1, timeout=2.0
        )
        # Suppress unused warning while still asserting we kept the slow id.
        assert slow_id > 0
        block_release.set()

    # Fast subscriber kept up.
    assert len(fast) >= 5


def test_register_after_close_returns_negative() -> None:
    server = MonitorServer()
    server.close()
    sub_id = server.register(lambda _line: True, label="post-close")
    assert sub_id < 0


def test_publish_uses_redacted_payload_only() -> None:
    """The server is transport-agnostic — it just encodes whatever it gets.

    This regression-tests that ``publish`` correctly serialises the inner
    payload and surfaces ``request_id`` / ``session_id`` at the top level.
    """
    received: list[bytes] = []
    with MonitorServer() as server:
        server.register(lambda line: bool(received.append(line)) or True, label="x")
        server.publish(
            "iteration_started",
            {"request_id": "r-1", "session_id": "s-1", "iteration": 2},
        )
        assert _wait_until(lambda: len(received) == 1)

    line = received[0].decode("utf-8")
    assert '"request_id":"r-1"' in line
    assert '"session_id":"s-1"' in line
    assert '"event":"iteration_started"' in line
    assert '"iteration":2' in line
