# Changelog

All notable changes to Foundation CLI are documented here. This project follows
semantic-ish versioning: feature releases bump the minor version; bug fixes and
small enhancements land on patch releases.

## [Unreleased] — hardening batch (2026-06)

Closes the gaps found in the 2026-06-10 full-project review (see
`plans/fcli-hardening-roadmap.md` for stage-by-stage detail and the findings
that were checked and rejected).

### Changed

- **Executor invariants fail loudly.** All 16 `assert` statements in the
  action executor (kind/payload narrowing, file/git service wiring) were
  replaced with a typed `ExecutorInvariantError`; violations now surface as
  FAILED execution results in the trace instead of interpreter crashes, and
  survive `python -O`.
- **Plan-action validation closed its last holes.** A stray `question`
  payload on EXPLANATION/SHELL/TOOL_CALL actions (and a stray `explanation`
  on QUESTION actions) is now rejected at validation time and routed through
  the existing plan-repair retry.
- **One source of truth for git mutation subcommands.**
  `GIT_MUTATION_SUBCOMMANDS` lives in `models/git.py`; the planner and the
  guardrail policy engine both alias it, so they can no longer diverge.
- **Audit-trail failures are visible.** Event-sink failures are counted and
  warned about; a sink failing 3 consecutive times is disabled with one
  final warning instead of spamming. A crash inside the NDJSON event-log
  writer now marks the session `write_truncated` in `sessions.jsonl` instead
  of letting the index claim a complete log. Gap-message phrasing fallbacks
  log their reason (provider-error / empty / json-or-fenced / plan-shaped).
- **History migrations have safety rails.** Before any schema migration the
  database file is backed up to `<db>.pre-v<target>.bak` (newest kept). The
  v6 rebuild validates row counts before dropping the source table; failures
  raise `HistoryMigrationError` naming the backup, with the original data
  intact.
- **Diff applier leniency is bounded and reported.** Hunks whose declared
  source-line count disagrees with their body, and hunks with no additions
  or removals, are rejected at parse time. Bare context lines and
  newline-normalized matching remain accepted but are reported through
  `FileMutationResult.leniency_notes` into the trace.

### Fixed

- A plan naming a nonexistent capability id crashed the turn with an
  unwrapped `ValueError`; it now routes through the plan-repair retry.
- The live detail panel parsed the user's request text as Rich markup,
  allowing styling injection and a `MarkupError` crash; it renders literally
  now.

### Added

- 100+ new tests, including isolated `PlannerService` unit tests
  (`tests/test_planner.py`), Codex provider failure-path coverage, live
  rendering edge cases, sink failure/circuit-breaker tests, and migration
  backup/sabotage tests.

## [0.2.0] — unreleased (v3)

v3 makes `foundation` behave like a real coding-agent shell on top of the v2
conversational runtime. The entrypoint is unified, typed file and git
capabilities replace raw shell mutations, one user turn can iterate through
read/edit/run/fix cycles inside a bounded replan loop, and multi-iteration runs
remain inspectable both in concise chat output and the full trace store.

### Added

- **Primary agent entrypoint.** `foundation` with no request starts or resumes
  the interactive agent shell; `foundation <request>` runs a one-shot turn.
  Admin subcommands (`run`, `tools`, `history`, `trace`, `config`, `doctor`)
  keep precedence. `foundation chat` is a strict alias.
- **Typed file capabilities** — `foundation.file.{read,read_chunk,write,edit,apply_diff}`.
  Atomic writes with sha256 conflict detection and a pure-Python unified-diff
  applier that aborts all hunks if any one fails.
- **Typed git capabilities** — `foundation.git.{status,diff,show,log,stage,unstage,commit}`.
  Workspace-confined, porcelain v2 status parsing. Stage and unstage are
  auto-allowed; `commit` requires approval and never stages implicitly.
- **Bounded replan loop.** Hard caps of 32 planning iterations × 40 actions per
  iteration × 200 total actions per user turn, with six explicit stop reasons.
- **Iteration-aware trace model.** `PlanningStep` and `ExecutionStep` carry
  `iteration_index`; step ids are scoped as `planning:{req}:{iter}` and
  `action:{req}:{iter}:{action_id}`; `TraceEdgeKind.REPLANNED_FROM` links the
  last execution step of one iteration to the next planning step.
- **Concise iteration notices.** Multi-iteration turns surface changed-files,
  commands-run, verification outcome, and approval-required notices in
  concise mode; verbose mode still shows the full plan/execution detail.
- **Verification outcome taxonomy.** `VerificationOutcome` distinguishes
  `PASSED` / `FAILED` / `UNAVAILABLE` / `NOT_ATTEMPTED` so missing binaries
  and failing tests don't get misreported as successful verification.
- **Doctor approval-boundary visibility.** `foundation doctor` prints risk
  class, trust tier, and declared side effects for every capability.

### Changed

- **Planner instructions.** Prefer typed file/git capabilities over shell
  mutation commands; shell remains the home of verification runs
  (tests, builds, linters) and environment inspection.
- **Live turn status.** The inline live renderer now tracks explicit phases
  and last-event age, so long turns distinguish planning, tool execution,
  observation, approval/input waits, stale event periods, and terminal states.
- **Provider hardening.** Ollama adapter only sends `think=true` for Qwen 3.x
  structured-output calls; other thinking models (e.g. `deepseek-v3.2:cloud`)
  are no longer misrouted. Structured-JSON responses no longer fall back to
  the `thinking` field, so reasoning narrative can't be parsed as a plan.
- **Ollama role mapping.** OpenAI-style `developer` role is mapped to `system`
  before hitting Ollama's chat endpoint.

### Fixed

- **Command usage recovery.** Repeated command invocation errors such as
  unsupported flags are no longer framed as capability gaps. FCLI now feeds the
  concrete stderr back to the planner for one repair attempt and, if still
  unrepaired, shows the failed command and stderr in the final message.
- **GitHub CLI planning.** Plans using `gh api ... -r` are rejected before
  approval or execution because `gh api` does not support jq's standalone raw
  output flag.
- **Recovered command errors.** An early invalid command no longer pollutes the
  final assistant message after a later iteration recovers and finishes
  successfully.
- **Deferred file writes.** Planner shape hints no longer advertise the
  internal `_file_write_note` placeholder as a real tool-call field. If a model
  still returns that older malformed shape, FCLI converts it to
  `arguments.content_brief` instead of failing the turn during plan repair.
- **Read-only loop detection.** Repeated successful read/search actions with
  identical arguments now stop as no-progress instead of growing the planner
  prompt until the provider fails.
- **Static quality gates.** Formatting and strict mypy checks are clean again.

### Migration

- **History database schema v4 → v5.** First open of an existing database
  runs `_migrate_to_v5`: `replan` edges are rewritten to `replanned_from`,
  and older trace step records without `iteration_index` load with the
  default value of 1. Migration is idempotent and runs automatically — no
  user action required.

### Out of scope (v3)

- Binary file editing.
- Networked git (push, fetch, pull, PR automation).
- Additional generic shell capabilities beyond `foundation.shell.command`.
- Full replay or branchable rerun UX.
- Replacing the interactive shell with a full-screen TUI.

## [0.1.0]

Initial release covering the v1 roadmap: scaffolding, CLI surface + config,
shell runtime, local tooling, provider adapter + orchestrator, SQLite
history, guardrails and approvals, interactive REPL, structured logging,
and the v2 additions (conversational brain, capability registry, policy
engine, trace + audit, concise chat surface).
