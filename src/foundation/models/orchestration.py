"""Typed orchestration models for Stage 5."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator


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


class ToolName(StrEnum):
    """Local context tools supported by Stage 5 planning."""

    SEARCH = "search"
    FILES = "files"
    GIT = "git"
    MAN = "man"
    TLDR = "tldr"


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
    """One typed local tool invocation proposed by the model."""

    tool: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


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

        if self.requires_approval and not self.approval_reason:
            raise ValueError("Approval-required actions must include approval_reason")
        return self


class AssistantPlan(StrictModel):
    """Structured plan returned by the provider before execution."""

    assistant_message: str = Field(min_length=1)
    actions: list[PlannedAction] = Field(default_factory=list, max_length=5)

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


class OrchestrationSummary(StrictModel):
    """High-level summary of what the orchestrator did."""

    executed_actions: int = Field(ge=0)
    pending_approval_actions: int = Field(ge=0)
    blocked_actions: int = Field(ge=0)
    failed_actions: int = Field(ge=0)
    skipped_actions: int = Field(ge=0)
    text: str = Field(min_length=1)


class OrchestrationResult(StrictModel):
    """End-to-end Stage 5 orchestration result."""

    session_id: str | None = None
    request: UserRequest
    context: ContextSnapshot
    plan: AssistantPlan
    planning_metadata: ProviderResponseMetadata
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    execution_results: list[ExecutionResult] = Field(default_factory=list)
    assistant_message: AssistantMessage
    summary: OrchestrationSummary
