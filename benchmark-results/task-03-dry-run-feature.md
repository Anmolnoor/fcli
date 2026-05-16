# Task Outcome — Task 03: --dry-run Feature

## Model

Model/tool: Claude Code (claude-opus-4-7, 1M context)
Task number: 03
Task name: Add a --dry-run flag to the CLI
Repo/worktree: /Users/anmolnoor/Developer/fcli-claude (branch: claude)
Base commit: 477c706f2a9e9c80809f27a2f62d0017aea9a257 (Task 02 commit 0bf6d7b stacked on top)

## Time

Start time: ~2026-05-16 16:10:00
End time: ~2026-05-16 16:18:00
Total time: ~8 minutes

## Token / Usage Count

Input tokens: Not available
Output tokens: Not available
Total tokens: Not available
Usage source: Not available

## Prompts

Initial prompt count: 1
Follow-up prompt count: 0
Total prompt count: 1

## Result Summary

What was done:
- Added a **global** `--dry-run` flag to the top-level Typer callback in [src/foundation/cli.py](src/foundation/cli.py). Carried it on `CLIContext.dry_run` so any subcommand can read it.
- `foundation run`: when dry-run is set, prints the resolved `command:` / `cwd:` / `mode:` / `timeout:` (+ `env:` if any), then returns. **No history-store session is started, no shell is spawned.**
- `foundation chat` / one-shot agent invocation: when dry-run is set, force `plan_only=True` and print a yellow `DRY RUN — planning only; no actions will be executed.` notice before the existing chat flow. This reuses the long-standing `--plan-only` semantic instead of inventing a new code path.
- Other admin commands (`config`, `doctor`, `tools`, `history`, `trace`) are already read-only, so they accept the flag and behave normally without extra wiring.

Design choice: I deliberately did not invent a new "dry-run mode" for the orchestrator. The orchestrator already has a typed `plan_only` flag that means "produce and persist a plan but skip side-effecting actions". The right architectural move was to map the new global flag onto that existing concept rather than fork the loop. That keeps the surface small and reuses the policy + planning paths verbatim.

Files changed:
- [src/foundation/cli.py](src/foundation/cli.py) — added the `--dry-run` option on the callback (with help text describing per-subcommand behavior), the `dry_run: bool` field on `CLIContext`, the dry-run early-exit branch in `run`, and the dry-run → `plan_only=True` mapping plus logging field in `chat`.
- [tests/test_cli.py](tests/test_cli.py) — two new tests:
  - `test_run_dry_run_does_not_execute` writes a side-effect Python script and verifies `foundation --dry-run run --` prints the `DRY RUN` preview, exits 0, **and the side-effect file is never created**.
  - `test_chat_dry_run_implies_plan_only` patches `_execute_chat_request` and asserts the `plan_only` kwarg is `True` whenever `--dry-run` is set, plus the `DRY RUN` notice appears.

Diff stat:
```
 src/foundation/cli.py | 41 +++++++++++++++++++++++++++++--
 tests/test_cli.py     | 68 +++++++++++++++++++++++++++++++++++++++++++++++++++
 2 files changed, 107 insertions(+), 2 deletions(-)
```

Commands run:
- `./scripts/uv run pytest tests/test_cli.py -q -k "dry_run or test_run_executes"` — 3 passed
- `./scripts/uv run pytest -q` — **386 passed** (up from 384, +2 for the new tests)
- `./scripts/uv run ruff check src tests` — All checks passed
- `./scripts/uv run ruff format --check src tests` — clean

Tests/build/lint result: 386 / 386 green; ruff clean; formatter clean.

## Acceptance Criteria Check

- [x] Command parses `--dry-run` — added as a global flag on the Typer callback so it works for any subcommand (`foundation --dry-run run …`, `foundation --dry-run chat …`).
- [x] No file-writing or tool-execution side effects in dry-run mode — `run` returns before `_build_history_store`, before `_build_shell_runtime`, and before any `runtime.execute(…)`. `chat` routes through the existing `plan_only=True` path which is already audited as preview-only. The new test physically asserts no marker file is created.
- [x] Output clearly shows what would have happened — `run` prints a four-line preview (command/cwd/mode/timeout); `chat` uses the existing plan-only render path; both prefix with a yellow `DRY RUN` notice.
- [x] Implementation follows current CLI architecture — global flag on the existing `CLIContext`; reuses `_resolve_cli_request_cwd`, `shlex.join`, `console.print`, and the existing `plan_only` orchestrator semantic. No new module, no new abstraction.
- [x] Adds tests — two CLI-level tests covering both side-effecting subcommands.
- [x] Runs relevant test/build/lint — pytest + ruff check + ruff format check all clean.

## Problems

Hallucinated files/functions/modules: none.
Over-engineered: no. Resisted creating a new `DryRunOrchestrator` or a new ExecutionMode — mapping to the existing `plan_only` is the right call.
Broke existing behavior: no — all 384 pre-existing tests still pass; the new flag is opt-in.
Needed manual help: no.
Got stuck: no.

Minor noted-but-not-changed item: lines 3188 and 3209 in `cli.py` re-bind `cli_ctx` after I introduced a top-level `cli_ctx` at the top of `chat()`. These re-binds are redundant but pre-existed and aren't part of the dry-run scope; cleaning them belongs to a follow-up that touches `chat()` more broadly (see Task 04 candidate).

## Score

| Metric | Weight | Score | Notes |
|---|---:|---:|---|
| Correctness | 30 | 29 | All criteria met; verified no side-effect file is created |
| Tests/build pass | 15 | 15 | Full suite green; ruff clean |
| Minimal clean diff | 15 | 15 | 105 net lines across 2 files; one file edited (cli.py) |
| Architecture fit | 15 | 15 | Reused `plan_only`; global flag on existing `CLIContext`; no new abstractions |
| Explanation quality | 10 | 9 | Per-subcommand semantics documented in help text + this report |
| Needed less babysitting | 10 | 10 | Zero follow-ups |
| Caught risks/security | 5 | 4 | Verified no history write + no shell spawn in dry-run; surfaced redundant cli_ctx rebinds as Task 04 candidate |
| **Total** | **100** | **97** | |

## Merge Decision

Would I merge this? **Yes.**

Reason: Small, well-scoped feature. Reuses the existing `plan_only` orchestrator semantic rather than forking a new code path. Two tests lock in the most important invariant (no side effects). Help text documents per-subcommand behavior so the next reader doesn't have to guess.
