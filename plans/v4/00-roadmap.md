# Foundation CLI v4 Roadmap

## Purpose

v4 turns Foundation CLI from a fast-to-use coding agent into a **transparent, observable one**. The v3 runtime already has the event plumbing (22 `EVENT_*` constants across orchestrator, executor, shell, provider, observer) and a structured audit trail. What's missing is a live UX that tells the user what's happening *while* a turn runs, and an external stream so a separate monitoring tool — GUI or terminal — can subscribe to the agent's activity in real time.

v4 starts from the v3 runtime without touching the bounded replan loop, approval model, or trace store. New work strictly adds: in-terminal live feedback and an opt-in external event subscription surface.

## Baseline Assumptions

v4 starts from v3 (commit `34b2012` and later), which already has:
- Typed file and git capabilities with approval boundaries.
- Bounded replan loop (32 iterations × 40 actions × 200 total).
- Iteration-aware traces, `REPLANNED_FROM` edges, schema v5.
- VerificationOutcome taxonomy; doctor surfaces approval boundaries.
- Observation accumulation + "commands already executed" summary for the planner.
- Concise presenter with changed-files, commands-run, verification, approval-required notices.

v4 should rebase on that runtime. Nothing in v3 gets rewritten; v4 adds a listener/subscriber surface on top of the existing observer events.

## Locked Decisions

- **In-turn live UX: status line with expandable detail.** The default is one updating status line using Rich `Live`. Pressing `?` toggles an expanded panel showing completed steps + durations + the in-flight step. When the turn ends, the widget tears down and the existing concise result renders.
- **Stage 01 ships the live UX only. Stage 02 adds the external event stream.** Groundwork for both lands in stage 01 (the `event_sink` hook on `ObserverService`), so stage 02 is purely a transport + schema decision.
- **External monitoring transport: Unix socket / local HTTP.** Not a JSONL file. Live subscribe — monitors connect to a running `fcli` process.
- **No full-screen TUI.** The live widget is inline; the session shell stays prompt-toolkit-driven.
- **Auto-disable when stdout isn't a TTY** (e.g. `fcli "..." > out.txt`, CI). v3 behavior is preserved in those environments.
- **Approval prompts pause the live widget.** The existing `EVENT_APPROVAL_REQUESTED` / `EVENT_APPROVAL_RESOLVED` events drive pause/resume around the existing prompt.

## Planning Artifacts

- `plans/v4/00-roadmap.md`  ← this file
- `plans/v4/01-live-turn-ux.md`  ← the first stage, implementation-ready

Stage 02 will be drafted after stage 01 ships and the external-monitor consumer (your GUI/terminal tool) has concrete requirements.

## Stage Sequence

| Stage | Outcome | Blocks Next Stage Until | Primary Artifact |
| --- | --- | --- | --- |
| 01 | Pressing enter on a request shows a live status line and `?`-expandable detail; turn end returns to normal rendering. | Live widget renders reliably across interactive + one-shot modes, gracefully disables on non-TTY, and cleanly handles approvals and Ctrl-C. | `LiveTurnRenderer` + `ObserverService.event_sink` hook |
| 02 | External monitors can subscribe to the running fcli's event stream via Unix socket / local HTTP. | Schema stability, authentication story, graceful start/stop. | `event_sink` transport implementation + client protocol doc |

## Cross-Stage Rules

1. Stage 01's exit criteria are fully met before stage 02 starts.
2. All new surfaces are opt-in (flag or env var). Default CLI behavior is preserved for scripts, CI, and piped stdout.
3. Event payloads are redacted by the existing observability pipeline before they reach any new sink. No secrets leak to live UX or monitors.
4. Live UX adds zero work on the orchestrator's hot path — it's a passive listener. Performance of a turn is unchanged with the UX on or off.
5. Trace persistence continues unchanged. The live widget is a **presentation** layer; the source of truth remains the SQLite trace store.

## Out of Scope for v4

- Full-screen TUI or multi-pane inspector.
- Mutating agent state from the external monitor (it's read-only subscribe).
- Replay / time-travel over the live stream.
- Web dashboard built on the external stream — that's a separate tool, not part of the CLI.

## Definition of Done for v4

Foundation CLI v4 is done when:
- A user running `fcli` interactive or `fcli <request>` sees a live status line with `?`-expandable detail while the turn executes.
- The widget auto-disables on non-TTY stdout and under approval prompts.
- Ctrl-C mid-turn cancels cleanly without leaving the terminal in raw mode.
- An external monitoring client (user's GUI or terminal tool) can connect to the running fcli via the documented transport and receive redacted events in near-real-time.
- Existing v3 behaviors (plan table, execution panels, trace inspection, history, concise/verbose parity) remain unchanged.
