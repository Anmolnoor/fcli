"""v4 Stage 02 — external event stream surfaces.

Pass A (current): persistent NDJSON event log + sessions index + retention.
Pass B (next):    optional live transports (Unix socket, HTTP/SSE).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from foundation.monitor.event_log import EventLogWriter
from foundation.monitor.protocol import (
    EVENT_SCHEMA_VERSION,
    build_envelope,
    encode_envelope,
)
from foundation.monitor.server import MonitorServer
from foundation.monitor.transports import (
    LocalHttpSseTransport,
    TransportStartError,
    UnixSocketTransport,
)
from foundation.observability import SINK_DISABLE_AFTER_CONSECUTIVE_FAILURES

EventSink = Callable[[str, Mapping[str, Any]], None]

logger = logging.getLogger("foundation.monitor")


def compose_event_sink(*sinks: EventSink | None) -> EventSink:
    """Return one ``event_sink`` that fans out to each provided sink.

    ``None`` entries are skipped. A sink raising an exception is logged at
    WARNING and the remaining sinks still receive the event — orchestration
    must never break because of a misbehaving observer. A sink that fails on
    several consecutive events is disabled for the rest of the session (with
    one final warning) so a flapping sink cannot flood the log.
    """
    active: list[EventSink] = [sink for sink in sinks if sink is not None]
    consecutive_failures = [0] * len(active)
    disabled = [False] * len(active)

    def _fanout(event_name: str, payload: Mapping[str, Any]) -> None:
        for index, sink in enumerate(active):
            if disabled[index]:
                continue
            try:
                sink(event_name, payload)
            except Exception:  # noqa: BLE001 - sink errors must not propagate
                consecutive_failures[index] += 1
                if consecutive_failures[index] >= SINK_DISABLE_AFTER_CONSECUTIVE_FAILURES:
                    disabled[index] = True
                    logger.warning(
                        "event_sink_disabled sink=%r after %d consecutive failures",
                        sink,
                        consecutive_failures[index],
                        exc_info=True,
                    )
                else:
                    logger.warning(
                        "event_sink_failed event=%s sink=%r",
                        event_name,
                        sink,
                        exc_info=True,
                    )
            else:
                consecutive_failures[index] = 0

    return _fanout


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "EventLogWriter",
    "EventSink",
    "LocalHttpSseTransport",
    "MonitorServer",
    "TransportStartError",
    "UnixSocketTransport",
    "build_envelope",
    "compose_event_sink",
    "encode_envelope",
]
