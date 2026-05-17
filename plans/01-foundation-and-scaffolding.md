# Stage 1: Foundation and Scaffolding

## Goal
Create the baseline repository structure, Python packaging, and quality tooling needed for every later stage. This stage should leave the repo in a state where implementation can start without revisiting project setup.

## Entry Criteria
- Stage 0 roadmap exists and is accepted as the implementation sequence.
- The repo is still greenfield or any pre-existing files have been reviewed for compatibility.

## Locked Decisions
- Use `uv` for environment and dependency management.
- Use a `src/` layout with the main package under `src/foundation/`.
- Expose the console entrypoint as `foundation`.
- Use `ruff`, `mypy`, `pytest`, `coverage`, and `pre-commit` from the start.

## Step-by-Step Plan
1. Create the repository baseline files:
   - `pyproject.toml`
   - `.python-version` if desired for local consistency
   - `.gitignore`
   - `.pre-commit-config.yaml`
   - `README.md`
2. Create the package layout:
   - `src/foundation/__init__.py`
   - `src/foundation/cli.py`
   - `src/foundation/settings.py`
   - `src/foundation/logging.py`
   - `src/foundation/models/__init__.py`
   - `src/foundation/services/__init__.py`
3. Wire the console script so `foundation --help` resolves through Typer.
4. Add baseline tool configuration:
   - Ruff lint and format settings
   - mypy strictness appropriate for a new codebase
   - pytest discovery and async configuration
   - coverage command or config
5. Add the first smoke tests:
   - package import test
   - CLI help smoke test
   - settings load smoke test
6. Document the developer bootstrap path in `README.md`:
   - install dependencies
   - run tests
   - run lint/type-check
   - invoke the CLI

## Deliverables
- Installable Python package
- Working `foundation` command with placeholder help output
- Baseline code quality tools wired into the repo
- Initial test suite and developer instructions

## Exit Criteria
- `uv` can install and run the project locally.
- `foundation --help` returns a valid help screen.
- Lint, type-check, and tests all run successfully.
- The package layout leaves clear homes for later modules.

## Test Focus
- CLI import and entrypoint behavior
- Tooling config sanity
- Test runner and async test compatibility

## Handoff to Stage 2
Do not start config and command behavior until the package, entrypoint, and quality tooling are stable. Stage 2 depends on the CLI shell and settings module created here.
