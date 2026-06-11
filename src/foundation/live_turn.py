"""v4 Stage 01 — live in-terminal status line for one orchestrated turn.

The renderer subscribes to redacted events emitted by ``ObserverService`` via
its ``event_sink`` hook, folds them into a small ``TurnLiveState``, and
projects that state into a Rich ``Live`` widget. Pressing ``?`` toggles a
detail panel. The widget tears down at turn end; the existing concise/verbose
result panels render as before.
"""

from __future__ import annotations

import enum
import os
import queue
import select
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from foundation.observability import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_RESOLVED,
    EVENT_ITERATION_COMPLETED,
    EVENT_ITERATION_STARTED,
    EVENT_PLAN_FINISHED,
    EVENT_PLAN_STARTED,
    EVENT_QUESTION_ANSWERED,
    EVENT_QUESTION_ASKED,
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_SHELL_EXECUTION_FAILED,
    EVENT_SHELL_EXECUTION_FINISHED,
    EVENT_SHELL_EXECUTION_STARTED,
    EVENT_TOOL_CALL_FAILED,
    EVENT_TOOL_CALL_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    EVENT_USER_REQUEST,
)

_DISABLE_ENV = "FOUNDATION_DISABLE_LIVE_UX"
_REFRESH_PER_SECOND = 8
_MAX_COMPLETED_DETAIL = 12
_MAX_ACTIVITY_LINES = 5
_TOGGLE_KEY = "?"


class LivePhase(enum.Enum):
    STARTING = "starting"
    THINKING = "thinking"
    PLANNING = "planning"
    RUNNING_TOOL = "running_tool"
    OBSERVING = "observing"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class CompletedAction:
    action_id: str
    tool: str
    duration_seconds: float
    outcome: str  # "ok" | "failed"
    error: str | None = None


@dataclass
class TurnLiveState:
    """Pure state folded from the redacted observer event stream."""

    request_text: str = ""
    request_id: str | None = None
    phase: LivePhase = LivePhase.STARTING
    phase_started_at: float = 0.0
    last_event_at: float = 0.0
    last_event_name: str | None = None
    iteration: int = 0
    iteration_started_at: float = 0.0
    planning_started_at: float | None = None
    planning_action_count: int | None = None
    current_action_id: str | None = None
    current_action_tool: str | None = None
    current_action_started_at: float | None = None
    completed: list[CompletedAction] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    awaiting_approval: bool = False
    approval_summary: str | None = None
    awaiting_input: bool = False
    question_summary: str | None = None
    activity_lines: list[str] = field(default_factory=list)
    finished: bool = False
    final_status: str | None = None

    def fold(self, event_name: str, payload: Mapping[str, Any]) -> None:
        """Apply one redacted event to the state."""
        now = time.monotonic()
        self.last_event_at = now
        self.last_event_name = event_name
        if event_name == EVENT_USER_REQUEST:
            self.request_text = str(payload.get("request_text") or "")
            self.request_id = payload.get("request_id")
            return
        if event_name == EVENT_SESSION_START:
            self._set_phase(LivePhase.THINKING, now)
            return
        if event_name == EVENT_ITERATION_STARTED:
            iteration = payload.get("iteration")
            if isinstance(iteration, int):
                self.iteration = iteration
                self.iteration_started_at = now
            self.planning_action_count = None
            self._set_phase(LivePhase.THINKING, now)
            return
        if event_name == EVENT_PLAN_STARTED:
            self.planning_started_at = now
            self._push_activity("next: planning actions")
            self._set_phase(LivePhase.PLANNING, now)
            return
        if event_name == EVENT_PLAN_FINISHED:
            self.planning_started_at = None
            count = payload.get("action_count")
            if isinstance(count, int):
                self.planning_action_count = count
                noun = "action" if count == 1 else "actions"
                self._push_activity(f"done: planned {count} {noun}")
            else:
                self._push_activity("done: planned actions")
            self._set_phase(LivePhase.THINKING, now)
            return
        if event_name == EVENT_TOOL_CALL_STARTED:
            self.current_action_id = str(payload.get("action_id") or "")
            self.current_action_tool = str(payload.get("tool") or "")
            self.current_action_started_at = now
            if self.current_action_tool:
                self._push_activity(f"next: {self.current_action_tool}")
            self._set_phase(LivePhase.RUNNING_TOOL, now)
            return
        if event_name == EVENT_TOOL_CALL_FINISHED:
            tool = str(payload.get("tool") or self.current_action_tool or "")
            self._close_current(now, outcome="ok")
            self.success_count += 1
            if tool:
                self._push_activity(f"done: {tool}")
            self._set_phase(LivePhase.OBSERVING, now)
            return
        if event_name == EVENT_TOOL_CALL_FAILED:
            error = payload.get("error")
            tool = str(payload.get("tool") or self.current_action_tool or "")
            self._close_current(now, outcome="failed", error=str(error) if error else None)
            self.failure_count += 1
            if tool:
                self._push_activity(f"failed: {tool}")
            self._set_phase(LivePhase.OBSERVING, now)
            return
        if event_name == EVENT_SHELL_EXECUTION_STARTED:
            command = _payload_text(payload, "command_preview")
            self.current_action_id = str(payload.get("action_id") or "")
            self.current_action_tool = "shell"
            self.current_action_started_at = now
            if command:
                self._push_activity(f"run: {command}")
            self._set_phase(LivePhase.RUNNING_TOOL, now)
            return
        if event_name == EVENT_SHELL_EXECUTION_FINISHED:
            stdout = _preview_line(_payload_text(payload, "stdout_preview"))
            stderr = _preview_line(_payload_text(payload, "stderr_preview"))
            self._close_current(now, outcome="ok")
            self.success_count += 1
            if stdout:
                self._push_activity(f"output: {stdout}")
            elif stderr:
                self._push_activity(f"stderr: {stderr}")
            self._set_phase(LivePhase.OBSERVING, now)
            return
        if event_name == EVENT_SHELL_EXECUTION_FAILED:
            error = _payload_text(payload, "error")
            stderr = _preview_line(_payload_text(payload, "stderr_preview"))
            self._close_current(now, outcome="failed", error=error or stderr or None)
            self.failure_count += 1
            if stderr:
                self._push_activity(f"stderr: {stderr}")
            elif error:
                self._push_activity(f"failed: {error}")
            self._set_phase(LivePhase.OBSERVING, now)
            return
        if event_name == EVENT_APPROVAL_REQUESTED:
            self.awaiting_approval = True
            self.approval_summary = (
                f"approval required for {payload.get('action_id') or '<action>'}"
            )
            self._set_phase(LivePhase.WAITING_APPROVAL, now)
            return
        if event_name == EVENT_APPROVAL_RESOLVED:
            self.awaiting_approval = False
            self.approval_summary = None
            self._set_phase(LivePhase.THINKING, now)
            return
        if event_name == EVENT_QUESTION_ASKED:
            self.awaiting_input = True
            self.question_summary = str(payload.get("prompt") or "a question")
            self._set_phase(LivePhase.WAITING_USER, now)
            return
        if event_name == EVENT_QUESTION_ANSWERED:
            self.awaiting_input = False
            self.question_summary = None
            self._set_phase(LivePhase.THINKING, now)
            return
        if event_name == EVENT_ITERATION_COMPLETED:
            self._set_phase(LivePhase.THINKING, now)
            return
        if event_name == EVENT_SESSION_END:
            self.finished = True
            status = payload.get("status")
            if status:
                self.final_status = str(status)
            self._set_phase(_terminal_phase(self.final_status), now)
            return

    def _set_phase(self, phase: LivePhase, now: float) -> None:
        if self.phase is phase:
            return
        self.phase = phase
        self.phase_started_at = now

    def _push_activity(self, line: str) -> None:
        line = _truncate(" ".join(line.split()), limit=96)
        if not line:
            return
        if self.activity_lines and self.activity_lines[-1] == line:
            return
        if (
            self.activity_lines
            and line.startswith(("output: ", "stderr: "))
            and self.activity_lines[-1].startswith(("output: ", "stderr: "))
        ):
            self.activity_lines[-1] = line
            return
        self.activity_lines.append(line)
        if len(self.activity_lines) > _MAX_ACTIVITY_LINES:
            del self.activity_lines[: len(self.activity_lines) - _MAX_ACTIVITY_LINES]

    def _close_current(self, now: float, *, outcome: str, error: str | None = None) -> None:
        if self.current_action_id is None:
            return
        started = self.current_action_started_at or now
        self.completed.append(
            CompletedAction(
                action_id=self.current_action_id,
                tool=self.current_action_tool or "",
                duration_seconds=max(now - started, 0.0),
                outcome=outcome,
                error=error,
            )
        )
        self.current_action_id = None
        self.current_action_tool = None
        self.current_action_started_at = None


def _terminal_phase(status: str | None) -> LivePhase:
    normalized = (status or "").lower()
    if "cancel" in normalized:
        return LivePhase.CANCELLED
    if "fail" in normalized or "error" in normalized:
        return LivePhase.FAILED
    return LivePhase.COMPLETED


def _format_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def _truncate(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 1)] + "…"


def _payload_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return str(value or "")


def _preview_line(value: str) -> str:
    for line in value.splitlines():
        line = line.strip()
        if line:
            return _truncate(line, limit=96)
    return _truncate(value.strip(), limit=96)


def render_status_line(
    state: TurnLiveState,
    *,
    elapsed_seconds: float,
    now: float | None = None,
) -> RenderableType:
    """One-line status (collapsed mode)."""
    if state.finished or state.phase in {
        LivePhase.COMPLETED,
        LivePhase.FAILED,
        LivePhase.CANCELLED,
    }:
        verb = state.final_status or "done"
        style = "red" if state.phase is LivePhase.FAILED else "green"
        return Text(f"✓ {verb} · {_format_duration(elapsed_seconds)}", style=style)
    if state.awaiting_approval or state.phase is LivePhase.WAITING_APPROVAL:
        return Text("Approval needed", style="yellow")
    if state.awaiting_input or state.phase is LivePhase.WAITING_USER:
        return Text("Answer needed", style="yellow")
    if state.current_action_id is not None or state.phase is LivePhase.RUNNING_TOOL:
        return Text("Working", style="cyan")
    if state.planning_started_at is not None or state.phase is LivePhase.PLANNING:
        return Text("Planning", style="cyan")
    if state.phase is LivePhase.OBSERVING:
        return Text("Checking", style="cyan")
    if state.phase is LivePhase.THINKING or state.iteration > 0:
        return Text("Thinking", style="cyan")
    return Text("Starting", style="cyan")


def render_detail_panel(state: TurnLiveState, *, elapsed_seconds: float) -> RenderableType:
    """Expanded panel: completed steps + in-flight step."""
    now = time.monotonic()
    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(no_wrap=False)

    request = _truncate(state.request_text or "(no request)", limit=80)
    # Text() keeps user-controlled request text literal — raw strings would be
    # parsed as Rich markup (styling injection, MarkupError on stray tags).
    table.add_row("request", Text(request))
    phase_elapsed = max(now - state.phase_started_at, 0.0) if state.phase_started_at else 0.0
    table.add_row("phase", f"{state.phase.value} · {_format_duration(phase_elapsed)}")
    table.add_row("last event", state.last_event_name or "(none)")
    table.add_row(
        "iteration",
        f"{state.iteration} · ok={state.success_count} fail={state.failure_count}",
    )

    tail = state.completed[-_MAX_COMPLETED_DETAIL:]
    if tail:
        body = Table.grid(padding=(0, 1))
        body.add_column(style="dim", no_wrap=True, width=4)
        body.add_column(no_wrap=True)
        body.add_column(no_wrap=True, style="dim")
        body.add_column(no_wrap=False)
        for action in tail:
            mark = "✓" if action.outcome == "ok" else "✗"
            style = "green" if action.outcome == "ok" else "red"
            body.add_row(
                Text(mark, style=style),
                Text(_truncate(action.tool, limit=32), style="cyan"),
                _format_duration(action.duration_seconds),
                Text(_truncate(action.error or "", limit=60), style="red")
                if action.error
                else Text(""),
            )
        table.add_row("steps", body)

    status = render_status_line(state, elapsed_seconds=elapsed_seconds, now=now)
    table.add_row("now", status)
    table.add_row("press", Text("? to collapse · Ctrl-C to cancel", style="dim"))

    return Panel(table, border_style="cyan", title="foundation · live turn", title_align="left")


def render_collapsed(
    state: TurnLiveState,
    *,
    elapsed_seconds: float,
    spinner: Spinner | None = None,
) -> RenderableType:
    status = render_status_line(state, elapsed_seconds=elapsed_seconds)
    if state.finished:
        return status
    if spinner is None:
        spinner = Spinner("dots", text=status, style="cyan")
    else:
        spinner.update(text=status, style="cyan")
    lines: list[RenderableType] = [spinner]
    lines.extend(Text(line, style="dim") for line in state.activity_lines)
    lines.append(Text("press ? for detail · Ctrl-C to cancel", style="dim"))
    return Group(*lines)


_active_renderer_lock = threading.Lock()
_active_renderer: LiveTurnRenderer | None = None


def get_active_renderer() -> LiveTurnRenderer | None:
    """Return the currently-mounted renderer (used to pause around prompts)."""
    with _active_renderer_lock:
        return _active_renderer


def live_ux_disabled() -> bool:
    """Return True when the live widget should not mount in this environment."""
    if os.environ.get(_DISABLE_ENV, "").strip() in {"1", "true", "yes"}:
        return True
    try:
        return not sys.stdout.isatty()
    except Exception:
        return True


class LiveTurnRenderer:
    """Owns a Rich ``Live`` widget driven by the observer event sink."""

    def __init__(
        self,
        *,
        console: Console,
        enable_keypress: bool | None = None,
        refresh_per_second: int = _REFRESH_PER_SECOND,
    ) -> None:
        self._console = console
        self._refresh = refresh_per_second
        self._state = TurnLiveState()
        self._state_lock = threading.Lock()
        self._events: queue.Queue[tuple[str, Mapping[str, Any]]] = queue.Queue()
        self._expanded = False
        self._started_at = 0.0
        self._live: Live | None = None
        self._spinner = Spinner("dots", style="cyan")
        self._keypress_thread: threading.Thread | None = None
        self._keypress_stop = threading.Event()
        self._old_termios: Any = None
        self._stdin_fd: int | None = None
        if enable_keypress is None:
            try:
                enable_keypress = sys.stdin.isatty()
            except Exception:
                enable_keypress = False
        self._enable_keypress = bool(enable_keypress)
        self._paused = False

    @property
    def state(self) -> TurnLiveState:
        with self._state_lock:
            return self._state

    @property
    def expanded(self) -> bool:
        return self._expanded

    def on_event(self, event_name: str, payload: Mapping[str, Any]) -> None:
        """Sink callback — safe to call from any thread."""
        with self._state_lock:
            self._state.fold(event_name, payload)
        try:
            self._events.put_nowait((event_name, dict(payload)))
        except queue.Full:  # pragma: no cover - unbounded queue
            pass

    def __enter__(self) -> LiveTurnRenderer:
        global _active_renderer
        self._started_at = time.monotonic()
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=self._refresh,
            transient=True,
            auto_refresh=True,
        )
        self._live.__enter__()
        self._install_keypress_reader()
        with _active_renderer_lock:
            _active_renderer = self
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        global _active_renderer
        try:
            self._teardown_keypress_reader()
        finally:
            try:
                if self._live is not None:
                    self._live.update(self._render())
                    self._live.__exit__(exc_type, exc, tb)
            finally:
                self._live = None
                with _active_renderer_lock:
                    if _active_renderer is self:
                        _active_renderer = None

    def tick(self) -> None:
        """Refresh the widget; call from the main thread between event drains."""
        if self._live is None or self._paused:
            return
        self._live.update(self._render())

    def drain_until_finished(
        self,
        *,
        worker: threading.Thread,
        cancel_event: threading.Event | None = None,
        poll_interval: float = 0.1,
    ) -> None:
        """Block until the worker thread completes, refreshing the widget."""
        while worker.is_alive():
            try:
                # Pull anything pending so fold() runs on the main thread too.
                while True:
                    self._events.get_nowait()
            except queue.Empty:
                pass
            self.tick()
            if cancel_event is not None and cancel_event.is_set():
                return
            worker.join(timeout=poll_interval)
        self.tick()

    def pause(self) -> None:
        """Stop the Live widget and fully release stdin for other prompts.

        The keypress reader competes with ``input()`` for stdin bytes — left
        running, it eats the user's answer at approval/question prompts and
        the prompt resolves to its default. Pausing must stop the reader
        thread, restore canonical terminal mode, and flush any type-ahead
        that the raw-mode reader may have left behind.
        """
        if self._live is None or self._paused:
            return
        self._paused = True
        try:
            self._live.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        stdin_fd = self._stdin_fd
        self._teardown_keypress_reader()
        if stdin_fd is not None:
            try:
                import termios

                termios.tcflush(stdin_fd, termios.TCIFLUSH)
            except Exception:  # pragma: no cover - defensive
                pass

    def resume(self) -> None:
        """Re-enter the Live widget (and the keypress reader) after a prompt."""
        if self._live is None or not self._paused:
            return
        self._paused = False
        self._install_keypress_reader()
        try:
            self._live.start(refresh=True)
        except Exception:  # pragma: no cover - defensive
            pass

    def _render(self) -> RenderableType:
        elapsed = max(time.monotonic() - self._started_at, 0.0)
        state_copy = self.state
        if self._expanded:
            return render_detail_panel(state_copy, elapsed_seconds=elapsed)
        return render_collapsed(state_copy, elapsed_seconds=elapsed, spinner=self._spinner)

    # --- keypress handling ------------------------------------------------

    def _install_keypress_reader(self) -> None:
        if not self._enable_keypress:
            return
        try:
            import termios

            self._stdin_fd = sys.stdin.fileno()
            self._old_termios = termios.tcgetattr(self._stdin_fd)
        except Exception:
            self._stdin_fd = None
            self._old_termios = None
            return
        try:
            import tty

            tty.setcbreak(self._stdin_fd)
        except Exception:
            self._restore_termios()
            return
        self._keypress_stop.clear()
        thread = threading.Thread(target=self._keypress_loop, name="fcli-live-keys", daemon=True)
        thread.start()
        self._keypress_thread = thread

    def _teardown_keypress_reader(self) -> None:
        self._keypress_stop.set()
        thread = self._keypress_thread
        if thread is not None:
            thread.join(timeout=0.5)
        self._keypress_thread = None
        self._restore_termios()

    def _restore_termios(self) -> None:
        if self._stdin_fd is None or self._old_termios is None:
            return
        try:
            import termios

            termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._old_termios)
        except Exception:
            pass
        self._stdin_fd = None
        self._old_termios = None

    def _keypress_loop(self) -> None:
        fd = self._stdin_fd
        if fd is None:
            return
        while not self._keypress_stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                ch = os.read(fd, 1).decode("utf-8", errors="ignore")
            except OSError:
                return
            if not ch:
                continue
            if ch == _TOGGLE_KEY:
                self._expanded = not self._expanded
                self.tick()
