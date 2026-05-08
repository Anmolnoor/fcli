"""Wire format for the v4 Stage 02 redacted event stream.

Each line in a session's NDJSON file is one envelope. The same envelope is
broadcast to live transports (stage 02 pass B). Bumping
``EVENT_SCHEMA_VERSION`` is reserved for breaking changes; additive fields
keep the version unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

EVENT_SCHEMA_VERSION = "1"

# Internal payload keys that should not be projected to external consumers —
# either they're noise (already implied by the event name) or they could leak
# context that the redaction pipeline isn't responsible for stripping.
_INTERNAL_KEYS = frozenset({"event_schema_version", "event_time", "level"})


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_envelope(
    event_name: str, payload: Mapping[str, Any], *, timestamp: str | None = None
) -> dict[str, Any]:
    """Build one outgoing envelope for the given (already-redacted) payload.

    The caller is responsible for ensuring ``payload`` has been through
    ``ObserverService``'s redaction pipeline. ``request_id`` and
    ``session_id`` are promoted to top-level fields when present so that
    consumers can index by them without parsing the inner payload.
    """
    inner: dict[str, Any] = {
        key: value
        for key, value in payload.items()
        if key not in _INTERNAL_KEYS and key not in {"request_id", "session_id"}
    }
    return {
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "event": event_name,
        "ts": timestamp or _utcnow_iso(),
        "request_id": payload.get("request_id"),
        "session_id": payload.get("session_id"),
        "payload": inner,
    }


def encode_envelope(envelope: Mapping[str, Any]) -> bytes:
    """Encode one envelope as a single NDJSON line (UTF-8, ``\\n`` terminated)."""
    return (
        json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
