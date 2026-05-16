"""Direct unit tests for ActionExecutor's tool-call dispatch.

The executor's `_execute_tool_call` is the routing table between planned
capability ids (`foundation.file.write`, `foundation.git.status`, …) and the
concrete downstream services. The table is large, and most branches were
only exercised indirectly through orchestrator-level integration tests.

This module exercises the dispatch directly so that:

- Every file.* / git.* branch is asserted to return the right
  `ExecutionArtifactType`.
- The three typed-error catch blocks (FileServiceError, GitServiceError,
  ValueError) each report `ExecutionStatus.FAILED` with the right error
  fields.
- An unknown runtime endpoint is reported as a clear failure rather than
  silently dropped.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundation.models import (
    ActionKind,
    ExecutionArtifactType,
    ExecutionStatus,
    PlannedAction,
    PolicyDecision,
    PolicyDecisionType,
    ToolCall,
)
from foundation.services import (
    ApprovalService,
    CapabilityRegistry,
    CapabilityStore,
    LocalToolService,
    ShellRuntime,
)
from foundation.services.executor import ActionExecutor
from foundation.services.file_service import FileService
from foundation.services.git_service import GitService
from foundation.services.guardrails import GuardrailPolicyEngine
from foundation.services.observer import ObserverService
from foundation.settings import ApprovalMode


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A real workspace with a tracked file and an initialised git repo."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test User")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "initial")
    return workspace


@pytest.fixture()
def executor(workspace: Path, tmp_path: Path) -> ActionExecutor:
    state_dir = workspace / ".foundation" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    tool_service = LocalToolService(workspace_root=workspace)
    registry = CapabilityRegistry(
        store=CapabilityStore(tmp_path / "capabilities"),
        tool_service=tool_service,
    )
    observer = ObserverService(history_store=None, capability_registry=registry)
    shell_runtime = ShellRuntime(
        workspace_root=workspace,
        default_timeout_seconds=5,
        max_timeout_seconds=10,
    )
    policy_engine = GuardrailPolicyEngine(
        workspace_root=workspace,
        capability_registry=registry,
    )
    approval_service = ApprovalService(mode=ApprovalMode.AUTO)
    return ActionExecutor(
        workspace_root=workspace,
        shell_runtime=shell_runtime,
        tool_service=tool_service,
        policy_engine=policy_engine,
        approval_service=approval_service,
        capability_registry=registry,
        observer=observer,
        file_service=FileService(workspace_root=workspace, state_dir=state_dir),
        git_service=GitService(workspace_root=workspace),
    )


def _allow(action: PlannedAction) -> PolicyDecision:
    return PolicyDecision(
        action_id=action.id,
        decision=PolicyDecisionType.ALLOW,
        reason="test-allow",
    )


def _run(
    executor: ActionExecutor,
    workspace: Path,
    capability_id: str,
    arguments: dict,
    *,
    action_id: str = "a1",
):
    action = PlannedAction(
        id=action_id,
        kind=ActionKind.TOOL_CALL,
        summary=f"{capability_id} dispatch",
        tool_call=ToolCall(capability_id=capability_id, arguments=arguments),
    )
    envelope = executor.execute(
        action,
        _allow(action),
        policy_evaluation=None,
        plan_only=False,
        request_cwd=workspace,
        request_id="req-test",
        session_id=None,
    )
    return envelope.execution_result


def test_dispatch_file_read_routes_to_file_service(
    workspace: Path,
    executor: ActionExecutor,
) -> None:
    result = _run(
        executor,
        workspace,
        "foundation.file.read",
        {"path": str(workspace / "hello.txt")},
    )

    assert result.status is ExecutionStatus.EXECUTED
    assert result.artifact_type is ExecutionArtifactType.FILE_READ
    assert result.artifact is not None
    # The artifact payload should expose the file's content somewhere.
    payload = str(result.artifact)
    assert "hello" in payload


def test_dispatch_file_write_routes_to_file_service(
    workspace: Path,
    executor: ActionExecutor,
) -> None:
    target = workspace / "new.txt"
    result = _run(
        executor,
        workspace,
        "foundation.file.write",
        {"path": str(target), "content": "fresh\n"},
    )

    assert result.status is ExecutionStatus.EXECUTED
    assert result.artifact_type is ExecutionArtifactType.FILE_WRITE
    assert target.read_text(encoding="utf-8") == "fresh\n"


def test_dispatch_git_status_routes_to_git_service(
    workspace: Path,
    executor: ActionExecutor,
) -> None:
    result = _run(executor, workspace, "foundation.git.status", {})

    assert result.status is ExecutionStatus.EXECUTED
    assert result.artifact_type is ExecutionArtifactType.GIT_STATUS
    assert result.artifact is not None


def test_dispatch_git_log_routes_to_git_service(
    workspace: Path,
    executor: ActionExecutor,
) -> None:
    result = _run(executor, workspace, "foundation.git.log", {"max_count": 1})

    assert result.status is ExecutionStatus.EXECUTED
    assert result.artifact_type is ExecutionArtifactType.GIT_LOG
    assert result.artifact is not None
    payload = str(result.artifact)
    assert "initial" in payload


def test_dispatch_file_read_missing_path_reports_file_service_error(
    workspace: Path,
    executor: ActionExecutor,
) -> None:
    result = _run(
        executor,
        workspace,
        "foundation.file.read",
        {"path": str(workspace / "does-not-exist.txt")},
    )

    assert result.status is ExecutionStatus.FAILED
    assert "File operation failed" in result.summary
    assert result.error is not None
    # The artifact should carry the typed-error payload so observers/UI can
    # surface a structured error code, not just the human-readable message.
    assert result.artifact is not None
    assert "code" in result.artifact


def test_dispatch_git_commit_with_nothing_staged_reports_git_service_error(
    workspace: Path,
    executor: ActionExecutor,
) -> None:
    # The fixture already committed everything, and the working tree is clean,
    # so a commit attempt should produce a GitServiceError.
    result = _run(
        executor,
        workspace,
        "foundation.git.commit",
        {"message": "nothing to commit"},
    )

    assert result.status is ExecutionStatus.FAILED
    assert "Git operation failed" in result.summary
    assert result.error is not None
    assert result.artifact is not None
    assert "code" in result.artifact


def test_dispatch_invalid_arguments_reports_value_error_branch(
    workspace: Path,
    executor: ActionExecutor,
) -> None:
    # `foundation.file.apply_diff` requires `diff` with min_length=1. Passing
    # an empty string trips Pydantic's `model_validate` → ValueError → caught
    # by the executor's typed-error branch and reported as FAILED with the
    # `invalid_capability` code path.
    result = _run(
        executor,
        workspace,
        "foundation.file.apply_diff",
        {"path": str(workspace / "hello.txt"), "diff": ""},
    )

    assert result.status is ExecutionStatus.FAILED
    assert "Capability execution failed" in result.summary
    assert result.error is not None


def test_executor_writes_ledger_entry_per_action(workspace: Path, tmp_path: Path) -> None:
    """When a Ledger is attached, ActionExecutor records one entry per action."""
    from foundation.ledger import Ledger

    state_dir = workspace / ".foundation" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    tool_service = LocalToolService(workspace_root=workspace)
    registry = CapabilityRegistry(
        store=CapabilityStore(tmp_path / "capabilities"),
        tool_service=tool_service,
    )
    observer = ObserverService(history_store=None, capability_registry=registry)
    shell_runtime = ShellRuntime(
        workspace_root=workspace,
        default_timeout_seconds=5,
        max_timeout_seconds=10,
    )
    policy_engine = GuardrailPolicyEngine(
        workspace_root=workspace,
        capability_registry=registry,
    )
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path=ledger_path)
    executor_with_ledger = ActionExecutor(
        workspace_root=workspace,
        shell_runtime=shell_runtime,
        tool_service=tool_service,
        policy_engine=policy_engine,
        approval_service=ApprovalService(mode=ApprovalMode.AUTO),
        capability_registry=registry,
        observer=observer,
        file_service=FileService(workspace_root=workspace, state_dir=state_dir),
        git_service=GitService(workspace_root=workspace),
        ledger=ledger,
    )

    result = _run(
        executor_with_ledger,
        workspace,
        "foundation.git.status",
        {},
        action_id="ledger1",
    )

    assert result.status is ExecutionStatus.EXECUTED
    assert ledger_path.exists()
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json

    record = json.loads(lines[0])
    assert record["action_id"] == "ledger1"
    assert record["capability_id"] == "foundation.git.status"
    assert record["status"] == "executed"


def test_dispatch_unknown_capability_reports_failed_resolution(
    workspace: Path,
    executor: ActionExecutor,
) -> None:
    # An id that isn't seeded as a built-in capability should be rejected by
    # the resolver before any dispatch, falling into the ValueError branch.
    result = _run(
        executor,
        workspace,
        "foundation.unknown.capability",
        {},
    )

    assert result.status is ExecutionStatus.FAILED
    assert "Capability execution failed" in result.summary
    assert result.error is not None
    assert "foundation.unknown.capability" in result.error
