"""Executor service for Stage 3 runtime splitting."""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
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
    PolicyReasonCode,
    QuestionAction,
    ShellAction,
    ToolCall,
)
from foundation.models.file import (
    FileApplyDiffRequest,
    FileEditRequest,
    FileMutationResult,
    FileReadChunkRequest,
    FileReadChunkResult,
    FileReadRequest,
    FileReadResult,
    FileServiceError,
    FileWriteRequest,
)
from foundation.models.git import (
    GitCommitRequest,
    GitDiffRequest,
    GitDiffResult,
    GitLogRequest,
    GitLogResult,
    GitMutationResult,
    GitServiceError,
    GitShowRequest,
    GitShowResult,
    GitStageRequest,
    GitStatusRequest,
    GitStatusResult,
    GitUnstageRequest,
)
from foundation.observability import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_RESOLVED,
    EVENT_QUESTION_ANSWERED,
    EVENT_QUESTION_ASKED,
    EVENT_SHELL_EXECUTION_FAILED,
    EVENT_SHELL_EXECUTION_FINISHED,
    EVENT_SHELL_EXECUTION_STARTED,
    EVENT_TOOL_CALL_FAILED,
    EVENT_TOOL_CALL_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    EVENT_TOOL_EXECUTION_FAILED,
    EVENT_TOOL_EXECUTION_FINISHED,
    EVENT_TOOL_EXECUTION_STARTED,
)
from foundation.services.approval import ApprovalService
from foundation.services.capabilities import CapabilityRegistry
from foundation.services.file_service import FileService
from foundation.services.git_service import GitService
from foundation.services.guardrails import GuardrailPolicyEngine
from foundation.services.observer import ObserverService
from foundation.services.scope_grants import ScopeGrantStore
from foundation.services.shell import (
    ExecutionMode,
    OutputCallback,
    ShellCommandRequest,
    ShellCommandResult,
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

_SHELL_OUTPUT_PREVIEW_LIMIT = 240


class ExecutorInvariantError(RuntimeError):
    """An internal executor invariant was violated.

    Raised instead of ``assert`` so the guard survives ``python -O`` and
    surfaces as a typed FAILED result rather than an interpreter crash.
    """


def _require[T](value: T | None, *, description: str) -> T:
    if value is None:
        raise ExecutorInvariantError(f"Internal executor invariant violated: {description}")
    return value


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _preview_output(text: str, *, limit: int = _SHELL_OUTPUT_PREVIEW_LIMIT) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            text = stripped
            break
    else:
        text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def _shell_result_event_payload(
    *,
    action_id: str,
    request_id: str,
    command_preview: str,
    result: ShellCommandResult,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "action_id": action_id,
        "command_preview": command_preview,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "stdout_preview": _preview_output(result.stdout),
        "stderr_preview": _preview_output(result.stderr),
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }


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
        file_service: FileService | None = None,
        git_service: GitService | None = None,
        question_callback: Callable[[QuestionAction], str | None] | None = None,
        grant_store: ScopeGrantStore | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._shell_runtime = shell_runtime
        self._tool_service = tool_service
        self._policy_engine = policy_engine
        self._approval_service = approval_service
        self._capability_registry = capability_registry
        self._observer = observer
        self._shell_output_callback = shell_output_callback
        self._file_service = file_service
        self._git_service = git_service
        self._question_callback = question_callback
        self._grant_store = grant_store

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
        try:
            execution_result, approval_request, approval_resolution = self._handle_action(
                action,
                decision,
                policy_evaluation=policy_evaluation,
                plan_only=plan_only,
                request_cwd=request_cwd,
                request_id=request_id,
                session_id=session_id,
            )
        except ExecutorInvariantError as exc:
            execution_result = ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Internal error: {exc}",
                error=str(exc),
            )
            approval_request = None
            approval_resolution = None
        completed_at = _utcnow()
        return ActionExecutionEnvelope(
            execution_result=execution_result,
            approval_request=approval_request,
            approval_resolution=approval_resolution,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=max(time.monotonic() - started_monotonic, 0.0),
        )

    def _handle_question(
        self,
        action_id: str,
        question: QuestionAction,
        *,
        request_id: str,
        session_id: str | None,
    ) -> ExecutionResult:
        self._observer.emit(
            EVENT_QUESTION_ASKED,
            payload={
                "request_id": request_id,
                "action_id": action_id,
                "prompt": question.prompt,
                "options": question.options,
            },
            session_id=session_id,
            logger_name="foundation.services.executor",
        )
        artifact: dict[str, object] = {
            "question": question.prompt,
            "options": question.options,
        }
        # No interactive prompt available (non-TTY / one-shot run): stop the
        # loop and surface the question rather than guessing an answer.
        if self._question_callback is None:
            return ExecutionResult(
                action_id=action_id,
                status=ExecutionStatus.AWAITING_INPUT,
                summary=question.prompt,
                artifact_type=ExecutionArtifactType.QUESTION,
                artifact={**artifact, "answer": None},
            )
        answer = self._question_callback(question)
        self._observer.emit(
            EVENT_QUESTION_ANSWERED,
            payload={
                "request_id": request_id,
                "action_id": action_id,
                "answered": answer is not None,
            },
            session_id=session_id,
            logger_name="foundation.services.executor",
        )
        if answer is None:
            return ExecutionResult(
                action_id=action_id,
                status=ExecutionStatus.AWAITING_INPUT,
                summary=f"Unanswered question: {question.prompt}",
                artifact_type=ExecutionArtifactType.QUESTION,
                artifact={**artifact, "answer": None},
            )
        return ExecutionResult(
            action_id=action_id,
            status=ExecutionStatus.EXECUTED,
            summary=f"Asked the user: {question.prompt}",
            artifact_type=ExecutionArtifactType.QUESTION,
            artifact={**artifact, "answer": answer},
        )

    def _handle_scope_escalation(
        self,
        action: PlannedAction,
        decision: PolicyDecision,
        *,
        request_id: str,
        session_id: str | None,
    ) -> ExecutionResult | None:
        """Ask the user to allow an out-of-scope read; grant on approval.

        Returns a BLOCKED result if the user declines (or cannot be prompted),
        or None if access was granted and execution should proceed.
        """
        paths = list(decision.paths)
        display = ", ".join(paths) if paths else "a path outside your workspace"
        question = QuestionAction(
            prompt=(
                f"The agent wants to read {display}, which is outside your workspace "
                "root. Allow read access for this session?"
            ),
            options=["Allow for this session", "Deny"],
            allow_free_text=False,
        )
        self._observer.emit(
            EVENT_QUESTION_ASKED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "prompt": question.prompt,
                "kind": "scope_escalation",
            },
            session_id=session_id,
            logger_name="foundation.services.executor",
        )
        answer = self._question_callback(question) if self._question_callback is not None else None
        self._observer.emit(
            EVENT_QUESTION_ANSWERED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "granted": answer == "Allow for this session",
            },
            session_id=session_id,
            logger_name="foundation.services.executor",
        )
        if answer != "Allow for this session":
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.BLOCKED,
                summary="Out-of-scope read was not approved.",
                error="Out-of-scope read was not approved.",
            )
        if self._grant_store is not None:
            for raw in paths:
                target = Path(raw).expanduser()
                if not target.is_absolute():
                    target = self._workspace_root / target
                root = target if target.is_dir() else target.parent
                self._grant_store.grant(root)
        return None

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

        if (
            decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
            and PolicyReasonCode.SCOPE_ESCALATION in decision.reason_codes
        ):
            blocked = self._handle_scope_escalation(
                action,
                decision,
                request_id=request_id,
                session_id=session_id,
            )
            if blocked is not None:
                return (blocked, None, None)
            # Granted: fall through to normal execution with the grant recorded.
            decision = PolicyDecision(
                action_id=decision.action_id,
                decision=PolicyDecisionType.ALLOW,
                reason="Out-of-scope read approved for this session.",
                risk_categories=list(decision.risk_categories),
                command_preview=decision.command_preview,
                paths=list(decision.paths),
            )
            approval_request = None
            approval_resolution = None
        elif decision.decision is PolicyDecisionType.REQUIRE_APPROVAL:
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

        if action.kind is ActionKind.QUESTION:
            question = _require(
                action.question,
                description=f"QUESTION action {action.id!r} is missing its question payload",
            )
            return (
                self._handle_question(
                    action.id,
                    question,
                    request_id=request_id,
                    session_id=session_id,
                ),
                approval_request,
                approval_resolution,
            )

        if policy_evaluation is not None:
            self._policy_engine.register_invocation(policy_evaluation)

        if action.kind is ActionKind.TOOL_CALL:
            tool_call = _require(
                action.tool_call,
                description=f"TOOL_CALL action {action.id!r} is missing its tool_call payload",
            )
            return (
                self._execute_tool_call(
                    action,
                    tool_call,
                    policy_evaluation=policy_evaluation,
                    request_cwd=request_cwd,
                    request_id=request_id,
                    session_id=session_id,
                ),
                approval_request,
                approval_resolution,
            )

        return (
            self._execute_shell_action(
                action,
                policy_evaluation=policy_evaluation,
                request_cwd=request_cwd,
                request_id=request_id,
                session_id=session_id,
            ),
            approval_request,
            approval_resolution,
        )

    def _require_file_service(self, endpoint: str) -> FileService:
        return _require(
            self._file_service,
            description=f"{endpoint} dispatched without a file service wired",
        )

    def _require_git_service(self, endpoint: str) -> GitService:
        return _require(
            self._git_service,
            description=f"{endpoint} dispatched without a git service wired",
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
        result: (
            SearchResult
            | FileDiscoveryResult
            | GitContextResult
            | HelpLookupResult
            | FileReadResult
            | FileReadChunkResult
            | FileMutationResult
            | GitStatusResult
            | GitDiffResult
            | GitShowResult
            | GitLogResult
            | GitMutationResult
        )
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
            elif manifest.runtime_endpoint == "builtin.file.read":
                result = self._require_file_service("builtin.file.read").read(
                    FileReadRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.FILE_READ
            elif manifest.runtime_endpoint == "builtin.file.read_chunk":
                result = self._require_file_service("builtin.file.read_chunk").read_chunk(
                    FileReadChunkRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.FILE_READ_CHUNK
            elif manifest.runtime_endpoint == "builtin.file.write":
                result = self._require_file_service("builtin.file.write").write(
                    FileWriteRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.FILE_WRITE
            elif manifest.runtime_endpoint == "builtin.file.edit":
                result = self._require_file_service("builtin.file.edit").edit(
                    FileEditRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.FILE_EDIT
            elif manifest.runtime_endpoint == "builtin.file.apply_diff":
                result = self._require_file_service("builtin.file.apply_diff").apply_diff(
                    FileApplyDiffRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.FILE_APPLY_DIFF
            elif manifest.runtime_endpoint == "builtin.git.status":
                result = self._require_git_service("builtin.git.status").status(
                    GitStatusRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT_STATUS
            elif manifest.runtime_endpoint == "builtin.git.diff":
                result = self._require_git_service("builtin.git.diff").diff(
                    GitDiffRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT_DIFF
            elif manifest.runtime_endpoint == "builtin.git.show":
                result = self._require_git_service("builtin.git.show").show(
                    GitShowRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT_SHOW
            elif manifest.runtime_endpoint == "builtin.git.log":
                result = self._require_git_service("builtin.git.log").log(
                    GitLogRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT_LOG
            elif manifest.runtime_endpoint == "builtin.git.stage":
                result = self._require_git_service("builtin.git.stage").stage(
                    GitStageRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT_STAGE
            elif manifest.runtime_endpoint == "builtin.git.unstage":
                result = self._require_git_service("builtin.git.unstage").unstage(
                    GitUnstageRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT_UNSTAGE
            elif manifest.runtime_endpoint == "builtin.git.commit":
                result = self._require_git_service("builtin.git.commit").commit(
                    GitCommitRequest.model_validate(tool_call.arguments)
                )
                artifact_type = ExecutionArtifactType.GIT_COMMIT
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
                    session_id=session_id,
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
        except GitServiceError as exc:
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
                summary=f"Git operation failed: {exc.error.message}",
                error=exc.error.message,
                artifact=exc.error.model_dump(mode="json"),
            )
        except FileServiceError as exc:
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
                summary=f"File operation failed: {exc.error.message}",
                error=exc.error.message,
                artifact=exc.error.model_dump(mode="json"),
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
        session_id: str | None,
    ) -> ExecutionResult:
        shell_action = _require(
            action.shell,
            description=f"SHELL action {action.id!r} is missing its shell payload",
        )
        shell_cwd = request_cwd if shell_action.cwd is None else Path(shell_action.cwd)
        command_preview = shlex.join([shell_action.command, *shell_action.args])
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
        self._observer.emit(
            EVENT_SHELL_EXECUTION_STARTED,
            payload={
                "request_id": request_id,
                "action_id": action.id,
                "command_preview": command_preview,
                "cwd": str(shell_cwd),
            },
            session_id=session_id,
            logger_name="foundation.services.orchestrator",
        )
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
            self._emit_shell_failed(
                action_id=action.id,
                request_id=request_id,
                session_id=session_id,
                command_preview=command_preview,
                error=str(exc),
            )
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Shell execution was rejected: {exc}",
                error=str(exc),
            )
        except ShellExecutionSpawnError as exc:
            self._emit_shell_failed(
                action_id=action.id,
                request_id=request_id,
                session_id=session_id,
                command_preview=command_preview,
                error=str(exc),
            )
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Shell execution failed to start: {exc}",
                error=str(exc),
            )
        except ShellExecutionTimeout as exc:
            artifact = exc.result.model_dump(mode="json") if exc.result is not None else None
            self._emit_shell_failed(
                action_id=action.id,
                request_id=request_id,
                session_id=session_id,
                command_preview=command_preview,
                error=str(exc),
                result=exc.result,
            )
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
            self._emit_shell_failed(
                action_id=action.id,
                request_id=request_id,
                session_id=session_id,
                command_preview=command_preview,
                error=str(exc),
                result=exc.result,
            )
            return ExecutionResult(
                action_id=action.id,
                status=ExecutionStatus.FAILED,
                summary=f"Shell execution was cancelled: {exc}",
                artifact_type=ExecutionArtifactType.SHELL if artifact is not None else None,
                artifact=artifact,
                error=str(exc),
            )

        status = ExecutionStatus.EXECUTED if result.ok else ExecutionStatus.FAILED
        payload = _shell_result_event_payload(
            action_id=action.id,
            request_id=request_id,
            command_preview=command_preview,
            result=result,
        )
        if result.ok:
            self._observer.emit(
                EVENT_SHELL_EXECUTION_FINISHED,
                payload=payload,
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
        else:
            self._observer.emit(
                EVENT_SHELL_EXECUTION_FAILED,
                payload={
                    **payload,
                    "error": result.stderr or f"Exit code {result.exit_code}",
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
        return ExecutionResult(
            action_id=action.id,
            status=status,
            summary=f"Executed shell command `{result.display_command}`.",
            artifact_type=ExecutionArtifactType.SHELL,
            artifact=result.model_dump(mode="json"),
            error=None if result.ok else result.stderr or f"Exit code {result.exit_code}",
        )

    def _emit_shell_failed(
        self,
        *,
        action_id: str,
        request_id: str,
        session_id: str | None,
        command_preview: str,
        error: str,
        result: ShellCommandResult | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "request_id": request_id,
            "action_id": action_id,
            "command_preview": command_preview,
            "error": error,
        }
        if result is not None:
            payload.update(
                _shell_result_event_payload(
                    action_id=action_id,
                    request_id=request_id,
                    command_preview=command_preview,
                    result=result,
                )
            )
            payload["error"] = error
        self._observer.emit(
            EVENT_SHELL_EXECUTION_FAILED,
            payload=payload,
            session_id=session_id,
            logger_name="foundation.services.orchestrator",
        )
