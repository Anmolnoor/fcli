# Stage 4: Bounded Replanning and Coding Loop

## Goal
Replace the current single planning pass with a bounded replan loop so one user turn can do real coding work: inspect context, edit files, run verification, observe failures, repair the code, and rerun verification before answering. The loop must stay bounded, inspectable, and approval-aware.

## Entry Criteria
- Stage 3 exit criteria are met.
- File and git capabilities are available through the registry and executor.
- The current planner prompt, execution summary model, and trace flow are understood.

## Locked Decisions
- v3 replaces the one-pass orchestrator with a bounded loop:
  - max 32 planning iterations per user turn,
  - max 40 actions per iteration,
  - max 200 actions total.
- Request context is regathered before each iteration so capability availability, file state, and git state reflect prior edits.
- After each non-terminal iteration, the runtime appends one normalized observation block to the planner conversation containing:
  - executed capability ids,
  - changed paths,
  - exit codes,
  - approval outcomes,
  - stdout and stderr previews capped at 8 KB or 200 lines.
- The loop stops on:
  - zero-action plan,
  - pending approval,
  - fatal execution failure,
  - max-iteration cap,
  - max-action cap.
- The terminating iteration's `assistant_message` becomes the user-facing answer.
- If the loop stops early, the final answer must explicitly say why.
- `foundation.shell.command` remains the only generic command runner.
- Shell is for read-only environment inspection and for running tests, builds, or scripts.
- File and git capabilities are the default path for code changes and repo mutations.
- For code-changing turns, at least one relevant verification command must run before a final zero-action completion unless the answer explicitly states why verification was unavailable.

## Public Interfaces Introduced
- `LoopStopReason`
- `OrchestrationIteration`
- `IterationObservation`
- `VerificationNotice`

## Step-by-Step Plan
1. Refactor orchestration result assembly so one request can contain multiple planning and execution iterations while still producing one aggregated result.
2. Add an iteration controller that:
   - tracks iteration index,
   - tracks total actions,
   - regathers context before every plan request,
   - evaluates stop conditions after every iteration.
3. Define the normalized observation block format that gets appended back into the planner conversation after execution.
4. Upgrade planner instructions so:
   - file capabilities are preferred for reads and edits,
   - git capabilities are preferred for repo inspection and staging,
   - shell mutation commands are not used when typed capabilities exist,
   - failed verification should trigger diagnosis and repair,
   - code-changing turns should end with verification or an explicit reason they could not verify.
5. Feed failed verification results into later iterations instead of terminating immediately after the first failure.
6. Detect no-progress replans:
   - repeated identical verification failures with no intervening file changes,
   - repeated identical action sequences that do not change state.
7. Preserve approval semantics:
   - pending approval stops the loop,
   - denied approval produces a clear blocked outcome,
   - approved commit can continue only after the approval result is recorded.
8. Ensure one-shot and interactive turns both persist the full multi-iteration run as one logical user request.

## Edge Cases and Failure Modes
- A zero-action plan on the first iteration should be allowed for explanation-only or already-satisfied requests.
- A zero-action plan after code changes but before verification should be rejected or converted into an explicit “verification unavailable” completion path.
- Verification commands may fail for environmental reasons such as missing dependencies or missing binaries; the loop should report that distinction clearly rather than pretending the code itself is broken.
- Repeated failing verification with no file changes should stop with an explicit no-progress or max-cap reason instead of burning all remaining actions silently.
- Pending approval after files have already changed should stop cleanly and explain that the workspace was modified before approval was required.
- Fatal capability failures should stop the loop without attempting speculative extra actions.
- Observation previews must be truncated deterministically so large test logs do not blow up prompt size.
- Context refresh must see changes from earlier iterations, including git index changes and file hashes.
- A turn that hits the max-action or max-iteration cap must return a final answer that says the work is incomplete and why the loop stopped.

## Deliverables
- A bounded multi-iteration orchestrator
- Iteration-aware planner observation blocks
- Verification-aware coding turn behavior
- Explicit stop reasons for approval, failure, no-op completion, and budget exhaustion

## Exit Criteria
- One user turn can complete a read/edit/run/fix/rerun cycle without requiring a second user message.
- Context is refreshed between iterations.
- Failed verification can trigger repair attempts within the same turn.
- The loop stops deterministically on the defined caps and terminal conditions.
- The final assistant message reflects the terminating iteration and states early-stop reasons explicitly.

## Test Focus
- Multi-iteration success paths
- Verification-failure repair cycles
- Pending-approval stop behavior
- Fatal-failure stop behavior
- Max-iteration and max-action stop behavior
- No-progress detection and observation-block truncation
- Verification-required behavior for code-changing turns

## Handoff to Stage 5
Once the runtime can replan within one turn, upgrade trace storage and concise rendering so those extra iterations remain understandable without turning the terminal back into an audit dump.
