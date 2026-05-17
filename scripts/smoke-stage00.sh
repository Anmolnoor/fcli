#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTEST_BIN="${ROOT_DIR}/.venv/bin/pytest"
FOUNDATION_BIN="${ROOT_DIR}/.venv/bin/foundation"

if [[ ! -x "${PYTEST_BIN}" ]]; then
  printf 'Missing pytest at %s\n' "${PYTEST_BIN}" >&2
  printf 'Run ./scripts/bootstrap.sh first.\n' >&2
  exit 1
fi

if [[ ! -x "${FOUNDATION_BIN}" ]]; then
  printf 'Missing foundation CLI at %s\n' "${FOUNDATION_BIN}" >&2
  printf 'Run ./scripts/bootstrap.sh first.\n' >&2
  exit 1
fi

TESTS=(
  "tests/test_session_manager.py::test_session_manager_loads_memory_layers_in_order"
  "tests/test_session_manager.py::test_session_manager_resolves_latest_and_explicit_sessions"
  "tests/test_session_manager.py::test_session_manager_recovers_last_checkpoint_after_interrupted_turn"
  "tests/test_session_manager.py::test_session_manager_compacts_older_turns"
  "tests/test_cli.py::test_chat_interactive_shell_prefix_routes_to_direct_shell_execution"
  "tests/test_cli.py::test_chat_interactive_can_override_approval_mode"
  "tests/test_cli.py::test_chat_interactive_persists_transcript_across_turns_and_restarts"
  "tests/test_cli.py::test_chat_interactive_reset_clears_persisted_transcript"
  "tests/test_cli.py::test_chat_interactive_persists_shell_turns_into_transcript"
  "tests/test_cli.py::test_chat_interactive_manual_approval_history_stays_pending"
  "tests/test_cli.py::test_chat_interactive_memory_command_updates_project_memory"
  "tests/test_cli.py::test_chat_interactive_model_command_updates_subsequent_request"
  "tests/test_cli.py::test_chat_interactive_can_resume_specific_persistent_session"
  "tests/test_cli.py::test_build_chat_prompt_session_enables_multiline"
)

printf 'Stage 00 smoke test\n'
"${FOUNDATION_BIN}" --version

"${PYTEST_BIN}" -q "${TESTS[@]}" "$@"
