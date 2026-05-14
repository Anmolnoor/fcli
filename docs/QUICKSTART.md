# Quickstart

Foundation CLI is a local agent that reads, edits, and runs git in your workspace under an explicit `plan → approve → execute → observe` loop. This page gets you from clone to first chat.

## 1. Install

```bash
git clone https://github.com/Anmolnoor/fcli.git && cd fcli
./scripts/install.sh
```

You need Python 3.12. The installer:

1. Installs `pipx` via `python3.12 -m pip install --user pipx` if it isn't already on PATH (calls `pipx ensurepath` afterward).
2. Runs `pipx install --force git+https://github.com/Anmolnoor/fcli.git@main`, which gives you a clean isolated venv at `~/.local/pipx/venvs/foundation-cli/` and puts `foundation` on PATH.

After install, open a new shell (or `source ~/.zshrc`) so PATH picks up the binary. Re-running `./scripts/install.sh` is safe — it always uses `--force` to refresh the install.

**Dev setup** (working on Foundation itself) — `./scripts/bootstrap.sh` provisions a repo-local venv via `uv`; use `./scripts/uv run foundation …` to invoke that build. See [`docs/TECHNICAL.md`](TECHNICAL.md).

## 2. Configure

```bash
foundation init
```

The wizard asks five things:

| Prompt | Default | Notes |
| --- | --- | --- |
| Provider | `openai` | One of `openai`, `ollama`. |
| Model | `gpt-5-mini` (openai) / `qwen3:8b` (ollama) | Free-text — any model the provider serves. |
| Workspace root | current directory | Foundation will only read/write inside this tree. |
| API key | — | Stored in `~/.config/foundation/foundation.env` with `chmod 600`. Leave blank for a local Ollama. |
| Install `fcli` alias? | `N` | Optional. Appends `alias fcli="foundation"` to your shell's rc file (detected from `$SHELL`). Re-running replaces the block in-place; previous rc is backed up to `<rc>.bak`. |

What it writes:

- `~/.config/foundation/config.toml` — TOML config matching `AppSettings`. Re-running with `--force` backs the previous file up to `config.toml.bak`.
- `~/.config/foundation/foundation.env` — your API key as `OPENAI_API_KEY=…` (or `OLLAMA_API_KEY=…`). Mode `0600`. Other entries in the file are preserved.

By default, the wizard runs a 1-token "ping" against the provider so you find out about a wrong key now, not on your first chat. Skip it with `--no-probe`.

### Non-interactive

For scripts and CI:

```bash
foundation init \
  --non-interactive \
  --provider openai \
  --model gpt-5-mini \
  --api-key "$OPENAI_API_KEY" \
  --workspace "$(pwd)" \
  --no-probe
```

Add `--force` to overwrite an existing config. Add `--alias` (plus optional `--alias-name fcli`, `--alias-target foundation`, `--shell-rc ~/.zshrc`) to install the shell alias in the same call.

## 3. Verify

```bash
foundation doctor
```

PASS / WARN / FAIL across config readability, required directories, provider credential resolution, history DB, and the capability registry. If anything is FAIL, the chat won't work; fix it before step 4.

## 4. Run

```bash
foundation                          # interactive shell
foundation "list files in src"      # one-shot
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

## Update

```bash
foundation update          # detects pipx / pip-user / dev checkout and runs the right upgrade
foundation update --dry-run         # show the command without running
foundation update --ref v0.3.0      # install a specific branch or tag
```

What it does:

| Install mechanism | Command run |
| --- | --- |
| `pipx` | `pipx install --force git+https://github.com/Anmolnoor/fcli.git@main` |
| `pip --user` | `python -m pip install --user --upgrade git+…@main` |
| Dev checkout | prints `git pull && ./scripts/uv sync --extra dev` — does not self-modify |

After an upgrade, open a new shell or re-run from a fresh terminal so the shell's PATH cache picks up the new binary.

## Uninstall

```bash
foundation uninstall                   # removes shell alias block; prints pipx uninstall command
foundation uninstall --run             # also runs `pipx uninstall foundation-cli`
foundation uninstall --purge --yes     # additionally wipes ~/.config/foundation, ~/.local/share/foundation, ~/.local/state/foundation
foundation uninstall --keep-alias      # leave the rc file alone
```

By default, your config + chat history + capability store are **preserved** so a reinstall picks them up. Add `--purge --yes` (or omit `--yes` for an interactive confirm) to delete them.

The `--run` flag uses `os.execvp` to swap the running process to `pipx uninstall`, which avoids the "Python tries to import after its own venv is gone" trap.

## Next

- `docs/TECHNICAL.md` — full CLI surface, configuration reference, architecture notes.
- `docs/monitor-protocol.md` — wire format for the live event log (Unix socket / HTTP).
- `plans/` — the stage-by-stage specs the agent built this from.
