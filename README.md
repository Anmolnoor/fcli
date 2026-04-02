# Foundation CLI

Foundation CLI is a local-first, shell-native assistant that follows an explicit `plan -> approve -> execute -> observe` loop. This repository now includes Stage 3 of the roadmap: a typed configuration system, config inspection and validation commands, environment readiness checks, and a real shell runtime with buffered, streaming, and PTY-backed execution.

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
Stage 3 makes `foundation run` real while `chat` and `history` remain placeholders.

```bash
foundation run -- pwd
foundation run --mode buffered -- python -c "print('hello')"
foundation run --mode pty -- python -c "import sys; print(sys.stdout.isatty())"
foundation chat
foundation config
foundation config show
foundation config validate
foundation config locations
foundation history
foundation doctor
```

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

`foundation config show` prints the effective configuration without exposing secret values. `foundation doctor` checks Python version, config readability, required directories, and provider credential lookup health.

## Known Limitations
- `foundation chat` and `foundation history` are still scaffold commands until later stages land.
- Secret lookup is read-only in Stage 2. The CLI can validate and consume keychain or environment credentials, but it does not write credentials yet.
- `foundation doctor` reports missing-but-creatable directories as warnings rather than mutating the filesystem.

## Development Notes
- `foundation --help` is the primary smoke check for the CLI entrypoint.
- `src/foundation/settings.py` owns the typed Stage 2 configuration model and precedence rules.
- `src/foundation/logging.py` provides a small stdlib logging baseline that later stages can replace or expand.
- `./scripts/uv` wraps the project-local `uv` binary and pins `UV_CACHE_DIR` to `.uv-cache/` so the workflow works in restricted environments.
