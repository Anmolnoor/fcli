"""Tests for v4 Stage 01 — live turn UX (event sink + reducer + renderer)."""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console

from foundation.live_turn import (
    LivePhase,
    LiveTurnRenderer,
    TurnLiveState,
    render_collapsed,
    render_detail_panel,
    render_status_line,
)
from foundation.observability import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_RESOLVED,
    EVENT_ITERATION_COMPLETED,
    EVENT_ITERATION_STARTED,
    EVENT_PLAN_FINISHED,
    EVENT_PLAN_STARTED,
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_CALL_FAILED,
    EVENT_TOOL_CALL_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    EVENT_USER_REQUEST,
)
from foundation.services.capabilities import CapabilityRegistry, CapabilityStore
from foundation.services.observer import ObserverService
from foundation.services.tools import LocalToolService


def _build_observer(
    tmp_path,
    *,
    event_sink=None,
):
    tool_service = LocalToolService(workspace_root=tmp_path)
    registry = CapabilityRegistry(
        store=CapabilityStore(tmp_path / "capabilities"),
        tool_service=tool_service,
    )
    return ObserverService(
        history_store=None,
        capability_registry=registry,
        event_sink=event_sink,
    )


def test_observer_event_sink_receives_redacted_payload(tmp_path):
    received: list[tuple[str, dict[str, Any]]] = []
    observer = _build_observer(
        tmp_path,
        event_sink=lambda name, payload: received.append((name, dict(payload))),
    )

    observer.emit(
        "tool_call_started",
        payload={"action_id": "a1", "tool": "git", "api_token": "secret"},
    )

    assert received == [
        ("tool_call_started", {"action_id": "a1", "tool": "git", "api_token": "[redacted]"})
    ]


def test_observer_event_sink_receives_emit_exception(tmp_path):
    received: list[tuple[str, dict[str, Any]]] = []
    observer = _build_observer(
        tmp_path,
        event_sink=lambda name, payload: received.append((name, dict(payload))),
    )

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        observer.emit_exception(
            "exception",
            exc,
            payload={"request_id": "req-1"},
        )

    assert received[0][0] == "exception"
    body = received[0][1]
    assert body["error"] == "boom"
    assert body["error_type"] == "RuntimeError"
    assert body["request_id"] == "req-1"


def test_observer_event_sink_failure_does_not_break_emit(tmp_path):
    def boom(_name, _payload):
        raise ValueError("sink fail")

    observer = _build_observer(tmp_path, event_sink=boom)
    # Must not raise.
    observer.emit("session_start", payload={"request_id": "x"})


def test_observer_set_event_sink_updates_callback(tmp_path):
    received: list[str] = []
    observer = _build_observer(tmp_path)
    observer.set_event_sink(lambda name, _p: received.append(name))
    observer.emit("session_start", payload={"x": 1})
    observer.set_event_sink(None)
    observer.emit("session_end", payload={"x": 2})
    assert received == ["session_start"]


def _drive(state: TurnLiveState, events: list[tuple[str, dict[str, Any]]]) -> None:
    for name, payload in events:
        state.fold(name, payload)


def test_state_reducer_happy_path():
    state = TurnLiveState()
    _drive(
        state,
        [
            (EVENT_USER_REQUEST, {"request_text": "fix bug", "request_id": "r"}),
            (EVENT_ITERATION_STARTED, {"iteration": 1}),
            (EVENT_PLAN_STARTED, {}),
            (EVENT_PLAN_FINISHED, {"action_count": 2}),
            (EVENT_TOOL_CALL_STARTED, {"action_id": "a1", "tool": "foundation.file.read"}),
            (EVENT_TOOL_CALL_FINISHED, {"action_id": "a1", "tool": "foundation.file.read"}),
            (EVENT_TOOL_CALL_STARTED, {"action_id": "a2", "tool": "foundation.git.stage"}),
            (EVENT_TOOL_CALL_FINISHED, {"action_id": "a2", "tool": "foundation.git.stage"}),
            (EVENT_ITERATION_COMPLETED, {"iteration": 1}),
            (EVENT_SESSION_END, {"status": "completed"}),
        ],
    )
    assert state.iteration == 1
    assert state.success_count == 2
    assert state.failure_count == 0
    assert state.finished is True
    assert state.final_status == "completed"
    assert [a.tool for a in state.completed] == [
        "foundation.file.read",
        "foundation.git.stage",
    ]


def test_state_tracks_phase_and_last_event():
    state = TurnLiveState()

    state.fold(EVENT_SESSION_START, {"request_id": "r"})
    assert state.phase is LivePhase.THINKING
    assert state.last_event_name == EVENT_SESSION_START

    state.fold(EVENT_PLAN_STARTED, {})
    assert state.phase is LivePhase.PLANNING
    planning_started = state.phase_started_at

    state.fold(EVENT_TOOL_CALL_STARTED, {"action_id": "a1", "tool": "foundation.file.read"})
    assert state.phase is LivePhase.RUNNING_TOOL
    assert state.phase_started_at >= planning_started
    assert state.last_event_name == EVENT_TOOL_CALL_STARTED


def test_state_reducer_failure_path():
    state = TurnLiveState()
    _drive(
        state,
        [
            (EVENT_ITERATION_STARTED, {"iteration": 1}),
            (EVENT_TOOL_CALL_STARTED, {"action_id": "a1", "tool": "foundation.file.write"}),
            (EVENT_TOOL_CALL_FAILED, {"action_id": "a1", "error": "permission denied"}),
        ],
    )
    assert state.failure_count == 1
    assert state.success_count == 0
    assert state.completed[0].outcome == "failed"
    assert state.completed[0].error == "permission denied"
    assert state.current_action_id is None


def test_state_reducer_approval_pending_path():
    state = TurnLiveState()
    _drive(
        state,
        [
            (EVENT_ITERATION_STARTED, {"iteration": 1}),
            (EVENT_APPROVAL_REQUESTED, {"action_id": "commit_change"}),
        ],
    )
    assert state.awaiting_approval is True
    assert state.approval_summary is not None
    state.fold(EVENT_APPROVAL_RESOLVED, {"action_id": "commit_change", "status": "approved"})
    assert state.awaiting_approval is False
    assert state.approval_summary is None


def _render_to_text(renderable) -> str:
    console = Console(file=io.StringIO(), force_terminal=False, width=80, record=True)
    console.print(renderable)
    return console.export_text()


def _render_to_text_at(renderable, now: float) -> str:
    console = Console(
        file=io.StringIO(),
        force_terminal=False,
        width=80,
        record=True,
        get_time=lambda: now,
    )
    console.print(renderable)
    return console.export_text()


def test_render_status_line_shows_running_action():
    state = TurnLiveState(
        iteration=2,
        current_action_id="a1",
        current_action_tool="foundation.git.commit",
        current_action_started_at=0.0,
    )
    text = _render_to_text(render_status_line(state, elapsed_seconds=1.5))
    assert "iter 2" in text
    assert "foundation.git.commit" in text


def test_render_status_line_shows_planning_when_no_action():
    state = TurnLiveState(iteration=1, planning_started_at=0.0)
    text = _render_to_text(render_status_line(state, elapsed_seconds=0.4))
    assert "planning" in text
    assert "iteration 1" in text


def test_render_status_line_shows_stale_without_mutating_phase():
    state = TurnLiveState(
        phase=LivePhase.THINKING,
        phase_started_at=5.0,
        last_event_at=10.0,
        last_event_name=EVENT_PLAN_FINISHED,
    )

    text = _render_to_text(render_status_line(state, elapsed_seconds=18.0, now=28.0))

    assert "Still waiting on model" in text
    assert "no events for 18.0s" in text
    assert state.phase is LivePhase.THINKING


def test_render_status_line_shows_hard_stale():
    state = TurnLiveState(
        phase=LivePhase.THINKING,
        last_event_at=10.0,
        last_event_name=EVENT_PLAN_FINISHED,
    )

    text = _render_to_text(render_status_line(state, elapsed_seconds=67.0, now=77.0))

    assert "No live events for 1m07s" in text
    assert "Ctrl-C to cancel" in text


def test_render_status_line_shows_done_when_finished():
    state = TurnLiveState(finished=True, final_status="completed")
    text = _render_to_text(render_status_line(state, elapsed_seconds=2.1))
    assert "✓" in text or "done" in text.lower() or "completed" in text


def test_render_collapsed_includes_help_hint():
    state = TurnLiveState(iteration=1, planning_started_at=0.0)
    text = _render_to_text(render_collapsed(state, elapsed_seconds=0.2))
    assert "?" in text


def test_renderer_reuses_spinner_across_collapsed_refreshes():
    renderer = LiveTurnRenderer(
        console=Console(file=io.StringIO(), force_terminal=False, width=80),
        enable_keypress=False,
    )
    renderer.on_event(EVENT_SESSION_START, {"request_id": "r"})

    first = _render_to_text_at(renderer._render(), now=0.0)
    second = _render_to_text_at(renderer._render(), now=0.5)

    assert first[0] != second[0]


def test_render_detail_panel_lists_completed_steps():
    state = TurnLiveState(
        iteration=1,
        request_text="implement feature X",
        phase=LivePhase.OBSERVING,
        phase_started_at=1.0,
        last_event_at=2.0,
        last_event_name=EVENT_TOOL_CALL_FINISHED,
        success_count=1,
        failure_count=0,
    )
    state.fold(EVENT_TOOL_CALL_STARTED, {"action_id": "a1", "tool": "foundation.file.read"})
    state.fold(EVENT_TOOL_CALL_FINISHED, {"action_id": "a1", "tool": "foundation.file.read"})
    text = _render_to_text(render_detail_panel(state, elapsed_seconds=0.5))
    assert "implement feature X" in text
    assert "observing" in text
    assert EVENT_TOOL_CALL_FINISHED in text
    assert "foundation.file.read" in text


def test_renderer_on_event_folds_state(tmp_path):
    renderer = LiveTurnRenderer(
        console=Console(file=io.StringIO(), force_terminal=False, width=80),
        enable_keypress=False,
    )
    renderer.on_event(EVENT_USER_REQUEST, {"request_text": "hello", "request_id": "r"})
    renderer.on_event(EVENT_ITERATION_STARTED, {"iteration": 3})
    assert renderer.state.iteration == 3
    assert renderer.state.request_text == "hello"


def test_renderer_pause_resume_safe_when_unmounted(tmp_path):
    renderer = LiveTurnRenderer(
        console=Console(file=io.StringIO(), force_terminal=False, width=80),
        enable_keypress=False,
    )
    # Both should be no-ops when the Live widget hasn't been entered.
    renderer.pause()
    renderer.resume()
