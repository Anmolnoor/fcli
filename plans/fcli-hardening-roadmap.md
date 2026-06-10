# FCLI Hardening Roadmap

## Purpose

Close the gaps found in the 2026-06-10 full-project review (architecture,
code-quality, and test-depth passes). The theme of the findings: the project
is stricter about what gets in (policy, approval, validation) than about
noticing when its own internals fail (stripped asserts, swallowed exceptions,
unvalidated migrations). This roadmap fixes that, one stage at a time.

Stages are ordered by risk: runtime correctness first, then audit integrity,
then test depth, then structural cleanup, then docs. Each stage is small
enough to land as one PR.

## Source Inputs

- 2026-06-10 review findings (architecture 7/10, code quality 7/10,
  tests 7.5/10, docs 8/10).
- Current local verification results on `main` (commit `d00ab98`).

## Current Baseline

Verified on 2026-06-10:

- `./scripts/uv run pytest` passes: 460 tests.
- `./scripts/uv run ruff check src tests` passes.
- `./scripts/uv run ruff format --check src tests` passes.
- `./scripts/uv run mypy` passes (strict, 44 source files).
- `./scripts/uv run foundation doctor` passes; all capabilities healthy.

Note: `plans/fcli-fixes-roadmap.md` stages appear fully shipped (command-usage
classifier in `gap_handoff.py`/`orchestrator.py`, `LivePhase` in
`live_turn.py`, static gates green) but the roadmap was never marked complete.
Stage 9 closes that loop.

## Reviewed And Rejected

Findings from the review that were checked against the code and dropped:

- "Scope grants persist without expiration" — wrong. `ScopeGrantStore`
  (`services/scope_grants.py`) is session-scoped, in-memory, read-only, and
  never persisted. No action needed.

## Out Of Scope

- Beekeeper Queen/worker architecture.
- New capabilities, providers, or UX surfaces.
- Full discriminated-union rewrite of the plan wire format (stage 2 adds
  validation without changing the schema providers emit).

## Stage 1: Executor Invariants Fail Loudly

### Goal

Replace production-path `assert` statements with typed errors. `python -O`
strips asserts entirely; this code mutates files and runs shell commands, so
its invariant guards must not be removable by an interpreter flag.

### Findings

16 asserts in `services/executor.py`:

- Action-union guards: lines 485 (`action.question`), 501 (`action.tool_call`),
  515 and 862 (`action.shell`).
- Service-injection guards: lines 612–672 (`self._file_service`,
  `self._git_service` not None, 12 occurrences).

### Tasks

1. Add a small module-level helper in `executor.py` that raises a typed
   internal error (reuse `ToolExecutionError` or add an
   `InternalExecutorError`) instead of asserting. Keep it private to the
   module — no new abstractions.
2. Replace the 4 action-union asserts: a mismatch between `action.kind` and
   its payload becomes a FAILED `ExecutionResult` with a clear error message,
   not a crash.
3. Replace the 12 service-injection asserts: a `builtin.file.*` /
   `builtin.git.*` dispatch without the service wired becomes a typed error
   naming the missing service.
4. Grep the rest of `src/foundation/` for asserts on real code paths and apply
   the same treatment (tests may keep asserts).

### Tests

Add before implementation:

1. An action with `kind=TOOL_CALL` but `tool_call=None` produces a FAILED
   result with the typed error message — under both normal and `-O` execution
   semantics (simulate by calling the guard helper directly).
2. Dispatching `builtin.file.read` with `file_service=None` produces a typed
   error, not `AttributeError` or `AssertionError`.
3. Existing 460 tests still pass.

### Done Criteria

- `grep -n "assert " src/foundation/services/executor.py` returns nothing.
- Invariant violations surface as FAILED execution results in the trace, not
  interpreter crashes.

## Stage 2: Plan-Action Union Validation At The Boundary

### Goal

Make invalid action shapes unrepresentable at validation time instead of
crash-time. Today Pydantic accepts an action whose `kind` says one thing and
whose payload fields say another; every consumer then re-checks by hand.

### Tasks

1. Add a `model_validator(mode="after")` to the planned-action model in
   `models/` enforcing: the payload field matching `kind` is present, and
   payload fields for other kinds are absent.
2. Keep the wire format unchanged — providers still emit `kind` + optional
   payload fields. This is validation, not a schema migration.
3. Route validation failures through the existing plan-repair path (the
   planner already retries on malformed plans), so a model that emits a
   mismatched action gets one repair attempt rather than a hard stop.
4. Remove the per-consumer `isinstance`/None re-checks in `planner.py`
   (~lines 476–498) that the validator now makes redundant. Stage 1's typed
   guards in the executor stay — defense in depth at the execution boundary.

### Tests

1. `kind=TOOL_CALL` with `shell` payload set and `tool_call=None` fails
   validation with a message naming both fields.
2. A mismatched action from the provider triggers one plan-repair round trip.
3. Valid plans for every `ActionKind` still validate.

### Done Criteria

- A mismatched kind/payload can no longer reach the executor.
- Plan-repair handles the new validation failure shape.

## Stage 3: One Source Of Truth For Git Mutation Subcommands

### Goal

`planner.py:91` (`_GIT_MUTATION_SUBCOMMANDS`) and `guardrails.py:52`
(`_WRITE_GIT_SUBCOMMANDS`) define the same 16-entry set independently. If
they diverge, the planner will permit what policy blocks — or policy will
miss what the planner emits.

### Tasks

1. Define `GIT_MUTATION_SUBCOMMANDS: frozenset[str]` once, in `models/git.py`
   (it is domain knowledge, not service logic).
2. Import it in both `planner.py` and `guardrails.py`; delete the local
   copies. Keep local aliases if it keeps diffs small.

### Tests

1. Both modules reference the shared constant (identity check:
   `planner module set is guardrails module set`).
2. Existing planner-validation and guardrails tests pass unchanged.

### Done Criteria

- Exactly one definition of the set exists in `src/foundation/`.

## Stage 4: Audit-Trail Failures Become Visible

### Goal

The trace/event pipeline is the project's accountability story, but failures
in it are currently invisible: `observer.py:64` suppresses all event-sink
exceptions, and `gap_handoff.py:300` silently falls back when provider
phrasing fails. Keep the "never break the turn" property; lose the silence.

### Tasks

1. In `ObserverService`: count sink failures per session. On the first
   failure, emit a WARNING the user can see (stderr notice via the existing
   notice path, not just `logger.exception`). After N consecutive failures
   (suggest N=3), disable that sink for the rest of the session and say so
   once — a flapping sink should not spam.
2. Record sink degradation in the session's NDJSON index entry
   (`sessions.jsonl`) so monitors can tell a complete event log from a
   truncated one.
3. In `gap_handoff.py`: when `make_provider_phraser` falls back (exception or
   `_sanitize_phrased_message` rejection), log the reason at WARNING with the
   rejection category (exception / json-shaped / fenced / empty / too-long).
   The user-facing fallback behavior stays the same.

### Tests

1. A sink that raises once: turn completes, WARNING notice emitted, sink
   stays enabled.
2. A sink that raises 3 times consecutively: sink disabled, one
   disabled-notice, subsequent events do not call it.
3. Sink degradation appears in the sessions index entry.
4. Phraser returning JSON-shaped output: fallback message used, WARNING
   logged with category `json-shaped`.

### Done Criteria

- No silent audit-trail loss: every suppressed failure leaves a user-visible
  or index-visible trace.
- A turn still never fails because of a sink or phrasing failure.

## Stage 5: Migration Safety Rails

### Goal

`history.py:1382` (`_migrate_to_v6`) rebuilds `assistant_plans` to change a
unique constraint. Migrations run automatically against the user's real
history DB with no backup and no post-check. Add safety rails for v6 and
every future migration.

### Tasks

1. Before running any migration chain, copy the SQLite file to
   `<db>.pre-v<target>.bak` (cheap, file-level). Remove or rotate old
   backups; keep the most recent one only.
2. Wrap the whole migration chain in one transaction so a mid-chain failure
   cannot leave a half-migrated schema.
3. After `_migrate_to_v6`'s table rebuild, validate row counts: rebuilt table
   row count must equal the source count. On mismatch, roll back and raise
   with the backup path in the message.
4. Apply the same count-validation pattern to any future rebuild-style
   migration (note it in a comment at the migration dispatcher).

### Tests

1. Synthetic v5 DB fixture migrates to v6 with all rows preserved (count and
   spot-check content).
2. A sabotaged rebuild (fixture with a row the new constraint rejects) rolls
   back, raises with the backup path, and leaves the original DB readable.
3. Backup file exists after a successful migration.

### Done Criteria

- A failed migration can never destroy history: either it completes verified,
  or the original DB and a backup survive.

## Stage 6: Diff Applier Strictness Decision

### Goal

The unified-diff applier (`file_service.py` ~118–257) is lenient: bare lines
parse as context, and newline normalization lets CRLF/LF mismatches succeed
silently. Some leniency is deliberate (model-generated diffs are imperfect);
the problem is that it is undocumented and unbounded. Decide the contract,
then enforce it at parse time.

### Tasks

1. Decide and document per quirk: bare lines as context (keep — models drop
   the leading space often — but count them), CRLF/LF normalization (keep,
   but only as a fallback after an exact match fails), anything else found
   while reading the parser.
2. Reject at parse time what is never valid: hunks whose declared counts
   disagree with their body, hunks with no `+`/`-` lines at all.
3. Surface leniency: when a diff applies only via a fallback (normalized
   newlines, bare-line context), include that fact in the execution artifact
   so it lands in the trace.

### Tests

1. Hunk with wrong declared counts → parse-time `FileOperationError`, not a
   match-time failure.
2. CRLF file + LF diff → applies, artifact notes normalized matching.
3. Existing apply_diff tests pass unchanged.

### Done Criteria

- The applier's leniency is documented in the module docstring, bounded, and
  visible in traces when exercised.

## Stage 7: Test Depth Where It Is Thin

### Goal

Coverage is strong on the orchestrator and providers but thin exactly where
failures are most likely in daily use: planner prompt/validation logic, the
Codex provider's failure paths, and live rendering edge cases.

### Tasks

1. New `tests/test_planner.py` exercising `PlannerService` in isolation with
   `StubProvider`: observation injection (iteration, remaining-actions),
   plan-time endpoint validation (`builtin.file.*`/`builtin.git.*` rejection
   of unknown endpoints), zero-action plan repair, deferred-write
   materialization, and the stage-2 union-validation repair path.
2. Codex provider failure paths in `tests/test_provider.py`: `codex` binary
   missing, login/auth-expired stderr shape, malformed/non-JSON output, and
   timeout — each mapping to the right `ProviderErrorCode`.
3. Live rendering edges in `tests/test_live_turn.py`: narrow terminal widths
   (20 cols), spinner animation state preserved across `Live` refreshes,
   stale-phase rendering, and ANSI-bearing event text.

### Tests

This stage is tests; the gate is meaningfulness, not count. Each new test
must assert behavior (output shape, error code, rendered text), not
implementation strings.

### Done Criteria

- `planner.py` edge cases are debuggable without running the orchestrator.
- Every `ProviderErrorCode` the Codex adapter can emit has a test.
- Full suite still passes; ruff and mypy stay green.

## Stage 8: Orchestrator Slimming

### Goal

`RequestOrchestrator` (`orchestrator.py`) takes 14 constructor parameters and
owns ~40 methods, several of which are presentation or provider-salvage
concerns. Shrink it incrementally — no behavior change, no big-bang rewrite.

### Tasks

1. Group the 14 constructor parameters into one (or two) frozen context
   objects (e.g. `OrchestratorRuntime` holding services, policy, stores).
   Update call sites and test factories; keep keyword compatibility shims out
   — fix the call sites instead.
2. Move provider-output salvage (`_unwrap_generated_file_body`, ~290–321)
   into `planner.py` next to the other plan-repair logic.
3. Move presentation helpers (`_tool_result_preview` ~344,
   `_format_tool_call_log_entry` ~443–463) into the rendering layer
   (`cli_rendering.py`) or a small observation-formatting module — wherever
   their only callers live.
4. Stop if any step requires changing behavior to proceed; this stage is
   strictly mechanical.

### Tests

1. No new tests required; the gate is the existing suite passing unchanged
   plus mypy strict on the new context objects.
2. Test factories (`tests/` orchestrator fixtures) updated to build the
   context object once instead of threading 14 kwargs.

### Done Criteria

- `RequestOrchestrator.__init__` takes ≤ 4 parameters.
- No plan-salvage or presentation code remains in `orchestrator.py`.
- Diff shows moves and signature changes only — no logic edits.

## Stage 9: Docs And Plans Hygiene

### Goal

Make `plans/` trustworthy again: a reader should be able to tell what is
done, what is pending, and what was abandoned.

### Tasks

1. Mark `plans/fcli-fixes-roadmap.md` complete (its stages shipped: command
   error recovery, static gates, live phases). Add a one-line status header
   with the verifying commit.
2. Add status headers to `plans/v2`, `plans/v3`, `plans/v4` roadmaps where
   missing (v3/v4 are done).
3. Add a `CHANGELOG.md` entry for this hardening batch as stages land
   (typed invariants, union validation, audit visibility, migration rails,
   diff strictness).
4. Update `docs/TECHNICAL.md` only where stage 4 (visible degradation
   notices) and stage 5 (migration backups) change user-visible behavior.

### Done Criteria

- Every file in `plans/` states whether it is shipped, in progress, or
  superseded.
- CHANGELOG names the hardening changes without overpromising.

## Cross-Stage Rules

1. One stage per PR; the maintainer merges (agents prepare and hand off).
2. Tests are written before implementation within each stage.
3. Stages 1–5 are strictly ordered. Stages 6–7 may land in any order after 5.
   Stage 8 lands only after 1–7 (it moves code the earlier stages touch).
   Stage 9 closes the batch.
4. No stage adds dependencies, new capabilities, or new UX surfaces.

## Final Completion Gate

The hardening batch is complete when all of these pass on `main`:

```bash
./scripts/uv run ruff check src tests
./scripts/uv run ruff format --check src tests
./scripts/uv run mypy
./scripts/uv run pytest
./scripts/uv run foundation doctor
```

…and additionally:

- `grep -rn "assert " src/foundation/services/executor.py` is empty.
- Exactly one git-mutation subcommand set exists.
- A sabotaged-migration test proves history survives a failed migration.
- `plans/` has no unmarked completed roadmaps.
