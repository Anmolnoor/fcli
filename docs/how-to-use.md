# How to Use Foundation CLI

This is the shortest path for a macOS user trying the repo from a fresh machine.

## 1. Install Python 3.12

Foundation CLI requires Python 3.12. On macOS, Homebrew is the simplest install path:

```bash
brew install python@3.12
python3.12 --version
```

If `python3.12` is still not on your `PATH`, use the explicit Homebrew path when bootstrapping:

```bash
PYTHON=/opt/homebrew/bin/python3.12 ./scripts/bootstrap.sh
```

On Intel Macs, the Homebrew path may be:

```bash
PYTHON=/usr/local/bin/python3.12 ./scripts/bootstrap.sh
```

## 2. Bootstrap the repo

```bash
./scripts/bootstrap.sh
./scripts/uv run foundation --help
```

## 3. Configure Ollama Cloud

Create the Foundation config directory:

```bash
mkdir -p "$HOME/Library/Application Support/foundation"
```

Create `config.toml`:

```bash
cat > "$HOME/Library/Application Support/foundation/config.toml" <<'EOF'
[provider]
name = "ollama"
model = "qwen3.5:397b-cloud"
base_url = "https://ollama.com/api"
request_timeout_seconds = 180
api_key_env_var = "OLLAMA_API_KEY"
EOF
```

Add your Ollama Cloud API key to the paired env file:

```bash
cat > "$HOME/Library/Application Support/foundation/foundation.env" <<'EOF'
OLLAMA_API_KEY=your-ollama-cloud-api-key
EOF
```

Then verify the setup:

```bash
./scripts/uv run foundation doctor
```

You want to see `Provider: ollama`, `Base URL: https://ollama.com/api`, and a secret lookup line saying credentials resolved from `$OLLAMA_API_KEY`.

## Codex / ChatGPT subscription

To use your ChatGPT/Codex subscription instead of an OpenAI API key, sign in to
the Codex CLI with your ChatGPT account, then use the `codex` provider:

```bash
codex
```

Choose ChatGPT sign-in when Codex prompts for authentication. Then configure
Foundation:

```bash
cat > "$HOME/Library/Application Support/foundation/config.toml" <<'EOF'
[provider]
name = "codex"
model = "gpt-5.5"
request_timeout_seconds = 180
EOF
```

Verify with:

```bash
./scripts/uv run foundation doctor
```

You want to see `Provider: codex`, `Base URL: codex://local`, and a secret
lookup line saying the provider uses local Codex ChatGPT login. This route does
not use `OPENAI_API_KEY`.

## OpenAI

To use OpenAI instead, use this provider config:

```bash
cat > "$HOME/Library/Application Support/foundation/config.toml" <<'EOF'
[provider]
name = "openai"
model = "gpt-5-mini"
request_timeout_seconds = 180
api_key_env_var = "OPENAI_API_KEY"
EOF
```

Add your OpenAI API key to the paired env file:

```bash
cat > "$HOME/Library/Application Support/foundation/foundation.env" <<'EOF'
OPENAI_API_KEY=your-openai-api-key
EOF
```

Then verify with:

```bash
./scripts/uv run foundation doctor
```

You want to see `Provider: openai`, `Base URL: https://api.openai.com/v1`, and a secret lookup line saying credentials resolved from `$OPENAI_API_KEY`.

## 4. Start Foundation

```bash
./scripts/uv run foundation
```

## Local Ollama

Local Ollama usually does not need an API key. Use a local model name and local base URL instead:

```toml
[provider]
name = "ollama"
model = "gpt-oss:20b"
base_url = "http://localhost:11434/api"
request_timeout_seconds = 180
```

Then verify with:

```bash
./scripts/uv run foundation doctor
```
