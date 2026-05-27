"""Typed orchestration models for Stage 5."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

from foundation.models.capability import CapabilitySnapshot, PolicyEvaluationRecord


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class ProviderMessageRole(StrEnum):
    """Supported provider prompt roles."""

    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"


class ProviderResponseFormat(StrEnum):
    """Supported provider response shapes."""

    TEXT = "text"
    JSON_OBJECT = "json_object"


class ShellActionMode(StrEnum):
    """Supported shell execution modes for planned actions."""

    BUFFERED = "buffered"
    STREAM = "stream"
    PTY = "pty"


class ActionKind(StrEnum):
    """Structured action kinds supported by the orchestrator."""

    EXPLANATION = "explanation"
    SHELL = "shell"
    TOOL_CALL = "tool_call"
    QUESTION = "question"


class PolicyDecisionType(StrEnum):
    """High-level policy outcomes for one action."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class ExecutionStatus(StrEnum):
    """Execution outcomes for one planned action."""

    NOT_EXECUTED = "not_executed"
    EXECUTED = "executed"
    PENDING_APPROVAL = "pending_approval"
    AWAITING_INPUT = "awaiting_input"
    BLOCKED = "blocked"
    FAILED = "failed"


class ExecutionArtifactType(StrEnum):
    """Artifact payload types recorded in execution results."""

    EXPLANATION = "explanation"
    SHELL = "shell"
    SEARCH = "search"
    FILES = "files"
    GIT = "git"
    MAN = "man"
    TLDR = "tldr"
    FILE_READ = "file_read"
    FILE_READ_CHUNK = "file_read_chunk"
    FILE_WRITE = "file_write"
    FILE_EDIT = "file_edit"
    FILE_APPLY_DIFF = "file_apply_diff"
    GIT_STATUS = "git_status"
    GIT_DIFF = "git_diff"
    GIT_SHOW = "git_show"
    GIT_LOG = "git_log"
    GIT_STAGE = "git_stage"
    GIT_UNSTAGE = "git_unstage"
    GIT_COMMIT = "git_commit"
    QUESTION = "question"


class LoopStopReason(StrEnum):
    """Why the bounded replan loop terminated."""

    ZERO_ACTION_PLAN = "zero_action_plan"
    PENDING_APPROVAL = "pending_approval"
    AWAITING_USER_INPUT = "awaiting_user_input"
    FATAL_EXECUTION_FAILURE = "fatal_execution_failure"
    MAX_ITERATIONS = "max_iterations"
    MAX_ACTIONS = "max_actions"
    NO_PROGRESS = "no_progress"


class VerificationOutcome(StrEnum):
    """Outcome of verification for a code-changing turn."""

    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    NOT_ATTEMPTED = "not_attempted"


class ProviderMessage(StrictModel):
    """One prompt message sent to the configured model provider."""

    role: ProviderMessageRole
    content: str = Field(min_length=1)


class ProviderPrompt(StrictModel):
    """Normalized provider prompt request."""

    messages: list[ProviderMessage] = Field(min_length=1)
    response_format: ProviderResponseFormat = ProviderResponseFormat.TEXT
    schema_name: str | None = None
    output_schema: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_schema_requirements(self) -> ProviderPrompt:
        if self.response_format is ProviderResponseFormat.JSON_OBJECT:
            if not self.schema_name:
                raise ValueError("schema_name is required for JSON object responses")
            if not self.output_schema:
                raise ValueError("output_schema is required for JSON object responses")
        return self


class ProviderUsage(StrictModel):
    """Provider token accounting metadata."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ProviderResponseMetadata(StrictModel):
    """Normalized provider response metadata."""

    provider: str
    model: str
    response_id: str | None = None
    latency_seconds: float = Field(ge=0.0)
    attempts: PositiveInt = 1
    usage: ProviderUsage | None = None


class ProviderResponse(StrictModel):
    """Provider output plus metadata."""

    content: str = Field(min_length=1)
    structured_output: dict[str, Any] | None = None
    metadata: ProviderResponseMetadata


class UserRequest(StrictModel):
    """One user request submitted to the orchestrator."""

    message: str = Field(min_length=1)
    conversation_history: list[ProviderMessage] = Field(default_factory=list)
    cwd: Path | None = None
    plan_only: bool = False

    @field_validator("cwd", mode="before")
    @classmethod
    def _normalize_cwd(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(value).expanduser()


class ToolCall(StrictModel):
    """One capability invocation proposed by the planning model."""

    capability_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$",
    )
    version: str | None = Field(
        default=None,
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capability_id", mode="before")
    @classmethod
    def _normalize_capability_id(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("capability_id must be a string")
        return value.strip().lower()


class ShellAction(StrictModel):
    """One shell command proposed by the model."""

    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    timeout_seconds: PositiveInt | None = None
    mode: ShellActionMode = ShellActionMode.BUFFERED

    @field_validator("args", mode="before")
    @classmethod
    def _normalize_args(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list | tuple):
            raise TypeError("args must be a list or tuple")
        return [str(item) for item in value]


class QuestionAction(StrictModel):
    """A clarifying question the model asks the user mid-turn."""

    prompt: str = Field(min_length=1, max_length=1000)
    options: list[str] | None = None
    allow_free_text: bool = True

    @field_validator("options", mode="before")
    @classmethod
    def _normalize_options(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list | tuple):
            raise TypeError("options must be a list or tuple")
        normalized = [str(item) for item in value]
        return normalized or None


class PlannedAction(StrictModel):
    """One validated action returned by the planning model."""

    id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    kind: ActionKind
    summary: str = Field(min_length=1, max_length=240)
    requires_approval: bool = False
    approval_reason: str | None = None
    explanation: str | None = None
    shell: ShellAction | None = None
    tool_call: ToolCall | None = None
    question: QuestionAction | None = None

    @model_validator(mode="after")
    def _validate_payload_shape(self) -> PlannedAction:
        if self.kind is ActionKind.EXPLANATION:
            if not self.explanation:
                raise ValueError("Explanation actions require the explanation field")
            if self.shell is not None or self.tool_call is not None:
                raise ValueError("Explanation actions cannot include shell or tool payloads")
        elif self.kind is ActionKind.SHELL:
            if self.shell is None:
                raise ValueError("Shell actions require the shell field")
            if self.explanation is not None or self.tool_call is not None:
                raise ValueError("Shell actions cannot include explanation or tool payloads")
        elif self.kind is ActionKind.TOOL_CALL:
            if self.tool_call is None:
                raise ValueError("Tool-call actions require the tool_call field")
            if self.explanation is not None or self.shell is not None:
                raise ValueError("Tool-call actions cannot include explanation or shell payloads")
        elif self.kind is ActionKind.QUESTION:
            if self.question is None:
                raise ValueError("Question actions require the question field")
            if self.shell is not None or self.tool_call is not None:
                raise ValueError("Question actions cannot include shell or tool payloads")

        if self.requires_approval and not self.approval_reason:
            raise ValueError("Approval-required actions must include approval_reason")
        return self


class AssistantPlan(StrictModel):
    """Structured plan returned by the provider before execution."""

    assistant_message: str = Field(min_length=1)
    actions: list[PlannedAction] = Field(default_factory=list, max_length=40)

    @field_validator("actions")
    @classmethod
    def _validate_unique_action_ids(cls, actions: list[PlannedAction]) -> list[PlannedAction]:
        action_ids = [action.id for action in actions]
        duplicates = {action_id for action_id in action_ids if action_ids.count(action_id) > 1}
        if duplicates:
            joined = ", ".join(sorted(duplicates))
            raise ValueError(f"Action ids must be unique: {joined}")
        return actions


class ContextSnapshot(StrictModel):
    """Compact request context passed into planning and exposed in results."""

    workspace_root: str
    request_cwd: str
    approval_mode: str
    available_tools: list[str] = Field(default_factory=list)
    available_capabilities: list[CapabilitySnapshot] = Field(default_factory=list)
    git_context: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)


class PolicyDecision(StrictModel):
    """Policy outcome for one planned action."""

    action_id: str
    decision: PolicyDecisionType
    reason: str = Field(min_length=1)
    risk_categories: list[str] = Field(default_factory=list)
    command_preview: str | None = None
    paths: list[str] = Field(default_factory=list)


class ExecutionResult(StrictModel):
    """Normalized execution result suitable for rendering and audit."""

    action_id: str
    status: ExecutionStatus
    summary: str = Field(min_length=1)
    artifact_type: ExecutionArtifactType | None = None
    artifact: dict[str, Any] | None = None
    error: str | None = None


class AssistantMessage(StrictModel):
    """Assistant-facing message emitted after planning and execution."""

    content: str = Field(min_length=1)


class ActionOutcome(StrictModel):
    """One executed action's outcome as seen by the observation block."""

    action_id: str
    capability_id: str | None = None
    status: ExecutionStatus
    exit_code: int | None = None
    changed_paths: list[str] = Field(default_factory=list)
    stdout_preview: str | None = None
    stderr_preview: str | None = None
    error: str | None = None


class IterationObservation(StrictModel):
    """Normalized observation block fed back to the planner after one iteration."""

    iteration: int = Field(ge=1)
    action_outcomes: list[ActionOutcome] = Field(default_factory=list)
    approval_outcomes: list[str] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    remaining_iterations: int = Field(ge=0)
    remaining_actions: int = Field(ge=0)


class VerificationNotice(StrictModel):
    """Notice about verification state for code-changing turns."""

    outcome: VerificationOutcome = VerificationOutcome.NOT_ATTEMPTED
    verification_commands_run: list[str] = Field(default_factory=list)
    reason: str | None = None

    @property
    def verified(self) -> bool:
        return self.outcome is VerificationOutcome.PASSED


class GovernanceNoticeCode(StrEnum):
    """Categories of runtime-governance advisories."""

    COMMIT_APPROVAL_MISSING = "commit_approval_missing"


class GovernanceNotice(StrictModel):
    """Notice emitted when the runtime enforces a governance invariant.

    Distinct from :class:`VerificationNotice`: governance notices describe a
    deliberate runtime intervention (e.g. overriding session status because
    the commit-approval contract wasn't satisfied), not the outcome of a
    user-requested verification step.
    """

    code: GovernanceNoticeCode
    message: str = Field(min_length=1)
    staged_paths: list[str] = Field(default_factory=list)


class OrchestrationSummary(StrictModel):
    """High-level summary of what the orchestrator did."""

    executed_actions: int = Field(ge=0)
    pending_approval_actions: int = Field(ge=0)
    blocked_actions: int = Field(ge=0)
    failed_actions: int = Field(ge=0)
    skipped_actions: int = Field(ge=0)
    total_iterations: int = Field(default=1, ge=1)
    total_actions_planned: int = Field(default=0, ge=0)
    text: str = Field(min_length=1)


class OrchestrationIteration(StrictModel):
    """One planning+execution pass within the bounded replan loop."""

    iteration: int = Field(ge=1)
    context: ContextSnapshot
    plan: AssistantPlan
    planning_metadata: ProviderResponseMetadata
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    policy_evaluations: list[PolicyEvaluationRecord] = Field(default_factory=list)
    execution_results: list[ExecutionResult] = Field(default_factory=list)
    observation: IterationObservation | None = None
    stop_reason: LoopStopReason | None = None


class OrchestrationResult(StrictModel):
    """End-to-end orchestration result."""

    session_id: str | None = None
    request: UserRequest
    context: ContextSnapshot
    plan: AssistantPlan
    planning_metadata: ProviderResponseMetadata
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    policy_evaluations: list[PolicyEvaluationRecord] = Field(default_factory=list)
    execution_results: list[ExecutionResult] = Field(default_factory=list)
    assistant_message: AssistantMessage
    summary: OrchestrationSummary
    iterations: list[OrchestrationIteration] = Field(default_factory=list)
    stop_reason: LoopStopReason | None = None
    verification_notice: VerificationNotice | None = None
    governance_notice: GovernanceNotice | None = None
