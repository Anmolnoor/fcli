# How to Use Foundation CLI

This is the shortest path for a macOS user trying the repo from a fresh machine.

## 1. Install macOS prerequisites

Foundation CLI requires Python 3.12. On macOS, Homebrew is the simplest install path.
For the Codex provider, also install Node.js so you can install the Codex CLI:

```bash
brew install python@3.12 node git
python3.12 --version
node --version
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

If you have not cloned the repo yet:

```bash
mkdir -p "$HOME/Developer"
cd "$HOME/Developer"
git clone https://github.com/Anmolnoor/fcli.git
cd fcli
```

Then bootstrap:

```bash
./scripts/bootstrap.sh
./scripts/uv run foundation --help
```

## 3. Configure Codex / ChatGPT subscription

Use this path when you want Foundation to use your ChatGPT/Codex subscription
instead of an OpenAI API key.

Install the official Codex CLI:

```bash
npm install -g @openai/codex
codex --version
```

Sign in with your ChatGPT account:

```bash
codex login
```

Choose ChatGPT sign-in when Codex opens the browser login flow. If the browser
flow cannot complete, use device-code auth instead:

```bash
codex login --device-auth
```

Create the Foundation config directory:

```bash
mkdir -p "$HOME/Library/Application Support/foundation"
```

Create `config.toml` for the Codex provider:

```bash
cat > "$HOME/Library/Application Support/foundation/config.toml" <<'EOF'
[provider]
name = "codex"
model = "gpt-5.5"
request_timeout_seconds = 180
EOF
```

Verify the setup:

```bash
./scripts/uv run foundation doctor
./scripts/uv run foundation chat --render concise "Reply with exactly: fcli-codex-ready"
```

You want to see `Provider: codex`, `Base URL: codex://local`, and a secret
lookup line saying the provider uses local Codex ChatGPT login. This route does
not use `OPENAI_API_KEY`.

## Ollama Cloud

Use this path when you want Foundation to call Ollama Cloud directly with an
Ollama API key.

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

You can also run a one-shot check:

```bash
./scripts/uv run foundation chat --render concise "Reply with exactly: fcli-ready"
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
