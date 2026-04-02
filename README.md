# Foundation CLI

Foundation CLI is a local-first, shell-native assistant that follows an explicit `plan -> approve -> execute -> observe` loop. This repository now includes Stage 6 of the roadmap: typed configuration, config inspection and validation commands, environment readiness checks, a real shell runtime with buffered, streaming, and PTY-backed execution, typed local-context tooling, a first model adapter and one-shot orchestrator, plus SQLite-backed history, approval flows, and workspace guardrails.

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

## Repository Layout
```text
src/foundation/         Application package
tests/                  Smoke tests for the scaffold
plans/                  Stage-by-stage implementation plans
```

## Current CLI Surface
Stage 6 keeps `foundation chat` as a one-shot orchestration command while the fully interactive REPL still waits for Stage 7.

```bash
foundation run -- pwd
foundation run --mode buffered -- python -c "print('hello')"
foundation run --mode pty -- python -c "import sys; print(sys.stdout.isatty())"
foundation chat summarize the current git status
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
foundation doctor
```

`foundation chat` now gathers local context, asks the configured provider for a structured plan, validates it through Pydantic contracts, auto-executes allowed actions, prompts or defers when approval is required, and persists the request, plan, approvals, and outcomes into the history database. `foundation run` is also audited into history, and `foundation history` can list recent sessions or render one session in detail. `foundation tools availability` still shows which local binaries are present, and `search`, `files`, and `git` remain available as direct subcommands.

## Configuration
Foundation reads settings in this order:
1. Code defaults
2. TOML config file
3. Environment variables
4. Keychain or environment secret resolution
5. Explicit CLI overrides such as `--config`, `--workspace-root`, `--approval-mode`, and `--debug`

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

Representative environment overrides:

```bash
FOUNDATION_APP__WORKSPACE_ROOT=/tmp/workspace
FOUNDATION_LOGGING__LEVEL=DEBUG
FOUNDATION_APPROVAL__MODE=manual
```

`foundation config show` prints the effective configuration without exposing secret values. `foundation doctor` checks Python version, config readability, required directories, provider credential lookup health, and Stage 4 tool binary availability.

## Known Limitations
- `foundation chat` is a one-shot Stage 6 command, not the interactive REPL planned for Stage 7.
- Approval decisions are per-action and per-invocation. Persistent allowlists or reusable approval rules are not implemented yet.
- Secret lookup is read-only in Stage 2. The CLI can validate and consume keychain or environment credentials, but it does not write credentials yet.
- `foundation doctor` reports missing-but-creatable directories as warnings rather than mutating the filesystem.
- `foundation tools files` depends on `fd` or `fdfind`, and `foundation tools tldr` depends on a local TLDR client. Missing binaries are reported clearly but are not installed automatically.

## Development Notes
- `foundation --help` is the primary smoke check for the CLI entrypoint.
- `src/foundation/settings.py` owns the typed Stage 2 configuration model and precedence rules.
- `src/foundation/logging.py` provides a small stdlib logging baseline that later stages can replace or expand.
- `src/foundation/services/tools.py` owns the typed Stage 4 local-context wrappers and shared ignore-rule filtering.
- `src/foundation/services/provider.py` owns the Stage 5 provider adapter contract and the first OpenAI responses implementation.
- `src/foundation/services/orchestrator.py` owns the structured planning, approval, execution, and audit loop.
- `src/foundation/services/history.py` owns the Stage 6 SQLite persistence and history queries.
- `src/foundation/services/guardrails.py` owns Stage 6 shell-action classification and workspace guardrails.
- `src/foundation/services/approval.py` owns Stage 6 prompt/manual/auto approval resolution.
- `./scripts/uv` wraps the project-local `uv` binary, pins `UV_CACHE_DIR` to `.uv-cache/`, and defaults `uv run` to `UV_NO_SYNC=1` so verification stays reliable in restricted environments.
