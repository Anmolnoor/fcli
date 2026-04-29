"""Shared execution helpers for the playbook harness."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.manual_playbook.graders import (
    GradeContext,
    GradeOutcome,
    assert_no_invented_paths,
)

from foundation.models import UserRequest
from foundation.services import ApprovalService, HistoryStore, LocalToolService, ShellRuntime
from foundation.services.orchestrator import RequestOrchestrator
from foundation.services.provider import ProviderAdapter
from foundation.settings import ApprovalMode

if TYPE_CHECKING:
    from tests.manual_playbook.scenarios._base import Scenario


@dataclass(frozen=True)
class PlaybookRun:
    context: GradeContext
    outcomes: list[GradeOutcome]

    @property
    def hard_failures(self) -> list[GradeOutcome]:
        return [
            outcome
            for outcome in self.outcomes
            if not outcome.passed and outcome.severity == "error"
        ]


def build_playbook_orchestrator(
    *,
    workspace: Path,
    provider: ProviderAdapter,
    approval_mode: ApprovalMode = ApprovalMode.AUTO,
    approval_callback: Callable[[Any], bool] | None = None,
    history_database_path: Path | None = None,
    shell_timeout_seconds: int = 5,
    shell_max_timeout_seconds: int = 15,
    shell_capture_limit_kb: int = 64,
    shell_allow_pty: bool = True,
    shell_enforce_workspace_boundary: bool = True,
    pass_through_foundation_env: bool = False,
) -> RequestOrchestrator:
    runtime = ShellRuntime(
        workspace_root=workspace,
        default_timeout_seconds=shell_timeout_seconds,
        max_timeout_seconds=shell_max_timeout_seconds,
        allow_pty=shell_allow_pty,
        capture_limit_kb=shell_capture_limit_kb,
        enforce_workspace_boundary=shell_enforce_workspace_boundary,
        pass_through_foundation_env=pass_through_foundation_env,
    )
    tool_service = LocalToolService(
        workspace_root=workspace,
        default_timeout_seconds=shell_timeout_seconds,
        capture_limit_kb=shell_capture_limit_kb,
        pass_through_foundation_env=pass_through_foundation_env,
    )
    approval_service = ApprovalService(
        mode=approval_mode,
        prompt_callback=approval_callback,
    )
    history_store = (
        HistoryStore(database_path=history_database_path)
        if history_database_path is not None
        else None
    )
    return RequestOrchestrator(
        workspace_root=workspace,
        approval_mode=approval_mode,
        provider=provider,
        shell_runtime=runtime,
        tool_service=tool_service,
        approval_service=approval_service,
        history_store=history_store,
    )


def run_playbook_scenario(
    scenario: Scenario,
    *,
    workspace: Path,
    orchestrator: RequestOrchestrator,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> PlaybookRun:
    if monkeypatch is not None:
        return _run_playbook_scenario(
            scenario=scenario,
            workspace=workspace,
            orchestrator=orchestrator,
            monkeypatch=monkeypatch,
        )

    with pytest.MonkeyPatch.context() as local_monkeypatch:
        return _run_playbook_scenario(
            scenario=scenario,
            workspace=workspace,
            orchestrator=orchestrator,
            monkeypatch=local_monkeypatch,
        )


def _run_playbook_scenario(
    *,
    scenario: Scenario,
    workspace: Path,
    orchestrator: RequestOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> PlaybookRun:
    artifacts: dict[str, Any] = scenario.setup(workspace, monkeypatch) or {}
    result = orchestrator.orchestrate(UserRequest(message=scenario.prompt))
    session_status = RequestOrchestrator._session_status_for_result(
        result.summary,
        result.stop_reason,
        result.iterations,
        result.governance_notice,
    )
    ctx = GradeContext(
        scenario_name=scenario.name,
        workspace_root=workspace,
        result=result,
        session_status=session_status,
        artifacts=artifacts,
    )
    outcomes = [grader(ctx) for grader in _resolve_graders(scenario, workspace)]
    return PlaybookRun(context=ctx, outcomes=outcomes)


def _resolve_graders(scenario: Scenario, workspace: Path) -> list:
    """Rebind graders whose parameters depend on the scenario workspace."""
    resolved = []
    for grader in scenario.graders:
        if getattr(grader, "__qualname__", "").startswith(
            "assert_no_invented_paths"
        ):
            resolved.append(assert_no_invented_paths(path_roots=[workspace]))
        else:
            resolved.append(grader)
    return resolved
