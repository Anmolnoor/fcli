# Task Outcome — Task 02: Bug Fix (config UX)

## Model

Model/tool: Claude Code (claude-opus-4-7, 1M context)
Task number: 02
Task name: Config error UX — missing/invalid file
Repo/worktree: /Users/anmolnoor/Developer/fcli-claude (branch: claude)
Base commit: 477c706f2a9e9c80809f27a2f62d0017aea9a257

## Time

Start time: ~2026-05-16 16:02:30
End time: ~2026-05-16 16:10:00
Total time: ~8 minutes (one false-start on test wrapping, recovered quickly)

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
- Reproduced the bug by invoking `foundation --config /tmp/definitely_missing.toml config show` — it exited 0 with `config_exists: false` and defaults, silently ignoring the user's explicit `--config`.
- Added an opt-in `require_existing: bool` parameter to [load_settings](src/foundation/settings.py:649) and [_validate_config_file](src/foundation/settings.py:609); when set, a missing path raises a clear `SettingsLoadError` instead of silently falling back.
- The CLI's [_load_runtime_settings](src/foundation/cli.py:379) now passes `require_existing=True` whenever the user gave `--config` explicitly (`cli_context.config_path is not None`). When no `--config` was supplied, the default-path-missing case still falls back to defaults — that's still correct behavior.
- Tightened the messages in `_validate_config_file` to be actionable: each error now ends with a `Fix:` line pointing at the most likely remedy.
- Library callers and existing tests that intentionally point `load_settings` at a tmp-path stub for hermetic isolation keep working unchanged — only the CLI flips on strict mode.

Files changed:
- [src/foundation/settings.py](src/foundation/settings.py) — added `require_existing` flag, raise on missing+explicit, friendlier `Fix:` lines on all three SettingsLoadError sites.
- [src/foundation/cli.py](src/foundation/cli.py:387) — single line: pass `require_existing=cli_context.config_path is not None`.
- [tests/test_settings.py](tests/test_settings.py) — added two new tests; extended existing invalid-TOML test to verify the new `Fix:` hint.
- [tests/test_cli.py](tests/test_cli.py) — added two CLI-level tests (`test_cli_errors_when_explicit_config_path_missing`, `test_cli_errors_when_explicit_config_path_invalid`).

Diff stat:
```
 src/foundation/cli.py      |  1 +
 src/foundation/settings.py | 26 ++++++++++++++++++++++----
 tests/test_cli.py          | 25 +++++++++++++++++++++++++
 tests/test_settings.py     | 28 +++++++++++++++++++++++++++-
 4 files changed, 75 insertions(+), 5 deletions(-)
```

Commands run:
- `./scripts/uv run pytest tests/test_settings.py tests/test_cli.py -q` — 76 passed
- `./scripts/uv run pytest -q` — **384 passed**
- `./scripts/uv run ruff check src tests` — All checks passed
- `./scripts/uv run ruff format src/foundation/settings.py` (auto-reformat after edit)
- `./scripts/uv run ruff format --check src tests` — clean

Tests/build/lint result: full suite 384 / 384 green, ruff clean, formatter clean.

## Acceptance Criteria Check

- [x] Invalid config does not crash with a raw stack trace — both pre-existing `SettingsLoadError` capture in `_load_runtime_settings` and the new `Fix:` suffix together produce a clean console message and exit 1.
- [x] Missing config shows a helpful message — when `--config` is explicit. New error names the file, explains why falling back would be unsafe, and lists three concrete remediations.
- [x] Existing behavior stays the same for valid config — verified: `foundation --config <valid-toml> config show` still emits the JSON payload exit 0, and library callers passing missing paths without `require_existing=True` still get the silent-fallback behavior they relied on. All 384 pre-existing tests pass unchanged.
- [x] Adds or updates tests — 4 new tests across `test_settings.py` and `test_cli.py` covering: explicit missing → error, explicit present → ok, invalid TOML → `Fix:` hint, CLI flow for both error paths.
- [x] Runs relevant test/build/lint command — pytest + ruff check + ruff format check all clean.

## Problems

Hallucinated files/functions/modules: none.
Over-engineered: no. The change is one opt-in flag and one CLI call-site; library callers untouched.
Broke existing behavior: no — 384 tests pass; the silent-fallback semantics remain available for library callers that need them.
Needed manual help: no.
Got stuck: brief stumble — first iteration of one CLI test asserted the full `str(missing)` path appears in `result.stdout`, but Rich wrapped the path with `...` to fit console width. Fixed by asserting on `missing.name` (filename only). Single retry, no human intervention.

## Score

| Metric | Weight | Score | Notes |
|---|---:|---:|---|
| Correctness | 30 | 29 | Acceptance criteria all met; the explicit-vs-implicit distinction is the right semantic |
| Tests/build pass | 15 | 15 | Full suite 384 / 384 green; ruff clean |
| Minimal clean diff | 15 | 15 | 70 net lines across 4 files; no unrelated edits |
| Architecture fit | 15 | 15 | Used the existing `SettingsLoadError` flow; respected the library/CLI boundary |
| Explanation quality | 10 | 9 | Concrete repro + remediation listed |
| Needed less babysitting | 10 | 9 | One self-recovered test fix (Rich wrapping) |
| Caught risks/security | 5 | 4 | Improved error messaging surfaces the silent-fallback risk explicitly |
| **Total** | **100** | **96** | |

## Merge Decision

Would I merge this? **Yes.**

Reason: Small, additive change with a clear user-facing improvement, full backward compatibility for library callers, and four new tests that lock in the new behavior. No coupling to anything else.
