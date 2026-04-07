"""Executor service for Stage 3 runtime splitting."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from foundation.models import (
    ActionKind,
    ApprovalDecisionStatus,
    ApprovalRequest,
    ApprovalResolution,
    ExecutionArtifactType,
    ExecutionResult,
    ExecutionStatus,
    PlannedAction,
    PolicyDecision,
    PolicyDecisionType,
    PolicyEvaluationRecord,
    ShellAction,
    ToolCall,
)
from foundation.observability import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_RESOLVED,
    EVENT_TOOL_CALL_FAILED,
    EVENT_TOOL_CALL_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    EVENT_TOOL_EXECUTION_FAILED,
    EVENT_TOOL_EXECUTION_FINISHED,
    EVENT_TOOL_EXECUTION_STARTED,
)
from foundation.services.approval import ApprovalService
from foundation.services.capabilities import CapabilityRegistry
from foundation.services.guardrails import GuardrailPolicyEngine
from foundation.services.observer import ObserverService
from foundation.services.shell import (
    ExecutionMode,
    OutputCallback,
    ShellCommandRequest,
    ShellExecutionCancelled,
    ShellExecutionSpawnError,
    ShellExecutionTimeout,
    ShellRuntime,
)
from foundation.services.tools import (
    FileDiscoveryRequest,
    FileDiscoveryResult,
    GitContextRequest,
    GitContextResult,
    HelpLookupRequest,
    HelpLookupResult,
    HelpLookupSource,
    LocalToolService,
    SearchRequest,
    SearchResult,
    ToolExecutionError,
)


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class ActionExecutionEnvelope:
    """Execution result plus approval metadata and timings."""

    execution_result: ExecutionResult
    approval_request: ApprovalRequest | None
    approval_resolution: ApprovalResolution | None
    started_at: str
    completed_at: str
    duration_seconds: float


class ActionExecutor:
    """Perform constrained action execution once planning and policy are ready."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        shell_runtime: ShellRuntime,
        tool_service: LocalToolService,
        policy_engine: GuardrailPolicyEngine,
        approval_service: ApprovalService,
        capability_registry: CapabilityRegistry,
        observer: ObserverService,
        shell_output_callback: OutputCallback | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._shell_runtime = shell_runtime
        self._tool_service = tool_service
        self._policy_engine = policy_engine
        self._approval_service = approval_service
        self._capability_registry = capability_registry
        self._observer = observer
        self._shell_output_callback = shell_output_callback

    def execute(
        self,
        action: PlannedAction,
        decision: PolicyDecision,
        *,
        policy_evaluation: PolicyEvaluationRecord | None,
        plan_only: bool,
        request_cwd: Path,
        request_id: str,
        session_id: str | None,
    ) -> ActionExecutionEnvelope:
        started_at = _utcnow()
        started_monotonic = time.monotonic()
        execution_result, approval_request, approval_resolution = self._handle_action(
            action,
            decision,
            policy_evaluation=policy_evaluation,
            plan_only=plan_only,
            request_cwd=request_cwd,
            request_id=request_id,
            session_id=session_id,
        )
        completed_at = _utcnow()
        return ActionExecutionEnvelope(
            execution_result=execution_result,
            approval_request=approval_request,
            approval_resolution=approval_resolution,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=max(time.monotonic() - started_monotonic, 0.0),
        )

    def _handle_action(
        self,
        action: PlannedAction,
        decision: PolicyDecision,
        *,
        policy_evaluation: PolicyEvaluationRecord | None,
        plan_only: bool,
        request_cwd: Path,
        request_id: str,
        session_id: str | None,
    ) -> tuple[ExecutionResult, ApprovalRequest | None, ApprovalResolution | None]:
        if plan_only:
            return (
                ExecutionResult(
                    action_id=action.id,
                    status=ExecutionStatus.NOT_EXECUTED,
                    summary="Execution skipped because plan_only was requested.",
                ),
                None,
                None,
            )

        if decision.decision is PolicyDecisionType.BLOCK:
            return (
                ExecutionResult(
                    action_id=action.id,
                    status=ExecutionStatus.BLOCKED,
                    summary=decision.reason,
                    error=decision.reason,
                ),
                None,
                None,
            )

        if decision.decision is PolicyDecisionType.REQUIRE_APPROVAL:
            if policy_evaluation is None:
                raise RuntimeError(
                    f"Approval-required action {action.id!r} is missing a policy evaluation."
                )
            approval_request, approval_resolution = self._approval_service.resolve(
                action,
                policy_evaluation,
                request_cwd=request_cwd,
            )
            self._observer.emit(
                EVENT_APPROVAL_REQUESTED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "risk_categories": list(decision.risk_categories),
                    "mode": approval_resolution.mode,
                },
                session_id=session_id,
                logger_name="foundation.services.approval",
            )
            if approval_resolution.status is ApprovalDecisionStatus.PENDING:
                self._observer.emit(
                    EVENT_APPROVAL_RESOLVED,
                    payload={
                        "request_id": request_id,
                        "action_id": action.id,
                        "status": approval_resolution.status.value,
                    },
                    session_id=session_id,
                    logger_name="foundation.services.approval",
                )
                return (
                    ExecutionResult(
                        action_id=action.id,
                        status=ExecutionStatus.PENDING_APPROVAL,
                        summary=decision.reason,
                    ),
                    approval_request,
                    approval_resolution,
                )
            if approval_resolution.status is ApprovalDecisionStatus.DENIED:
                self._observer.emit(
                    EVENT_APPROVAL_RESOLVED,
                    payload={
                        "request_id": request_id,
                        "action_id": action.id,
                        "status": approval_resolution.status.value,
                    },
                    session_id=session_id,
                    logger_name="foundation.services.approval",
                )
                return (
                    ExecutionResult(
                        action_id=action.id,
                        status=ExecutionStatus.BLOCKED,
                        summary=approval_resolution.reason,
                        error=approval_resolution.reason,
                    ),
                    approval_request,
                    approval_resolution,
                )
            self._observer.emit(
                EVENT_APPROVAL_RESOLVED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "status": approval_resolution.status.value,
                },
                session_id=session_id,
                logger_name="foundation.services.approval",
            )
        else:
            approval_request = None
            approval_resolution = None

        if action.kind is ActionKind.EXPLANATION:
            return (
                ExecutionResult(
                    action_id=action.id,
                    status=ExecutionStatus.NOT_EXECUTED,
                    summary=action.explanation or action.summary,
                    artifact_type=ExecutionArtifactType.EXPLANATION,
                    artifact={"message": action.explanation or action.summary},
                ),
                approval_request,
                approval_resolution,
            )

        if policy_evaluation is not None:
            self._policy_engine.register_invocation(policy_evaluation)

        if action.kind is ActionKind.TOOL_CALL:
            assert action.tool_call is not None
            return (
                self._execute_tool_call(
                    action,
                    action.tool_call,
                    policy_evaluation=policy_evaluation,
                    request_cwd=request_cwd,
                    request_id=request_id,
                    session_id=session_id,
                ),
                approval_request,
                approval_resolution,
            )

        assert action.shell is not None
        return (
            self._execute_shell_action(
                action,
                policy_evaluation=policy_evaluation,
                request_cwd=request_cwd,
                request_id=request_id,
            ),
            approval_request,
            approval_resolution,
        )

    def _execute_tool_call(
        self,
        action: PlannedAction,
        tool_call: ToolCall,
        *,
        policy_evaluation: PolicyEvaluationRecord | None,
        request_cwd: Path,
        request_id: str,
        session_id: str | None,
    ) -> ExecutionResult:
        self._observer.emit(
            EVENT_TOOL_CALL_STARTED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "tool": tool_call.capability_id,
            },
            session_id=session_id,
            logger_name="foundation.services.orchestrator",
        )
        self._observer.emit(
            EVENT_TOOL_EXECUTION_STARTED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "tool": tool_call.capability_id,
            },
            session_id=session_id,
            logger_name="foundation.services.orchestrator",
        )
        result: SearchResult | FileDiscoveryResult | GitContextResult | HelpLookupResult
        try:
            manifest = self._capability_registry.resolve(
                tool_call.capability_id,
                tool_call.version,
            )
            if manifest.runtime_endpoint == "builtin.search":
                search_request = SearchRequest.model_validate(tool_call.arguments)
                result = self._tool_service.search(search_request)
                artifact_type = ExecutionArtifactType.SEARCH
            elif manifest.runtime_endpoint == "builtin.files":
                result = self._tool_service.discover_files(
                    FileDiscoveryRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.FILES
            elif manifest.runtime_endpoint == "builtin.git":
                result = self._tool_service.git_context(
                    GitContextRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT
            elif manifest.runtime_endpoint == "builtin.man":
                result = self._tool_service.lookup_help(
                    HelpLookupRequest.model_validate(
                        {
                            **tool_call.arguments,
                            "source": HelpLookupSource.MAN,
                        }
                    )
                )
                artifact_type = ExecutionArtifactType.MAN
            elif manifest.runtime_endpoint == "builtin.tldr":
                result = self._tool_service.lookup_help(
                    HelpLookupRequest.model_validate(
                        {
                            **tool_call.arguments,
                            "source": HelpLookupSource.TLDR,
                        }
                    )
                )
                artifact_type = ExecutionArtifactType.TLDR
            elif manifest.runtime_endpoint == "builtin.shell":
                shell_action = ShellAction.model_validate(tool_call.arguments)
                shell_planned_action = action.model_copy(
                    update={
                        "kind": ActionKind.SHELL,
                        "shell": shell_action,
                        "tool_call": None,
                    }
                )
                return self._execute_shell_action(
                    shell_planned_action,
                    policy_evaluation=policy_evaluation,
                    request_cwd=request_cwd,
                    request_id=request_id,
                )
            else:
                raise ValueError(
                    f"Unsupported capability runtime endpoint: {manifest.runtime_endpoint}"
                )
        except (ValueError, TypeError) as exc:
            self._observer.emit(
                EVENT_TOOL_EXECUTION_FAILED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "tool": tool_call.capability_id,
                    "error": str(exc),
                    "code": "invalid_capability",
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            self._observer.emit(
                EVENT_TOOL_CALL_FAILED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "tool": tool_call.capability_id,
                    "error": str(exc),
                    "code": "invalid_capability",
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Capability execution failed: {exc}",
                error=str(exc),
            )
        except ToolExecutionError as exc:
            self._observer.emit(
                EVENT_TOOL_EXECUTION_FAILED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "tool": tool_call.capability_id,
                    "error": exc.error.message,
                    "code": exc.error.code.value,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            self._observer.emit(
                EVENT_TOOL_CALL_FAILED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "tool": tool_call.capability_id,
                    "error": exc.error.message,
                    "code": exc.error.code.value,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Tool execution failed: {exc.error.message}",
                error=exc.error.message,
            )

        self._observer.emit(
            EVENT_TOOL_EXECUTION_FINISHED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "tool": tool_call.capability_id,
                "artifact_type": artifact_type.value,
            },
            session_id=session_id,
            logger_name="foundation.services.orchestrator",
        )
        self._observer.emit(
            EVENT_TOOL_CALL_FINISHED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "tool": tool_call.capability_id,
            },
            session_id=session_id,
            logger_name="foundation.services.orchestrator",
        )
        return ExecutionResult(
            action_id=action.id,
            status=ExecutionStatus.EXECUTED,
            summary=f"Executed capability `{tool_call.capability_id}` for action {action.id}.",
            artifact_type=artifact_type,
            artifact=result.model_dump(mode="json"),
        )

    def _execute_shell_action(
        self,
        action: PlannedAction,
        *,
        policy_evaluation: PolicyEvaluationRecord | None,
        request_cwd: Path,
        request_id: str,
    ) -> ExecutionResult:
        assert action.shell is not None
        shell_action = action.shell
        shell_cwd = request_cwd if shell_action.cwd is None else Path(shell_action.cwd)
        effective_timeout = shell_action.timeout_seconds
        effective_capture_limit_kb: int | None = None
        if policy_evaluation is not None:
            budget = (
                policy_evaluation.verdict.constraints or policy_evaluation.policy_input.constraints
            ).invocation_budget
            if budget is not None:
                effective_capture_limit_kb = budget.output_limit_kb
                if budget.timeout_seconds is not None and effective_timeout is not None:
                    effective_timeout = min(effective_timeout, budget.timeout_seconds)
        try:
            result = self._shell_runtime.execute(
                ShellCommandRequest(
                    command=shell_action.command,
                    args=shell_action.args,
                    cwd=shell_cwd,
                    timeout_seconds=effective_timeout,
                    capture_limit_kb=effective_capture_limit_kb,
                    mode=ExecutionMode(shell_action.mode.value),
                    approval_context={
                        "source": "orchestrator",
                        "action_id": action.id,
                        "request_id": request_id,
                    },
                ),
                on_event=self._shell_output_callback,
            )
        except ValueError as exc:
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Shell execution was rejected: {exc}",
                error=str(exc),
            )
        except ShellExecutionSpawnError as exc:
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Shell execution failed to start: {exc}",
                error=str(exc),
            )
        except ShellExecutionTimeout as exc:
            artifact = exc.result.model_dump(mode="json") if exc.result is not None else None
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Shell execution timed out: {exc}",
                artifact_type=ExecutionArtifactType.SHELL if artifact is not None else None,
                artifact=artifact,
                error=str(exc),
            )
        except ShellExecutionCancelled as exc:
            artifact = exc.result.model_dump(mode="json") if exc.result is not None else None
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Shell execution was cancelled: {exc}",
                artifact_type=ExecutionArtifactType.SHELL if artifact is not None else None,
                artifact=artifact,
                error=str(exc),
            )

        status = ExecutionStatus.EXECUTED if result.ok else ExecutionStatus.FAILED
        return ExecutionResult(
            action_id=action.id,
            status=status,
            summary=f"Executed shell command `{result.display_command}`.",
            artifact_type=ExecutionArtifactType.SHELL,
            artifact=result.model_dump(mode="json"),
            error=None if result.ok else result.stderr or f"Exit code {result.exit_code}",
        )
