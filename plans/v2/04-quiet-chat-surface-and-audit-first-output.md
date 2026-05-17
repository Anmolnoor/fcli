# Stage 4: Quiet Chat Surface and Audit-First Output

## Goal
Make `foundation chat` feel like a normal terminal assistant: the user asks a question, the assistant answers, and the next prompt appears. Internal orchestration detail such as planned actions, policy tables, execution summaries, provider metadata, token counts, and raw structured log lines should stop dominating the default terminal view. Those details must still be preserved in logs, history, and audit surfaces.

## Entry Criteria
- Stage 00 persistent chat is working and session continuity is stable enough to change presentation without changing core behavior.
- Stage 3 trace and audit direction is accepted as the long-term home for execution detail.
- The current chat surface has been reviewed and categorized into user-facing content versus audit-only detail.
- The current logging pipeline is understood, including why structured provider and runtime events can currently appear in the terminal.

## Locked Decisions
- Default interactive chat output is concise by default.
- This stage changes presentation, not execution semantics. The planner, policy engine, approvals, shell runtime, and persistence model continue to work underneath the quieter UI.
- The default terminal transcript should resemble a standard assistant shell: prompt, answer, next prompt.
- Internal orchestration detail is hidden from the default chat surface, not discarded.
- Audit and forensic detail remain first-class and recoverable through logs, history, and later trace inspection.
- Approval prompts, explicit errors, and direct shell output for user-invoked `!` commands remain visible because they are operationally important.
- Raw logger output should not interleave with the active chat transcript in normal operation; it should route to file-backed logs unless the user explicitly opts into debug-style terminal output.
- No performance claim is implied by this stage. The change is about reducing cognitive load, not promising faster model responses.

## Public Interfaces Introduced
- `RenderMode`
- `ChatSurfacePolicy`
- `ChatTurnPresentation`
- `AuditDetailRef`
- `TerminalLogRouting`
- `InteractiveDetailCommand`

## Step-by-Step Plan
1. Classify all current chat output into presentation classes:
   - assistant answer
   - approval prompt
   - direct shell stdout and stderr
   - action plan tables
   - execution-result panels
   - orchestration summary panels
   - provider and runtime log lines
2. Define the default chat presentation contract:
   - show the assistant answer as the primary output
   - keep the prompt-to-answer loop visually tight
   - avoid rendering policy, token, latency, and plan tables by default
   - show concise operational notices only when needed, such as approval required, command failed, or session recovered
3. Split output sinks so the terminal is no longer the catch-all destination:
   - interactive terminal transcript for concise user-facing content
   - file-backed logs for runtime and provider events
   - history persistence for request and outcome summaries
   - audit and trace persistence for step-level execution detail
4. Introduce explicit verbosity controls instead of always-on detail:
   - CLI-level render mode for concise versus verbose output
   - interactive slash commands to inspect the last plan, actions, or orchestration summary on demand
   - optional one-turn expansion flow so a user can ask for details after seeing the concise answer
5. Refactor chat rendering around a presentation policy:
   - replace unconditional rendering of assistant, plan, action, and summary blocks with one presenter that chooses what to show per render mode
   - keep direct shell command rendering separate so `!` commands still behave like terminal commands
   - ensure approval prompts bypass the quiet presenter because they require immediate user attention
6. Move raw logger output out of the main chat experience:
   - stop attaching generic stream handlers to the active terminal transcript by default
   - write structured runtime events to the configured log path
   - allow terminal log streaming only under explicit debug or verbose settings
7. Preserve hidden detail with clear audit references:
   - keep plan, policy, execution, provider, token, and latency metadata attached to the run record
   - expose references from the concise chat turn to the stored history or trace entry when useful
   - make sure hidden execution detail can still explain why an answer was produced or why a step was blocked
8. Align one-shot and interactive chat output policy:
   - default `foundation chat <request>` to the same concise presentation model
   - allow one-shot verbose inspection when explicitly requested
   - keep history and audit records structurally identical regardless of presentation mode
9. Update documentation and examples so the product expectation is clear:
   - concise by default
   - detail on demand
   - logs and audit remain the source of truth for internals

## Deliverables
- A quiet-by-default chat surface for interactive and one-shot chat
- Presentation-policy controls that separate concise versus verbose rendering
- File-backed runtime logging that no longer pollutes the active chat transcript
- Audit-preserving storage of hidden plan, policy, and execution detail
- Interactive commands or flags for on-demand inspection of the hidden detail

## Exit Criteria
- A normal chat turn shows the assistant’s answer without rendering the full plan, action table, execution panels, or orchestration summary.
- Provider and runtime log lines no longer appear inline in the active chat transcript during normal operation.
- Approval prompts, explicit failures, and direct `!` shell output remain visible and understandable.
- Hidden plan and execution detail is still queryable through history, logs, or audit surfaces.
- Concise and verbose render modes are both tested and deterministic.

## Test Focus
- Default interactive chat output contains only the intended concise presentation for success cases
- Approval, failure, and shell-command paths still surface the right operational detail
- Verbose and inspect-on-demand modes reveal the hidden orchestration detail correctly
- Structured logs route to file instead of interleaving with the terminal transcript
- History and audit persistence still contain plan, policy, provider, and execution metadata even when the terminal stays quiet
- One-shot and interactive chat stay behaviorally consistent under the new presentation policy

## Handoff Beyond Stage 4
Once the chat surface is quiet by default and audit detail is safely preserved elsewhere, later v2 work can build richer inspect, replay, and trace views without forcing every normal chat turn to display internal runtime scaffolding.
