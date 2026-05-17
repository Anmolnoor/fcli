# Stage 5: Iteration Traces and Concise Notices

## Goal
Make multi-iteration v3 runs traceable and readable. The trace model must capture each planning and execution pass with iteration-aware ids and replanning edges, while the default terminal view stays concise and shell-like: final answer first, then only short notices about what changed, what ran, and whether verification passed.

## Entry Criteria
- Stage 4 exit criteria are met.
- The trace store already captures planning and execution steps from v2.
- Concise-by-default rendering is already the normal chat surface.

## Locked Decisions
- Planning step ids become iteration-scoped: `planning:<request_id>:<iteration>`.
- Execution step ids become iteration-scoped: `action:<request_id>:<iteration>:<action_id>`.
- Planning and execution step models gain `iteration_index`.
- `TraceEdgeKind.REPLANNED_FROM` links the last execution step of iteration `n` to the planning step of iteration `n+1`.
- Existing `PLANNED` and `SEQUENTIAL` edges remain in use.
- `OrchestrationResult` keeps its aggregated summary but adds `iterations: list[OrchestrationIteration]`.
- Concise mode remains the default for both interactive and one-shot turns.
- Concise mode should show:
  - the final assistant text,
  - short notices for files changed,
  - short notices for commands run,
  - test or verification pass/fail state.
- Full plan tables, execution panels, and orchestration summaries stay behind verbose render mode and existing detail commands.
- Approval prompts, explicit `!` shell commands, and hard failures keep full operational visibility.

## Public Interfaces Introduced
- `LoopStopReason`
- `OrchestrationIteration`
- `iteration_index` on planning and execution trace steps
- `TraceEdgeKind.REPLANNED_FROM`
- `ConciseTurnNotice`

## Step-by-Step Plan
1. Extend planning and execution step models, persistence, and reconstruction to carry `iteration_index`.
2. Update trace id generation so planning and execution step ids are unique across iterations.
3. Persist `REPLANNED_FROM` edges whenever one iteration leads to another planning pass.
4. Extend `OrchestrationResult` to carry both:
   - aggregated summary data for existing renderers,
   - per-iteration detail for trace and verbose inspection.
5. Build concise notice generation from structured iteration outputs:
   - changed paths from file and git mutations,
   - commands run from shell verification,
   - pass/fail notices from verification results,
   - approval-required notices when the loop stops for approval.
6. Keep verbose rendering and history/trace inspection aligned with the new per-iteration structure.
7. Add compatibility handling for older v2 traces that do not yet contain iteration metadata.

## Edge Cases and Failure Modes
- Iterations that plan actions but execute none because everything is blocked or pending approval must still be traceable with the correct iteration index.
- A run that stops after the first iteration should not emit spurious `REPLANNED_FROM` edges.
- Older trace records in the same database must remain inspectable even though they lack `iteration_index`.
- Concise notices should deduplicate repeated file paths and repeated commands so a multi-iteration repair cycle stays readable.
- Large command outputs should not leak into concise mode; only stable summary notices belong there.
- Hard failures must still surface enough operational detail for the user to understand what broke.
- Verbose mode must still reveal the full multi-iteration plan and execution chain without losing causal ordering.

## Deliverables
- Iteration-aware trace models and persistence
- Replanning edges between iterations
- Per-iteration orchestration results
- Concise notices for changed files, commands run, and verification outcome
- Backward-compatible trace inspection for older v2 records

## Exit Criteria
- A multi-iteration coding turn can be reconstructed from stored traces with stable iteration-scoped ids and causal edges.
- Concise mode stays compact while still showing the final answer and key operational notices.
- Verbose mode and detail commands still expose the full planning and execution chain.
- Older traces remain readable after the schema upgrade.

## Test Focus
- Unique step ids across iterations
- Correct `PLANNED`, `SEQUENTIAL`, and `REPLANNED_FROM` edge creation
- Trace reconstruction for multi-iteration requests
- Concise notice rendering for changed files, commands, and verification state
- Verbose render parity and backward compatibility with older traces

## Handoff to Stage 6
Once multi-iteration runs are both traceable and readable, finish the migration-safe hardening work: schema upgrades, docs, and release-gate end-to-end scenarios.
