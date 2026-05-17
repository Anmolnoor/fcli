#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON:-python3.12}"
PIP_BIN="${VENV_DIR}/bin/pip"
UV_BIN="${VENV_DIR}/bin/uv"

if [[ ! -d "${VENV_DIR}" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

if [[ ! -x "${UV_BIN}" ]]; then
  "${PIP_BIN}" install uv
fi

"${ROOT_DIR}/scripts/uv" sync --extra dev
