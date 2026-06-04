# Agent Rules

Rules for AI coding agents (Claude Code, Codex, Aider, etc.) helping work on this repo. Humans should follow these too — they're just rules of good hygiene that matter more when the typist isn't human.

## Don't open PRs autonomously

Agents do not open PRs against this repo. The human contributor reviews the change end-to-end first, *then* opens the PR themselves. If a maintainer sees a PR opened directly by a `[bot]` account or a fresh account that obviously didn't write the description, it gets closed.

Exception: when AnmolNoor is the author actively working on the change, agents may follow direct PR instructions from AnmolNoor without this rule blocking the PR.

## Surgical changes

- Touch only what the task requires.
- Don't reformat or "tidy" adjacent code.
- Match the existing style. If you'd do it differently, that's not relevant.
- Don't add features, abstractions, configurability, or "future-proofing" that wasn't asked for.
- If your change orphans imports/variables/functions, remove them. Don't remove pre-existing dead code.

## Git hygiene

- Never `git add -A` or `git add .`. Stage only files you modified.
- Never `git reset --hard`, `git checkout .`, `git clean -fd`, or `git stash` in a worktree shared with the user.
- Never `git commit --no-verify` to skip pre-commit hooks. Fix the hook output instead.
- Never force-push to `main`.

## Tests

- Tests live in `tests/`. Suite must stay green: `./scripts/uv run pytest`.
- If you create or modify a test file, you must run that test file and iterate until it passes — don't ship a broken test.
- Issue-specific regression tests go in `tests/regressions/<issue-number>-<short-slug>.py` if/when that directory is created.
- Don't mock things that would be cheap to set up for real.

## Lint

- `./scripts/uv run ruff check src tests` and `./scripts/uv run ruff format --check src tests` must be clean.
- Don't fix a type or lint error by removing functionality. Fix the underlying issue or upgrade the dependency.

## Commenting on issues and PRs

- Use `gh issue comment <n> --body-file <path>` and `gh pr comment <n> --body-file <path>`. Never pass multi-line markdown via inline `--body` — quoting eats characters.
- Preview the exact comment text before posting.
- Post one final comment unless the user asks for more. If you malform a comment, delete it and post one corrected version.
- Match the maintainer's tone: technical, concise, no filler, no emojis.

## Closing issues via commit

When a commit fully resolves an issue, include `fixes #<n>` or `closes #<n>` in the commit message body. GitHub will close the issue when the commit reaches `main`.

## When this file conflicts with user instructions

Ask the user to confirm they want to override these rules. Then proceed.
