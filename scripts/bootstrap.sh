#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PIP_BIN="${VENV_DIR}/bin/pip"
UV_BIN="${VENV_DIR}/bin/uv"

resolve_command() {
  local candidate="$1"

  if [[ "${candidate}" == */* ]]; then
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
    return 1
  fi

  command -v "${candidate}" 2>/dev/null
}

is_python_312() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1
}

print_python_install_hint() {
  local os_name
  os_name="$(uname -s 2>/dev/null || printf 'unknown')"

  printf 'Python 3.12 is required to bootstrap Foundation CLI.\n' >&2

  case "${os_name}" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        cat >&2 <<'EOF'

Install it with Homebrew:
  brew install python@3.12

Then rerun:
  ./scripts/bootstrap.sh

If Homebrew installed Python outside your PATH, rerun with:
  PYTHON=/opt/homebrew/bin/python3.12 ./scripts/bootstrap.sh
EOF
      else
        cat >&2 <<'EOF'

Install Homebrew from https://brew.sh, then run:
  brew install python@3.12
  ./scripts/bootstrap.sh

Or install Python 3.12 from https://www.python.org/downloads/ and rerun with:
  PYTHON=/path/to/python3.12 ./scripts/bootstrap.sh
EOF
      fi
      ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        cat >&2 <<'EOF'

Install it with apt:
  sudo apt-get update
  sudo apt-get install -y python3.12 python3.12-venv
EOF
      elif command -v dnf >/dev/null 2>&1; then
        cat >&2 <<'EOF'

Install it with dnf:
  sudo dnf install python3.12
EOF
      elif command -v yum >/dev/null 2>&1; then
        cat >&2 <<'EOF'

Install it with yum:
  sudo yum install python3.12
EOF
      elif command -v pacman >/dev/null 2>&1; then
        cat >&2 <<'EOF'

Install it with pacman:
  sudo pacman -S python
EOF
      elif command -v apk >/dev/null 2>&1; then
        cat >&2 <<'EOF'

Install it with apk:
  sudo apk add python3
EOF
      else
        cat >&2 <<'EOF'

Install Python 3.12 with your system package manager, then rerun:
  ./scripts/bootstrap.sh
EOF
      fi
      ;;
    *)
      cat >&2 <<'EOF'

Install Python 3.12, then rerun:
  ./scripts/bootstrap.sh
EOF
      ;;
  esac
}

find_python_312() {
  local python_override="${PYTHON:-}"
  local candidate
  local resolved

  if [[ -n "${python_override}" ]]; then
    if ! resolved="$(resolve_command "${python_override}")"; then
      printf 'Requested Python interpreter not found: PYTHON=%s\n\n' "${python_override}" >&2
      print_python_install_hint
      return 1
    fi

    if ! is_python_312 "${resolved}"; then
      printf 'Requested Python interpreter is not Python 3.12: %s\n\n' "${resolved}" >&2
      print_python_install_hint
      return 1
    fi

    printf '%s\n' "${resolved}"
    return 0
  fi

  for candidate in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    if resolved="$(resolve_command "${candidate}")" && is_python_312 "${resolved}"; then
      printf '%s\n' "${resolved}"
      return 0
    fi
  done

  print_python_install_hint
  return 1
}

PYTHON_BIN="$(find_python_312)"

if [[ ! -d "${VENV_DIR}" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
elif [[ ! -x "${PIP_BIN}" && ! -x "${UV_BIN}" ]]; then
  # A uv-recreated venv (e.g. after a Homebrew Python upgrade) contains
  # neither pip nor uv; rebuild it so the pip -> uv -> sync chain below works.
  echo "Existing .venv has neither pip nor uv; rebuilding it." >&2
  "${PYTHON_BIN}" -m venv --clear "${VENV_DIR}"
fi

if [[ ! -x "${UV_BIN}" ]]; then
  "${PIP_BIN}" install uv
fi

"${ROOT_DIR}/scripts/uv" sync --extra dev
