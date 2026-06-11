"""Event-sink failure visibility and circuit-breaking (hardening stage 4).

A failing sink must never break the turn, but it must not fail silently
either: failures are counted and warned about, a flapping sink is disabled
after three consecutive failures, and a crash inside the event-log writer
marks the session's index row as truncated instead of claiming a complete
log.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from foundation.monitor import compose_event_sink
from foundation.monitor.event_log import EventLogWriter
from foundation.observability import EVENT_SESSION_END, EVENT_SESSION_START
from foundation.services.observer import ObserverService


def _observer(event_sink: Any) -> ObserverService:
    return ObserverService(
        history_store=None,
        capability_registry=None,  # type: ignore[arg-type]
        event_sink=event_sink,
    )


class TestObserverSinkBreaker:
    def test_single_failure_warns_and_keeps_sink_enabled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []

        def flaky(event_name: str, payload: Any) -> None:
            calls.append(event_name)
            if len(calls) == 1:
                raise RuntimeError("boom")

        observer = _observer(flaky)
        with caplog.at_level(logging.WARNING):
            observer.emit("event_one", payload={})
            observer.emit("event_two", payload={})

        assert len(calls) == 2
        assert observer.sink_failure_count == 1
        assert not observer.sink_disabled
        assert any("sink" in record.message for record in caplog.records)

    def test_disabled_after_three_consecutive_failures(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []

        def broken(event_name: str, payload: Any) -> None:
            calls.append(event_name)
            raise RuntimeError("boom")

        observer = _observer(broken)
        with caplog.at_level(logging.WARNING):
            for index in range(5):
                observer.emit(f"event_{index}", payload={})

        assert observer.sink_disabled
        assert len(calls) == 3
        assert observer.sink_failure_count == 3
        disabled_warnings = [r for r in caplog.records if "disabled" in r.message]
        assert len(disabled_warnings) == 1

    def test_success_resets_consecutive_count(self) -> None:
        outcomes = iter([True, True, False, True, True, False])

        def sometimes(event_name: str, payload: Any) -> None:
            if next(outcomes):
                raise RuntimeError("boom")

        observer = _observer(sometimes)
        for index in range(6):
            observer.emit(f"event_{index}", payload={})

        assert not observer.sink_disabled
        assert observer.sink_failure_count == 4

    def test_replacing_sink_resets_breaker(self) -> None:
        def broken(event_name: str, payload: Any) -> None:
            raise RuntimeError("boom")

        observer = _observer(broken)
        for index in range(3):
            observer.emit(f"event_{index}", payload={})
        assert observer.sink_disabled

        replacement_calls: list[str] = []
        observer.set_event_sink(lambda event_name, payload: replacement_calls.append(event_name))
        observer.emit("after_replacement", payload={})
        assert not observer.sink_disabled
        assert replacement_calls == ["after_replacement"]


class TestComposedSinkBreaker:
    def test_flapping_sink_disabled_but_others_keep_receiving(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad_calls: list[str] = []
        good_calls: list[str] = []

        def bad(event_name: str, payload: Any) -> None:
            bad_calls.append(event_name)
            raise RuntimeError("boom")

        def good(event_name: str, payload: Any) -> None:
            good_calls.append(event_name)

        fanout = compose_event_sink(bad, good)
        with caplog.at_level(logging.WARNING):
            for index in range(5):
                fanout(f"event_{index}", {})

        assert len(bad_calls) == 3
        assert len(good_calls) == 5
        disabled_warnings = [
            r for r in caplog.records if r.message.startswith("event_sink_disabled")
        ]
        assert len(disabled_warnings) == 1


class TestEventLogWriterDegradation:
    def test_sink_crash_marks_index_row_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writer = EventLogWriter(events_dir=tmp_path, install_signal_handlers=False)
        writer.write_event(
            EVENT_SESSION_START,
            {"session_id": "s1", "request_id": "r1"},
        )

        def boom(event_name: str, payload: Any) -> Any:
            raise RuntimeError("envelope bug")

        monkeypatch.setattr("foundation.monitor.event_log.build_envelope", boom)
        # Must not raise out of the sink, and must poison the session status.
        writer.write_event("some_event", {"session_id": "s1"})
        monkeypatch.undo()

        writer.write_event(
            EVENT_SESSION_END,
            {"session_id": "s1", "status": "completed"},
        )
        index_lines = (tmp_path / "sessions.jsonl").read_text().splitlines()
        rows = [json.loads(line) for line in index_lines if line.strip()]
        assert rows[-1]["session_id"] == "s1"
        assert rows[-1]["status"] == "write_truncated"
