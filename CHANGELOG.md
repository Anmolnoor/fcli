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
- **`foundation init` setup wizard.** Interactive (or `--non-interactive`)
  first-run flow that picks the provider, prompts for an API key, and
  writes `~/.config/foundation/config.toml` plus `~/.config/foundation/foundation.env`
  (chmod 600, atomic, preserves other env entries). Flags: `--force`
  (backs the prior config to `config.toml.bak`), `--probe`/`--no-probe`
  (1-token provider ping), `--alias` plus `--alias-name` / `--alias-target`
  / `--shell-rc`. `foundation config init` is an alias for the same wizard.
- **`fcli` shell alias step.** The wizard can install a marker-fenced
  `alias fcli="foundation"` block into the user's shell rc (auto-detected
  from `$SHELL`; bash, zsh, and fish supported). Re-running replaces the
  block in place; previous rc is backed up to `<rc>.bak`. Lines outside
  the fence are never touched.
- **Planning sub-steps in the live UI.** Planner now emits
  `plan_provider_call_started/_finished`, `plan_validation_started`, and
  `plan_repair_attempt` events around the provider call. The live status
  line transitions through `… planning iter N · contacting provider · T`
  → `validating plan` instead of a stuck-looking timer, fixing the
  "is it frozen?" perception on slow providers.
- **`foundation update`.** Detects the install mechanism (pipx,
  pip --user, or dev checkout via `pyproject.toml` sniff) and runs the
  matching upgrade command. `--dry-run`, `--non-interactive`,
  `--ref <branch-or-tag>`. Dev checkouts get a `git pull && uv sync`
  hint instead of self-modification. Best-effort latest-commit-SHA fetch
  from the GitHub API (5 s timeout) so the prompt knows whether anything
  changed.
- **`foundation uninstall`.** Strips the marker-fenced shell alias,
  optionally `--purge --yes` to wipe `~/.config/foundation`,
  `~/.local/share/foundation`, and `~/.local/state/foundation`, then
  prints (or with `--run` execvps into) `pipx uninstall foundation-cli`.
  `--keep-alias`, `--non-interactive`, `--run` flags supported.
- **End-user install scripts.** `scripts/install.sh` (bootstraps pipx via
  `python3.12 -m pip install --user pipx` if missing, then
  `pipx install --force git+https://github.com/Anmolnoor/fcli.git@main`),
  `scripts/update.sh`, and `scripts/uninstall.sh` (mirrors the CLI for
  users without a working `foundation` binary; uses inline Python for
  the marker-fence alias removal so the shell-script path doesn't
  duplicate logic).

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
