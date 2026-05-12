#!/usr/bin/env bash
#
# Re-install Foundation from `main` via pipx. Use when `foundation update`
# isn't available (e.g. the binary is broken). Otherwise prefer
# `foundation update`, which detects your install method.

set -euo pipefail

REPO_URL="${FOUNDATION_REPO_URL:-https://github.com/Anmolnoor/fcli.git}"
REF="${FOUNDATION_REF:-main}"

if ! command -v pipx >/dev/null 2>&1; then
  printf '[update] pipx not found. Run scripts/install.sh first.\n' >&2
  exit 1
fi

target="git+${REPO_URL}@${REF}"
printf '[update] pipx install --force %s\n' "${target}"
pipx install --force "${target}"
printf '[update] ✓ done. Open a new shell so PATH cache picks up the new binary.\n'
