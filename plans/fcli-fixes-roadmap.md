# FCLI Fixes Roadmap

## Purpose

Track the near-term fixes needed to get Foundation CLI back to a clean,
trustworthy baseline before any Beekeeper worker integration work starts.

This roadmap is intentionally scoped to FCLI only.

## Source Inputs

- `/Users/anmolnoor/Developer/future-things/fcli-command-error-recovery-plan.md`
- `/Users/anmolnoor/Developer/future-things/fcli-live/loading-and-pending-state-plan.md`
- Current local verification results from this branch.

## Current Baseline

Verified on 2026-06-01:

- `./scripts/uv run pytest` passes: 426 tests.
- `./scripts/uv run ruff check src tests` passes.
- `./scripts/uv run ruff format --check src tests` fails.
- `./scripts/uv run mypy` fails with 11 strict typing errors.
- `./scripts/uv run foundation doctor` passes.

Current worktree notes:

- `uv.lock` changed after bootstrap added `click` metadata.
- `gh-cli-cheatsheet.md` is untracked.
- `res/` is untracked.

## Out Of Scope

- Beekeeper Queen/worker architecture.
- Worker registry, task queue, sandbox backend, skill promotion, memory
  curation, and external integrations.
- Product dashboard work outside the current FCLI terminal experience.

## Stage 1: Worktree And Quality Baseline

### Goal

Make the repo state explicit before fixing behavior.

### Tasks

1. Decide whether the `uv.lock` bootstrap change should be kept.
2. Decide whether `gh-cli-cheatsheet.md` and `res/` belong in the repo,
   should be ignored, or should remain local scratch files.
3. Run and record the baseline checks:
   - `./scripts/uv run pytest`
   - `./scripts/uv run ruff check src tests`
   - `./scripts/uv run ruff format --check src tests`
   - `./scripts/uv run mypy`
   - `./scripts/uv run foundation doctor`

### Done Criteria

- Worktree changes are understood and intentionally kept or left untouched.
- The failing gates are known before implementation starts.

## Stage 2: Command Error Recovery

### Goal

Stop turning ordinary command usage failures into capability-gap messages.

Reference failure:

```text
gh api repos/anmolnoor/anmolnoor/readme --jq .content -r
```

The command fails because `gh api` does not support `-r`, but FCLI can frame
the result as an external-access or capability gap. That diagnosis is wrong.
The final user-facing message should preserve the concrete stderr and classify
the problem as a command invocation error.

### Tasks

1. Add a small command-failure classifier near the orchestrator/gap-handoff
   boundary.
2. Classify usage-shaped failures as recoverable command errors:
   - `unknown shorthand flag`
   - `unknown option`
   - `unrecognized option`
   - `invalid option`
   - `Usage:`
   - `Try '--help'`
3. Feed the planner one repair observation when a shell argv is invalid:
   - include the failed command,
   - include exit code,
   - include stderr excerpt,
   - instruct the planner not to repeat the same argv.
4. Gate capability-gap handoff more narrowly so normal nonzero shell exits,
   tests, linters, and command usage errors do not become gap reports.
5. Keep provider-backed phrasing only for true structural gap kinds.
6. Add deterministic final presentation for unrepaired command usage errors.

### Tests

Add tests before implementation:

1. `build_gap_handoff()` does not produce a missing-capability handoff for
   `unknown shorthand flag: 'r' in -r`.
2. Repeating the invalid `gh api ... -r` command does not classify the turn as
   a capability gap.
3. The second planner call receives the failed command, stderr, and a
   do-not-repeat instruction.
4. True missing capability still gets a capability-gap handoff.
5. Missing binary / command unavailable still gets the appropriate handoff.
6. CLI presentation includes the failed command and stderr excerpt.

### Done Criteria

- The exact `gh api ... --jq .content -r` shape no longer becomes a
  capability-gap/provider-access message.
- FCLI either repairs the command on the next iteration or stops with the real
  stderr visible.
- True capability gaps still render handoff options.

## Stage 3: Static Quality Gates

### Goal

Restore the repo's expected static checks.

### Tasks

1. Run formatting on the affected files:
   - `src/foundation/cli.py`
   - `src/foundation/services/gap_handoff.py`
   - `src/foundation/services/guardrails.py`
   - `src/foundation/services/planner.py`
   - `tests/test_cli.py`
   - `tests/test_file_service.py`
   - `tests/test_orchestrator.py`
2. Fix strict mypy errors without weakening type checking.
3. Avoid behavior changes unless a type issue exposes a real bug.

### Known Mypy Areas

- Untyped `__exit__` parameters in monitor/live context managers.
- `_make_sse_handler()` missing return type.
- `_resolve_read_path()` has a raise-helper control-flow issue.
- `Any` leaks in JSON/result handling.
- One `PlannedAction | None` assignment mismatch in orchestrator code.

### Done Criteria

- `./scripts/uv run ruff format --check src tests` passes.
- `./scripts/uv run ruff check src tests` passes.
- `./scripts/uv run mypy` passes.
- `./scripts/uv run pytest` still passes.

## Stage 4: Live Loading UX Polish

### Goal

Make the terminal live state truthful and less vague during long turns.

This is the first patch scope from the FCLI live plan only. It should not touch
provider internals, worker architecture, or add new dependencies.

### Tasks

1. Add an explicit live phase model in `src/foundation/live_turn.py`.
2. Track:
   - current phase,
   - phase start time,
   - last event time,
   - last event name.
3. Render clearer collapsed status text for:
   - starting,
   - thinking,
   - planning,
   - running tool,
   - observing,
   - waiting for approval,
   - waiting for user input,
   - stale,
   - completed,
   - failed.
4. Add stale/no-event display as a view-layer concern:
   - soft stale after 15 seconds,
   - hard stale after 60 seconds.
5. Improve expanded detail with phase, elapsed time, last event, current
   action, counters, and recent actions.
6. Keep non-TTY and disabled live UX behavior unchanged.

### Tests

Add or update `tests/test_live_turn.py` for:

- event-to-phase transitions,
- phase timing,
- last event tracking,
- stale rendering,
- collapsed status text,
- expanded detail rows,
- terminal states never showing stale.

### Done Criteria

- The collapsed loader never shows only a vague thinking counter.
- The user can tell whether FCLI is planning, running a tool, waiting for
  approval/input, stale, done, or failed.
- Existing concise/verbose result rendering still happens after the live widget
  exits.

## Stage 5: Docs And Changelog

### Goal

Document only behavior that actually changed.

### Tasks

1. Update `docs/TECHNICAL.md` if command recovery or live UX behavior changes
   user-visible semantics.
2. Update `CHANGELOG.md` with concise entries for:
   - command error recovery,
   - static quality cleanup,
   - live UX polish.
3. Do not add Beekeeper roadmap material to FCLI docs.

### Done Criteria

- Docs match runtime behavior.
- Changelog names the fixes without overpromising future work.

## Final Completion Gate

The FCLI fix batch is complete when all of these pass:

```bash
./scripts/uv run ruff check src tests
./scripts/uv run ruff format --check src tests
./scripts/uv run mypy
./scripts/uv run pytest
./scripts/uv run foundation doctor
```

The final worktree should contain only intentional changes tied to this
roadmap.
