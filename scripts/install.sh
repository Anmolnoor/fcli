#!/usr/bin/env bash
#
# Foundation CLI installer.
# - Installs pipx if missing (via `python3.12 -m pip install --user pipx`).
# - Installs Foundation from the GitHub `main` branch via pipx.
# - Idempotent: re-running just refreshes the install (`--force`).
#
# After this script: `foundation init` to configure, `foundation doctor` to verify.

set -euo pipefail

REPO_URL="${FOUNDATION_REPO_URL:-https://github.com/Anmolnoor/fcli.git}"
REF="${FOUNDATION_REF:-main}"
PYTHON_BIN="${PYTHON:-python3.12}"

err() {
  printf '[install] error: %s\n' "$1" >&2
  exit 1
}

info() {
  printf '[install] %s\n' "$1"
}

need_python() {
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    err "Could not find ${PYTHON_BIN}. Install Python 3.12 (e.g. \`brew install python@3.12\` on macOS) and re-run."
  fi
}

ensure_pipx() {
  if command -v pipx >/dev/null 2>&1; then
    return 0
  fi
  info "pipx not found — installing via \`${PYTHON_BIN} -m pip install --user pipx\`"
  if ! "${PYTHON_BIN}" -m pip install --user pipx; then
    cat >&2 <<'EOF'
[install] pip install failed. On PEP 668 systems try one of:
  - python3.12 -m pip install --user --break-system-packages pipx
  - brew install pipx     # macOS
  - sudo apt install pipx # Ubuntu / Debian
EOF
    exit 1
  fi
  info "Wiring pipx into PATH (\`pipx ensurepath\`)."
  "${PYTHON_BIN}" -m pipx ensurepath || true

  if ! command -v pipx >/dev/null 2>&1; then
    # ensurepath edits rc files but doesn't update the running shell.
    USER_BASE_BIN="$(${PYTHON_BIN} -c 'import site,sys;print(site.getuserbase()+"/bin")')"
    export PATH="${USER_BASE_BIN}:${PATH}"
  fi

  if ! command -v pipx >/dev/null 2>&1; then
    err "pipx still not on PATH after install. Open a new shell and re-run this script."
  fi
}

main() {
  need_python
  ensure_pipx

  local target="git+${REPO_URL}@${REF}"
  info "Installing foundation-cli from ${target}"
  pipx install --force "${target}"

  cat <<EOF

[install] ✓ done.

Next:
  foundation init      # interactive setup wizard
  foundation doctor    # verify readiness
  foundation           # start chatting

Update later with:
  foundation update    # or: scripts/update.sh
EOF
}

main "$@"
