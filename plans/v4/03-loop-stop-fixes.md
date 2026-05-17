# Stage 3: Loop-Stop Fixes (no-progress detector, tool-call observation, success classification)

## Goal

Stop misclassifying successful turns as failed. Today, a turn that writes the
right file in iter 2 and then has the planner re-issue the same write in iter 3
trips `NoProgressDetector` and prints "[Loop stopped: no progress detected
across iterations.]" with a red "2 failed" summary — even though the workspace
state actually matches the user's intent. Stage 03 closes the gap so:

- Successful tool-call results feed the planner observation, so the planner
  can't re-plan a write it already completed.
- The no-progress detector tolerates more than one repeat and respects
  cumulative changes across iterations.
- "Already exists" / "no-op idempotent" errors don't poison the loop.
- When the loop does stop with a clean workspace, the user-facing notice and
  session status reflect "completed" rather than "stuck".

## Reference Incident

Session `7d3ccb33` (saved in `~/Library/Application Support/foundation/history.sqlite3`):

- Iter 1: `foundation.file.read /Users/anmolnoor/anmolnoor_github.md` → failed
  (file did not exist — exploratory probe).
- Iter 2: `foundation.file.write` → executed; file written to disk.
- Iter 3: `foundation.file.write` (same path, same content) → failed with
  "File already exists. Set overwrite=true to replace it."
- Detector tripped → `NO_PROGRESS` → status `completed_inconclusive`,
  presenter shows red "Executed 1 action(s), 2 failed."

The on-disk artifact is correct. The user-facing summary is not.

## Entry Criteria

- v3 runtime shipped; `v3-fixes` branch merged or queued. Bounded loop budgets
  at 32 × 40 × 200.
- `NoProgressDetector`, `IterationObservation`, `_observation_to_messages`, and
  `_session_status_for_result` exist in `services/orchestrator.py`.
- Stages 01 and 02 are independent of this work; they can land in any order.

## Locked Decisions

- The fix lives in **the orchestrator and observation builder**, not the
  planner prompt. The planner improves implicitly because it sees better
  observations.
- `NoProgressDetector.is_stuck()` requires **two** consecutive identical
  fingerprints (window = 2) before declaring stuck.
- The detector's `has_changes` check becomes **cumulative across iterations**,
  not per-iteration — so a successful change earlier in the loop counts as
  progress even if the latest iteration produced none.
- Successful tool-call invocations (every `foundation.file.*`,
  `foundation.git.*`, and any future builtin tool) are recorded in the
  cumulative `executed_command_log` so the planner sees them in the
  "COMMANDS ALREADY EXECUTED" summary on the next iteration.
- Specific idempotent-failure error codes are classified as **soft outcomes**
  for the detector — they count as neither a "failure" nor a "no-change".
  Initial allowlist: `FileErrorCode.FILE_EXISTS` when the same path was
  successfully written earlier in the same session.
- `LoopStopReason.NO_PROGRESS` is renamed in the user-facing notice (not the
  enum) when cumulative changed_paths is non-empty: from "no progress
  detected" to "task completed; planner kept retrying after success".
- `_session_status_for_result` returns `COMPLETED` (not
  `COMPLETED_INCONCLUSIVE`) for `NO_PROGRESS` when cumulative
  `changed_paths` is non-empty **and** no fatal error occurred.
- Schema fix for `assistant_plans` is included as a v6 migration: the
  `UNIQUE(session_id)` constraint becomes `UNIQUE(session_id, iteration)`
  so per-iteration plans are actually persisted (today, only the last
  iteration survives because the column exists but the uniqueness constraint
  doesn't include it).
- Read-before-write probes (a `file.read` whose path is the target of a
  later `file.write` in the same session) are tagged so a `FileNotFound`
  on the probe is excluded from the detector's failure set.

## Public Interfaces Touched

- `NoProgressDetector.__init__` — gains `window: int = 2` (kept private,
  documented in the docstring).
- `NoProgressDetector.is_stuck(... cumulative_changed_paths=...)` — new
  keyword-only parameter; backwards-compatible default keeps existing callers
  working in tests.
- `IterationObservation` — gains `cumulative_changed_paths: list[str]` (the
  per-iteration `changed_paths` field stays).
- `executed_command_log` (orchestrator-local) — entries now include builtin
  tool invocations, formatted as
  `tool_call:foundation.file.write path=/Users/.../anmolnoor_github.md` so
  the planner can de-duplicate them like it already does for shell.
- `_session_status_for_result` — keeps signature, gains the cumulative-changes
  override for `NO_PROGRESS`.
- New `_STOP_REASON_SUFFIXES_SOFT` table for the "completed; planner retried"
  variant. Selected by the orchestrator based on cumulative state, not by
  changing the enum.
- History schema v6: drops `UNIQUE(session_id)` on `assistant_plans`,
  adds `UNIQUE(session_id, iteration)`.

## Step-by-Step Plan

1. **Detector window.** Change `is_stuck()` to walk the last `window=2`
   fingerprints and require an exact match across both. Keep the failure
   gate (`has_failures`) as the trigger so a clean run never trips it.
2. **Cumulative changes.** Track a session-level
   `cumulative_changed_paths: set[str]` in the orchestrator loop; pass it
   into `is_stuck()` and into `IterationObservation`. The detector returns
   `False` when cumulative is non-empty, regardless of per-iteration
   `has_changes`.
3. **Tool-call entries in `executed_command_log`.** In the loop's post-execute
   block, after appending shell entries, also append one line per successful
   tool call: `tool_call:{capability_id}{key=value...}` where the key/value
   pairs are a redacted projection of the action arguments (path, ref, etc.).
   Limit to capabilities with side effects so read-only probes don't bloat
   the log.
4. **Soft-failure classifier.** Add `_SOFT_FAILURE_CODES` (initial members:
   `FileErrorCode.FILE_EXISTS` paired with prior-write evidence). When the
   detector inspects failures, exclude any result whose error matches a
   soft code AND whose target path appears in
   `cumulative_changed_paths`. Those errors don't count as "stuck failures".
5. **Probe tagging.** When the planner emits a `foundation.file.read` whose
   path matches a same-iteration or later `foundation.file.write` target,
   tag the read in observation as `probe=true`. The detector excludes
   probe results entirely from the failure set. (No change to the planner
   prompt; this is a structural inference at observation time.)
6. **Stop-reason notice override.** In the presenter path that builds the
   final `assistant_message`, when `stop_reason is NO_PROGRESS` AND the run
   has non-empty cumulative `changed_paths` AND no fatal error, swap the
   suffix to `[Run complete; planner re-issued already-finished actions.]`.
7. **Status mapping.** `_session_status_for_result` returns `COMPLETED` when
   `NO_PROGRESS` fires with cumulative changes and no fatal. Otherwise the
   existing `COMPLETED_INCONCLUSIVE` mapping stands.
8. **Schema v6.** New `_migrate_to_v6` in `services/history.py`:
   - create `assistant_plans_new` with `UNIQUE(session_id, iteration)`,
   - copy rows over (preserving the latest plan per (session, iteration)),
   - drop the old table, rename. Auto-run on startup like prior migrations.
9. **Tests.**
   - Unit: `NoProgressDetector` requires two repeats; one repeat does not
     trip; cumulative changes suppress the trip.
   - Unit: `executed_command_log` includes successful `foundation.file.write`
     and `foundation.git.commit` entries with a stable shape.
   - Unit: `_SOFT_FAILURE_CODES`: `FILE_EXISTS` after a prior write is
     filtered from the failure fingerprint.
   - Unit: probe-tagging excludes the read's failure from the detector.
   - Unit: `_session_status_for_result` returns `COMPLETED` for the
     reference-incident shape; returns `COMPLETED_INCONCLUSIVE` when
     cumulative changes are empty.
   - Integration: replay the reference-incident scenario with a stub provider
     and assert the loop stops with `COMPLETED`, no red notice, file on
     disk, single change reported.
   - Migration: v5 → v6 fixture; assert per-iteration plans survive.

## Files to modify / add

- `src/foundation/services/orchestrator.py` — detector signature,
  cumulative tracking, soft-failure classifier, probe tagging,
  observation builder, status mapping, notice override.
- `src/foundation/models/orchestration.py` — `IterationObservation`
  gains `cumulative_changed_paths`; `ActionOutcome` gains optional
  `probe: bool = False`.
- `src/foundation/services/history.py` — `_migrate_to_v6`.
- `src/foundation/cli.py` — pick up the soft-stop notice variant in the
  presenter.
- `tests/test_orchestrator.py` — detector + status mapping cases.
- `tests/test_orchestrator_integration.py` (new or extend existing
  e2e file) — reference-incident replay.
- `tests/test_history.py` (or wherever migrations are tested) — v6
  migration test.
- `CHANGELOG.md` — entry under v0.2.x or v0.3.0.
- `plans/v4/00-roadmap.md` — add Stage 03 row.

## Edge Cases and Failure Modes

- **First iteration repeats itself.** Detector now needs two repeats; first
  iteration alone never trips. Already true today, kept explicit.
- **Idempotent re-write with different content.** If iter 3's write has
  different content than iter 2's (same path, new sha), it's not a no-op —
  treat as normal progress, do not soft-classify.
- **Failure on a path never previously changed.** Cumulative-changes
  override does not apply; detector behaves as today. Stuck remains stuck.
- **Mixed success/failure in the same iteration.** A successful write
  alongside a failing verification still counts as progress (cumulative
  changed_paths non-empty); the detector won't trip on the verify failure
  alone.
- **`git.commit` re-plan after success.** `executed_command_log` now records
  the commit; planner sees it and won't re-issue. If it does anyway, the
  commit-approval invariant from v3-fixes still gates side effects.
- **Schema v6 migration on a large history DB.** Copy is O(rows) but
  per-row size is small (a few KB JSON). Runs once. Tested on the same
  fixture path as v5.
- **Old (v3) callers of `is_stuck()`.** `cumulative_changed_paths` is
  keyword-only with a default; existing tests / callers compile unchanged.

## Deliverables

- Detector with window=2 and cumulative-change awareness.
- Tool-call entries in the executed-command summary.
- Soft-failure code allowlist + probe tagging.
- Updated stop-reason notice and session-status mapping for the
  "completed but retried" shape.
- History schema v6 migration.
- Tests covering each bullet, plus a full reference-incident integration test.

## Exit Criteria

- Replaying the reference incident with a stub provider produces:
  - `LoopStopReason` may still be `NO_PROGRESS` *or* `ZERO_ACTION_PLAN`,
  - `SessionStatus` is `COMPLETED`,
  - presenter prints "Run complete" not "no progress detected",
  - `Executed N action(s), 0 failed.` (idempotent failures are filtered
    from the failure count when their target path was successfully changed
    earlier in the same session).
- No-progress still trips correctly when the loop is genuinely stuck (e.g.
  same failing test re-run with no edits in between, twice).
- v6 migration applies cleanly on a v5 fixture; per-iteration plans become
  inspectable for the first time.
- Full suite green; ruff / mypy clean.

## Out of Scope

- Planner prompt rewrites. (The improved observation should be sufficient.)
- A general "is the goal achieved" classifier driven by an LLM judge — that
  belongs to a future stage.
- Backporting the schema fix to v5 readers — v5 is read-only after v6 lands.
