"""Invariant guards in the action executor (hardening stage 1).

These tests deliberately bypass model validation (``model_construct``) to
simulate the states the executor's invariant guards protect against: a
kind/payload mismatch that slipped past planning, or a builtin endpoint
dispatched without its backing service wired. The guards must surface these
as typed FAILED results, never as ``AssertionError`` (which ``python -O``
strips entirely).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundation.models import (
    ActionKind,
    ExecutionStatus,
    PlannedAction,
    PolicyDecision,
    PolicyDecisionType,
    ToolCall,
)
from foundation.services import executor as executor_module
from foundation.services.executor import ActionExecutor


class _NullObserver:
    def emit(self, *args: Any, **kwargs: Any) -> None:
        pass


class _StubRegistry:
    """Resolves every capability to a manifest with a fixed runtime endpoint."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def resolve(self, capability_id: str, version: str | None) -> Any:
        return SimpleNamespace(runtime_endpoint=self._endpoint)


def _make_executor(tmp_path: Path, *, registry: Any = None) -> ActionExecutor:
    return ActionExecutor(
        workspace_root=tmp_path,
        shell_runtime=None,  # type: ignore[arg-type]
        tool_service=None,  # type: ignore[arg-type]
        policy_engine=None,  # type: ignore[arg-type]
        approval_service=None,  # type: ignore[arg-type]
        capability_registry=registry,
        observer=_NullObserver(),  # type: ignore[arg-type]
        file_service=None,
        git_service=None,
    )


def _allow(action_id: str) -> PolicyDecision:
    return PolicyDecision(
        action_id=action_id,
        decision=PolicyDecisionType.ALLOW,
        reason="allowed for invariant test",
    )


def _invalid_action(kind: ActionKind, *, tool_call: ToolCall | None = None) -> PlannedAction:
    """Build a kind/payload-mismatched action, bypassing model validation."""
    return PlannedAction.model_construct(
        id="a1",
        kind=kind,
        summary="invariant test action",
        requires_approval=False,
        approval_reason=None,
        explanation=None,
        shell=None,
        tool_call=tool_call,
        question=None,
    )


def _execute(executor: ActionExecutor, action: PlannedAction, tmp_path: Path) -> Any:
    return executor.execute(
        action,
        _allow(action.id),
        policy_evaluation=None,
        plan_only=False,
        request_cwd=tmp_path,
        request_id="req-1",
        session_id=None,
    )


class TestRequireHelper:
    def test_returns_value_when_present(self) -> None:
        assert executor_module._require("value", description="anything") == "value"

    def test_raises_typed_error_when_missing(self) -> None:
        with pytest.raises(executor_module.ExecutorInvariantError, match="missing thing"):
            executor_module._require(None, description="missing thing")


class TestKindPayloadInvariants:
    def test_question_action_missing_payload_fails_typed(self, tmp_path: Path) -> None:
        action = _invalid_action(ActionKind.QUESTION)
        envelope = _execute(_make_executor(tmp_path), action, tmp_path)
        result = envelope.execution_result
        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "invariant" in result.error.lower()
        assert "question" in result.error.lower()

    def test_tool_call_action_missing_payload_fails_typed(self, tmp_path: Path) -> None:
        action = _invalid_action(ActionKind.TOOL_CALL)
        envelope = _execute(_make_executor(tmp_path), action, tmp_path)
        result = envelope.execution_result
        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "invariant" in result.error.lower()
        assert "tool_call" in result.error.lower()

    def test_shell_action_missing_payload_fails_typed(self, tmp_path: Path) -> None:
        envelope = _execute(_make_executor(tmp_path), _invalid_action(ActionKind.SHELL), tmp_path)
        result = envelope.execution_result
        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "invariant" in result.error.lower()
        assert "shell" in result.error.lower()


class TestServiceWiringInvariants:
    def _tool_action(self, capability_id: str) -> PlannedAction:
        return PlannedAction(
            id="a1",
            kind=ActionKind.TOOL_CALL,
            summary="invariant test action",
            tool_call=ToolCall(capability_id=capability_id, arguments={}),
        )

    def test_file_endpoint_without_file_service_fails_typed(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path, registry=_StubRegistry("builtin.file.read"))
        envelope = _execute(executor, self._tool_action("foundation.file.read"), tmp_path)
        result = envelope.execution_result
        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "invariant" in result.error.lower()
        assert "file service" in result.error.lower()

    def test_git_endpoint_without_git_service_fails_typed(self, tmp_path: Path) -> None:
        executor = _make_executor(tmp_path, registry=_StubRegistry("builtin.git.status"))
        envelope = _execute(executor, self._tool_action("foundation.git.status"), tmp_path)
        result = envelope.execution_result
        assert result.status is ExecutionStatus.FAILED
        assert result.error is not None
        assert "invariant" in result.error.lower()
        assert "git service" in result.error.lower()
