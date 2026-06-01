"""In-process pub/sub fan-out for live monitor transports.

Each subscriber owns a bounded ``queue.Queue`` of pre-encoded NDJSON lines.
A dedicated daemon thread per subscriber drains its queue and writes to
the subscriber's transport. On overflow, the slow subscriber is evicted
(final ``subscriber_overflow`` line + close); other subscribers and the
file writer are unaffected.

This module deliberately stays transport-agnostic. ``MonitorServer.publish``
is the sink callable wired into ``ObserverService.event_sink`` (via
``compose_event_sink``); transports register themselves with
``MonitorServer.register`` and are fed pre-encoded NDJSON bytes.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any

from foundation.monitor.protocol import build_envelope, encode_envelope

logger = logging.getLogger("foundation.monitor.server")


# A SubscriberWriter takes one fully-encoded NDJSON line (bytes) and
# returns True on success, False if the connection is gone.
SubscriberWriter = Callable[[bytes], bool]


_SUBSCRIBER_OVERFLOW_LINE = encode_envelope(
    {
        "event_schema_version": "1",
        "event": "subscriber_overflow",
        "ts": "",
        "request_id": None,
        "session_id": None,
        "payload": {"reason": "queue_full"},
    }
)


class _Subscriber:
    """One live subscriber. Owns a bounded queue and a draining thread."""

    _id_counter = itertools.count(1)

    def __init__(
        self,
        *,
        write: SubscriberWriter,
        queue_size: int,
        on_disconnect: Callable[[_Subscriber], None],
        label: str,
    ) -> None:
        self.id = next(_Subscriber._id_counter)
        self.label = label
        self._write = write
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_size)
        self._on_disconnect = on_disconnect
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._drain,
            name=f"fcli-monitor-sub-{self.id}",
            daemon=True,
        )
        self.overflowed = False

    def start(self) -> None:
        self._thread.start()

    def enqueue(self, line: bytes) -> None:
        if self._stopped.is_set():
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            self.overflowed = True
            try:
                self._queue.put(_SUBSCRIBER_OVERFLOW_LINE, timeout=0.1)
            except queue.Full:
                pass
            self.stop()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def join(self, *, timeout: float = 1.0) -> None:
        self._thread.join(timeout=timeout)

    def _drain(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                try:
                    ok = self._write(item)
                except Exception:  # noqa: BLE001 - transports may raise broad
                    logger.warning(
                        "monitor_subscriber_write_failed id=%s label=%s",
                        self.id,
                        self.label,
                        exc_info=True,
                    )
                    return
                if not ok:
                    return
        finally:
            self._stopped.set()
            try:
                self._on_disconnect(self)
            except Exception:  # pragma: no cover - defensive
                logger.exception("monitor_on_disconnect_failed")


class MonitorServer:
    """Holds the set of live subscribers and fans every event out to them."""

    def __init__(self, *, queue_size: int = 1024) -> None:
        self._queue_size = max(16, int(queue_size))
        self._lock = threading.Lock()
        self._subscribers: dict[int, _Subscriber] = {}
        self._closed = False

    def __enter__(self) -> MonitorServer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def register(
        self,
        write: SubscriberWriter,
        *,
        label: str = "anonymous",
    ) -> int:
        """Add a subscriber. Returns its id; the caller may pass it to
        :meth:`unregister` when the underlying transport disconnects."""
        sub = _Subscriber(
            write=write,
            queue_size=self._queue_size,
            on_disconnect=self._handle_disconnect,
            label=label,
        )
        with self._lock:
            if self._closed:
                # Refuse new subscribers after shutdown begins.
                return -1
            self._subscribers[sub.id] = sub
        sub.start()
        return sub.id

    def unregister(self, subscriber_id: int) -> None:
        with self._lock:
            sub = self._subscribers.pop(subscriber_id, None)
        if sub is not None:
            sub.stop()

    def publish(self, event_name: str, payload: Mapping[str, Any]) -> None:
        """Sink callable wired into ``ObserverService.event_sink``.

        Encodes each event once; each subscriber receives the same bytes
        through its bounded queue.
        """
        envelope = build_envelope(event_name, payload)
        line = encode_envelope(envelope)
        with self._lock:
            subs = list(self._subscribers.values())
        for sub in subs:
            sub.enqueue(line)

    def close(self, *, timeout: float = 1.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subs = list(self._subscribers.values())
            self._subscribers.clear()
        for sub in subs:
            sub.stop()
        for sub in subs:
            sub.join(timeout=timeout)

    def _handle_disconnect(self, sub: _Subscriber) -> None:
        with self._lock:
            self._subscribers.pop(sub.id, None)
