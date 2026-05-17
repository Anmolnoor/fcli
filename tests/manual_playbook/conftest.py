"""Pytest fixtures for the manual playbook harness.

Provides:

- An env sweep that clears any ambient ``FOUNDATION_*`` variables before
  each scenario runs (prevents the harness from reproducing the exact leak
  it is meant to catch).
- ``playbook_workspace``: an ephemeral workspace root under ``tmp_path``.
- ``orchestrator_factory``: a callable that builds a fully wired
  :class:`RequestOrchestrator` pointed at the playbook workspace, with the
  supplied stub provider and approval callback.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.manual_playbook.harness import build_playbook_orchestrator
from tests.manual_playbook.provider_stubs import StubProvider

from foundation.services import HistoryStore
from foundation.services.orchestrator import RequestOrchestrator
from foundation.settings import ApprovalMode


@pytest.fixture(autouse=True)
def _sweep_foundation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any ambient ``FOUNDATION_*`` env vars so scenarios start clean."""
    for key in list(os.environ):
        if key.startswith("FOUNDATION_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def playbook_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture()
def history_store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(database_path=tmp_path / "history.sqlite3")


OrchestratorFactory = Callable[..., RequestOrchestrator]


@pytest.fixture()
def orchestrator_factory(
    playbook_workspace: Path,
    history_store: HistoryStore,
) -> OrchestratorFactory:
    def _build(
        *,
        provider: StubProvider,
        approval_mode: ApprovalMode = ApprovalMode.AUTO,
        approval_callback: Callable[[Any], bool] | None = None,
        workspace: Path | None = None,
    ) -> RequestOrchestrator:
        target_workspace = workspace or playbook_workspace
        return build_playbook_orchestrator(
            workspace=target_workspace,
            provider=provider,
            approval_mode=approval_mode,
            approval_callback=approval_callback,
            history_database_path=history_store.database_path,
        )

    return _build


def init_git_repo(workspace: Path) -> None:
    """Initialize ``workspace`` as a git repo with one commit."""

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    has_user_content = any(entry.name != ".git" for entry in workspace.iterdir())
    git("init", "-q", "-b", "main")
    git("config", "user.email", "playbook@example.com")
    git("config", "user.name", "Playbook Runner")
    if has_user_content:
        git("add", ".")
        git("commit", "-q", "-m", "initial commit")


def session_row_count(history_store: HistoryStore) -> int:
    """Cheap helper for graders that want to assert DB isolation."""
    database_path = history_store.database_path
    with sqlite3.connect(database_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
