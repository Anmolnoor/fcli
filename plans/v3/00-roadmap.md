# Foundation CLI v3 Roadmap

## Status

**v3 definition-of-done met** — all six stages complete; release gates green.
Merge gates (branch `v3-fixes`): pytest 271 passed, ruff clean, mypy clean;
package metadata at `0.2.0`; spec/code/docs agree on bounded loop limits
(32 iterations × 40 actions × 200 total); `history.database_path` defaults under
`app.state_dir` when not explicitly overridden.
See `CHANGELOG.md` for the v0.2.0 release notes.

## Purpose
This planning set defines the v3 upgrade on top of the current v2 Stage 4 baseline. The goal is not to replace the existing conversational runtime, registry, policy engine, trace store, or concise chat surface. The goal is to make that baseline feel like a real coding agent shell by:
- making `foundation` the primary entrypoint,
- replacing shell-based file and git hacks with typed first-class capabilities,
- adding a bounded replan loop so one user turn can read, edit, run, fix, and rerun,
- keeping the default terminal surface concise while preserving full trace detail.

## Baseline Assumptions
v3 starts from the current v2 Stage 4 runtime, which already has:
- persistent chat sessions,
- a capability registry and local store,
- a capability-wide policy engine,
- an observer-driven trace and audit store,
- concise-by-default rendering for normal chat turns.

v3 should rebase on that runtime rather than fork it. New work should preserve the current session model, approval model, history persistence, and trace inspection surfaces unless a stage explicitly upgrades them.

## Planning Artifacts
This v3 plan set is split into these canonical documents:
- `plans/v3/00-roadmap.md`
- `plans/v3/01-shell-entrypoint-and-routing.md`
- `plans/v3/02-file-capabilities-and-safe-text-editing.md`
- `plans/v3/03-native-git-capabilities-and-approval-boundaries.md`
- `plans/v3/04-bounded-replanning-and-coding-loop.md`
- `plans/v3/05-iteration-traces-and-concise-notices.md`
- `plans/v3/06-hardening-and-end-to-end-validation.md`

Implementation should start at the roadmap, then move through the stage files in numeric order.

## Locked Defaults
- `foundation` with no request starts or resumes the interactive agent shell.
- `foundation <request...>` runs a one-shot agent turn.
- Existing admin subcommands keep precedence: `run`, `tools`, `history`, `trace`, `config`, `doctor`.
- `foundation chat ...` remains supported with identical behavior to the new primary entrypoint.
- `foundation.shell.command` remains the only generic shell capability in v3.
- Code-changing behavior should prefer typed file and git capabilities over shell mutation commands whenever those capabilities are available.
- The orchestrator becomes a bounded replan loop with these hard caps:
  - maximum 32 planning iterations per user turn,
  - maximum 40 planned actions per iteration,
  - maximum 200 executed or attempted actions total.
- Concise mode remains the default for interactive and one-shot use.
- Full internal detail remains available through verbose rendering and trace inspection.
- Default approval posture for v3 is:
  - auto-allow workspace file reads,
  - auto-allow workspace file writes, edits, and apply-diff operations,
  - auto-allow shell verification commands,
  - auto-allow workspace git stage and unstage,
  - require approval for git commit,
  - require approval for destructive shell actions, unknown side effects, and all networked actions.

## Out of Scope for v3
- Binary file editing
- Git push, fetch, pull, or PR automation
- A second generic command execution capability
- Full replay or branchable rerun UX
- Replacing the existing interactive shell with a full-screen TUI

## v3 Architecture Delta
v3 adds or upgrades these runtime seams:
- CLI router:
  - unify bare `foundation` and `foundation chat` behind one agent entrypoint
  - preserve admin subcommand precedence
- File service:
  - workspace-bound text reads and mutations with typed inputs, outputs, and conflict handling
- Git service:
  - typed inspect helpers and bounded mutation helpers for stage, unstage, and commit
- Orchestration loop:
  - request context refresh between iterations
  - normalized observation blocks fed back into planning
  - explicit stop reasons and action caps
- Iteration-aware trace model:
  - iteration-scoped step ids
  - replanning edges
  - per-iteration orchestration results
- Concise presenter:
  - final answer plus short operational notices
  - full tables and deep execution detail only in verbose or inspect surfaces

## Stage Sequence
| Stage | Outcome | Blocks Next Stage Until | Primary Artifact |
| --- | --- | --- | --- |
| 1 | `foundation` becomes the primary agent entrypoint without duplicating chat logic | Bare entrypoint, one-shot routing, and `chat` alias parity are deterministic | CLI router |
| 2 | File operations become typed capabilities instead of shell hacks | Safe workspace text reads and atomic edits work with structured errors | File service |
| 3 | Git inspection and mutation become typed capabilities | Status, diff, show, log, stage, unstage, and commit behave through one registry/policy path | Git service |
| 4 | One user turn can iterate through read/edit/run/fix cycles | Replanning loop, observation blocks, and verification rules work within hard caps | Orchestrator loop |
| 5 | Iteration-aware traces and concise notices explain multi-pass runs cleanly | Trace integrity and concise/verbose render parity hold across multi-iteration turns | Trace and presentation |
| 6 | v3 is migration-safe and release-ready | End-to-end coding scenarios and failure-mode coverage pass | Hardening |

## Cross-Stage Rules
Every stage must satisfy these rules before the next stage begins:
1. The previous stage's exit criteria are fully met.
2. New behavior ships behind typed models and structured errors instead of stringly typed ad hoc payloads.
3. Workspace confinement is enforced by the service layer, not only by prompt instructions.
4. File mutations are atomic and traceable.
5. The planner is instructed to prefer typed capabilities over shell mutation commands once those capabilities exist.
6. For code-changing turns, at least one relevant verification command must run before a final zero-action completion unless the final answer explicitly states why verification was unavailable.
7. Normal success output stays concise by default.
8. Approval prompts, explicit failures, and direct `!` shell commands remain operationally visible.

## Cross-Stage Edge Conditions
The stage plans must explicitly handle these recurring edge cases:
- Requests whose first token collides with an admin subcommand name
- Requests running from a subdirectory inside the workspace or inside a nested git repository
- Concurrent file edits that invalidate `expected_sha256` between iterations
- Large or non-text files that should not be read or edited through the new file service
- Multi-file diffs where one failed hunk must abort the entire mutation
- Dirty worktrees that exist before the agent starts making edits
- Verification commands that fail because of code regressions, environment problems, or missing binaries
- Replanning loops that stop due to pending approval, repeated no-progress failures, or hard iteration caps
- Mixed trace history where older v2 records do not yet have iteration metadata

## Definition of Done for v3
Foundation CLI v3 is done when a user can:
- run `foundation` to resume or start the interactive agent shell,
- run `foundation <request...>` for a one-shot coding turn,
- inspect files and edit text files through typed capabilities rather than shell hacks,
- inspect git state and stage or unstage paths through typed capabilities,
- let one request run a bounded read/edit/run/fix cycle inside a single turn,
- see a concise final answer with short notices about changed files, commands run, and verification state,
- inspect a trace that clearly shows each planning iteration and the replanning edges between them,
- stage changes and reach a commit approval prompt without the agent implicitly staging extra files.
