# Foundation CLI

[![CI](https://github.com/Anmolnoor/fcli/actions/workflows/ci.yml/badge.svg)](https://github.com/Anmolnoor/fcli/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](pyproject.toml)

> **Heads up — this is an experiment.** I'm using this project to learn how to build software *with* AI coding agents (Claude Code, mostly). The repo is public so the journey is visible, not because it's a polished product. Expect rough edges, breaking changes, and ideas that get rewritten as I learn.

## What this is

Foundation CLI (`foundation`) is a local-first, shell-native coding agent. It runs an explicit `plan → approve → execute → observe` loop, with typed capabilities for files and git, a bounded replan loop, and a redacted event log you can stream into your own tools.

But honestly, the *interesting* part of this repo isn't the CLI — it's that almost every line of it was written by collaborating with an AI agent. I'm using this as a hands-on lab to figure out:

- How do you scope a project so an agent can actually finish a stage?
- What does "good code review" look like when you didn't type the code?
- Where does the agent need guardrails (typed APIs, bounded loops, approval gates) vs. where can you just let it run?
- How do you keep architecture coherent across dozens of agent-driven commits?

So: it's part working tool, part learning notebook.

## What this is *not*

- **Not a product.** No support, no SLAs, no roadmap commitments.
- **Not stable.** Schema migrations, renamed commands, and rewritten subsystems happen often.
- **Not a recommendation.** I'm sharing what I'm learning, not telling you to build agents this way.
- **Not a replacement for Claude Code, Aider, Cursor, etc.** It's a learning project that happens to be runnable.

If any of that is a dealbreaker, that's totally fair — come back in a few months.

## Try it (at your own risk)

```bash
./scripts/bootstrap.sh
./scripts/uv run foundation --help
./scripts/uv run foundation
```

You'll need Python 3.12 and an API key for either OpenAI or Ollama. See [`docs/TECHNICAL.md`](docs/TECHNICAL.md) for full setup, configuration, the CLI surface, and architecture notes.

## Where things live

- [`docs/TECHNICAL.md`](docs/TECHNICAL.md) — the detailed README: features, configuration, commands, layout, limitations.
- [`docs/monitor-protocol.md`](docs/monitor-protocol.md) — event-log wire format and live transports.
- [`plans/`](plans/) — stage-by-stage implementation plans. These are the prompts/specs the agent worked from. Probably the most honest record of how the project actually got built.
- [`CHANGELOG.md`](CHANGELOG.md) — versioned notes, including which stage shipped what.

## Following along

If you're curious about the *learning* side more than the code, the `plans/` directory is where I'd start. Each stage is a small, scoped spec — that's the unit of work I've found agents handle well. Reading a plan and then `git log`-ing the commits that came out of it is the closest thing to a "how was this built" tour.

I'm not blogging about this (yet). The repo is the journal.

## Contributing

Yes, contributions are welcome — but read [`CONTRIBUTING.md`](CONTRIBUTING.md) first. To keep agent-generated noise out of the tracker, new issues and PRs from new contributors are **auto-closed by default**, and reopened by the maintainer when they meet the quality bar. The mechanic is borrowed from [`badlogic/pi-mono`](https://github.com/badlogic/pi-mono); credit to Mario Zechner for the pattern.

If you're using an AI agent to help, also read [`AGENTS.md`](AGENTS.md). The one rule: **you must understand your code.**

See also: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), [`SECURITY.md`](SECURITY.md).

## License

[GPL-3.0-or-later](LICENSE) — see the `LICENSE` file for the full text.
