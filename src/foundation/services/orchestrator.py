"""Typed Stage 4 request orchestration with bounded replan loop."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from foundation.models import (
    ActionKind,
    ActionOutcome,
    AssistantMessage,
    AssistantPlan,
    ContextSnapshot,
    ExecutionArtifactType,
    ExecutionResult,
    ExecutionStatus,
    GovernanceNotice,
    GovernanceNoticeCode,
    IterationObservation,
    LoopStopReason,
    OrchestrationIteration,
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
    QuestionAction,
    SessionKind,
    SessionStatus,
    UserRequest,
    VerificationNotice,
    VerificationOutcome,
)
from foundation.models.git import GitServiceError, GitStatusRequest
from foundation.models.trace import TraceEdge, TraceEdgeKind
from foundation.observability import (
    EVENT_CAPABILITY_GAP,
    EVENT_EXCEPTION,
    EVENT_ITERATION_COMPLETED,
    EVENT_ITERATION_STARTED,
    EVENT_PLAN_FAILED,
    EVENT_PLAN_FINISHED,
    EVENT_PLAN_STARTED,
    EVENT_SESSION_END,
    EVENT_SESSION_START,
    EVENT_USER_REQUEST,
)
from foundation.services.approval import ApprovalService
from foundation.services.capabilities import CapabilityRegistry, CapabilityStore
from foundation.services.executor import ActionExecutor
from foundation.services.file_service import FileService
from foundation.services.gap_handoff import build_gap_handoff, make_provider_phraser
from foundation.services.git_service import GitService
from foundation.services.guardrails import GuardrailPolicyEngine
from foundation.services.history import HistoryStore
from foundation.services.observer import EventSink, ObserverService
from foundation.services.planner import PlannerService, PlanningError
from foundation.services.provider import ProviderAdapter, ProviderError
from foundation.services.scope_grants import ScopeGrantStore
from foundation.services.shell import OutputCallback, ShellRuntime
from foundation.services.tools import LocalToolService
from foundation.settings import ApprovalMode

logger = logging.getLogger("foundation.services.orchestrator")

_MAX_PLAN_ACTIONS = 40
_MAX_LOOP_ITERATIONS = 32
_MAX_TOTAL_ACTIONS = 200
_OBSERVATION_MAX_BYTES = 8 * 1024
_OBSERVATION_MAX_LINES = 200

_FATAL_ERROR_PATTERNS = frozenset(
    {
        "failed to start",
        "could not start command",
        "unsupported capability",
        "invalid_capability",
        "no such file or directory",
    }
)

# Heuristic intent markers used by the commit-approval runtime invariant.
# When the user's message contains "commit" as a whole word, or names any of
# these phrases, the runtime expects the final iteration to either plan a git
# commit (requires_approval=True) or to explain why it couldn't. Missing both
# triggers a governance notice.
_COMMIT_INTENT_WORD_RE = re.compile(r"\bcommit(?:ing|ted|s)?\b", re.IGNORECASE)
_COMMIT_INTENT_PHRASES = (
    "stop for approval",
    "approve the change",
)
_COMMIT_CAPABILITY_ID = "foundation.git.commit"

# Errors that indicate the binary could not run at all (vs. tests failed).
_VERIFICATION_UNAVAILABLE_PATTERNS = frozenset(
    {
        "failed to start",
        "could not start command",
        "no such file or directory",
        "command not found",
    }
)


def _verification_outcome_for_result(
    result: ExecutionResult,
) -> VerificationOutcome:
    if result.status is ExecutionStatus.EXECUTED:
        return VerificationOutcome.PASSED
    if result.status is ExecutionStatus.FAILED:
        error_lower = (result.error or "").lower()
        if any(p in error_lower for p in _VERIFICATION_UNAVAILABLE_PATTERNS):
            return VerificationOutcome.UNAVAILABLE
        return VerificationOutcome.FAILED
    # Blocked / pending-approval verification commands count as not-attempted.
    return VerificationOutcome.NOT_ATTEMPTED


_VERIFICATION_OUTCOME_SEVERITY = {
    VerificationOutcome.NOT_ATTEMPTED: 0,
    VerificationOutcome.PASSED: 1,
    VerificationOutcome.FAILED: 2,
    VerificationOutcome.UNAVAILABLE: 3,
}


def _worst_verification_outcome(
    a: VerificationOutcome,
    b: VerificationOutcome,
) -> VerificationOutcome:
    """Return whichever outcome reflects the more severe state (worst-wins)."""
    if _VERIFICATION_OUTCOME_SEVERITY[b] > _VERIFICATION_OUTCOME_SEVERITY[a]:
        return b
    return a


_CODE_CHANGING_ARTIFACT_TYPES = frozenset(
    {
        ExecutionArtifactType.FILE_WRITE,
        ExecutionArtifactType.FILE_EDIT,
        ExecutionArtifactType.FILE_APPLY_DIFF,
    }
)

_VERIFICATION_COMMANDS = frozenset(
    {
        "pytest",
        "python",
        "npm",
        "yarn",
        "make",
        "cargo",
        "go",
        "mypy",
        "ruff",
        "flake8",
        "eslint",
        "tsc",
    }
)

_STOP_REASON_SUFFIXES = {
    LoopStopReason.MAX_ITERATIONS: (
        "\n\n[Loop stopped: maximum iteration limit reached. Work may be incomplete.]"
    ),
    LoopStopReason.MAX_ACTIONS: (
        "\n\n[Loop stopped: maximum action budget exhausted. Work may be incomplete.]"
    ),
    LoopStopReason.PENDING_APPROVAL: (
        "\n\n[Loop stopped: an action requires approval before continuing.]"
    ),
    LoopStopReason.AWAITING_USER_INPUT: (
        "\n\n[Loop stopped: waiting for your answer to a question before continuing.]"
    ),
    LoopStopReason.FATAL_EXECUTION_FAILURE: (
        "\n\n[Loop stopped: a fatal execution failure occurred.]"
    ),
    LoopStopReason.NO_PROGRESS: ("\n\n[Loop stopped: no progress detected across iterations.]"),
}

# v4 stage 03 — soft-completion notice when NO_PROGRESS fires *after* the
# loop already produced cumulative changes (i.e. the planner kept retrying
# already-finished work). Selected by the orchestrator at result-build time.
_NO_PROGRESS_SOFT_SUFFIX = "\n\n[Run complete; planner re-issued already-finished actions.]"

# Capabilities whose successful execution should be surfaced in the
# planner's "COMMANDS ALREADY EXECUTED" summary on the next iteration.
_SIDE_EFFECTING_CAPABILITY_PREFIXES: tuple[str, ...] = (
    "foundation.file.write",
    "foundation.file.edit",
    "foundation.file.apply_diff",
    "foundation.git.stage",
    "foundation.git.unstage",
    "foundation.git.commit",
)


def _is_side_effecting_capability(capability_id: str | None) -> bool:
    if not capability_id:
        return False
    return any(
        capability_id == prefix or capability_id.startswith(prefix + ".")
        for prefix in _SIDE_EFFECTING_CAPABILITY_PREFIXES
    )


_TOOL_CALL_LOG_KEYS: tuple[str, ...] = (
    "path",
    "paths",
    "ref",
    "message",
    "summary",
    "branch",
)

# v4 stage 03 — error fragments that mark "soft" idempotent failures the
# detector should ignore when the action's target path was already
# successfully written earlier in the same session.
_SOFT_FAILURE_FRAGMENTS: tuple[str, ...] = (
    "file already exists",
    "nothing to commit",
    "no changes added to commit",
)


def _is_soft_failure_for_path(
    error: str | None, path: str | None, cumulative_changed_paths: set[str]
) -> bool:
    if not error or not path:
        return False
    lowered = error.lower()
    if not any(fragment in lowered for fragment in _SOFT_FAILURE_FRAGMENTS):
        return False
    return path in cumulative_changed_paths


def _unwrap_generated_file_body(text: str) -> str:
    """Salvage raw file content if the model wrapped it in an AssistantPlan blob.

    Some models, when asked to generate file content mid-loop, keep "planning"
    and return a ``{assistant_message, actions: [...]}`` object with the real
    content buried in a write action's ``content`` argument. Detect that shape
    and extract the inner content; otherwise return the text untouched.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return text
    if not isinstance(payload, dict) or "actions" not in payload:
        return text
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return text
    for action in actions:
        if not isinstance(action, dict):
            continue
        tool_call = action.get("tool_call")
        if not isinstance(tool_call, dict):
            continue
        args = tool_call.get("arguments")
        if isinstance(args, dict) and isinstance(args.get("content"), str):
            return args["content"]
    return text


# Read-only typed results whose payload should be surfaced to the planner so it
# can actually use what the tool returned (not just see that it ran). Writes and
# mutations are excluded — echoing their content back only bloats the prompt.
_RESULT_PREVIEW_TYPES = frozenset(
    {
        ExecutionArtifactType.FILE_READ,
        ExecutionArtifactType.FILE_READ_CHUNK,
        ExecutionArtifactType.SEARCH,
        ExecutionArtifactType.FILES,
        ExecutionArtifactType.GIT,
        ExecutionArtifactType.GIT_STATUS,
        ExecutionArtifactType.GIT_DIFF,
        ExecutionArtifactType.GIT_SHOW,
        ExecutionArtifactType.GIT_LOG,
        ExecutionArtifactType.MAN,
        ExecutionArtifactType.TLDR,
    }
)


def _tool_result_preview(
    artifact: dict[str, object] | None,
    artifact_type: ExecutionArtifactType | None,
) -> str:
    """Render a read-only tool result's payload for the planner observation.

    Typed capabilities (file reads, search, discovery, git inspect) don't use
    the shell ``stdout`` field, so without this their output never reaches the
    planner and it can't act on what it just fetched.
    """
    if not artifact or artifact_type not in _RESULT_PREVIEW_TYPES:
        return ""
    content = artifact.get("content")
    if isinstance(content, str) and content:
        return content
    try:
        return json.dumps(artifact, default=str)
    except (TypeError, ValueError):
        return ""


def _action_target_path(action: PlannedAction) -> str | None:
    if action.kind is not ActionKind.TOOL_CALL or action.tool_call is None:
        return None
    args = action.tool_call.arguments or {}
    if not isinstance(args, dict):
        return None
    path = args.get("path")
    if isinstance(path, str):
        return path
    return None


def _filter_results_for_detector(
    execution_results: list[ExecutionResult],
    actions: list[PlannedAction],
    *,
    cumulative_changed_paths: set[str],
) -> list[ExecutionResult]:
    """Return ``execution_results`` with soft failures + probes marked OK.

    The detector's failure fingerprint sees only "real" stuckness signals.
    Soft failures (e.g. ``foundation.file.write`` returning FILE_EXISTS for
    a path the session already wrote) and probe-style failures (a
    ``foundation.file.read`` whose path is the target of a write action
    in the same iteration) are converted to a synthetic NOT_EXECUTED so
    they no longer count toward the failure set.
    """
    actions_by_id = {action.id: action for action in actions}

    write_targets: set[str] = set()
    for action in actions:
        if (
            action.kind is ActionKind.TOOL_CALL
            and action.tool_call is not None
            and action.tool_call.capability_id == "foundation.file.write"
        ):
            target = _action_target_path(action)
            if target:
                write_targets.add(target)

    filtered: list[ExecutionResult] = []
    for result in execution_results:
        if result.status is not ExecutionStatus.FAILED:
            filtered.append(result)
            continue
        action = actions_by_id.get(result.action_id)
        if action is None:
            filtered.append(result)
            continue
        target = _action_target_path(action)
        # Probe: read whose path is also a write target this iteration.
        if (
            action.kind is ActionKind.TOOL_CALL
            and action.tool_call is not None
            and action.tool_call.capability_id == "foundation.file.read"
            and target is not None
            and target in write_targets
        ):
            filtered.append(_demote_to_soft(result))
            continue
        # Soft failure: idempotent error whose path is already in the
        # cumulative changed-paths set.
        if _is_soft_failure_for_path(result.error, target, cumulative_changed_paths):
            filtered.append(_demote_to_soft(result))
            continue
        filtered.append(result)
    return filtered


def _demote_to_soft(result: ExecutionResult) -> ExecutionResult:
    return result.model_copy(
        update={
            "status": ExecutionStatus.NOT_EXECUTED,
            "error": None,
        }
    )


def _format_tool_call_log_entry(tool_call: object) -> str:
    """Render a stable single-line summary of a tool call for the planner log."""
    capability_id = getattr(tool_call, "capability_id", "")
    arguments = getattr(tool_call, "arguments", {}) or {}
    if not isinstance(arguments, dict):
        return f"tool_call:{capability_id}"
    parts: list[str] = []
    for key in _TOOL_CALL_LOG_KEYS:
        if key not in arguments:
            continue
        value = arguments[key]
        if isinstance(value, list):
            joined = ",".join(str(item) for item in value)[:200]
            parts.append(f"{key}={joined}")
        elif isinstance(value, str):
            parts.append(f"{key}={value[:200]}")
        else:
            parts.append(f"{key}={value}")
    if not parts:
        return f"tool_call:{capability_id}"
    return f"tool_call:{capability_id} " + " ".join(parts)


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class NoProgressDetector:
    """Detect replanning loops that make no forward progress.

    The detector requires ``window`` consecutive identical failure (or
    action) fingerprints before declaring stuck. v4 stage 03 sets the
    default to ``2`` to tolerate a single idempotent re-issue. When the
    caller passes ``cumulative_changed_paths`` and that set is non-empty,
    the detector never declares stuck — earlier-iteration progress is
    treated as proof the workspace already moved forward.
    """

    def __init__(self, *, window: int = 2) -> None:
        self._window = max(2, int(window))
        self._failure_fingerprints: list[str] = []
        self._action_fingerprints: list[str] = []

    def is_stuck(
        self,
        execution_results: list[ExecutionResult],
        changed_paths: list[str],
        actions: list[PlannedAction],
        *,
        cumulative_changed_paths: list[str] | None = None,
    ) -> bool:
        failures = sorted(
            r.error or "" for r in execution_results if r.status is ExecutionStatus.FAILED
        )
        has_failures = len(failures) > 0
        failure_fp = hashlib.sha256("|".join(failures).encode()).hexdigest()[:16]

        action_sigs = sorted(
            f"{a.kind}:{a.tool_call.capability_id if a.tool_call else ''}:"
            f"{a.shell.command if a.shell else ''}"
            for a in actions
        )
        action_fp = hashlib.sha256("|".join(action_sigs).encode()).hexdigest()[:16]

        has_changes = len(changed_paths) > 0
        cumulative_has_changes = bool(cumulative_changed_paths)

        # Append fingerprints for this iteration up front so window checks
        # always see a consistent history regardless of the early-return.
        self._failure_fingerprints.append(failure_fp)
        self._action_fingerprints.append(action_fp)

        # Only detect no-progress when this iteration has failures and no
        # changes, AND the cumulative session hasn't already moved forward.
        if not has_failures or has_changes or cumulative_has_changes:
            return False

        if len(self._failure_fingerprints) >= self._window and all(
            fp == failure_fp for fp in self._failure_fingerprints[-self._window :]
        ):
            return True
        if len(self._action_fingerprints) >= self._window and all(
            fp == action_fp for fp in self._action_fingerprints[-self._window :]
        ):
            return True
        return False


class OrchestrationError(RuntimeError):
    """Base error for orchestration failures."""


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
        event_sink: EventSink | None = None,
        question_callback: Callable[[QuestionAction], str | None] | None = None,
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
        # Shared, session-scoped read grants for out-of-workspace escalation.
        self._grant_store = ScopeGrantStore()
        self._policy_engine = policy_engine or GuardrailPolicyEngine(
            workspace_root=self._workspace_root,
            capability_registry=self._capability_registry,
            grant_store=self._grant_store,
        )
        self._approval_service = approval_service or ApprovalService(mode=approval_mode)
        self._history_store = history_store
        self._shell_output_callback = shell_output_callback
        self._max_plan_attempts = max_plan_attempts
        self._observer = ObserverService(
            history_store=self._history_store,
            capability_registry=self._capability_registry,
            event_sink=event_sink,
        )
        self._planner = PlannerService(
            workspace_root=str(self._workspace_root),
            approval_mode=self._approval_mode,
            provider=self._provider,
            tool_service=self._tool_service,
            capability_registry=self._capability_registry,
            max_plan_attempts=max_plan_attempts,
        )
        state_dir = self._workspace_root / ".foundation" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        self._git_service = GitService(workspace_root=self._workspace_root)
        self._executor = ActionExecutor(
            workspace_root=self._workspace_root,
            shell_runtime=self._shell_runtime,
            tool_service=self._tool_service,
            policy_engine=self._policy_engine,
            approval_service=self._approval_service,
            capability_registry=self._capability_registry,
            observer=self._observer,
            shell_output_callback=self._shell_output_callback,
            file_service=FileService(
                workspace_root=self._workspace_root,
                state_dir=state_dir,
                read_grant_store=self._grant_store,
            ),
            git_service=self._git_service,
            question_callback=question_callback,
            grant_store=self._grant_store,
        )

    def set_event_sink(self, event_sink: EventSink | None) -> None:
        """Replace (or clear) the observer's redacted event sink callback."""
        self._observer.set_event_sink(event_sink)

    def orchestrate(self, request: UserRequest) -> OrchestrationResult:
        """Run the Stage 4 orchestration flow with bounded replan loop."""
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
            result = self._run_replan_loop(
                request=request,
                request_id=request_id,
                resolved_request_cwd=resolved_request_cwd,
                session_id=session_id,
            )
            self._observer.emit(
                EVENT_SESSION_END,
                payload={
                    "request_id": request_id,
                    "session_id": session_id,
                    "status": self._session_status_for_result(
                        result.summary,
                        result.stop_reason,
                        result.iterations,
                        result.governance_notice,
                    ).value,
                    "executed_actions": result.summary.executed_actions,
                    "pending_approval_actions": result.summary.pending_approval_actions,
                    "blocked_actions": result.summary.blocked_actions,
                    "failed_actions": result.summary.failed_actions,
                    "skipped_actions": result.summary.skipped_actions,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            if self._history_store is not None and session_id is not None:
                self._history_store.record_summary(
                    session_id,
                    assistant_message=result.assistant_message.content,
                    summary_text=result.summary.text,
                    executed_actions=result.summary.executed_actions,
                    pending_approval_actions=result.summary.pending_approval_actions,
                    blocked_actions=result.summary.blocked_actions,
                    failed_actions=result.summary.failed_actions,
                    skipped_actions=result.summary.skipped_actions,
                    total_iterations=result.summary.total_iterations,
                )
                self._history_store.finalize_session(
                    session_id,
                    status=self._session_status_for_result(
                        result.summary,
                        result.stop_reason,
                        result.iterations,
                        result.governance_notice,
                    ),
                )
            return result
        except Exception as exc:
            # Preserve the raw (capped) provider response on parse/truncation
            # failures so the persisted event log is self-diagnosing.
            failure_extra: dict[str, str] = {}
            if isinstance(exc, ProviderError) and exc.response_text:
                failure_extra["response_text"] = exc.response_text[:4096]
            self._observer.emit_exception(
                EVENT_EXCEPTION,
                exc,
                payload={
                    "request_id": request_id,
                    "session_id": session_id,
                    "request_text": request.message,
                    **failure_extra,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            self._observer.emit_exception(
                EVENT_PLAN_FAILED,
                exc,
                payload={
                    "request_id": request_id,
                    "session_id": session_id,
                    **failure_extra,
                },
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

    # ------------------------------------------------------------------
    # Bounded replan loop
    # ------------------------------------------------------------------

    def _materialize_deferred_writes(
        self,
        actions: list[PlannedAction],
        *,
        request: UserRequest,
        observation_messages: list[ProviderMessage],
        request_id: str,
        session_id: str | None,
    ) -> None:
        """Generate file bodies for writes that deferred content via content_brief.

        On success the brief is replaced with literal ``content`` for the
        executor. On a provider failure the brief is left in place so the
        write fails as a normal action (the executor rejects the unknown
        ``content_brief`` field) rather than killing the turn.
        """
        for action in actions:
            if action.kind is not ActionKind.TOOL_CALL or action.tool_call is None:
                continue
            tool_call = action.tool_call
            if tool_call.capability_id != "foundation.file.write":
                continue
            brief = tool_call.arguments.get("content_brief")
            if not brief or tool_call.arguments.get("content"):
                continue
            path = str(tool_call.arguments.get("path", "<file>"))
            try:
                body = self._generate_file_body(
                    path=path,
                    brief=str(brief),
                    request=request,
                    observation_messages=observation_messages,
                )
            except ProviderError as exc:
                logger.warning(
                    "deferred_write_generation_failed action=%s path=%s error=%s",
                    action.id,
                    path,
                    exc,
                )
                continue
            tool_call.arguments.pop("content_brief", None)
            tool_call.arguments["content"] = body

    def _generate_file_body(
        self,
        *,
        path: str,
        brief: str,
        request: UserRequest,
        observation_messages: list[ProviderMessage],
    ) -> str:
        # Fold the gathered context into a single plain-text reference block.
        # We deliberately do NOT replay the planning conversation as assistant
        # turns: those are JSON plans, and feeding them back primes the model to
        # keep "planning" (emit another actions object) instead of writing the
        # file's raw bytes.
        reference = "\n\n".join(m.content for m in observation_messages).strip()
        user_parts = [
            f"Write the complete, literal contents of the file `{path}`.",
            f"\nWhat the file should contain:\n{brief}",
        ]
        if reference:
            user_parts.append(
                "\nReference data gathered so far (use the real values from it; "
                f"do not invent facts):\n{reference}"
            )
        user_parts.append(f"\nThe original user request was: {request.message}")
        messages = [
            ProviderMessage(
                role=ProviderMessageRole.DEVELOPER,
                content=(
                    "You are a file-content writer, not a planner. Output ONLY the "
                    "raw, literal bytes to save to the file — nothing else. Do NOT "
                    "wrap the output in JSON, do NOT emit an actions/plan object, do "
                    "NOT fence the whole file, and do NOT add commentary before or "
                    "after the content."
                ),
            ),
            ProviderMessage(role=ProviderMessageRole.USER, content="\n".join(user_parts)),
        ]
        response = self._provider.complete(
            ProviderPrompt(messages=messages, response_format=ProviderResponseFormat.TEXT)
        )
        return _unwrap_generated_file_body(response.content)

    def _run_replan_loop(
        self,
        *,
        request: UserRequest,
        request_id: str,
        resolved_request_cwd: Path,
        session_id: str | None,
    ) -> OrchestrationResult:
        iterations: list[OrchestrationIteration] = []
        total_actions_executed = 0
        observation_messages: list[ProviderMessage] = []
        observation_message_history: list[ProviderMessage] = []
        executed_command_log: list[str] = []
        stop_reason: LoopStopReason | None = None
        had_code_changes = False
        verification_outcome: VerificationOutcome = VerificationOutcome.NOT_ATTEMPTED
        verification_commands: list[str] = []
        # v4 stage 03 — cumulative changed-paths set drives both the
        # detector's progress check and the soft-completion notice override.
        cumulative_changed_paths: list[str] = []
        cumulative_changed_paths_set: set[str] = set()
        progress_detector = NoProgressDetector()
        prev_last_step_id: str | None = None

        for iteration_index in range(1, _MAX_LOOP_ITERATIONS + 1):
            self._observer.emit(
                EVENT_ITERATION_STARTED,
                payload={
                    "request_id": request_id,
                    "iteration": iteration_index,
                    "total_actions_so_far": total_actions_executed,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )

            # 1. Regather context
            context = self._planner.gather_context(request_cwd=str(resolved_request_cwd))

            # 2. Request plan
            remaining_actions = _MAX_TOTAL_ACTIONS - total_actions_executed
            planning_started_at = _utcnow()
            planning_started_monotonic = time.monotonic()
            self._observer.emit(
                EVENT_PLAN_STARTED,
                payload={
                    "request_id": request_id,
                    "request_text": request.message,
                    "iteration": iteration_index,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )
            try:
                plan, planning_metadata = self._planner.request_plan(
                    request,
                    context,
                    request_id=request_id,
                    observation_messages=observation_messages or None,
                    iteration=iteration_index,
                    remaining_actions=remaining_actions,
                )
            except PlanningError as exc:
                # On a *replan* (iter > 1), a PlanningError means the model
                # is no longer producing a workable plan even though earlier
                # iterations did real work. Bail gracefully and let the
                # post-loop checks (governance / verification / status) fire
                # against the iterations we already have. v4 stage 03.
                if iteration_index > 1:
                    stop_reason = LoopStopReason.NO_PROGRESS
                    break
                raise OrchestrationPlanError(str(exc)) from exc
            planning_completed_at = _utcnow()
            planning_duration = max(time.monotonic() - planning_started_monotonic, 0.0)

            # Record plan
            if self._history_store is not None and session_id is not None:
                self._history_store.record_plan(
                    session_id,
                    iteration=iteration_index,
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
                action_ids=[a.id for a in plan.actions],
                planning_metadata=planning_metadata,
                started_at=planning_started_at,
                completed_at=planning_completed_at,
                duration_seconds=planning_duration,
                iteration=iteration_index,
            )

            # REPLANNED_FROM edge from prior iteration's last step
            if (
                prev_last_step_id is not None
                and self._history_store is not None
                and session_id is not None
            ):
                self._history_store.record_trace_edge(
                    session_id,
                    edge=TraceEdge(
                        trace_id=session_id,
                        source_step_id=prev_last_step_id,
                        target_step_id=planning_step_id,
                        edge_kind=TraceEdgeKind.REPLANNED_FROM,
                    ),
                )

            self._observer.emit(
                EVENT_PLAN_FINISHED,
                payload={
                    "request_id": request_id,
                    "session_id": session_id,
                    "action_count": len(plan.actions),
                    "iteration": iteration_index,
                    "approval_mode": self._approval_mode.value,
                    "provider": planning_metadata.provider,
                    "model": planning_metadata.model,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )

            # 3. Zero-action plan → stop
            if not plan.actions:
                stop_reason = LoopStopReason.ZERO_ACTION_PLAN
                iterations.append(
                    OrchestrationIteration(
                        iteration=iteration_index,
                        context=context,
                        plan=plan,
                        planning_metadata=planning_metadata,
                        stop_reason=stop_reason,
                    )
                )
                break

            # 4. Enforce action budget
            budget = _MAX_TOTAL_ACTIONS - total_actions_executed
            actions_to_execute = plan.actions[:budget]

            # 4b. Materialize deferred file bodies (content_brief -> content) via a
            # separate text-generation call, keeping large content out of the
            # schema-constrained plan JSON. A generation failure leaves the brief
            # in place so the write degrades to a normal failed action.
            self._materialize_deferred_writes(
                actions_to_execute,
                request=request,
                observation_messages=observation_messages,
                request_id=request_id,
                session_id=session_id,
            )

            # 5. Policy evaluate + execute
            execution_results, decisions, evaluations, last_step_id = (
                self._execute_iteration_actions(
                    actions_to_execute,
                    context=context,
                    request=request,
                    resolved_request_cwd=resolved_request_cwd,
                    request_id=request_id,
                    session_id=session_id,
                    planning_step_id=planning_step_id,
                    iteration=iteration_index,
                )
            )
            total_actions_executed += len(actions_to_execute)
            prev_last_step_id = last_step_id

            # 6. Track mutations and verification
            iter_changed, iter_code_change, iter_outcome, iter_verify_cmds = self._classify_results(
                execution_results, actions_to_execute
            )
            had_code_changes = had_code_changes or iter_code_change
            verification_outcome = _worst_verification_outcome(
                verification_outcome,
                iter_outcome,
            )
            verification_commands.extend(iter_verify_cmds)
            for path in iter_changed:
                if path not in cumulative_changed_paths_set:
                    cumulative_changed_paths_set.add(path)
                    cumulative_changed_paths.append(path)

            # Append every executed shell action to the running command log so
            # future iterations' observation prompts can surface the
            # do-not-re-run history. v4 stage 03 also records side-effecting
            # tool calls (file writes, git mutations) so the planner sees
            # them and won't re-issue.
            for action, result in zip(
                actions_to_execute,
                execution_results,
                strict=True,
            ):
                if result.status is not ExecutionStatus.EXECUTED:
                    continue
                if action.kind is ActionKind.SHELL and action.shell is not None:
                    display = " ".join([action.shell.command, *action.shell.args])
                    entry = f"[iter {iteration_index}] $ {display}"
                elif (
                    action.kind is ActionKind.TOOL_CALL
                    and action.tool_call is not None
                    and _is_side_effecting_capability(action.tool_call.capability_id)
                ):
                    descriptor = _format_tool_call_log_entry(action.tool_call)
                    entry = f"[iter {iteration_index}] {descriptor}"
                else:
                    continue
                if entry not in executed_command_log:
                    executed_command_log.append(entry)

            # 7. Check stop conditions
            has_pending = any(
                r.status is ExecutionStatus.PENDING_APPROVAL for r in execution_results
            )
            has_awaiting_input = any(
                r.status is ExecutionStatus.AWAITING_INPUT for r in execution_results
            )
            has_fatal = any(self._is_fatal_result(r) for r in execution_results)

            if has_pending:
                stop_reason = LoopStopReason.PENDING_APPROVAL
            elif has_awaiting_input:
                stop_reason = LoopStopReason.AWAITING_USER_INPUT
            elif has_fatal:
                stop_reason = LoopStopReason.FATAL_EXECUTION_FAILURE
            elif total_actions_executed >= _MAX_TOTAL_ACTIONS:
                stop_reason = LoopStopReason.MAX_ACTIONS
            elif iteration_index >= _MAX_LOOP_ITERATIONS:
                stop_reason = LoopStopReason.MAX_ITERATIONS

            # 8. Build observation
            observation = self._build_observation(
                iteration_index,
                execution_results,
                actions_to_execute,
                iter_changed,
                remaining_iterations=_MAX_LOOP_ITERATIONS - iteration_index,
                remaining_actions=_MAX_TOTAL_ACTIONS - total_actions_executed,
            )

            iterations.append(
                OrchestrationIteration(
                    iteration=iteration_index,
                    context=context,
                    plan=plan,
                    planning_metadata=planning_metadata,
                    policy_decisions=decisions,
                    policy_evaluations=[e for e in evaluations if e is not None],
                    execution_results=execution_results,
                    observation=observation if stop_reason is None else None,
                    stop_reason=stop_reason,
                )
            )

            self._observer.emit(
                EVENT_ITERATION_COMPLETED,
                payload={
                    "request_id": request_id,
                    "iteration": iteration_index,
                    "actions_executed": len(actions_to_execute),
                    "stop_reason": stop_reason.value if stop_reason else None,
                },
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
            )

            if stop_reason is not None:
                break

            # 9. No-progress detection (v4 stage 03):
            #   - drop soft idempotent failures whose target path is already
            #     in the cumulative changed-paths set,
            #   - drop file.read probes whose path is the target of any
            #     file.write action in this iteration.
            detector_results = _filter_results_for_detector(
                execution_results,
                actions_to_execute,
                cumulative_changed_paths=cumulative_changed_paths_set,
            )
            if progress_detector.is_stuck(
                detector_results,
                iter_changed,
                actions_to_execute,
                cumulative_changed_paths=cumulative_changed_paths,
            ):
                iterations[-1] = iterations[-1].model_copy(
                    update={"stop_reason": LoopStopReason.NO_PROGRESS},
                )
                stop_reason = LoopStopReason.NO_PROGRESS
                break

            # 10. Accumulate observation messages for next iteration.  We
            # append this iteration's observation to a running history so the
            # planner sees every prior iteration's plan + outcome.  Then we
            # rebuild observation_messages for the next iteration as:
            # [all prior observations] + [one cumulative "already executed"
            # summary], so the model can't re-plan commands it has run.
            observation_message_history.extend(self._observation_to_messages(plan, observation))
            observation_messages = list(observation_message_history)
            if executed_command_log:
                summary_content = (
                    "COMMANDS ALREADY EXECUTED (do NOT re-run these unless "
                    "the workspace state has meaningfully changed since — "
                    "their stdout is in the observation blocks above):\n"
                    + "\n".join(executed_command_log)
                )
                observation_messages.append(
                    ProviderMessage(
                        role=ProviderMessageRole.DEVELOPER,
                        content=summary_content,
                    )
                )

        # Fallback
        if stop_reason is None:
            stop_reason = LoopStopReason.MAX_ITERATIONS

        # Build result
        terminal_plan = iterations[-1].plan
        terminal_context = iterations[-1].context
        terminal_metadata = iterations[-1].planning_metadata
        all_decisions = [d for it in iterations for d in it.policy_decisions]
        all_evaluations = [e for it in iterations for e in it.policy_evaluations]
        all_results = [r for it in iterations for r in it.execution_results]

        had_fatal_failure = any(self._is_fatal_result(r) for r in all_results)

        # When the loop is structurally stuck (missing capability, bad path, or
        # no progress), reframe the raw failure as a graceful capability-gap
        # handoff: the chat surface shows a plain-language message and options
        # instead of an error. The underlying failure stays in execution_results
        # and is recorded to the trace + event log via EVENT_CAPABILITY_GAP.
        gap_handoff = build_gap_handoff(
            request=request.message,
            stop_reason=stop_reason,
            results=all_results,
            iteration=len(iterations),
            had_cumulative_changes=bool(cumulative_changed_paths),
            phraser=make_provider_phraser(self._provider),
        )
        if gap_handoff is not None:
            msg_content = gap_handoff.message
            self._observer.emit(
                EVENT_CAPABILITY_GAP,
                payload=gap_handoff.report.model_dump(mode="json"),
                session_id=session_id,
                logger_name="foundation.services.orchestrator",
                level=logging.WARNING,
            )
        else:
            msg_content = self._augment_message_with_stop_reason(
                terminal_plan.assistant_message,
                stop_reason,
                cumulative_changed_paths=cumulative_changed_paths,
                had_fatal=had_fatal_failure,
            )
        assistant_message = AssistantMessage(content=msg_content)

        verification_notice = self._build_verification_notice(
            had_code_changes,
            verification_outcome,
            verification_commands,
        )

        summary = self._build_summary(
            iterations,
            all_results,
            plan_only=request.plan_only,
            stop_reason=stop_reason,
        )

        governance_notice = self._check_commit_approval_invariant(
            request=request,
            final_iteration=iterations[-1],
        )

        return OrchestrationResult(
            session_id=session_id,
            request=request,
            context=terminal_context,
            plan=terminal_plan,
            planning_metadata=terminal_metadata,
            policy_decisions=all_decisions,
            policy_evaluations=all_evaluations,
            execution_results=all_results,
            assistant_message=assistant_message,
            summary=summary,
            iterations=iterations,
            stop_reason=stop_reason,
            verification_notice=verification_notice,
            governance_notice=governance_notice,
            gap_handoff=gap_handoff,
        )

    # ------------------------------------------------------------------
    # Iteration action execution
    # ------------------------------------------------------------------

    def _execute_iteration_actions(
        self,
        actions: list[PlannedAction],
        *,
        context: ContextSnapshot,
        request: UserRequest,
        resolved_request_cwd: Path,
        request_id: str,
        session_id: str | None,
        planning_step_id: str,
        iteration: int = 1,
    ) -> tuple[
        list[ExecutionResult],
        list[PolicyDecision],
        list[PolicyEvaluationRecord | None],
        str | None,
    ]:
        evaluations = [
            self._policy_engine.evaluate(
                action,
                request_cwd=resolved_request_cwd,
                approval_mode=self._approval_mode,
            )
            for action in actions
        ]
        decisions = [
            self._policy_engine.to_policy_decision(evaluation)
            if evaluation is not None
            else PolicyDecision(
                action_id=action.id,
                decision=PolicyDecisionType.ALLOW,
                reason="Explanation-only actions do not execute anything.",
            )
            for action, evaluation in zip(actions, evaluations, strict=True)
        ]
        execution_results: list[ExecutionResult] = []
        candidate_capability_ids = [str(s.capability_id) for s in context.available_capabilities]
        prior_step_id: str | None = None
        last_step_id: str | None = None

        for action, decision, evaluation in zip(
            actions,
            decisions,
            evaluations,
            strict=True,
        ):
            if (
                self._history_store is not None
                and session_id is not None
                and evaluation is not None
            ):
                self._history_store.record_policy_evaluation(session_id, record=evaluation)

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

            last_step_id = self._observer.record_execution_step(
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
                iteration=iteration,
            )
            prior_step_id = last_step_id

        return execution_results, decisions, evaluations, last_step_id

    # ------------------------------------------------------------------
    # Observation block builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_observation(
        iteration: int,
        execution_results: list[ExecutionResult],
        actions: list[PlannedAction],
        changed_paths: list[str],
        remaining_iterations: int,
        remaining_actions: int,
    ) -> IterationObservation:
        action_outcomes: list[ActionOutcome] = []
        approval_outcomes: list[str] = []

        for action, result in zip(actions, execution_results, strict=True):
            capability_id = None
            if action.kind is ActionKind.TOOL_CALL and action.tool_call:
                capability_id = action.tool_call.capability_id

            exit_code = None
            action_changed: list[str] = []
            stdout_preview = None
            stderr_preview = None

            if result.artifact:
                exit_code = result.artifact.get("exit_code")
                stdout_preview = _truncate_preview(str(result.artifact.get("stdout", "")))
                stderr_preview = _truncate_preview(str(result.artifact.get("stderr", "")))
                if result.artifact.get("path"):
                    action_changed.append(str(result.artifact["path"]))
                # Surface a user's answer to a question action so the next
                # planning iteration can act on it.
                if action.kind is ActionKind.QUESTION and result.artifact.get("answer") is not None:
                    stdout_preview = _truncate_preview(
                        f'User answered: "{result.artifact["answer"]}"'
                    )
                # Surface typed read-only results (file reads, search, git
                # inspect) so the planner sees the data it fetched instead of an
                # empty outcome — without this it re-runs the same read forever.
                if not stdout_preview:
                    preview = _tool_result_preview(result.artifact, result.artifact_type)
                    if preview:
                        stdout_preview = _truncate_preview(preview)

            if result.status is ExecutionStatus.PENDING_APPROVAL:
                approval_outcomes.append(f"{action.id}: pending approval")

            action_outcomes.append(
                ActionOutcome(
                    action_id=action.id,
                    capability_id=capability_id,
                    status=result.status,
                    exit_code=exit_code,
                    changed_paths=action_changed,
                    stdout_preview=stdout_preview,
                    stderr_preview=stderr_preview,
                    error=result.error,
                )
            )

        return IterationObservation(
            iteration=iteration,
            action_outcomes=action_outcomes,
            approval_outcomes=approval_outcomes,
            changed_paths=changed_paths,
            remaining_iterations=remaining_iterations,
            remaining_actions=remaining_actions,
        )

    @staticmethod
    def _observation_to_messages(
        plan: AssistantPlan,
        observation: IterationObservation,
    ) -> list[ProviderMessage]:
        assistant_content = json.dumps(plan.model_dump(mode="json"), indent=2)
        observation_content = (
            f"EXECUTION OBSERVATION (iteration {observation.iteration}):\n"
            f"{json.dumps(observation.model_dump(mode='json'), indent=2)}\n\n"
            f"You have {observation.remaining_iterations} iteration(s) and "
            f"{observation.remaining_actions} action(s) remaining.\n"
            "Based on these results, decide your next actions. "
            "If verification failed, diagnose the error and issue repair actions. "
            "If all work is complete and verified, return zero actions with your final answer."
        )
        return [
            ProviderMessage(role=ProviderMessageRole.ASSISTANT, content=assistant_content),
            ProviderMessage(role=ProviderMessageRole.DEVELOPER, content=observation_content),
        ]

    # ------------------------------------------------------------------
    # Result classification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_results(
        execution_results: list[ExecutionResult],
        actions: list[PlannedAction],
    ) -> tuple[list[str], bool, VerificationOutcome, list[str]]:
        """Return (changed_paths, had_code_changes, iter_outcome, verify_cmds).

        ``iter_outcome`` is the worst observed verification outcome across all
        verification commands in this iteration (UNAVAILABLE > FAILED > PASSED),
        or NOT_ATTEMPTED if none was planned.
        """
        changed_paths: list[str] = []
        had_code_changes = False
        verify_cmds: list[str] = []
        iter_outcome = VerificationOutcome.NOT_ATTEMPTED

        for action, result in zip(actions, execution_results, strict=True):
            if result.artifact_type in _CODE_CHANGING_ARTIFACT_TYPES:
                had_code_changes = True
                if result.artifact and result.artifact.get("path"):
                    changed_paths.append(str(result.artifact["path"]))

            if action.kind is ActionKind.SHELL and action.shell:
                cmd_basename = action.shell.command.split("/")[-1]
                if cmd_basename in _VERIFICATION_COMMANDS:
                    display = " ".join([action.shell.command, *action.shell.args])
                    verify_cmds.append(display)
                    cmd_outcome = _verification_outcome_for_result(result)
                    iter_outcome = _worst_verification_outcome(iter_outcome, cmd_outcome)

        return changed_paths, had_code_changes, iter_outcome, verify_cmds

    @staticmethod
    def _is_fatal_result(result: ExecutionResult) -> bool:
        if result.status is not ExecutionStatus.FAILED:
            return False
        if result.error is None:
            return False
        error_lower = result.error.lower()
        return any(p in error_lower for p in _FATAL_ERROR_PATTERNS)

    @staticmethod
    def _build_verification_notice(
        had_code_changes: bool,
        outcome: VerificationOutcome,
        verification_commands: list[str] | None = None,
    ) -> VerificationNotice | None:
        if not had_code_changes:
            return None
        reason_by_outcome = {
            VerificationOutcome.PASSED: None,
            VerificationOutcome.FAILED: ("A verification command ran and reported failure."),
            VerificationOutcome.UNAVAILABLE: (
                "Verification was attempted but the command could not run "
                "(binary missing or spawn error)."
            ),
            VerificationOutcome.NOT_ATTEMPTED: (
                "Code was modified but no verification command was executed."
            ),
        }
        return VerificationNotice(
            outcome=outcome,
            verification_commands_run=verification_commands or [],
            reason=reason_by_outcome[outcome],
        )

    @staticmethod
    def _augment_message_with_stop_reason(
        message: str,
        stop_reason: LoopStopReason,
        *,
        cumulative_changed_paths: list[str] | None = None,
        had_fatal: bool = False,
    ) -> str:
        # v4 stage 03: when the loop bails with NO_PROGRESS *after* having
        # already produced cumulative changes and no fatal failure, the
        # workspace state actually reflects the user's intent. Swap the
        # red "no progress" suffix for the soft-completion variant.
        if stop_reason is LoopStopReason.NO_PROGRESS and cumulative_changed_paths and not had_fatal:
            return message + _NO_PROGRESS_SOFT_SUFFIX
        suffix = _STOP_REASON_SUFFIXES.get(stop_reason)
        if suffix:
            return message + suffix
        return message

    @staticmethod
    def _has_commit_intent(message: str) -> bool:
        if _COMMIT_INTENT_WORD_RE.search(message):
            return True
        lower = message.lower()
        return any(phrase in lower for phrase in _COMMIT_INTENT_PHRASES)

    def _check_commit_approval_invariant(
        self,
        *,
        request: UserRequest,
        final_iteration: OrchestrationIteration,
    ) -> GovernanceNotice | None:
        """Enforce: if the user asked to commit and the run left staged files
        without planning an approval-gated commit, surface a governance
        notice and (via the classifier) flip the session status to
        PENDING_APPROVAL.

        Returns ``None`` when the run satisfied the contract (or the user
        never asked for a commit in the first place).
        """
        if not self._has_commit_intent(request.message):
            return None

        planned_commit = any(
            action.tool_call is not None and action.tool_call.capability_id == _COMMIT_CAPABILITY_ID
            for action in final_iteration.plan.actions
        )
        if planned_commit:
            return None

        try:
            status = self._git_service.status(GitStatusRequest())
        except GitServiceError:
            # Git status can't be read (not a repo, access error, etc.).
            # Absence of evidence isn't evidence of staged files — stay silent.
            return None

        staged_paths = [change.path for change in status.staged]
        if not staged_paths:
            return None

        return GovernanceNotice(
            code=GovernanceNoticeCode.COMMIT_APPROVAL_MISSING,
            message=(
                "User request asked for a commit-approval stop; the final "
                "iteration ended with staged files and no commit action. "
                "No commit was performed."
            ),
            staged_paths=sorted(staged_paths),
        )

    # ------------------------------------------------------------------
    # History and session helpers
    # ------------------------------------------------------------------

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

    @classmethod
    def _session_status_for_result(
        cls,
        summary: OrchestrationSummary,
        stop_reason: LoopStopReason | None,
        iterations: list[OrchestrationIteration],
        governance_notice: GovernanceNotice | None = None,
        *,
        cumulative_changed_paths: list[str] | None = None,
        had_fatal: bool | None = None,
    ) -> SessionStatus:
        """Classify a terminated orchestration run.

        Precedence:

        1. ``PENDING_APPROVAL`` if any action is still awaiting approval,
           or if the runtime emitted a governance notice overriding the
           status (e.g. the commit-approval invariant fired).
        2. ``FAILED`` when the loop hit a fatal, unresolved stop.
        3. ``COMPLETED`` when the planner returned zero actions as the final
           step — even if earlier iterations had failures and the later ones
           recovered.
        4. ``COMPLETED_INCONCLUSIVE`` when the loop stopped because the
           no-progress detector fired, or because the iteration/action budget
           ran out with a clean final iteration. The run is not corrupt, but
           didn't fully satisfy the request.
        5. ``FAILED`` when the budget ran out mid-failure.
        """
        if governance_notice is not None:
            return SessionStatus.PENDING_APPROVAL
        if summary.pending_approval_actions > 0:
            return SessionStatus.PENDING_APPROVAL
        if stop_reason is LoopStopReason.PENDING_APPROVAL:
            return SessionStatus.PENDING_APPROVAL
        if stop_reason is LoopStopReason.AWAITING_USER_INPUT:
            # Stopped to ask the user something we couldn't prompt for inline
            # (non-interactive run, or the user dismissed the prompt).
            return SessionStatus.COMPLETED_INCONCLUSIVE
        if stop_reason is LoopStopReason.FATAL_EXECUTION_FAILURE:
            return SessionStatus.FAILED
        if stop_reason is LoopStopReason.ZERO_ACTION_PLAN:
            return SessionStatus.COMPLETED
        if stop_reason is LoopStopReason.NO_PROGRESS:
            # v4 stage 03 — soft completion: cumulative changes already
            # landed and nothing fatal happened, so the workspace
            # already reflects the user's intent. Surface as COMPLETED.
            cumulative = (
                cumulative_changed_paths
                if cumulative_changed_paths is not None
                else cls._aggregate_changed_paths(iterations)
            )
            fatal = had_fatal if had_fatal is not None else cls._iterations_had_fatal(iterations)
            if cumulative and not fatal:
                return SessionStatus.COMPLETED
            return SessionStatus.COMPLETED_INCONCLUSIVE
        if stop_reason in {LoopStopReason.MAX_ITERATIONS, LoopStopReason.MAX_ACTIONS}:
            last_iter_failed = bool(iterations) and any(
                r.status is ExecutionStatus.FAILED for r in iterations[-1].execution_results
            )
            return (
                SessionStatus.FAILED if last_iter_failed else SessionStatus.COMPLETED_INCONCLUSIVE
            )
        # Fallback: no stop reason (e.g. zero-action first plan). Treat as completed
        # unless the aggregate shows unresolved failures, which is a legacy path.
        if summary.failed_actions > 0:
            return SessionStatus.FAILED
        return SessionStatus.COMPLETED

    @staticmethod
    def _aggregate_changed_paths(
        iterations: list[OrchestrationIteration],
    ) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for it in iterations:
            obs = it.observation
            if obs is None:
                continue
            for path in obs.changed_paths:
                if path in seen:
                    continue
                seen.add(path)
                ordered.append(path)
        return ordered

    @classmethod
    def _iterations_had_fatal(cls, iterations: list[OrchestrationIteration]) -> bool:
        for it in iterations:
            for r in it.execution_results:
                if cls._is_fatal_result(r):
                    return True
        return False

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

    # ------------------------------------------------------------------
    # Summary builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_summary(
        iterations: list[OrchestrationIteration],
        execution_results: list[ExecutionResult],
        *,
        plan_only: bool,
        stop_reason: LoopStopReason,
    ) -> OrchestrationSummary:
        executed = sum(r.status is ExecutionStatus.EXECUTED for r in execution_results)
        pending = sum(r.status is ExecutionStatus.PENDING_APPROVAL for r in execution_results)
        blocked = sum(r.status is ExecutionStatus.BLOCKED for r in execution_results)
        failed = sum(r.status is ExecutionStatus.FAILED for r in execution_results)
        skipped = sum(r.status is ExecutionStatus.NOT_EXECUTED for r in execution_results)
        total_planned = sum(len(it.plan.actions) for it in iterations)

        if not any(it.plan.actions for it in iterations):
            text = "No actions were needed for this request."
        elif plan_only:
            text = (
                f"Planned {total_planned} action(s); execution was skipped because plan_only "
                "was requested."
            )
        else:
            parts = [f"Executed {executed} action(s)"]
            if pending:
                parts.append(f"{pending} pending approval")
            if failed:
                parts.append(f"{failed} failed")
            if blocked:
                parts.append(f"{blocked} blocked")
            if skipped:
                parts.append(f"{skipped} skipped")
            text = ", ".join(parts) + "."
            if len(iterations) > 1:
                text += f" ({len(iterations)} iterations)"

        return OrchestrationSummary(
            executed_actions=executed,
            pending_approval_actions=pending,
            blocked_actions=blocked,
            failed_actions=failed,
            skipped_actions=skipped,
            total_iterations=len(iterations),
            total_actions_planned=total_planned,
            text=text,
        )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _truncate_preview(text: str) -> str | None:
    if not text:
        return None
    lines = text.split("\n")
    if len(lines) > _OBSERVATION_MAX_LINES:
        lines = lines[:_OBSERVATION_MAX_LINES]
        text = "\n".join(lines) + "\n... (truncated)"
    encoded = text.encode("utf-8")
    if len(encoded) > _OBSERVATION_MAX_BYTES:
        text = encoded[:_OBSERVATION_MAX_BYTES].decode("utf-8", errors="ignore")
        text += "\n... (truncated)"
    return text if text else None
