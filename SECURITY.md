# Security Policy

## Reporting a vulnerability

Email **anmolnoor59@gmail.com** with a clear description and minimal reproduction. Do not open a public issue for security-relevant bugs.

I'll acknowledge the report on a best-effort basis (this is a personal project, not a service). Please give me a reasonable window to investigate and patch before public disclosure.

## Supported versions

Only the latest release of `foundation-cli` on `main` receives security fixes. There are no LTS branches.

## Scope

In scope:

- The CLI itself (`src/foundation/`)
- The capability execution surface (file/git operations, shell capability)
- The history database and event log
- Any configuration parsing

Out of scope:

- Vulnerabilities in upstream dependencies (please report those upstream — I'll bump versions when patches land)
- Issues that require already-compromised local credentials or filesystem access
- Bugs in third-party LLM providers (OpenAI, Ollama, etc.)

## Threat model note

Foundation CLI executes shell commands and writes files on your machine. Running it against an untrusted prompt or model output is, by design, equivalent to giving a stranger your shell. The approval gate is the primary defense. If you find a way to bypass approvals or trigger side effects without an explicit `[approve]`, that's a vulnerability — please report it.
