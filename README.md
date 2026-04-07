# Foundation CLI

Foundation CLI is a local-first, shell-native assistant that follows an explicit `plan -> approve -> execute -> observe` loop. This repository now includes the Stage 6 foundation, the Stage 00 bridge for persistent conversational sessions, the v2 Stage 1 capability registry, the v2 Stage 2 capability policy engine, the v2 Stage 3 trace and audit foundation, and the v2 Stage 4 quiet chat surface: typed configuration, config inspection and validation commands, environment readiness checks, a real shell runtime with buffered, streaming, and PTY-backed execution, typed local-context tooling, model adapters for OpenAI and Ollama, a one-shot orchestrator split across planner, executor, and observer responsibilities, a local capability store with built-in manifest seeding and health checks, capability-aware approval flows, executor-side policy constraints, SQLite-backed history plus step-level trace persistence, trace and audit inspection commands, and an interactive `foundation chat` session shell with layered memory, resume support, and concise-by-default chat rendering.

## Requirements
- Python 3.12
- repo-local `uv` bootstrap via `./scripts/bootstrap.sh`
- `pip` works as a fallback if you want to manage the virtualenv manually

## Quickstart
### Verified bootstrap
```bash
./scripts/bootstrap.sh
./scripts/uv run foundation --help
./scripts/uv run pytest
```

`./scripts/uv run ...` defaults to `UV_NO_SYNC=1`, so repeat verification commands keep using the
bootstrapped environment instead of trying to rebuild from PyPI. After dependency changes, refresh
the environment explicitly with `./scripts/uv sync --extra dev`, or set
`FOUNDATION_UV_RUN_SYNC=1` when you intentionally want `uv run` to perform a sync.

### With venv + pip
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
foundation --help
pytest
```

## Useful Commands
```bash
./scripts/uv run ruff check .
./scripts/uv run ruff format --check .
./scripts/uv run mypy src tests
./scripts/uv run pytest
./scripts/uv run python -m coverage run -m pytest
./scripts/uv run python -m coverage report
```

## Stage 8 Developer Workflow
```bash
./scripts/uv run ruff check src tests
./scripts/uv run ruff format --check src tests
./scripts/uv run mypy src
./scripts/uv run pytest
./scripts/uv run pytest -m "asyncio"
./scripts/uv run python -m coverage run -m pytest
./scripts/uv run python -m coverage report
```

Use this loop while implementing changes:
1. `ruff check src tests`
2. `ruff format src tests` while editing, or `ruff format --check src tests` when verifying
3. `mypy src`
4. `pytest`
5. `python -m coverage run -m pytest && python -m coverage report`

## Stage 8 Benchmark Guidance
Use these commands to spot latency regressions in common paths:
```bash
/usr/bin/time -p ./scripts/uv run foundation --help
/usr/bin/time -p ./scripts/uv run foundation run --mode buffered -- python -c "print('foundation run')"
/usr/bin/time -p ./scripts/uv run foundation tools availability
/usr/bin/time -p ./scripts/uv run foundation doctor
```
For richer startup timing, track import and startup separately:
```bash
./scripts/uv run python -X importtime -m foundation.cli --help
```

## Repository Layout
```text
src/foundation/         Application package
tests/                  Smoke tests for the scaffold
plans/                  Stage-by-stage implementation plans
```

## Current CLI Surface
`foundation chat` now supports both a persistent interactive session shell and the Stage 6 one-shot orchestration flow when a request is passed explicitly.

```bash
foundation run -- pwd
foundation run --mode buffered -- python -c "print('hello')"
foundation run --mode pty -- python -c "import sys; print(sys.stdout.isatty())"
foundation chat
foundation chat --new
foundation chat --resume <session-id>
foundation chat summarize the current git status
foundation chat --render verbose summarize the current git status
foundation chat --plan-only find TODO comments under src
foundation chat --json inspect the workspace root
foundation config
foundation config show
foundation config validate
foundation config locations
foundation tools availability
foundation tools search TODO --path src --json
foundation tools files py --type file
foundation tools git --path .
foundation tools man git
foundation tools tldr git
foundation history
foundation history --json
foundation history --session <session-id>
foundation trace
foundation trace --session <session-id>
foundation trace --session <session-id> --step <step-id> --predecessors --audit
foundation doctor
```

`foundation chat` now resumes the latest compatible interactive session by default when no request is supplied. Use `--new` to start fresh or `--resume <session-id>` to reopen one explicit session. Chat rendering is concise by default in both one-shot and interactive mode; pass `--render verbose` when you want the full assistant panel, plan table, execution panels, and orchestration summary inline. Inside the session, natural-language requests still use the structured planner, `!` runs a direct shell command through the same capability policy and approval flow, session state is checkpointed into SQLite, and slash commands such as `/plan`, `/actions`, `/summary`, `/memory`, `/sessions`, `/resume`, `/compact`, `/model`, `/tools`, `/history`, `/config`, `/cwd`, `/approval`, `/clear`, and `/reset` provide session-local controls. Global memory lives in `~/.config/foundation/FOUNDATION.md`, project memory lives in `<workspace-root>/FOUNDATION.md`, and older turns are compacted into a persisted session summary while recent turns stay in the active prompt window. When a request is supplied explicitly, `foundation chat` gathers local context, asks the configured provider for a structured plan, validates it through Pydantic contracts, evaluates every runnable capability through the Stage 2 policy engine, auto-executes allowed actions, prompts or defers when approval is required, and persists the request, plan, policy evaluations, approvals, outcomes, step-level traces, and causal edges into the history database. The runtime now records a planning step plus per-action execution steps with stable artifact references, manifest fingerprints, policy snapshots, and audit-oriented event records. The planner consumes a local capability snapshot sourced from `<data_dir>/capabilities`, with built-in manifests seeded for search, files, git, local help, and shell-backed execution. Runtime logs route to the configured log file instead of interleaving with the normal chat transcript unless debug logging is enabled. `foundation run` is also audited into history, `foundation history` can list recent sessions or render one session in detail, and `foundation trace` can inspect full traces, one step plus its predecessors, or an audit report. `foundation tools availability` still shows which local binaries are present, and `search`, `files`, and `git` remain available as direct subcommands.

## Configuration
Foundation reads settings in this order:
1. Code defaults
2. TOML config file
3. Paired env file (`foundation.env` next to the active config file)
4. Environment variables
5. Keychain or environment secret resolution
6. Explicit CLI overrides such as `--config`, `--workspace-root`, `--approval-mode`, and `--debug`

Provider selection does not need to live in environment variables. Persist `provider.name`,
`provider.model`, `provider.base_url`, and `provider.request_timeout_seconds` in the TOML config,
or override them per invocation with `--provider`, `--model`, `--base-url`, and
`--provider-timeout`. Foundation CLI currently supports:
- `openai` via the OpenAI Responses API
- `ollama` via the Ollama Chat API, including local Ollama at `http://localhost:11434/api` and Ollama Cloud at `https://ollama.com/api`

The environment variable path is mainly for credentials unless you prefer using a keychain entry.
When present, `foundation.env` next to the active config file is loaded automatically before
process environment variables, so real exported env vars still win.

Example `~/.config/foundation/config.toml`:

```toml
[app]
workspace_root = "~/Developer/fcli"
data_dir = "~/.local/share/foundation"
state_dir = "~/.local/state/foundation"
log_dir = "~/.local/state/foundation/logs"

[provider]
name = "openai"
model = "gpt-5-mini"
api_key_env_var = "OPENAI_API_KEY"

[provider.api_key_keychain]
service = "foundation"
username = "openai_api_key"

[shell]
default_timeout_seconds = 300
max_timeout_seconds = 3600

[approval]
mode = "prompt"
```

Example Ollama Cloud config:

```toml
[provider]
name = "ollama"
model = "gpt-oss:120b-cloud"
base_url = "https://ollama.com/api"
api_key_env_var = "OLLAMA_API_KEY"

[provider.api_key_keychain]
service = "foundation"
username = "ollama_api_key"
```

Representative environment overrides:

```bash
FOUNDATION_APP__WORKSPACE_ROOT=/tmp/workspace
FOUNDATION_LOGGING__LEVEL=DEBUG
FOUNDATION_APPROVAL__MODE=manual
FOUNDATION_PROVIDER__MODEL=gpt-5
OLLAMA_API_KEY=your-ollama-cloud-api-key
```

Representative CLI overrides:

```bash
foundation --model gpt-5 chat summarize the current git status
foundation --base-url https://api.openai.com/v1 --provider-timeout 90 doctor
foundation --provider ollama --model gpt-oss:20b chat inspect the workspace root
foundation --provider ollama --model gpt-oss:120b-cloud --base-url https://ollama.com/api doctor
```

`foundation config show` prints the effective configuration without exposing secret values, including the resolved provider base URL. `foundation doctor` checks Python version, config readability, required directories, provider credential lookup health, history and log readiness, and capability registry health for the seeded built-ins.

## Known Limitations
- The persistent chat shell is still a bridge stage on top of the v1 runtime. Built-in tools now flow through the Stage 3 trace-aware runtime, but external-service and user-authored capabilities are still modeled locally before later stages make them executable.
- Approval decisions are per-action and per-invocation. Persistent allowlists or reusable approval rules are not implemented yet.
- Secret lookup is read-only in Stage 2. The CLI can validate and consume keychain or environment credentials, but it does not write credentials yet.
- `foundation doctor` reports missing-but-creatable directories as warnings rather than mutating the filesystem.
- `foundation tools files` depends on `fd` or `fdfind`, and `foundation tools tldr` depends on a local TLDR client. Missing binaries are reported clearly but are not installed automatically.

## Development Notes
- `foundation --help` is the primary smoke check for the CLI entrypoint.
- `src/foundation/settings.py` owns the typed Stage 2 configuration model and precedence rules.
- `src/foundation/logging.py` provides a small stdlib logging baseline that later stages can replace or expand.
- `src/foundation/services/tools.py` owns the typed Stage 4 local-context wrappers and shared ignore-rule filtering.
- `src/foundation/services/provider.py` owns the Stage 5 provider adapter contract plus the OpenAI and Ollama implementations.
- `src/foundation/services/planner.py`, `src/foundation/services/executor.py`, and `src/foundation/services/observer.py` split the Stage 3 runtime into planning, execution, and audit responsibilities.
- `src/foundation/services/orchestrator.py` coordinates the structured planning, approval, execution, and audit loop across those services.
- `src/foundation/services/history.py` owns the Stage 6 SQLite persistence, trace storage, audit queries, and history views.
- `src/foundation/services/guardrails.py` owns the Stage 2 capability policy engine and shell-backed capability classification.
- `src/foundation/services/approval.py` owns Stage 2 capability-aware prompt/manual/auto approval resolution.
- `./scripts/uv` wraps the project-local `uv` binary, pins `UV_CACHE_DIR` to `.uv-cache/`, and defaults `uv run` to `UV_NO_SYNC=1` so verification stays reliable in restricted environments.
