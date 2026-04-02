"""Typed Stage 5 request orchestration."""

from __future__ import annotations

import json
import logging
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
    ProviderMessage,
    ProviderMessageRole,
    ProviderPrompt,
    ProviderResponseFormat,
    ProviderResponseMetadata,
    SessionKind,
    SessionStatus,
    ToolCall,
    ToolName,
    UserRequest,
)
from foundation.services.approval import ApprovalService
from foundation.services.guardrails import GuardrailPolicyEngine
from foundation.services.history import HistoryStore
from foundation.services.provider import ProviderAdapter, ProviderError, ProviderErrorCode
from foundation.services.shell import (
    ExecutionMode,
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
    ToolAvailabilityStatus,
    ToolExecutionError,
)
from foundation.settings import ApprovalMode

logger = logging.getLogger("foundation.services.orchestrator")

_MAX_PLAN_ACTIONS = 5


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
        max_plan_attempts: int = 2,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._approval_mode = approval_mode
        self._provider = provider
        self._shell_runtime = shell_runtime
        self._tool_service = tool_service
        self._policy_engine = policy_engine or GuardrailPolicyEngine(
            workspace_root=self._workspace_root
        )
        self._approval_service = approval_service or ApprovalService(mode=approval_mode)
        self._history_store = history_store
        self._max_plan_attempts = max_plan_attempts

    def orchestrate(self, request: UserRequest) -> OrchestrationResult:
        """Run the Stage 6 orchestration flow for one user request."""
        resolved_request_cwd = self._resolve_request_cwd(request.cwd)
        session_id: str | None = None
        if self._history_store is not None:
            session_id = self._history_store.start_session(
                kind=SessionKind.CHAT,
                workspace_root=self._workspace_root,
                request_cwd=resolved_request_cwd,
                approval_mode=self._approval_mode.value,
                plan_only=request.plan_only,
                request_text=request.message,
            )

        try:
            context = self._gather_context(resolved_request_cwd)
            plan, planning_metadata = self._request_plan(request, context)

            if self._history_store is not None and session_id is not None:
                self._history_store.record_plan(
                    session_id,
                    assistant_message=plan.assistant_message,
                    context=context.model_dump(mode="json"),
                    plan=plan.model_dump(mode="json"),
                    planning_metadata=planning_metadata.model_dump(mode="json"),
                )

            logger.info(
                "orchestration_plan_ready actions=%s approval_mode=%s",
                len(plan.actions),
                self._approval_mode.value,
            )

            decisions = [
                self._policy_engine.decide(action, request_cwd=resolved_request_cwd)
                for action in plan.actions
            ]
            execution_results: list[ExecutionResult] = []
            for action, decision in zip(plan.actions, decisions, strict=True):
                execution_result, approval_request, approval_resolution = self._handle_action(
                    action,
                    decision,
                    request=request,
                    resolved_request_cwd=resolved_request_cwd,
                )
                execution_results.append(execution_result)
                if self._history_store is not None and session_id is not None:
                    if approval_request is not None and approval_resolution is not None:
                        self._history_store.record_approval(
                            session_id,
                            request=approval_request,
                            resolution=approval_resolution,
                        )
                    self._record_action_history(
                        session_id,
                        action=action,
                        decision=decision,
                        execution_result=execution_result,
                        resolved_request_cwd=resolved_request_cwd,
                    )

            summary = self._build_summary(plan, execution_results, plan_only=request.plan_only)
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
                    status=(
                        SessionStatus.FAILED
                        if summary.failed_actions > 0
                        else SessionStatus.COMPLETED
                    ),
                )

            return OrchestrationResult(
                session_id=session_id,
                request=request,
                context=context,
                plan=plan,
                planning_metadata=planning_metadata,
                policy_decisions=decisions,
                execution_results=execution_results,
                assistant_message=assistant_message,
                summary=summary,
            )
        except Exception as exc:
            if self._history_store is not None and session_id is not None:
                self._history_store.record_event(
                    session_id,
                    "orchestration_error",
                    {
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                    },
                )
                self._history_store.finalize_session(session_id, status=SessionStatus.FAILED)
            raise

    def _gather_context(self, request_cwd: Path) -> ContextSnapshot:
        availability = self._tool_service.availability_report()
        available_tools = [
            item.name
            for item in availability
            if item.status is ToolAvailabilityStatus.AVAILABLE
        ]
        notes: list[str] = []
        git_context: dict[str, object] | None = None

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
            git_context=git_context,
            notes=notes,
        )

    def _request_plan(
        self,
        request: UserRequest,
        context: ContextSnapshot,
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
                        "tool": "search | files | git | man | tldr",
                        "arguments": "tool-specific JSON object",
                    },
                }
            ],
        }
        instructions = (
            "You are the planning model for Foundation CLI Stage 6. "
            f"Return at most {_MAX_PLAN_ACTIONS} actions. "
            "Prefer typed tool_call actions over shell actions when a local tool can answer "
            "the request. "
            "Use shell actions only for simple read-only inspection commands. "
            "Do not assume command or tool output before execution. "
            "If an action is risky, mutating, networked, or uncertain, mark requires_approval=true "
            "and explain why in approval_reason. "
            "If the user can be answered directly, return zero actions or an explanation action. "
            "Supported tools and their arguments:\n"
            "- search: query, scope, max_results, case_sensitive\n"
            "- files: pattern, scope, file_type, max_results\n"
            "- git: scope, max_status_entries, max_recent_commits\n"
            "- man: topic, max_characters\n"
            "- tldr: topic, max_characters\n"
            "Action shape guide:\n"
            f"{json.dumps(schema_outline, indent=2)}\n"
            "Context JSON:\n"
            f"{json.dumps(context.model_dump(mode='json'), indent=2)}"
        )
        return [
            ProviderMessage(role=ProviderMessageRole.DEVELOPER, content=instructions),
            ProviderMessage(role=ProviderMessageRole.USER, content=request.message),
        ]

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
        *,
        request: UserRequest,
        resolved_request_cwd: Path,
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
            approval_request, approval_resolution = self._approval_service.resolve(
                action,
                decision,
                request_cwd=resolved_request_cwd,
            )
            if approval_resolution.status is ApprovalDecisionStatus.PENDING:
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

        if action.kind is ActionKind.TOOL_CALL:
            assert action.tool_call is not None
            return (
                self._execute_tool_call(action, action.tool_call),
                approval_request,
                approval_resolution,
            )

        assert action.shell is not None
        return (
            self._execute_shell_action(action, request_cwd=resolved_request_cwd),
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
                tool=action.tool_call.tool.value,
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

    def _execute_tool_call(self, action: PlannedAction, tool_call: ToolCall) -> ExecutionResult:
        result: SearchResult | FileDiscoveryResult | GitContextResult | HelpLookupResult
        try:
            if tool_call.tool is ToolName.SEARCH:
                search_request = SearchRequest.model_validate(tool_call.arguments)
                result = self._tool_service.search(search_request)
                artifact_type = ExecutionArtifactType.SEARCH
            elif tool_call.tool is ToolName.FILES:
                result = self._tool_service.discover_files(
                    FileDiscoveryRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.FILES
            elif tool_call.tool is ToolName.GIT:
                result = self._tool_service.git_context(
                    GitContextRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT
            elif tool_call.tool is ToolName.MAN:
                result = self._tool_service.lookup_help(
                    HelpLookupRequest.model_validate(
                        {
                            **tool_call.arguments,
                            "source": HelpLookupSource.MAN,
                        }
                    )
                )
                artifact_type = ExecutionArtifactType.MAN
            else:
                result = self._tool_service.lookup_help(
                    HelpLookupRequest.model_validate(
                        {
                            **tool_call.arguments,
                            "source": HelpLookupSource.TLDR,
                        }
                    )
                )
                artifact_type = ExecutionArtifactType.TLDR
        except ToolExecutionError as exc:
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Tool execution failed: {exc.error.message}",
                error=exc.error.message,
            )

        summary = f"Executed tool `{tool_call.tool.value}` for action {action.id}."
        return ExecutionResult(
            action_id=action.id,
            status=ExecutionStatus.EXECUTED,
            summary=summary,
            artifact_type=artifact_type,
            artifact=result.model_dump(mode="json"),
        )

    def _execute_shell_action(self, action: PlannedAction, *, request_cwd: Path) -> ExecutionResult:
        assert action.shell is not None
        shell_action = action.shell
        shell_cwd = request_cwd if shell_action.cwd is None else Path(shell_action.cwd)
        try:
            result = self._shell_runtime.execute(
                ShellCommandRequest(
                    command=shell_action.command,
                    args=shell_action.args,
                    cwd=shell_cwd,
                    timeout_seconds=shell_action.timeout_seconds,
                    mode=ExecutionMode(shell_action.mode.value),
                    approval_context={
                        "source": "orchestrator",
                        "action_id": action.id,
                    },
                )
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
    ) -> SearchRequest | FileDiscoveryRequest | GitContextRequest | HelpLookupRequest:
        arguments = dict(tool_call.arguments)
        if tool_call.tool is ToolName.SEARCH:
            return SearchRequest.model_validate(arguments)
        if tool_call.tool is ToolName.FILES:
            return FileDiscoveryRequest.model_validate(arguments)
        if tool_call.tool is ToolName.GIT:
            return GitContextRequest.model_validate(arguments)
        if tool_call.tool is ToolName.MAN:
            return HelpLookupRequest.model_validate(
                {
                    **arguments,
                    "source": HelpLookupSource.MAN,
                }
            )
        if tool_call.tool is ToolName.TLDR:
            return HelpLookupRequest.model_validate(
                {
                    **arguments,
                    "source": HelpLookupSource.TLDR,
                }
            )
        raise OrchestrationPlanError(f"Unsupported tool name: {tool_call.tool.value}")

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
