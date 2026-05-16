# Task Outcome — Task 07: Long-Horizon Event Ledger

## Model

Model/tool: Claude Code (claude-opus-4-7, 1M context)
Task number: 07
Task name: Implement a basic event ledger for agent actions
Repo/worktree: /Users/anmolnoor/Developer/fcli-claude (branch: claude)
Base commit: 477c706f2a9e9c80809f27a2f62d0017aea9a257 (Task 02–06 commits stacked)

## Time

Start time: ~2026-05-16 16:45:00
End time: ~2026-05-16 17:00:00
Total time: ~15 minutes

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

### Design decision

The repo already has substantial event-tracking infrastructure: a SQLite-backed `HistoryStore`, an NDJSON `EventLogWriter` in `foundation.monitor`, and an `ObserverService` with redaction. A naive interpretation of "implement an event ledger" would duplicate them.

The task brief is specifically about **agent action** events with a narrow shape (`timestamp, actor, action_type, input_summary, output_summary, status`). That's different from the observer's raw event stream (which is `tool_call_started`, `tool_call_finished`, `plan_started`, etc. — much lower level) and from the SQLite history (which is rich and queryable but not stream-friendly for `tail -f` / `grep`).

So the ledger is built as a **third, complementary artifact**: a thin user-facing JSONL file with exactly one record per completed agent action, suitable for `tail`/`grep`/`jq`. It is **not** a replacement for the SQLite store; the two coexist by design.

### Files added/changed

- **Added** [src/foundation/ledger.py](src/foundation/ledger.py) (218 lines after formatter):
  - `LedgerEntry` — Pydantic model with `extra="forbid"`, fields: `schema_version`, `timestamp`, `actor`, `action_type`, `capability_id`, `action_id`, `input_summary`, `output_summary`, `status`, `error`. Module-level `_LEDGER_SCHEMA_VERSION = "1.0.0"` so future additions are visible to consumers.
  - `Ledger` — append-only JSONL writer. Constructor takes `path` and `max_record_bytes` (default 2 KB to stay under POSIX `PIPE_BUF`); ensures parent dir exists; uses a `threading.Lock` for in-process safety. `record()` opens the file in append-binary mode for each write and writes one line atomically. The atomicity invariant (and its 2 KB ceiling) is spelled out in the module docstring.
  - `build_entry(action, result, *, actor="agent", max_summary_chars=200)` — pure constructor. Pulls capability id from `tool_call`, shell command from `shell`, explanation text from explanation actions. Run secrets through two redaction passes: `redact_payload` (dict-key heuristics from `foundation.observability`) and a small regex set (Bearer tokens, OpenAI `sk-…` keys, AWS `AKIA…` access key ids, JWTs).
- **Modified** [src/foundation/services/executor.py](src/foundation/services/executor.py): added an optional `ledger: Ledger | None = None` constructor parameter; if provided, `execute()` records exactly one entry per completed action right before returning. Default is `None`, so the wiring is fully backward-compatible.
- **Added** [tests/test_ledger.py](tests/test_ledger.py) (233 lines) — 9 tests covering JSONL shape, append-only across instances, dict-key redaction, text-level redaction (sk-keys, Bearer tokens, JWTs), oversized-record truncation, `build_entry` for tool_call + shell + failed-action shapes, parent-dir creation.
- **Modified** [tests/test_executor_dispatch.py](tests/test_executor_dispatch.py) — added `test_executor_writes_ledger_entry_per_action` which builds a real `ActionExecutor` with a ledger attached, runs `foundation.git.status`, and asserts exactly one record in the JSONL file with the right `action_id`, `capability_id`, and `status`.

Diff stat (relative to Task 06 commit):
```
 src/foundation/ledger.py            | 218 +++++++++++ (new)
 src/foundation/services/executor.py |   5 +
 tests/test_executor_dispatch.py     |  56 +++
 tests/test_ledger.py                | 233 +++++ (new)
```

Commands run:
- `./scripts/uv run pytest tests/test_ledger.py -q` — 9 passed
- `./scripts/uv run pytest tests/test_executor_dispatch.py tests/test_ledger.py -q` — 18 passed
- `./scripts/uv run pytest -q` — **404 passed** (394 + 9 ledger + 1 dispatch-wiring test)
- `./scripts/uv run ruff check src tests --fix` — 5 errors found, 3 auto-fixed
- `./scripts/uv run ruff format src/foundation/ledger.py tests/test_ledger.py` — 2 files reformatted
- Final `ruff check` + `ruff format --check` — clean.

Tests/build/lint result: 404 / 404 green; ruff clean; formatter clean.

### Demo (manual)

A two-line REPL demonstration of the contract:
```python
from foundation.ledger import Ledger, build_entry
from foundation.models import (
    ActionKind, ExecutionResult, ExecutionStatus, PlannedAction, ToolCall,
)
ledger = Ledger(path="/tmp/foundation_ledger_demo.jsonl")
ledger.record(build_entry(
    PlannedAction(
        id="a1", kind=ActionKind.TOOL_CALL, summary="probe",
        tool_call=ToolCall(
            capability_id="foundation.file.read",
            arguments={"path": "README.md", "api_key": "shhh-12345"},
        ),
    ),
    ExecutionResult(action_id="a1", status=ExecutionStatus.EXECUTED, summary="Read 64 lines."),
))
```
The resulting line is plain JSONL, the `api_key` value is replaced with `[redacted]`, and the file can be `tail`-ed / `jq`-ed without further tooling.

## Acceptance Criteria Check

- [x] **Every tool/action event is recorded** — `ActionExecutor.execute()` writes one entry per completed action whenever a ledger is attached. The new wiring test verifies this end-to-end through the real dispatch path.
- [x] **Events are append-only** — `Ledger.record()` opens with `"ab"` (append-binary) and only writes; it never seeks, truncates, or reads. The `test_ledger_record_is_append_only_across_instances` test verifies that a second `Ledger` instance on the same path preserves the first instance's entries.
- [x] **Event shape is typed or documented** — `LedgerEntry` is a Pydantic model with `extra="forbid"`; the module docstring explains the schema-version contract.
- [x] **Includes timestamp, actor, action type, input summary, output summary, and status** — all six are required fields on `LedgerEntry`; `build_entry` populates each. Plus `capability_id`, `action_id`, `error`, and `schema_version` for downstream filtering.
- [x] **Does not store secrets directly** — two redaction passes (dict-key + text-level regex); both are covered by tests that pass in a fake `sk-…` key, a Bearer token, and a JWT, and assert the original tokens are absent from the output line.
- [x] **Includes a basic redaction/sanitization step** — `_scrub_text` applies a small regex set (Bearer / sk- / AKIA / JWT). The docstring explicitly says "intentionally conservative … not a full DLP layer" so the next reader knows the boundary.
- [x] **Adds tests or a small demo** — 9 new tests in `test_ledger.py` plus 1 wiring test in `test_executor_dispatch.py`; the demo above can be pasted into a Python REPL.
- [x] **Runs relevant tests/build/lint commands** — pytest + ruff check + ruff format check all clean.

## Problems

Hallucinated files/functions/modules: none.
Over-engineered: I considered but rejected (a) a full per-process singleton with signal handlers (the existing `EventLogWriter` already has that complexity at the raw-event layer) and (b) a separate "session_started"/"session_ended" record schema (the ledger is action-level only). The narrow scope keeps the module reviewable.
Broke existing behavior: no. The ledger is opt-in via constructor parameter; default `ledger=None` keeps the existing `ActionExecutor` contract unchanged. All 394 pre-existing tests still pass.
Needed manual help: no.
Got stuck: no. Two formatter passes after the first commit (ruff caught a long line + an import-order issue), both auto-fixed.

Noted but not wired: the ledger is constructed but **not yet plumbed into the CLI** end-to-end (there's no `foundation chat --ledger <path>` flag and no settings field). I deliberately stopped at the `ActionExecutor` boundary because the wiring into `cli.py` would significantly bloat the diff for what's a "basic event ledger" task. The plumbing change is a one-callsite addition in `_build_orchestrator` once a settings field is added, and is the natural follow-up.

## Score

| Metric | Weight | Score | Notes |
|---|---:|---:|---|
| Correctness | 30 | 29 | All criteria met; 10 tests lock the contract; default behavior unchanged |
| Tests/build pass | 15 | 15 | 404 / 404 green; ruff clean |
| Minimal clean diff | 15 | 14 | New module + 5-line executor change + tests; one point off for leaving the cli.py plumbing as follow-up |
| Architecture fit | 15 | 15 | Reused `redact_payload`; complements rather than duplicates the SQLite store and the event log; matches the typed-Pydantic convention used throughout |
| Explanation quality | 10 | 10 | Module docstring explains atomicity invariant, redactor boundaries, and schema-versioning contract |
| Needed less babysitting | 10 | 9 | Two auto-fixable lint issues required a follow-up command |
| Caught risks/security | 5 | 5 | Surfaced the POSIX `PIPE_BUF` concurrency invariant explicitly, capped record size to keep multi-process writers safe, redactor is documented as conservative |
| **Total** | **100** | **97** | |

## Merge Decision

Would I merge this? **Yes** — with the explicit note that the cli.py wiring follow-up is needed before users can opt in from the command line. The library-level surface is complete, typed, tested, and free of breaking changes.

Reason: New module with a clearly-stated contract; the design intentionally complements existing observability (history store + monitor event log) instead of competing with them; concurrency story is explicit; redaction story is honest about its limits.
