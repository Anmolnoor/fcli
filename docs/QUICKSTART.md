# Quickstart

Foundation CLI is a local agent that reads, edits, and runs git in your workspace under an explicit `plan → approve → execute → observe` loop. This page gets you from clone to first chat.

## 1. Install

```bash
git clone https://github.com/Anmolnoor/fcli.git && cd fcli
./scripts/bootstrap.sh
```

You need Python 3.12. The bootstrap script provisions a local venv and `uv`. All commands below use `./scripts/uv run <…>` to keep the bootstrapped environment in front of your PATH.

## 2. Configure

```bash
./scripts/uv run foundation init
```

The wizard asks four things:

| Prompt | Default | Notes |
| --- | --- | --- |
| Provider | `openai` | One of `openai`, `ollama`. |
| Model | `gpt-5-mini` (openai) / `qwen3:8b` (ollama) | Free-text — any model the provider serves. |
| Workspace root | current directory | Foundation will only read/write inside this tree. |
| API key | — | Stored in `~/.config/foundation/foundation.env` with `chmod 600`. Leave blank for a local Ollama. |

What it writes:

- `~/.config/foundation/config.toml` — TOML config matching `AppSettings`. Re-running with `--force` backs the previous file up to `config.toml.bak`.
- `~/.config/foundation/foundation.env` — your API key as `OPENAI_API_KEY=…` (or `OLLAMA_API_KEY=…`). Mode `0600`. Other entries in the file are preserved.

By default, the wizard runs a 1-token "ping" against the provider so you find out about a wrong key now, not on your first chat. Skip it with `--no-probe`.

### Non-interactive

For scripts and CI:

```bash
./scripts/uv run foundation init \
  --non-interactive \
  --provider openai \
  --model gpt-5-mini \
  --api-key "$OPENAI_API_KEY" \
  --workspace "$(pwd)" \
  --no-probe
```

Add `--force` to overwrite an existing config.

## 3. Verify

```bash
./scripts/uv run foundation doctor
```

PASS / WARN / FAIL across config readability, required directories, provider credential resolution, history DB, and the capability registry. If anything is FAIL, the chat won't work; fix it before step 4.

## 4. Run

```bash
./scripts/uv run foundation                          # interactive shell
./scripts/uv run foundation "list files in src"      # one-shot
```

During planning you'll see the status line transition: `… planning iter 1 · contacting provider · 2.3s` → `… planning iter 1 · validating plan · 4.1s`. Press `?` to expand the live detail panel; `Ctrl-C` cancels.

## Manual setup (skip the wizard)

Author the two files by hand:

`~/.config/foundation/config.toml`

```toml
[app]
workspace_root = "/abs/path/to/your/repo"

[provider]
name = "openai"
model = "gpt-5-mini"
api_key_env_var = "OPENAI_API_KEY"
```

`~/.config/foundation/foundation.env` (chmod 0600)

```
OPENAI_API_KEY=sk-...
```

`foundation doctor` will confirm both load correctly. See [`.env.example`](../.env.example) for the full list of `FOUNDATION_*` env overrides.

## Troubleshooting

- **`doctor` says "Provider credentials missing"** — the wizard didn't write the env file, or you renamed `api_key_env_var`. Re-run `foundation init --force`, or `export OPENAI_API_KEY=…` for the current shell.
- **`doctor` says "Keychain backend unavailable"** — harmless on Linux/CI. Foundation falls back to the env file. The wizard never uses the keychain.
- **`Config already exists at …; pass --force to replace it.`** — the file at `~/.config/foundation/config.toml` is intact; re-run with `--force` to swap it (a `.toml.bak` backup is written).
- **Status line stays on `contacting provider` for >30s** — your provider is slow or unreachable. `Ctrl-C` to cancel; check the provider's status page or your network.
- **State / logs / event NDJSON** — `~/.local/state/foundation/{history.sqlite3,logs,events}`. Inspect via `foundation history` and `foundation trace`.

## Next

- `docs/TECHNICAL.md` — full CLI surface, configuration reference, architecture notes.
- `docs/monitor-protocol.md` — wire format for the live event log (Unix socket / HTTP).
- `plans/` — the stage-by-stage specs the agent built this from.
