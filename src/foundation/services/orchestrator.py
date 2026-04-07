"""Typed Stage 5 request orchestration."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from foundation.models import (
    ActionKind,
    ApprovalDecisionStatus,
    ApprovalRequest,
    ApprovalResolution,
    AssistantMessage,
    AssistantPlan,
    ContextSnapshot,
    ExecutionArtifactType,
    ExecutionResult,
    ExecutionStatus,
    OrchestrationResult,
    OrchestrationSummary,
    PlannedAction,
    PolicyDecision,
    PolicyDecisionType,
    PolicyEvaluationRecord,
    ProviderMessage,
    ProviderMessageRole,
    ProviderPrompt,
    ProviderResponseFormat,
    ProviderResponseMetadata,
    SessionKind,
    SessionStatus,
    ShellAction,
    ToolCall,
    UserRequest,
)
from foundation.observability import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_RESOLVED,
    EVENT_EXCEPTION,
    EVENT_PLAN_FAILED,
    EVENT_PLAN_FINISHED,
    EVENT_PLAN_STARTED,
    EVENT_RETRY,
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_TOOL_CALL_FAILED,
    EVENT_TOOL_CALL_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    EVENT_TOOL_EXECUTION_FAILED,
    EVENT_TOOL_EXECUTION_FINISHED,
    EVENT_TOOL_EXECUTION_STARTED,
    EVENT_USER_REQUEST,
    emit_event,
)
from foundation.services.approval import ApprovalService
from foundation.services.capabilities import (
    GIT_CAPABILITY_ID,
    CapabilityRegistry,
    CapabilityStore,
)
from foundation.services.executor import ActionExecutor
from foundation.services.guardrails import GuardrailPolicyEngine
from foundation.services.history import HistoryStore
from foundation.services.observer import ObserverService
from foundation.services.planner import PlannerService, PlanningError
from foundation.services.provider import ProviderAdapter, ProviderError, ProviderErrorCode
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
from foundation.settings import ApprovalMode

logger = logging.getLogger("foundation.services.orchestrator")

_MAX_PLAN_ACTIONS = 5


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class OrchestrationError(RuntimeError):
    """Base error for Stage 5 orchestration failures."""


class OrchestrationPlanError(OrchestrationError):
    """Raised when the provider cannot produce a valid bounded plan."""


class RequestOrchestrator:
    """Gather context, request a structured plan, then execute allowed actions."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        approval_mode: ApprovalMode,
        provider: ProviderAdapter,
        shell_runtime: ShellRuntime,
        tool_service: LocalToolService,
        policy_engine: GuardrailPolicyEngine | None = None,
        approval_service: ApprovalService | None = None,
        history_store: HistoryStore | None = None,
        shell_output_callback: OutputCallback | None = None,
        capability_registry: CapabilityRegistry | None = None,
        capability_store_root: Path | None = None,
        max_plan_attempts: int = 2,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._approval_mode = approval_mode
        self._provider = provider
        self._shell_runtime = shell_runtime
        self._tool_service = tool_service
        store_root = (
            Path(capability_store_root).expanduser().resolve()
            if capability_store_root is not None
            else self._workspace_root / ".foundation" / "capabilities"
        )
        self._capability_registry = capability_registry or CapabilityRegistry(
            store=CapabilityStore(store_root),
            tool_service=self._tool_service,
        )
        self._policy_engine = policy_engine or GuardrailPolicyEngine(
            workspace_root=self._workspace_root,
            capability_registry=self._capability_registry,
        )
        self._approval_service = approval_service or ApprovalService(mode=approval_mode)
        self._history_store = history_store
        self._shell_output_callback = shell_output_callback
        self._max_plan_attempts = max_plan_attempts
        self._observer = ObserverService(
            history_store=self._history_store,
            capability_registry=self._capability_registry,
        )
        self._planner = PlannerService(
            workspace_root=str(self._workspace_root),
            approval_mode=self._approval_mode,
            provider=self._provider,
            tool_service=self._tool_service,
            capability_registry=self._capability_registry,
            max_plan_attempts=max_plan_attempts,
        )
        self._executor = ActionExecutor(
            workspace_root=self._workspace_root,
            shell_runtime=self._shell_runtime,
            tool_service=self._tool_service,
            policy_engine=self._policy_engine,
            approval_service=self._approval_service,
            capability_registry=self._capability_registry,
            observer=self._observer,
            shell_output_callback=self._shell_output_callback,
        )

    def orchestrate(self, request: UserRequest) -> OrchestrationResult:
        """Run the Stage 6 orchestration flow for one user request."""
        request_id = f"req-{uuid.uuid4().hex}"
        resolved_request_cwd = self._resolve_request_cwd(request.cwd)
        session_id: str | None = None
        self._observer.emit(
            EVENT_USER_REQUEST,
            payload={
                "request_id": request_id,
                "request_text": request.message,
                "request_cwd": str(resolved_request_cwd),
                "plan_only": request.plan_only,
                "approval_mode": self._approval_mode.value,
            },
            session_id=None,
            logger_name="foundation.services.orchestrator",
        )
        if self._history_store is not None:
            session_id = self._history_store.start_session(
                kind=SessionKind.CHAT,
                workspace_root=self._workspace_root,
                request_cwd=resolved_request_cwd,
                approval_mode=self._approval_mode.value,
                plan_only=request.plan_only,
                request_text=request.message,
            )
        self._observer.emit(
            EVENT_SESSION_START,
            payload={
                "request_id": request_id,
                "session_id": session_id,
                "plan_only": request.plan_only,
                "approval_mode": self._approval_mode.value,
            },
            session_id=session_id,
            logger_name="foundation.services.orchestrator",
        )

        try:
            context = self._planner.gather_context(request_cwd=str(resolved_request_cwd))
            planning_started_at = _utcnow()
            planning_started_monotonic = time.monotonic()
            self._observer.emit(
                EVENT_PLAN_STARTED,
                payload={"request_id": request_id, "request_text": request.message},
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            try:
                plan, planning_metadata = self._planner.request_plan(
                    request,
                    context,
                    request_id=request_id,
                )
            except PlanningError as exc:
                raise OrchestrationPlanError(str(exc)) from exc
            planning_completed_at = _utcnow()
            planning_duration_seconds = max(time.monotonic() - planning_started_monotonic, 0.0)

            if self._history_store is not None and session_id is not None:
                self._history_store.record_plan(
                    session_id,
                    assistant_message=plan.assistant_message,
                    context=context.model_dump(mode="json"),
                    plan=plan.model_dump(mode="json"),
                    planning_metadata=planning_metadata.model_dump(mode="json"),
                )
            planning_step_id = self._observer.record_planning_step(
                session_id,
                request_id=request_id,
                request_text=request.message,
                context=context,
                plan_assistant_message=plan.assistant_message,
                actions=plan.actions,
                action_ids=[action.id for action in plan.actions],
                planning_metadata=planning_metadata,
                started_at=planning_started_at,
                completed_at=planning_completed_at,
                duration_seconds=planning_duration_seconds,
            )

            logger.info(
                "orchestration_plan_ready actions=%s approval_mode=%s",
                len(plan.actions),
                self._approval_mode.value,
            )
            self._observer.emit(
                EVENT_PLAN_FINISHED,
                payload={
                    "request_id": request_id,
                    "session_id": session_id,
                    "action_count": len(plan.actions),
                    "approval_mode": self._approval_mode.value,
                    "provider": planning_metadata.provider,
                    "model": planning_metadata.model,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )

            evaluations = [
                self._policy_engine.evaluate(
                    action,
                    request_cwd=resolved_request_cwd,
                    approval_mode=self._approval_mode,
                )
                for action in plan.actions
            ]
            decisions = [
                self._policy_engine.to_policy_decision(evaluation)
                if evaluation is not None
                else PolicyDecision(
                    action_id=action.id,
                    decision=PolicyDecisionType.ALLOW,
                    reason="Explanation-only actions do not execute anything.",
                )
                for action, evaluation in zip(plan.actions, evaluations, strict=True)
            ]
            execution_results: list[ExecutionResult] = []
            candidate_capability_ids = [
                str(snapshot.capability_id) for snapshot in context.available_capabilities
            ]
            prior_step_id: str | None = None
            for action, decision, evaluation in zip(
                plan.actions,
                decisions,
                evaluations,
                strict=True,
            ):
                if (
                    self._history_store is not None
                    and session_id is not None
                    and evaluation is not None
                ):
                    self._history_store.record_policy_evaluation(
                        session_id,
                        record=evaluation,
                    )
                execution = self._executor.execute(
                    action,
                    decision,
                    policy_evaluation=evaluation,
                    plan_only=request.plan_only,
                    request_cwd=resolved_request_cwd,
                    request_id=request_id,
                    session_id=session_id,
                )
                execution_results.append(execution.execution_result)
                if self._history_store is not None and session_id is not None:
                    if (
                        execution.approval_request is not None
                        and execution.approval_resolution is not None
                    ):
                        self._history_store.record_approval(
                            session_id,
                            request=execution.approval_request,
                            resolution=execution.approval_resolution,
                        )
                    self._record_action_history(
                        session_id,
                        action=action,
                        decision=decision,
                        execution_result=execution.execution_result,
                        resolved_request_cwd=resolved_request_cwd,
                    )
                prior_step_id = self._observer.record_execution_step(
                    session_id,
                    request_id=request_id,
                    action=action,
                    request_cwd=resolved_request_cwd,
                    execution_result=execution.execution_result,
                    policy_evaluation=evaluation,
                    approval_request=execution.approval_request,
                    approval_resolution=execution.approval_resolution,
                    candidate_capability_ids=candidate_capability_ids,
                    planning_step_id=planning_step_id,
                    prior_step_id=prior_step_id,
                    started_at=execution.started_at,
                    completed_at=execution.completed_at,
                    duration_seconds=execution.duration_seconds,
                )

            summary = self._build_summary(plan, execution_results, plan_only=request.plan_only)
            self._observer.emit(
                EVENT_SESSION_END,
                payload={
                    "request_id": request_id,
                    "session_id": session_id,
                    "status": self._session_status_for_summary(summary).value,
                    "executed_actions": summary.executed_actions,
                    "pending_approval_actions": summary.pending_approval_actions,
                    "blocked_actions": summary.blocked_actions,
                    "failed_actions": summary.failed_actions,
                    "skipped_actions": summary.skipped_actions,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            assistant_message = AssistantMessage(content=plan.assistant_message)
            if self._history_store is not None and session_id is not None:
                self._history_store.record_summary(
                    session_id,
                    assistant_message=assistant_message.content,
                    summary_text=summary.text,
                    executed_actions=summary.executed_actions,
                    pending_approval_actions=summary.pending_approval_actions,
                    blocked_actions=summary.blocked_actions,
                    failed_actions=summary.failed_actions,
                    skipped_actions=summary.skipped_actions,
                )
                self._history_store.finalize_session(
                    session_id,
                    status=self._session_status_for_summary(summary),
                )

            return OrchestrationResult(
                session_id=session_id,
                request=request,
                context=context,
                plan=plan,
                planning_metadata=planning_metadata,
                policy_decisions=decisions,
                execution_results=execution_results,
                policy_evaluations=[item for item in evaluations if item is not None],
                assistant_message=assistant_message,
                summary=summary,
            )
        except Exception as exc:
            self._observer.emit_exception(
                EVENT_EXCEPTION,
                exc,
                payload={
                    "request_id": request_id,
                    "session_id": session_id,
                    "request_text": request.message,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            self._observer.emit_exception(
                EVENT_PLAN_FAILED,
                exc,
                payload={"request_id": request_id, "session_id": session_id},
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            self._observer.emit(
                EVENT_SESSION_END,
                payload={
                    "request_id": request_id,
                    "session_id": session_id,
                    "status": SessionStatus.FAILED.value,
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            if self._history_store is not None and session_id is not None:
                self._history_store.finalize_session(session_id, status=SessionStatus.FAILED)
            raise

    def _gather_context(self, request_cwd: Path) -> ContextSnapshot:
        capability_snapshots = self._capability_registry.planner_snapshot()
        available_tools = [
            str(item.capability_id)
            for item in capability_snapshots
            if item.kind.value == "tool"
        ]
        notes: list[str] = []
        git_context: dict[str, object] | None = None

        if any(str(item.capability_id) == GIT_CAPABILITY_ID for item in capability_snapshots):
            try:
                git_result = self._tool_service.git_context(
                    GitContextRequest(
                        scope=request_cwd,
                        max_status_entries=10,
                        max_recent_commits=3,
                    )
                )
            except ToolExecutionError as exc:
                notes.append(f"Git context unavailable: {exc.error.message}")
            else:
                git_context = git_result.model_dump(mode="json")

        return ContextSnapshot(
            workspace_root=str(self._workspace_root),
            request_cwd=str(request_cwd),
            approval_mode=self._approval_mode.value,
            available_tools=available_tools,
            available_capabilities=capability_snapshots,
            git_context=git_context,
            notes=notes,
        )

    def _request_plan(
        self,
        request: UserRequest,
        context: ContextSnapshot,
        request_id: str,
    ) -> tuple[AssistantPlan, ProviderResponseMetadata]:
        base_messages = self._base_plan_messages(request, context)
        supplemental_messages: list[ProviderMessage] = []
        last_error: Exception | None = None

        for attempt in range(1, self._max_plan_attempts + 1):
            prompt = ProviderPrompt(
                messages=[*base_messages, *supplemental_messages],
                response_format=ProviderResponseFormat.JSON_OBJECT,
                schema_name="assistant_plan",
                output_schema=AssistantPlan.model_json_schema(),
            )
            try:
                response = self._provider.complete(prompt)
            except ProviderError as exc:
                last_error = exc
                if (
                    exc.code is ProviderErrorCode.INVALID_RESPONSE
                    and attempt < self._max_plan_attempts
                ):
                    emit_event(
                        EVENT_RETRY,
                        payload={
                            "request_id": request_id,
                            "attempt": attempt,
                            "error": str(exc),
                        },
                        logger_name="foundation.services.orchestrator",
                    )
                    emit_event(
                        EVENT_PLAN_FAILED,
                        payload={
                            "request_id": request_id,
                            "attempt": attempt,
                            "error": str(exc),
                        },
                        logger_name="foundation.services.orchestrator",
                    )
                    supplemental_messages = self._repair_messages(
                        "The previous response was not valid JSON.",
                        invalid_output=exc.response_text,
                    )
                    continue
                raise

            if response.structured_output is None:
                last_error = OrchestrationPlanError(
                    "Provider did not return structured output for the plan request."
                )
                if attempt < self._max_plan_attempts:
                    emit_event(
                        EVENT_RETRY,
                        payload={
                            "request_id": request_id,
                            "attempt": attempt,
                            "error": str(last_error),
                        },
                        logger_name="foundation.services.orchestrator",
                    )
                    emit_event(
                        EVENT_PLAN_FAILED,
                        payload={
                            "request_id": request_id,
                            "attempt": attempt,
                            "error": str(last_error),
                        },
                        logger_name="foundation.services.orchestrator",
                    )
                    supplemental_messages = self._repair_messages(
                        "The previous response omitted the required JSON object."
                    )
                    continue
                break

            try:
                plan = AssistantPlan.model_validate(response.structured_output)
                self._validate_supported_actions(plan)
            except (ValidationError, OrchestrationPlanError) as exc:
                last_error = exc
                if attempt < self._max_plan_attempts:
                    emit_event(
                        EVENT_RETRY,
                        payload={
                            "request_id": request_id,
                            "attempt": attempt,
                            "error": str(exc),
                        },
                        logger_name="foundation.services.orchestrator",
                    )
                    emit_event(
                        EVENT_PLAN_FAILED,
                        payload={
                            "request_id": request_id,
                            "attempt": attempt,
                            "error": str(exc),
                        },
                        logger_name="foundation.services.orchestrator",
                    )
                if attempt < self._max_plan_attempts:
                    supplemental_messages = self._repair_messages(
                        f"The previous JSON failed validation: {exc}",
                        invalid_output=response.content,
                    )
                    continue
                break

            return plan, response.metadata

        detail = str(last_error) if last_error is not None else "Unknown planning failure."
        raise OrchestrationPlanError(
            "The provider did not produce a valid structured plan after "
            f"{self._max_plan_attempts} attempt(s): {detail}"
        )

    def _base_plan_messages(
        self,
        request: UserRequest,
        context: ContextSnapshot,
    ) -> list[ProviderMessage]:
        schema_outline = {
            "assistant_message": "string",
            "actions": [
                {
                    "id": "unique action identifier",
                    "kind": "explanation | shell | tool_call",
                    "summary": "short description",
                    "requires_approval": "boolean",
                    "approval_reason": "string | null",
                    "explanation": "required for explanation actions",
                    "shell": {
                        "command": "string",
                        "args": ["string"],
                        "cwd": "string | null",
                        "timeout_seconds": "integer | null",
                        "mode": "buffered | stream | pty",
                    },
                    "tool_call": {
                        "capability_id": "foundation.search",
                        "version": "1.0.0 | null",
                        "arguments": "tool-specific JSON object",
                    },
                }
            ],
        }
        capability_guide = [
            {
                "capability_id": str(snapshot.capability_id),
                "version": str(snapshot.version),
                "name": snapshot.name,
                "description": snapshot.description,
                "transport": snapshot.transport.value,
                "risk_class": snapshot.risk_class.value,
                "trust_tier": snapshot.trust_tier.value,
                "declared_side_effects": list(snapshot.declared_side_effects),
                "input_schema": snapshot.input_schema,
            }
            for snapshot in context.available_capabilities
        ]
        instructions = (
            "You are the planning model for Foundation CLI v2 Stage 1. "
            f"Return at most {_MAX_PLAN_ACTIONS} actions. "
            "Prefer typed tool_call actions that reference one available capability. "
            "Use shell actions only for simple read-only inspection commands. "
            "Do not assume command or tool output before execution. "
            "If an action is risky, mutating, networked, or uncertain, mark requires_approval=true "
            "and explain why in approval_reason. "
            "If the user can be answered directly, return zero actions or an explanation action. "
            "Available capability snapshot:\n"
            f"{json.dumps(capability_guide, indent=2)}\n"
            "Action shape guide:\n"
            f"{json.dumps(schema_outline, indent=2)}\n"
            "Context JSON:\n"
            f"{json.dumps(context.model_dump(mode='json'), indent=2)}"
        )
        return [
            ProviderMessage(role=ProviderMessageRole.DEVELOPER, content=instructions),
            *request.conversation_history,
            ProviderMessage(role=ProviderMessageRole.USER, content=request.message),
        ]

    def _session_status_for_summary(self, summary: OrchestrationSummary) -> SessionStatus:
        if summary.failed_actions > 0:
            return SessionStatus.FAILED
        if summary.pending_approval_actions > 0:
            return SessionStatus.PENDING_APPROVAL
        return SessionStatus.COMPLETED

    def _repair_messages(
        self,
        validation_feedback: str,
        *,
        invalid_output: str | None = None,
    ) -> list[ProviderMessage]:
        messages: list[ProviderMessage] = []
        if invalid_output:
            messages.append(
                ProviderMessage(
                    role=ProviderMessageRole.ASSISTANT,
                    content=invalid_output,
                )
            )
        messages.append(
            ProviderMessage(
                role=ProviderMessageRole.DEVELOPER,
                content=(
                    f"{validation_feedback} Return a corrected JSON object only. "
                    "Do not include markdown fences."
                ),
            )
        )
        return messages

    def _validate_supported_actions(self, plan: AssistantPlan) -> None:
        if len(plan.actions) > _MAX_PLAN_ACTIONS:
            raise OrchestrationPlanError(
                f"Structured plans are bounded to {_MAX_PLAN_ACTIONS} actions."
            )
        for action in plan.actions:
            if action.kind is ActionKind.SHELL:
                assert action.shell is not None
                if any(character.isspace() for character in action.shell.command):
                    raise OrchestrationPlanError(
                        f"Shell action {action.id!r} must split the executable and args."
                    )
            if action.kind is ActionKind.TOOL_CALL:
                assert action.tool_call is not None
                self._validated_tool_request(action.tool_call)

    def _handle_action(
        self,
        action: PlannedAction,
        decision: PolicyDecision,
        policy_evaluation: PolicyEvaluationRecord | None,
        *,
        request: UserRequest,
        resolved_request_cwd: Path,
        request_id: str,
    ) -> tuple[ExecutionResult, ApprovalRequest | None, ApprovalResolution | None]:
        if request.plan_only:
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
                raise OrchestrationError(
                    f"Approval-required action {action.id!r} is missing a policy evaluation."
                )
            approval_request, approval_resolution = self._approval_service.resolve(
                action,
                policy_evaluation,
                request_cwd=resolved_request_cwd,
            )
            emit_event(
                EVENT_APPROVAL_REQUESTED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "risk_categories": list(decision.risk_categories),
                    "mode": approval_resolution.mode,
                },
                logger_name="foundation.services.approval",
            )
            if approval_resolution.status is ApprovalDecisionStatus.PENDING:
                emit_event(
                    EVENT_APPROVAL_RESOLVED,
                    payload={
                        "request_id": request_id,
                        "action_id": action.id,
                        "status": approval_resolution.status.value,
                    },
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
                emit_event(
                    EVENT_APPROVAL_RESOLVED,
                    payload={
                        "request_id": request_id,
                        "action_id": action.id,
                        "status": approval_resolution.status.value,
                    },
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
                    request_cwd=resolved_request_cwd,
                    request_id=request_id,
                ),
                approval_request,
                approval_resolution,
            )

        assert action.shell is not None
        return (
            self._execute_shell_action(
                action,
                policy_evaluation=policy_evaluation,
                request_cwd=resolved_request_cwd,
                request_id=request_id,
            ),
            approval_request,
            approval_resolution,
        )

    def _record_action_history(
        self,
        session_id: str,
        *,
        action: PlannedAction,
        decision: PolicyDecision,
        execution_result: ExecutionResult,
        resolved_request_cwd: Path,
    ) -> None:
        assert self._history_store is not None

        if action.kind is ActionKind.TOOL_CALL:
            assert action.tool_call is not None
            self._history_store.record_tool_call(
                session_id,
                action_id=action.id,
                tool=action.tool_call.capability_id,
                arguments=dict(action.tool_call.arguments),
                policy_decision=decision.decision.value,
                policy_reason=decision.reason,
                risk_categories=list(decision.risk_categories),
                execution_status=execution_result.status.value,
                artifact=execution_result.artifact,
                error=execution_result.error,
            )
            return

        if action.kind is ActionKind.SHELL:
            assert action.shell is not None
            artifact = (
                execution_result.artifact
                if execution_result.artifact_type is ExecutionArtifactType.SHELL
                and execution_result.artifact is not None
                else {}
            )
            recorded_cwd = artifact.get("cwd")
            if recorded_cwd is None:
                if action.shell.cwd is None:
                    recorded_cwd = str(resolved_request_cwd)
                else:
                    candidate = Path(action.shell.cwd)
                    resolved_cwd = (
                        candidate.resolve()
                        if candidate.is_absolute()
                        else (self._workspace_root / candidate).resolve()
                    )
                    recorded_cwd = str(resolved_cwd)
            self._history_store.record_command(
                session_id,
                action_id=action.id,
                source="orchestrator",
                command=action.shell.command,
                args=list(action.shell.args),
                cwd=str(recorded_cwd),
                mode=str(artifact.get("mode", action.shell.mode.value)),
                policy_decision=decision.decision.value,
                policy_reason=decision.reason,
                risk_categories=list(decision.risk_categories),
                execution_status=execution_result.status.value,
                exit_code=artifact.get("exit_code"),
                duration_seconds=artifact.get("duration_seconds"),
                stdout=str(artifact.get("stdout", "")),
                stderr=str(artifact.get("stderr", "")),
                stdout_truncated=bool(artifact.get("stdout_truncated", False)),
                stderr_truncated=bool(artifact.get("stderr_truncated", False)),
                error=execution_result.error,
            )
            return

        self._history_store.record_event(
            session_id,
            "explanation_recorded",
            {
                "action_id": action.id,
                "execution_status": execution_result.status.value,
                "summary": execution_result.summary,
            },
        )

    def _execute_tool_call(
        self,
        action: PlannedAction,
        tool_call: ToolCall,
        *,
        policy_evaluation: PolicyEvaluationRecord | None,
        request_cwd: Path,
        request_id: str,
    ) -> ExecutionResult:
        emit_event(
            EVENT_TOOL_CALL_STARTED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "tool": tool_call.capability_id,
            },
            logger_name="foundation.services.orchestrator",
        )
        emit_event(
            EVENT_TOOL_EXECUTION_STARTED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "tool": tool_call.capability_id,
            },
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
                raise OrchestrationPlanError(
                    f"Unsupported capability runtime endpoint: {manifest.runtime_endpoint}"
                )
        except (OrchestrationPlanError, ValueError) as exc:
            emit_event(
                EVENT_TOOL_EXECUTION_FAILED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "tool": tool_call.capability_id,
                    "error": str(exc),
                    "code": "invalid_capability",
                },
                logger_name="foundation.services.orchestrator",
            )
            emit_event(
                EVENT_TOOL_CALL_FAILED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "tool": tool_call.capability_id,
                    "error": str(exc),
                    "code": "invalid_capability",
                },
                logger_name="foundation.services.orchestrator",
            )
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Capability execution failed: {exc}",
                error=str(exc),
            )
        except ToolExecutionError as exc:
            emit_event(
                EVENT_TOOL_EXECUTION_FAILED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "tool": tool_call.capability_id,
                    "error": exc.error.message,
                    "code": exc.error.code.value,
                },
                logger_name="foundation.services.orchestrator",
            )
            emit_event(
                EVENT_TOOL_CALL_FAILED,
                payload={
                    "request_id": request_id,
                    "action_id": action.id,
                    "tool": tool_call.capability_id,
                    "error": exc.error.message,
                    "code": exc.error.code.value,
                },
                logger_name="foundation.services.orchestrator",
            )
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Tool execution failed: {exc.error.message}",
                error=exc.error.message,
            )

        summary = f"Executed capability `{tool_call.capability_id}` for action {action.id}."
        emit_event(
            EVENT_TOOL_EXECUTION_FINISHED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "tool": tool_call.capability_id,
                "artifact_type": artifact_type.value,
            },
            logger_name="foundation.services.orchestrator",
        )
        emit_event(
            EVENT_TOOL_CALL_FINISHED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "tool": tool_call.capability_id,
            },
            logger_name="foundation.services.orchestrator",
        )
        return ExecutionResult(
            action_id=action.id,
            status=ExecutionStatus.EXECUTED,
            summary=summary,
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

    def _validated_tool_request(
        self,
        tool_call: ToolCall,
    ) -> SearchRequest | FileDiscoveryRequest | GitContextRequest | HelpLookupRequest | ShellAction:
        manifest = self._capability_registry.resolve(
            tool_call.capability_id,
            tool_call.version,
        )
        arguments = dict(tool_call.arguments)
        if manifest.runtime_endpoint == "builtin.search":
            return SearchRequest.model_validate(arguments)
        if manifest.runtime_endpoint == "builtin.files":
            return FileDiscoveryRequest.model_validate(arguments)
        if manifest.runtime_endpoint == "builtin.git":
            return GitContextRequest.model_validate(arguments)
        if manifest.runtime_endpoint == "builtin.man":
            return HelpLookupRequest.model_validate(
                {
                    **arguments,
                    "source": HelpLookupSource.MAN,
                }
            )
        if manifest.runtime_endpoint == "builtin.tldr":
            return HelpLookupRequest.model_validate(
                {
                    **arguments,
                    "source": HelpLookupSource.TLDR,
                }
            )
        if manifest.runtime_endpoint == "builtin.shell":
            return ShellAction.model_validate(arguments)
        raise OrchestrationPlanError(
            f"Unsupported capability id: {tool_call.capability_id}"
        )

    def _resolve_request_cwd(self, value: Path | None) -> Path:
        if value is None:
            return self._workspace_root
        candidate = value if value.is_absolute() else self._workspace_root / value
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise OrchestrationError(
                "Request cwd must stay within the configured workspace root."
            ) from exc
        if not resolved.exists():
            raise OrchestrationError(f"Request cwd does not exist: {resolved}")
        if not resolved.is_dir():
            raise OrchestrationError(f"Request cwd is not a directory: {resolved}")
        return resolved

    def _build_summary(
        self,
        plan: AssistantPlan,
        execution_results: list[ExecutionResult],
        *,
        plan_only: bool,
    ) -> OrchestrationSummary:
        executed = sum(result.status is ExecutionStatus.EXECUTED for result in execution_results)
        pending = sum(
            result.status is ExecutionStatus.PENDING_APPROVAL for result in execution_results
        )
        blocked = sum(result.status is ExecutionStatus.BLOCKED for result in execution_results)
        failed = sum(result.status is ExecutionStatus.FAILED for result in execution_results)
        skipped = sum(result.status is ExecutionStatus.NOT_EXECUTED for result in execution_results)

        if not plan.actions:
            text = "No actions were needed for this request."
        elif plan_only:
            text = (
                f"Planned {len(plan.actions)} action(s); execution was skipped because plan_only "
                "was requested."
            )
        else:
            text = (
                f"Executed {executed} action(s), {pending} pending approval, "
                f"{failed} failed, {blocked} blocked, and {skipped} skipped."
            )

        return OrchestrationSummary(
            executed_actions=executed,
            pending_approval_actions=pending,
            blocked_actions=blocked,
            failed_actions=failed,
            skipped_actions=skipped,
            text=text,
        )
