# Stage 6: Hardening and End-to-End Validation

## Goal
Close the v3 loop with migration-safe hardening. This stage turns the new entrypoint, file layer, git layer, replanning loop, and iteration-aware trace model into a releasable runtime by covering upgrade paths, failure modes, and end-to-end coding scenarios.

## Entry Criteria
- Stage 5 exit criteria are met.
- The new file and git capabilities are in the registry and used by the planner.
- Multi-iteration traces and concise notices are functioning.

## Locked Decisions
- v3 ships as an upgrade of the current runtime, not as a side-by-side alternate execution path.
- Existing v2 sessions, history, and traces must remain inspectable after the upgrade.
- End-to-end coding scenarios are release gates, not optional smoke tests.
- `git.commit` remains approval-gated and must not implicitly include unstaged changes.
- Binary editing, networked git actions, and PR automation remain out of scope.

## Step-by-Step Plan
1. Finalize schema and persistence migrations for any new iteration-aware trace fields and orchestration-result storage.
2. Audit the planner instructions and remove stale shell-editing assumptions now that file and git capabilities exist.
3. Update capability inspection, doctor output, and operator docs so v3 built-ins and approval boundaries are visible.
4. Add integration fixtures for the full coding workflow:
   - read files,
   - rewrite a file,
   - run tests,
   - fix a failure,
   - rerun tests,
   - stage changes,
   - request commit approval.
5. Add regression coverage for the main stop conditions:
   - pending approval,
   - fatal failure,
   - max-iteration stop,
   - max-action stop,
   - verification unavailable.
6. Validate concise and verbose output against the same end-to-end scenarios.
7. Update top-level docs and planning references so the repo documents v3 as the current agent-shell direction.

## Edge Cases and Failure Modes
- Existing databases may contain v2 traces with no iteration metadata; migrations must not make them unreadable.
- Dirty worktrees that predate the agent turn should remain visible so staging and commit flows do not hide unrelated changes.
- Approval prompts for commit should clearly surface staged paths so the user can catch accidentally included files.
- A turn that edits files but then stops on max-iteration, max-action, or fatal failure must clearly report that the workspace is left modified.
- Missing binaries or broken test environments must not be misreported as successful verification.
- Commit approval denial should leave the workspace and index unchanged apart from the already staged paths.
- End-to-end tests should cover both one-shot and interactive shells so the shared entrypoint contract remains real.

## Deliverables
- Migration-safe storage and trace upgrades
- Release-gate integration tests for the v3 coding workflow
- Updated docs and doctor/capability inspection output
- Hardened handling for incomplete or partially successful coding turns

## Exit Criteria
- The v3 upgrade preserves inspectability of older v2 data.
- End-to-end coding scenarios pass for both one-shot and interactive usage.
- The agent can read, edit, verify, fix, restage, and pause for commit approval inside one traceable turn.
- Concise and verbose rendering both remain deterministic across success, failure, and approval-required paths.
- Known limitations are documented clearly.

## Test Focus
- CLI routing and `chat` alias parity
- File capability success and failure cases
- Git helper behavior and commit approval
- Replanning-loop stop conditions
- Trace migration and edge integrity
- Concise versus verbose rendering
- End-to-end coding workflow coverage

## Handoff Beyond v3
Once v3 is hardened, the runtime is positioned for future work such as smarter verification heuristics, richer trace replay tooling, or more specialized non-shell capabilities without reopening the core agent-shell contract.
