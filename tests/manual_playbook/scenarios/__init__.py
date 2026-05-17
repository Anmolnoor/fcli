"""Playbook scenarios exposed as a plain list for parametrized discovery."""

from tests.manual_playbook.scenarios import (
    s01_git_status,
    s02_find_todos,
    s04_run_pytest,
    s05_repair_commit,
)

SCENARIOS = [
    s01_git_status.SCENARIO,
    s02_find_todos.SCENARIO,
    s04_run_pytest.SCENARIO,
    s05_repair_commit.SCENARIO,
]
