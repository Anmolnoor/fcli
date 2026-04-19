# Stage 1: Live Turn UX (status line + expandable detail)

## Goal

Replace the silent "hit enter and wait" experience with a live status line during a turn, à la Claude Code / ChatGPT Codex. Users get a running verb for the current step; pressing `?` expands an inline detail panel with completed steps + durations + the in-flight step; pressing `?` again collapses. When the turn ends, the widget tears down and the existing concise/verbose result renders unchanged.

## Entry Criteria

- v3 runtime shipped (commit `34b2012`+).
- 22 `EVENT_*` constants are emitted across `observability.py`, `observer.py`, `orchestrator.py`, `executor.py`, `shell.py`, `provider.py`.
- Rich ≥ 13.7 is already a dependency.
- `ObserverService` is the single chokepoint for event emission.

## Locked Decisions

- Rendering uses Rich `Live` with a dynamic renderable.
- Default = collapsed one-line status; `?` toggles an expanded detail pane.
- Inline widget only — no full-screen TUI, no paneled layout.
- Auto-disable when `sys.stdout.isatty()` is false or when `FOUNDATION_DISABLE_LIVE_UX=1` or `--no-live` is passed.
- Approval prompts pause the Live widget; the existing approval prompt renders normally; Live resumes on `EVENT_APPROVAL_RESOLVED`.
- Terminal mode changes (cbreak for `?` key detection) are isolated inside a `try`/`finally` + `atexit` and never leak out of a turn.
- No mutation of the orchestrator hot path. The event sink is a drop-in callback on `ObserverService`.

## Public Interfaces Introduced

- `ObserverService.__init__(..., event_sink: Callable[[str, Mapping[str, Any]], None] | None = None)` — new optional callback invoked after every `emit()` / `emit_exception()`, with the redacted payload.
- `foundation.live_turn.TurnLiveState` — a small Pydantic model consumed by the renderer.
- `foundation.live_turn.LiveTurnRenderer` — context-manager class that owns a Rich `Live`, a `TurnLiveState`, and a cbreak keypress reader.
- CLI flag `--no-live` (plus env var `FOUNDATION_DISABLE_LIVE_UX=1`) to opt out.

## Step-by-Step Plan

1. **Add the event sink hook to `ObserverService`.** New optional `event_sink` param; `emit()` and `emit_exception()` call it after the existing redaction + history persistence.
2. **Build `TurnLiveState` + event-to-state reducer.** Folds an event sequence into: current iteration, current action, completed actions (id, summary, duration, outcome), totals, verification status, approval-waiting flag.
3. **Build `LiveTurnRenderer`.** Rich `Live` with a dynamic `render()` method. Two modes (collapsed / expanded) toggled by a boolean. Owns a thread-safe `queue.Queue` for inbound events and a background keypress reader thread for the `?` toggle.
4. **Wire the renderer into the CLI.**
   - `_run_interactive_chat` and the one-shot path: wrap `orchestrator.orchestrate()` in a background thread, pass `event_sink=renderer.on_event`, run the Live loop on the main thread.
   - After orchestrator returns (or raises), tear down Live; render the existing result panel as today.
   - Respect `--no-live`, env var, and non-TTY detection.
5. **Approval integration.** When the renderer receives `EVENT_APPROVAL_REQUESTED`, pause Live (stop the widget, clear the region). Approval prompts render normally via the existing approval service. When `EVENT_APPROVAL_RESOLVED` arrives, resume Live.
6. **Cancellation.** Ctrl-C mid-turn: the main thread catches, signals the worker to stop, the renderer's `__exit__` restores terminal attrs, and we re-raise.
7. **Tests.**
   - Unit: `ObserverService.event_sink` is called for every event with the redacted payload.
   - Unit: `TurnLiveState` reducer folds a canned event sequence correctly (happy path, failure path, approval-pending path).
   - Unit: renderer golden-string for collapsed and expanded modes given a fixed state.
   - CLI: `--no-live` skips the live path; CliRunner-based tests (non-TTY) auto-disable live UX so existing tests keep passing unchanged.

## Event-to-state mapping

| Event | State change |
| --- | --- |
| `session_start` | Initialize; capture request id + text. |
| `iteration_started` | Increment iteration; set step to "Planning iteration N". |
| `plan_started` / `plan_finished` | Update "Planning iteration N…" then stash plan duration + action count. |
| `tool_call_started` / `shell_execution_started` | Set current action (id, summary, capability); start timer. |
| `tool_call_finished` / `shell_execution_finished` | Move current action to completed list (id, duration, outcome). |
| `tool_call_failed` / `shell_execution_failed` | Same, marked failed; bump failure counter. |
| `approval_requested` | Set `awaiting_approval=True` with summary; renderer pauses Live. |
| `approval_resolved` | Clear `awaiting_approval`; renderer resumes Live. |
| `iteration_completed` | Fold iteration counters into totals. |
| `session_end` | Mark terminal; Live exits on return to caller. |

## Keypress detection (the `?` toggle)

A short-lived background thread puts `sys.stdin.fileno()` into cbreak mode via `termios.tcgetattr` / `tcsetattr`, reads one char at a time via `select.select` with a short timeout, and on `?` flips `renderer.expanded`. Terminal attrs are always restored in `try`/`finally` and also via an `atexit` handler installed by the renderer on entry. Falls back to no-key-reader when stdin isn't a TTY.

## Files to modify / add

- `src/foundation/services/observer.py` — add `event_sink` param; fire from `emit()` and `emit_exception()`.
- `src/foundation/live_turn.py` — **new.** `TurnLiveState`, `LiveTurnRenderer`. ~200–300 LOC total.
- `src/foundation/cli.py` — wrap orchestration in a worker thread; instantiate the renderer; add `--no-live` flag on the root Typer app.
- `tests/test_live_turn.py` — **new.** State reducer + renderer golden-string tests.
- `tests/test_cli.py` — add a smoke test for `--no-live`.
- `tests/test_orchestrator.py` — optional: one regression test that `event_sink` receives all 22 event types across a happy-path turn.

## Edge Cases and Failure Modes

- **Non-TTY stdout (piped / CI).** Detect via `sys.stdout.isatty()` and skip the renderer. Existing silent behavior is preserved.
- **CliRunner tests.** Typer's `CliRunner` provides a non-TTY stdout, so live UX auto-disables. The full v3 test suite stays green unchanged.
- **Ctrl-C mid-turn.** Main thread catches, sets a `threading.Event` the worker polls between actions (or accepts that the current action finishes and then stops). `LiveTurnRenderer.__exit__` restores TTY attrs before re-raise.
- **Exception on the worker thread.** Propagated to the main thread after `join`; Live is torn down; a normal error panel renders.
- **Approval prompt mid-turn.** Renderer pauses; existing prompt renders; resume. No dropped events — the queue keeps buffering while paused.
- **Very long turns** (many iterations). State holds O(actions) entries but capped by the existing 50-action budget. Detail panel shows scrollable-tail (last N) if it exceeds terminal height. No unbounded growth.
- **Verbose render mode** stays exactly as today; live UX layers above and disappears when the turn ends, then verbose panels print.

## Deliverables

- Event sink hook on `ObserverService`.
- `TurnLiveState` + `LiveTurnRenderer` with collapsed / expanded render modes.
- CLI integration for interactive and one-shot paths.
- `--no-live` opt-out + non-TTY auto-disable + CliRunner-safe behavior.
- Unit + smoke tests; full existing suite green.

## Exit Criteria

- `fcli` interactive: hitting enter produces a live status line that updates through planning, action execution, verification, and done.
- Pressing `?` toggles an expanded detail pane that updates in place.
- Pressing Ctrl-C cancels cleanly; terminal mode is restored.
- Approval prompts render normally; Live pauses and resumes around them.
- `fcli "..." > out.txt` has no live widget; stdout is just the final result.
- `--no-live` disables the widget; test suite stays green.
- No performance regression on the orchestrator's hot path with the widget enabled.

## Handoff to Stage 02

Once Stage 01 ships, the `event_sink` hook is already in place. Stage 02 plugs a second sink — a Unix socket or local HTTP server — into the same interface, publishing the same redacted payloads to subscribed monitoring clients.
