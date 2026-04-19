# Changelog

All notable changes to Foundation CLI are documented here. This project follows
semantic-ish versioning: feature releases bump the minor version; bug fixes and
small enhancements land on patch releases.

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
- **Bounded replan loop.** Hard caps of 4 planning iterations × 5 actions per
  iteration × 20 total actions per user turn, with six explicit stop reasons.
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
- **Provider hardening.** Ollama adapter only sends `think=true` for Qwen 3.x
  structured-output calls; other thinking models (e.g. `deepseek-v3.2:cloud`)
  are no longer misrouted. Structured-JSON responses no longer fall back to
  the `thinking` field, so reasoning narrative can't be parsed as a plan.
- **Ollama role mapping.** OpenAI-style `developer` role is mapped to `system`
  before hitting Ollama's chat endpoint.

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
