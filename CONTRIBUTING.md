# Contributing to Foundation CLI

Foundation CLI is a personal learning project that I happen to develop in public. I welcome real contributions and I'm grateful when someone takes the time to file a thoughtful issue or send a careful PR. The rules below exist so that signal isn't drowned out by AI-generated noise.

The contribution gate (auto-close + `lgtmi`/`lgtm` allowlist) is patterned after the system Mario Zechner ([@badlogic](https://github.com/badlogic)) built for [`pi-mono`](https://github.com/badlogic/pi-mono). All credit for the mechanic to him.

## The one rule

**You must understand your code.** If you cannot explain what your changes do and how they interact with the rest of the system, your PR will be closed. AI-generated code is fine — submitting AI-generated code you do not understand is not.

## How approval works

- New issues from new contributors are auto-closed by `.github/workflows/issue-gate.yml`.
- New PRs from new contributors are auto-closed by `.github/workflows/pr-gate.yml`.
- I review auto-closed issues whenever I have time. Worthwhile ones get reopened. Issues that don't meet the quality bar below stay closed and don't get a reply.
- If I comment **`lgtmi`** on one of your issues, your future *issues* won't be auto-closed.
- If I comment **`lgtm`** on one of your issues, your future *issues and PRs* won't be auto-closed.

The allowlist lives at `.github/APPROVED_CONTRIBUTORS` and is updated automatically by `.github/workflows/approve-contributor.yml` when a maintainer comments.

## Issue quality bar

- **Concise.** If it doesn't fit on one screen, it's too long.
- **Clear.** State the bug or request in the first line.
- **Why it matters.** One sentence on the user-visible impact or use case.
- **Repro for bugs.** Commands, expected output, actual output. No "it doesn't work."

Use the issue templates. Don't open blank issues — they go to the bottom of the pile.

## PR prerequisites

Before opening a PR:

1. **Get `lgtm` first.** Open an issue, get a maintainer reply with `lgtm` on it, *then* open the PR. PRs without a referenced approved issue get auto-closed.
2. **Tests pass locally.** Run `./scripts/uv run pytest` and confirm all tests pass.
3. **Lint is clean.** Run `./scripts/uv run ruff check src tests` and `./scripts/uv run ruff format --check src tests`.
4. **You understand the change.** See "the one rule."
5. **Don't edit `CHANGELOG.md`.** I'll add the entry on merge.
6. **Read [`AGENTS.md`](AGENTS.md)** if you're using an AI agent to help write the change.

## Repeat-offender rule

If you ignore this document twice, or you spam the tracker with agent-generated issues or PRs, your GitHub account will be permanently blocked from this repo. No taksies backsies.

## Scope

- The core stays small and focused. Features that belong in a plugin or downstream tool should live there.
- Major architectural changes need a proposal in an issue first. Don't surprise me with a 2000-line PR.
- This is a personal project. There are no SLAs, no roadmap commitments, and I may go quiet for stretches at a time. That's okay.

## Disagreement

If you think a rule here is wrong, open an issue and tell me why. I'd rather hear it directly than have you work around it.
