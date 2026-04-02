# Stage 2: CLI Surface and Config

## Goal
Define the first stable user-facing command tree and the configuration system that controls provider access, workspace behavior, approvals, and logging.

## Entry Criteria
- Stage 1 exit criteria are met.
- The `foundation` command exists and can be extended without reworking project structure.

## Locked Decisions
- Typer owns the top-level command tree.
- Rich owns CLI output rendering.
- Pydantic plus `pydantic-settings` own typed configuration.
- TOML is the persisted config format.
- Secrets should resolve from keychain-backed storage by default, not plaintext.

## Public Interfaces Introduced
- `foundation run`
- `foundation chat`
- `foundation config`
- `foundation history`
- `foundation doctor`

## Step-by-Step Plan
1. Design the settings model with clear groups:
   - app directories and workspace root
   - provider settings
   - shell execution policy defaults
   - logging options
   - history retention
   - approval mode
2. Implement config loading precedence:
   - code defaults
   - TOML file
   - environment variables
   - keychain secret resolution
   - explicit CLI flags
3. Implement `foundation config` subcommands for:
   - show effective config
   - validate config
   - show config file locations
4. Implement `foundation doctor` checks for:
   - Python version
   - config readability
   - required directories
   - secret lookup health
5. Add user-safe rendering:
   - do not print secret values
   - show source locations for settings when useful
   - return actionable validation errors

## Deliverables
- Typed config models
- Working command tree for the first top-level commands
- Config inspection and validation flows
- Doctor checks for environment and config readiness

## Exit Criteria
- The CLI can parse the full MVP command tree.
- Invalid configuration fails early with clear messages.
- Effective config can be inspected without exposing secrets.
- Provider credentials can be resolved through the chosen secret path.

## Test Focus
- Config precedence ordering
- Missing and malformed config behavior
- Secret redaction
- Doctor output for healthy and unhealthy environments

## Handoff to Stage 3
Do not implement shell execution until config has a stable place to define workspace root, execution policies, and timeouts.
